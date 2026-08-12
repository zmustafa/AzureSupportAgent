"""Bulk firewall import/export tooling — parsing, preview, save wiring, and scale guards."""
from __future__ import annotations

import asyncio
import csv
import io
import ipaddress
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.api import firewall
from app.core import netaccess, netaccess_io
from app.core.security import Principal


def _rule(cidr: str, label: str = "existing", enabled: bool = True) -> dict:
    return {"cidr": cidr, "label": label, "enabled": enabled}


def _preview(
    text: str,
    *,
    source: str = "ranges.txt",
    fmt: str = "auto",
    label: str = "Imported",
    strategy: str = "merge",
    mode: str = "monitor",
    existing: list[dict] | None = None,
    caller: str | None = "203.0.113.7",
) -> dict:
    return netaccess_io.preview_import(
        text,
        source_name=source,
        requested_format=fmt,
        default_label=label,
        strategy=strategy,
        mode=mode,
        existing_rules=existing or [],
        caller_ip=caller,
        actor="tester",
    )


# --------------------------------------------------------------------------- TXT / CSV parsing


def test_txt_import_supports_comments_blank_lines_ipv4_ipv6_and_bare_ips():
    result = _preview(
        "\ufeff# corporate egress\n\n203.0.113.7\n198.51.100.9/24\n2001:db8::7\n"
    )

    assert result["can_apply"] is True
    assert [r["cidr"] for r in result["result_rules"]] == [
        "203.0.113.7/32",
        "198.51.100.0/24",
        "2001:db8::7/128",
    ]
    assert result["summary"]["input_rows"] == 3
    assert result["summary"]["canonicalized"] == 3


def test_csv_import_preserves_labels_and_disabled_state_with_utf8_bom():
    text = "\ufeffcidr,label,enabled\n203.0.113.7,Office,true\n2001:db8::/48,Legacy VPN,disabled\n"
    result = _preview(text, source="allowlist.csv")

    assert result["format"] == "csv"
    assert result["can_apply"] is True
    assert [(r["cidr"], r["label"], r["enabled"]) for r in result["result_rules"]] == [
        ("203.0.113.7/32", "Office", True),
        ("2001:db8::/48", "Legacy VPN", False),
    ]


def test_csv_auto_detects_from_header_when_pasted():
    result = _preview("cidr,label\n203.0.113.7,Pasted CSV\n")
    assert result["format"] == "csv"
    assert result["result_rules"][0]["label"] == "Pasted CSV"


@pytest.mark.parametrize("value", ["maybe", "2", "enabled-ish"])
def test_csv_rejects_ambiguous_enabled_values(value: str):
    result = _preview(f"cidr,label,enabled\n203.0.113.7,Office,{value}\n", fmt="csv")
    assert result["can_apply"] is False
    assert "enabled must be" in result["diagnostics"][0]["message"]


def test_invalid_rows_are_reported_with_line_numbers_and_block_apply():
    result = _preview("203.0.113.7\nnot-an-ip\n198.51.100.1\n")
    bad = next(d for d in result["diagnostics"] if d["status"] == "invalid")

    assert result["can_apply"] is False
    assert bad["line"] == 2
    assert bad["input"] == "not-an-ip"
    assert "not a valid IP" in bad["message"]
    assert result["summary"]["invalid_rows"] == 1


def test_duplicates_after_canonicalization_are_not_silently_discarded():
    result = _preview("198.51.100.7/24\n198.51.100.99/24\n")

    assert result["can_apply"] is False
    assert result["summary"]["duplicate_input"] == 1
    assert "Duplicates line 1" in result["diagnostics"][1]["message"]


# --------------------------------------------------------------------------- merge / replace semantics


