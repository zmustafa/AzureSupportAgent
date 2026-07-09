"""Tests for the enriched progress-feed summary (_summarize_result).

Pins that tool-result summaries surface item names, a status chip for single items,
truncation/total awareness, and scalar-string values — while staying robust (never
raising) on errors, malformed JSON, and unexpected shapes. Payload shapes mirror the
real Azure MCP responses captured live (containerapps_list, monitor_workspace_list,
arm execute_query, postgres_server_config_get).
"""
import json

from app.agent.orchestrator import _is_command_catalog, _summarize_result


def _res(payload) -> dict:
    """Wrap a payload the way MCPClient.call_tool returns it (content is a JSON string)."""
    return {"isError": False, "content": [json.dumps(payload)]}


def test_single_item_gets_name_and_status_chip():
    payload = {
        "status": 200,
        "results": {"containerApps": [
            {"name": "azsupagent", "location": "southcentralus", "provisioningState": "Succeeded"}
        ]},
    }
    assert _summarize_result(_res(payload)) == "Found 1 containerApps: azsupagent (Succeeded)"


def test_multiple_items_list_names():
    payload = {"results": {"workspaces": [
        {"name": "workspace-goQw", "customerId": "a"},
        {"name": "workspace-kt3L", "customerId": "b"},
    ]}}
    assert _summarize_result(_res(payload)) == "Found 2 workspaces: workspace-goQw, workspace-kt3L"


def test_more_than_three_shows_plus_more_and_maps_data_key():
    payload = {
        "results": {"$id": "1", "data": [
            {"name": "a"}, {"name": "b"}, {"name": "c"}, {"name": "d"}, {"name": "e"},
        ], "facets": []},
        "rowCount": 5,
    }
    # `data` is relabeled to `resources`; only 3 names shown + "+2 more".
    assert _summarize_result(_res(payload)) == "Found 5 resources: a, b, c +2 more"


def test_truncation_total_awareness():
    payload = {
        "results": {"data": [{"name": f"r{i}"} for i in range(3)]},
        "totalRecords": 143,
        "resultTruncated": True,
    }
    out = _summarize_result(_res(payload))
    assert out.startswith("Found 3 resources: r0, r1, r2")
    assert out.endswith("· 3 of 143")


def test_truncation_flag_inside_results():
    payload = {"results": {"items": [{"name": "x"}, {"name": "y"}], "areResultsTruncated": True}}
    assert _summarize_result(_res(payload)).endswith("· truncated")


def test_id_field_uses_trailing_segment():
    payload = {"results": {"servers": [
        {"id": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.DBforPostgreSQL/flexibleServers/pg86337",
         "state": "Ready"}
    ]}}
    assert _summarize_result(_res(payload)) == "Found 1 servers: pg86337 (Ready)"


def test_scalar_string_result_surfaces_first_line():
    payload = {"results": {"Configuration": "Server Name: pg86337\nLocation: centralus\nSKU: Standard_B1ms"}}
    assert _summarize_result(_res(payload)) == "Server Name: pg86337"


def test_empty_list_reports_zero():
    assert _summarize_result(_res({"results": {"alerts": []}})) == "Found 0 alerts"


def test_top_level_list_payload():
    # name-only single item (no status) → just the name.
    assert _summarize_result(_res([{"name": "one"}])) == "Found 1 items: one"


def test_error_result():
    out = _summarize_result({"isError": True, "content": ["Boom: bad request happened"]})
    assert out.startswith("Error: Boom")


def test_malformed_json_returns_snippet():
    assert _summarize_result({"isError": False, "content": ["not json at all"]}) == "not json at all"


def test_empty_content_returns_done():
    assert _summarize_result({"isError": False, "content": []}) == "Done"


def test_no_list_no_scalar_returns_success():
    assert _summarize_result(_res({"results": {"count": 5}})) == "Success"


def test_summary_is_length_bounded():
    payload = {"results": {"things": [{"name": "x" * 300}]}}
    assert len(_summarize_result(_res(payload))) <= 160

# --- Fix A: command-catalog collapse -------------------------------------------------
_CATALOG = (
    "Here are the available command and their parameters for 'monitor' tool.\n\n"
    "If you do not find a suitable command, run again with the \"learn=true\" ...\n\n"
    '[{"name":"monitor_workspace_list","description":"…","inputSchema":{}}]'
)


def test_command_catalog_collapses_even_without_learn_arg():
    # A tool called with a missing/unknown command returns the catalog; collapse it.
    res = {"isError": False, "content": [_CATALOG]}
    assert _is_command_catalog(res) is True
    assert _summarize_result(res) == "Loaded tool commands"


def test_command_catalog_detection_is_case_insensitive_and_lstripped():
    res = {"isError": False, "content": ["   here are THE available command for 'x'"]}
    assert _is_command_catalog(res) is True


def test_non_catalog_is_not_flagged():
    assert _is_command_catalog(_res({"results": {"apps": [{"name": "a"}]}})) is False
    assert _is_command_catalog({"isError": True, "content": ["Here are the available command…"]}) is False


# --- Fix B: display_summary short-circuit --------------------------------------------
def test_display_summary_is_preferred():
    res = {
        "isError": False,
        "display_summary": "📊 Built chart: cpu_percent, active_connections (1 datapoint)",
        "content": ["DONE — the interactive chart is built and the metrics are already fetched (…)"],
    }
    assert _summarize_result(res) == "📊 Built chart: cpu_percent, active_connections (1 datapoint)"


def test_display_summary_ignored_when_blank():
    res = {"isError": False, "display_summary": "  ", "content": [json.dumps({"results": {"apps": [{"name": "a"}]}})]}
    assert _summarize_result(res) == "Found 1 apps: a"


def test_ok_helper_attaches_display_summary():
    from app.connectors.base import ok

    assert ok("verbose text").get("display_summary") is None
    r = ok("verbose model-facing text", display_summary="short line")
    assert r["display_summary"] == "short line"
    assert r["isError"] is False
    assert _summarize_result(r) == "short line"