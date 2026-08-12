"""Regression tests for the bug hunt of 2026-07-24.

Each test pins a defect that was found and fixed, so it can't silently come back.
All offline (no Azure, no LLM).
"""
from __future__ import annotations

import asyncio

import pytest

from app.exec.command_runner import KqlResult
from app.workloads import discovery


# ------------------------------------------------------------------ B-1: fail-closed refresh
# resources_in_resource_groups used to `return []` on ANY query failure. api/workloads.py
# refresh_workload treats an empty result as "every member was deleted" and PERSISTS that,
# with no membership history to recover from - so a transient auth blip or a truncated
# response silently emptied the workload.
def test_resources_in_resource_groups_returns_none_when_the_query_fails(monkeypatch):
    async def fake_collect(*_a, **_kw):
        return KqlResult(ok=False, error="Service-principal sign-in failed")

    monkeypatch.setattr("app.exec.command_runner.run_kql_collect", fake_collect)
    out = asyncio.run(discovery.resources_in_resource_groups({"id": "c"}, [("sub", "rg")]))
    assert out is None, "a FAILED query must be distinguishable from an empty scope"


def test_resources_in_resource_groups_returns_empty_list_when_scope_is_genuinely_empty(monkeypatch):
    async def fake_collect(*_a, **_kw):
        return KqlResult(ok=True, rows=[])

    monkeypatch.setattr("app.exec.command_runner.run_kql_collect", fake_collect)
    out = asyncio.run(discovery.resources_in_resource_groups({"id": "c"}, [("sub", "rg")]))
    assert out == [], "a successful empty read must stay an empty list, not None"


def test_resources_in_resource_groups_no_pairs_is_empty_not_failure():
    assert asyncio.run(discovery.resources_in_resource_groups({"id": "c"}, [])) == []
    assert asyncio.run(discovery.resources_in_resource_groups({"id": "c"}, [("sub", "")])) == []


def test_refresh_aborts_instead_of_wiping_when_the_read_fails():
    """The endpoint must raise (502) rather than persist an empty membership."""
    import inspect

    from app.api import workloads as wl_api

    src = inspect.getsource(wl_api.refresh_workload_endpoint)
    assert "if current is None" in src, "refresh must branch on the could-not-evaluate sentinel"
    assert "502" in src, "refresh must fail closed with an error, not persist a wipe"
    # and the abort must happen BEFORE the membership is written
    assert src.index("if current is None") < src.index("upsert_workload")


# ------------------------------------------------------------------ B-2: exact connection lookup
# GET /connections/{id}/discover used resolve_connection(), which falls back to the DEFAULT
# connection for a missing or DISABLED id - reporting the default tenant's subscriptions and
# management groups as if they belonged to the connection the admin clicked.
def test_discover_endpoint_uses_an_exact_connection_lookup():
    import inspect
    import re

    from app.api import connections as conn_api

    src = inspect.getsource(conn_api.discover_endpoint)
    # Strip comments/docstring so the explanatory prose about resolve_connection doesn't
    # count as a usage.
    code = re.sub(r"#[^\n]*", "", src)
    code = re.sub(r'""".*?"""', "", code, flags=re.S)
    assert "get_connection(" in code, "must look the connection up EXACTLY"
    assert "resolve_connection(" not in code, "resolve_connection falls back to the default"


def test_resolve_connection_still_falls_back_by_design():
    """Guards the assumption behind B-2: the fallback is real, so path-param endpoints that
    identify a specific connection must not use it."""
    import inspect

    from app.core import azure_connections

    src = inspect.getsource(azure_connections.resolve_connection)
    assert "get_default_connection()" in src


# ------------------------------------------------------------------ B-3/B-4: silent truncation
# A captured Resource Graph payload cut at the 256 KB cap is invalid JSON. A plain json.loads
# returns [] -> "0 resources" / "resource not found". The shared salvage parser recovers the
# complete objects instead.
def test_discovery_parse_rows_salvages_a_truncated_payload():
    whole = '[{"id":"/a","name":"a"},{"id":"/b","name":"b"},{"id":"/c","nam'
    rows = discovery._parse_rows(whole)
    assert [r["name"] for r in rows] == ["a", "b"], "must salvage the complete objects"


def test_discovery_parse_rows_handles_clean_and_empty_payloads():
    assert discovery._parse_rows('[{"id":"/a"}]') == [{"id": "/a"}]
    assert discovery._parse_rows("") == []
    assert discovery._parse_rows("not json at all") == []


@pytest.mark.parametrize("fn_name", ["all_resources", "resources_in_subscriptions"])
def test_tag_projecting_listings_raise_the_capture_cap(fn_name):
    """Both project `tags`, which measures ~400 bytes/row - the default 256 KB cap is hit
    around 600 rows, inside their own 1000-row ceiling."""
    import inspect

    src = inspect.getsource(getattr(discovery, fn_name))
    assert "KQL_RESOURCE_CAPTURE_BYTES" in src


def test_seed_resolve_uses_the_salvage_parser_and_a_large_cap():
    """resolve_seed projects `properties`; one big resource (AKS/APIM/Front Door) can exceed
    256 KB, and truncation there reported an existing resource as 'not found'."""
    import inspect

    from app.workloads import seed_links

    src = inspect.getsource(seed_links.resolve_seed)
    assert "KQL_RESOURCE_CAPTURE_BYTES" in src
    assert "parse_kql_rows(" in src


def test_architecture_reverse_parser_salvages_truncation():
    from app.architectures import reverse

    whole = '[{"id":"/a"},{"id":"/b"},{"id":"/c"'
    assert [r["id"] for r in reverse._parse_rows(whole)] == ["/a", "/b"]
    # normal shapes still work
    assert reverse._parse_rows('[{"id":"/a"}]') == [{"id": "/a"}]
    assert reverse._parse_rows('{"data":[{"id":"/a"}]}') == [{"id": "/a"}]
    assert reverse._parse_rows("") == []


def test_evidence_collector_parser_salvages_truncation():
    from app.evidence import collector

    whole = '[{"id":"/a"},{"id":"/b"},{"id":"/c"'
    assert [r["id"] for r in collector._parse_rows(whole)] == ["/a", "/b"]
    assert collector._parse_rows('{"data":[{"id":"/a"}]}') == [{"id": "/a"}]
    assert collector._parse_rows("") == []


def test_reverse_property_dump_raises_the_capture_cap():
    """The bounded architecture context raises the server-side property capture cap and
    adaptively bisects a truncating batch instead of failing the whole workload."""
    import inspect

    from app.architectures import reverse

    src = inspect.getsource(reverse.build_architecture_context)
    assert "KQL_RESOURCE_CAPTURE_BYTES" in src
    assert "await enrich(chunk[:midpoint])" in src


def test_dead_resources_exist_helper_is_gone():
    """Unused (no callers) and carried the fail-open shape — removed rather than left as a
    trap for the next caller."""
    assert not hasattr(discovery, "resources_exist")