def test_merge_retains_existing_label_and_status_and_adds_only_new_ranges():
    existing = [_rule("203.0.113.7/32", "Existing label", False)]
    result = _preview(
        "cidr,label,enabled\n203.0.113.7,Ignored replacement,true\n198.51.100.1,New office,true\n",
        fmt="csv",
        existing=existing,
    )

    assert result["can_apply"] is True
    assert result["summary"]["added"] == 1
    assert result["summary"]["retained"] == 1
    assert result["summary"]["skipped_existing"] == 1
    assert [(r["cidr"], r["label"], r["enabled"]) for r in result["result_rules"]] == [
        ("203.0.113.7/32", "Existing label", False),
        ("198.51.100.1/32", "New office", True),
    ]
    assert result["diagnostics"][0]["status"] == "existing"


def test_replace_reports_removed_retained_and_added_ranges():
    existing = [_rule("203.0.113.7"), _rule("198.51.100.1")]
    result = _preview(
        "203.0.113.7\n192.0.2.1\n",
        strategy="replace",
        existing=existing,
    )

    assert result["can_apply"] is True
    assert result["summary"]["retained"] == 1
    assert result["summary"]["removed"] == 1
    assert result["summary"]["added"] == 1
    assert [r["cidr"] for r in result["result_rules"]] == [
        "203.0.113.7/32",
        "192.0.2.1/32",
    ]


def test_replace_rejects_an_empty_import_instead_of_erasing_the_policy():
    result = _preview("# no ranges\n", strategy="replace", existing=[_rule("203.0.113.7")])
    assert result["can_apply"] is False
    assert any("cannot erase" in error for error in result["errors"])


def test_preview_reports_current_address_coverage_without_blocking_draft_application():
    result = _preview("198.51.100.0/24\n", strategy="replace", caller="203.0.113.7")
    assert result["can_apply"] is True
    assert result["your_ip_covered"] is False


def test_enforce_mode_rejects_enabled_everywhere_range_but_monitor_allows_it():
    blocked = _preview("0.0.0.0/0\n", mode="enforce")
    allowed = _preview("0.0.0.0/0\n", mode="monitor")
    assert blocked["can_apply"] is False
    assert any("allows every address" in error for error in blocked["errors"])
    assert allowed["can_apply"] is True


def test_overlapping_ranges_are_retained_and_reported_as_non_blocking_warnings():
    result = _preview("203.0.113.0/24\n203.0.113.7/32\n")
    assert result["can_apply"] is True
    assert result["overlap_count"] == 1
    assert result["overlaps"][0]["overlaps"] == "203.0.113.0/24"


# --------------------------------------------------------------------------- hard limits


def test_import_rejects_nul_and_oversized_text():
    with pytest.raises(netaccess_io.NetAccessImportError, match="NUL"):
        _preview("203.0.113.7\x00")
    with pytest.raises(netaccess_io.NetAccessImportError, match="larger than 1 MiB"):
        _preview("#" + "x" * netaccess_io.MAX_IMPORT_BYTES)


def test_import_rejects_an_oversized_line():
    with pytest.raises(netaccess_io.NetAccessImportError, match="Line 1"):
        _preview("1" * (netaccess_io.MAX_LINE_CHARS + 1))


def test_resulting_policy_is_capped_at_five_thousand_ranges():
    existing = [_rule(f"10.{i // 256}.{i % 256}.1") for i in range(netaccess.MAX_RULES)]
    result = _preview("192.0.2.1\n", existing=existing)
    assert result["can_apply"] is False
    assert any("limit is 5,000" in error for error in result["errors"])


# --------------------------------------------------------------------------- export / round trip


def test_txt_export_contains_only_active_canonical_ranges_and_reimports():
    rules = [_rule("203.0.113.7", "Office"), _rule("198.51.100.7/24", "Disabled", False)]
    exported = netaccess_io.export_txt(rules, mode="monitor")
    assert exported == "203.0.113.7/32\n"
    assert _preview(exported)["result_rules"][0]["cidr"] == "203.0.113.7/32"


def test_csv_export_round_trips_labels_and_disabled_state():
    rules = [_rule("203.0.113.7", "Office"), _rule("2001:db8::/48", "Legacy VPN", False)]
    exported = netaccess_io.export_csv(rules, mode="monitor")
    imported = _preview(exported, source="roundtrip.csv")
    assert [(r["cidr"], r["label"], r["enabled"]) for r in imported["result_rules"]] == [
        ("203.0.113.7/32", "Office", True),
        ("2001:db8::/48", "Legacy VPN", False),
    ]


