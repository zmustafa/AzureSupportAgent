"""Unit tests for the coverage PDF builders (``app.core.coverage_pdf``).

Exercise the per-feature + estate report renderers and the Evidence Locker mapping on
synthetic snapshots — no DB/HTTP. The high-percentage cases are a regression guard for the
``bar()`` crash (a thin remainder cell at 90-99% used to collapse below xhtml2pdf's default
cell padding into a negative width and abort the whole PDF).
"""
from __future__ import annotations

import io

import pytest
from pypdf import PdfReader

from app.core.coverage_pdf import build_coverage_pdf, build_estate_pdf, build_evidence_content


def _resources(n: int = 4) -> list[dict]:
    return [
        {"id": f"/subscriptions/1234abcd-0000-0000-0000-000000000099/resourceGroups/rg-a/providers/microsoft.compute/virtualMachines/res-{i}",
         "name": f"res-{i}",
         "type": "microsoft.compute/virtualmachines", "resource_group": "rg-a",
         "location": "eastus", "subscription_id": "1234abcd-0000-0000-0000-000000000099"}
        for i in range(n)
    ]


def _amba_snap(pct: int) -> dict:
    return {
        "generated_at": "2026-06-19T10:00:00+00:00", "scope_kind": "workload", "scope_id": "wl-1",
        "scope_name": "Contoso Prod", "connection_configured": True, "source": "azure_resource_graph",
        "demo": False, "coverage_pct": pct,
        "kpis": {"total_resources_in_baseline": 12, "alerts_present": 30, "alerts_missing": 8,
                 "alerts_misconfigured": 2, "recommended_total": 40},
        "gaps": [
            {"resource_id": "/subscriptions/1234abcd-0000-0000-0000-000000000099/resourceGroups/rg-a/providers/microsoft.compute/virtualMachines/res-0",
             "resource_name": "res-0",
             "resource_type": "microsoft.compute/virtualmachines", "resource_group": "rg-a",
             "subscription_id": "1234abcd-0000-0000-0000-000000000099", "alert_name": "CPU > 90%",
             "amba_category": "Performance", "severity": "critical", "status": "missing", "why": "no alert"},
        ],
        "all_resources": _resources(),
    }


def _telemetry_snap(pct: int) -> dict:
    return {
        "generated_at": "2026-06-19T10:00:00+00:00", "scope_kind": "workload", "scope_id": "wl-1",
        "scope_name": "Contoso Prod", "connection_configured": True, "source": "azure_resource_graph",
        "demo": False, "coverage_pct": pct,
        "kpis": {"total_resources_in_reference": 12, "with_any_diag": 9, "pct_with_any_diag": 75,
                 "with_all_categories": 7, "pct_with_all_categories": pct, "to_approved_workspace": 6,
                 "pct_to_approved": 50, "unknown_destinations": 1, "unreadable": 0},
        "gaps": [
            {"resource_id": "/sub/0/rg/a/r1", "resource_name": "res-1",
             "resource_type": "microsoft.storage/storageaccounts", "resource_group": "rg-a",
             "subscription_id": "00000000-0000-0000-0000-000000000000", "status": "partial",
             "missing_categories": ["StorageWrite"], "has_drift": True,
             # A long ARM id must be shortened so it can't overflow the page.
             "drift_workspaces": ["/subscriptions/0/resourcegroups/rg/providers/microsoft.operationalinsights/workspaces/sandbox-law"],
             "severity": "high"},
        ],
        "all_resources": _resources(),
    }


def _backupdr_snap(pct: int) -> dict:
    return {
        "generated_at": "2026-06-19T10:00:00+00:00", "scope_kind": "workload", "scope_id": "wl-1",
        "scope_name": "Contoso Prod", "connection_configured": True, "source": "azure_resource_graph",
        "demo": False,
        "scorecard": {"total": 12, "protected": 9, "pct_protected": pct, "pct_offsite": 60,
                      "pct_recent_job": 80, "dr_pairs": 3, "dr_pairs_stale": 1, "dr_pairs_unhealthy": 0,
                      "last_drill_days": 45},
        "gaps": [
            {"resource_id": "/sub/0/rg/a/r0", "resource_name": "res-0",
             "resource_type": "microsoft.compute/virtualmachines", "resource_group": "rg-a",
             "subscription_id": "00000000-0000-0000-0000-000000000000", "region": "eastus",
             "backup_region": "westus", "status": "unprotected",
             "failed_checks": ["backup_enabled", "dr_pair"], "vault_name": "rsv-prod", "severity": "medium"},
        ],
        "all_resources": _resources(),
    }