def test_csv_export_neutralizes_spreadsheet_formula_labels():
    exported = netaccess_io.export_csv(
        [_rule("203.0.113.7", "  =WEBSERVICE(\"https://example.invalid\")")],
        mode="monitor",
    )
    row = next(csv.DictReader(io.StringIO(exported)))
    assert row["label"].startswith("'")


# --------------------------------------------------------------------------- prefix matcher and endpoint wiring


def test_prefix_index_matches_ipaddress_truth_for_large_mixed_policy():
    rules = [_rule(f"10.{i // 256}.{i % 256}.0/24") for i in range(4_000)]
    rules += [_rule(f"2001:db8:{i:x}::/48") for i in range(1_000)]
    probes = ["10.0.0.1", "10.15.159.254", "10.20.0.1", "2001:db8:3e7::5", "2001:db9::1"]
    expected = []
    networks = [ipaddress.ip_network(r["cidr"]) for r in rules]
    for probe in probes:
        addr = ipaddress.ip_address(probe)
        expected.append(any(addr.version == n.version and addr in n for n in networks))

    netaccess.reset_cache()
    assert [netaccess.matches(probe, rules) for probe in probes] == expected
    compiled = netaccess._compiled(rules)
    assert len(compiled[4]) <= 33 and len(compiled[6]) <= 129


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/admin/firewall/import/preview",
            "headers": [],
            "client": ("203.0.113.7", 1234),
        }
    )


def _principal() -> Principal:
    return Principal(subject="admin-id", email="admin@example.test", tenant_id="tenant", role="admin")


def test_preview_endpoint_is_dry_run_and_uses_manage_principal(monkeypatch):
    monkeypatch.setattr(
        netaccess,
        "write_config",
        lambda *args, **kwargs: pytest.fail(f"preview wrote config: {args!r} {kwargs!r}"),
    )
    payload = firewall.ImportPreviewIn(
        text="203.0.113.7\n",
        source_name="ranges.txt",
        mode="monitor",
        existing_rules=[],
    )
    result = asyncio.run(firewall.preview_import(payload, _request(), _principal()))
    assert result["can_apply"] is True
    assert result["your_ip_covered"] is True


def test_export_endpoint_returns_download_headers_and_saved_not_draft(monkeypatch):
    monkeypatch.setattr(
        netaccess,
        "load_config",
        lambda: {"mode": "monitor", "rules": [_rule("203.0.113.7", "Saved")]},
    )
    response = asyncio.run(firewall.export_rules("txt", _principal()))
    assert response.body == b"203.0.113.7/32\n"
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"


class _FakeDb:
    def __init__(self):
        self.added = []

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        return None


def test_save_audit_records_server_computed_import_change_counts(monkeypatch):
    previous = {"mode": "monitor", "rules": [_rule("203.0.113.7", "Old")], "confirm_by": None}
    written: list[dict] = []
    monkeypatch.setattr(netaccess, "load_config", lambda: previous)
    monkeypatch.setattr(netaccess, "write_config", lambda cfg: written.append(cfg) or cfg)
    db = _FakeDb()
    payload = firewall.ConfigIn(
        mode="monitor",
        rules=[
            firewall.RuleIn(cidr="203.0.113.7", label="Old", enabled=True),
            firewall.RuleIn(cidr="198.51.100.1", label="New", enabled=True),
        ],
        import_context=firewall.FirewallImportContextIn(
            source_name="C:\\fake\\ranges.txt", strategy="merge", skipped_existing=1
        ),
    )

    asyncio.run(
        firewall.update_config(payload, _request(), _principal(), cast(AsyncSession, db))
    )

    assert written and len(written[0]["rules"]) == 2
    audit = db.added[-1]
    assert audit.action == "firewall.update"
    assert audit.metadata_json["import"] == {
        "source_name": "ranges.txt",
        "strategy": "merge",
        "added": 1,
        "removed": 0,
        "retained": 1,
        "preview_skipped_existing": 1,
    }