_TREND = {"points": [{"at": "2026-06-01", "pct": 60}, {"at": "2026-06-10", "pct": 70}],
          "current": 70, "previous": 60, "delta": 10}


@pytest.mark.parametrize("feature, snap_fn", [
    ("amba", _amba_snap), ("telemetry", _telemetry_snap), ("backupdr", _backupdr_snap),
])
@pytest.mark.parametrize("pct", [0, 50, 78, 90, 95, 99, 100])
def test_build_coverage_pdf_valid_at_all_percentages(feature, snap_fn, pct):
    """Regression guard: the headline bar must not crash at high percentages (90-99%)."""
    pdf = build_coverage_pdf(feature, snap_fn(pct), _TREND)
    assert pdf[:5] == b"%PDF-"
    assert len(PdfReader(io.BytesIO(pdf)).pages) >= 3


def test_coverage_pdf_contains_sections_and_remediation():
    pdf = build_coverage_pdf("amba", _amba_snap(78), _TREND)
    text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf)).pages)
    assert "Azure Support Agent" in text
    assert "Monitoring Coverage" in text
    assert "Executive summary" in text
    assert "Gaps & remediation" in text or "Gaps &amp; remediation" in text
    assert "Remediation" in text
    assert "Methodology" in text


def test_telemetry_long_drift_workspace_is_shortened():
    """The full ARM id must be reduced to its last segment so it can't overflow the page."""
    pdf = build_coverage_pdf("telemetry", _telemetry_snap(91), _TREND)
    text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf)).pages)
    assert "sandbox-law" in text
    assert "microsoft.operationalinsights" not in text


def test_coverage_pdf_embeds_azure_portal_links():
    """Resource names link to the Azure portal blade for real ARM ids."""
    from app.core.coverage_pdf import _adapt, _gaps_section, _portal_url, _resources_section

    rid = "/subscriptions/1234abcd-0000-0000-0000-000000000099/resourceGroups/rg-a/providers/microsoft.compute/virtualMachines/res-0"
    assert _portal_url(rid) == f"https://portal.azure.com/#@/resource{rid}/overview"
    # An all-zero / demo subscription yields no link.
    assert _portal_url("/subscriptions/00000000-0000-0000-0000-000000000000/x/r") == ""
    assert _portal_url("not-an-arm-id") == ""

    model = _adapt("amba", _amba_snap(78))
    gaps_html = _gaps_section(model)
    res_html = _resources_section(model)
    assert f'href="https://portal.azure.com/#@/resource{rid}/overview"' in gaps_html
    assert "https://portal.azure.com/#@/resource" in res_html


def test_estate_pdf_has_blended_score_and_all_features():
    items = [("amba", _amba_snap(78), _TREND), ("telemetry", _telemetry_snap(91), _TREND),
             ("backupdr", _backupdr_snap(80), _TREND)]
    pdf = build_estate_pdf("Contoso Prod", items)
    assert pdf[:5] == b"%PDF-"
    text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf)).pages)
    assert "Estate Coverage Report" in text
    assert "Blended coverage" in text
    assert "Monitoring Coverage" in text
    assert "Telemetry Coverage" in text
    assert "Backup & DR Coverage" in text or "Backup &amp; DR Coverage" in text


def test_no_gaps_renders_all_clear():
    snap = _backupdr_snap(100)
    snap["gaps"] = []
    pdf = build_coverage_pdf("backupdr", snap, {"points": [], "current": 100, "previous": None, "delta": None})
    text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf)).pages)
    assert "No open coverage gaps" in text


def test_build_evidence_content_shape():
    name, scope, included, tags, content = build_evidence_content("telemetry", _telemetry_snap(91))
    assert "Telemetry Coverage" in name
    assert scope["kind"] == "workload"
    assert included == ["findings", "metrics", "inventory"]
    assert tags == ["coverage", "telemetry"]
    assert len(content["findings"]) == 1
    assert content["metrics"]["headline_pct"] == 91
