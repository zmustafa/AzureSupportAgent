"""Unit tests for the Performance Profiler PDF builder (``app.core.performance_pdf``).

Exercise the renderer + the Evidence Locker mapping on the demo run snapshot and on a
hand-built healthy snapshot — no DB/HTTP. Guards the report structure, the portal links,
and the empty/healthy paths.
"""
from __future__ import annotations

import io

import pytest
from pypdf import PdfReader

from app.core.performance_pdf import build_evidence_content, build_performance_pdf
from app.perfprofile.demo import build_demo_snapshot

_TREND = {"points": [{"pct": 60}, {"pct": 72}, {"pct": 82}], "current": 82, "previous": 72, "delta": 10}


def _pdf_text(pdf: bytes) -> str:
    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf)).pages)


def _healthy_snap() -> dict:
    """A run where every profiled resource is within threshold (no bottlenecks)."""
    return {
        "scope_kind": "workload",
        "scope_id": "wl-healthy",
        "scope_name": "Healthy Workload",
        "generated_at": "2026-06-20T12:00:00Z",
        "connection_configured": True,
        "demo": False,
        "source": "azure_monitor_metrics",
        "window": "P1D",
        "interval": "PT15M",
        "scorecard": {"workload_score": 100, "resources_profiled": 2, "breaching": 0, "approaching": 0, "healthy": 2, "bottleneck_count": 0},
        "top_bottleneck": None,
        "bottlenecks": [],
        "resources": [
            {
                "resource_id": "/subscriptions/1234abcd-0000-0000-0000-000000000099/resourceGroups/rg-a/providers/microsoft.compute/virtualMachines/vm-1",
                "resource_name": "vm-1", "resource_type": "microsoft.compute/virtualmachines", "display": "Virtual Machine",
                "resource_group": "rg-a", "subscription_id": "1234abcd-0000-0000-0000-000000000099", "region": "eastus",
                "score": 100, "state": "healthy",
                "cells": [{"alert_key": "vm_cpu", "metric": "Percentage CPU", "name": "CPU utilization high",
                           "severity": "warning", "unit": "%", "threshold": 90, "observed": 30.0,
                           "pct_of_threshold": 33.0, "trend_pct": 0.0, "state": "healthy"}],
            },
        ],
        "all_resources": [
            {"id": "/subscriptions/1234abcd-0000-0000-0000-000000000099/resourceGroups/rg-a/providers/microsoft.compute/virtualMachines/vm-1",
             "name": "vm-1", "type": "microsoft.compute/virtualmachines", "resourceGroup": "rg-a",
             "location": "eastus", "subscriptionId": "1234abcd-0000-0000-0000-000000000099", "in_reference": True},
        ],
    }


def test_build_performance_pdf_returns_valid_pdf_bytes():
    pdf = build_performance_pdf(build_demo_snapshot(), _TREND)
    assert pdf[:5] == b"%PDF-"
    assert len(PdfReader(io.BytesIO(pdf)).pages) >= 4


def test_performance_pdf_contains_key_sections_and_branding():
    pdf = build_performance_pdf(build_demo_snapshot(), _TREND)
    text = _pdf_text(pdf)
    assert "Azure Support Agent" in text
    assert "Performance Profile" in text
    assert "Executive summary" in text
    assert "Ranked bottlenecks" in text
    assert "Resource performance detail" in text
    assert "Performance heatmap" in text
    assert "Methodology" in text


def test_performance_pdf_healthy_run_has_no_bottlenecks():
    pdf = build_performance_pdf(_healthy_snap(), {"points": [], "current": 100, "previous": None, "delta": None})
    text = _pdf_text(pdf)
    assert pdf[:5] == b"%PDF-"
    assert "No bottlenecks" in text


def test_performance_pdf_handles_empty_run():
    """A run with no profiled resources still renders a valid PDF (cover + methodology)."""
    snap = {
        "scope_kind": "workload", "scope_id": "wl-empty", "scope_name": "Empty",
        "generated_at": "2026-06-20T12:00:00Z", "source": "azure_monitor_metrics",
        "scorecard": {"workload_score": 100, "resources_profiled": 0, "breaching": 0, "approaching": 0, "healthy": 0, "bottleneck_count": 0},
        "top_bottleneck": None, "bottlenecks": [], "resources": [], "all_resources": [],
    }
    pdf = build_performance_pdf(snap, None)
    assert pdf[:5] == b"%PDF-"
    assert len(PdfReader(io.BytesIO(pdf)).pages) >= 2


def test_performance_pdf_embeds_azure_portal_links():
    """Real ARM ids get a portal launch arrow; the bottleneck table links the resource."""
    from app.core.performance_pdf import _adapt, _bottlenecks_section

    snap = build_demo_snapshot()
    model = _adapt(snap)
    html = _bottlenecks_section(model)
    # The demo bottlenecks carry real ARM ids → at least one portal link is emitted.
    assert "https://portal.azure.com/#@/resource" in html


def test_performance_evidence_content_shape():
    name, scope, included, tags, content = build_evidence_content(build_demo_snapshot())
    assert "Performance Profile" in name
    assert scope["kind"] == "workload"
    assert set(included) == {"findings", "metrics", "inventory"}
    assert "performance" in tags
    assert isinstance(content["findings"], list)
    assert content["metrics"]["feature"] == "performance"
    # Every bottleneck becomes a finding.
    assert len(content["findings"]) == len(build_demo_snapshot().get("bottlenecks", []))


@pytest.mark.parametrize("score", [0, 42, 50, 79, 80, 95, 100])
def test_performance_pdf_valid_at_all_scores(score):
    """The score bar must not crash at any score (regression guard for the bar() math)."""
    snap = _healthy_snap()
    snap["scorecard"]["workload_score"] = score
    pdf = build_performance_pdf(snap, _TREND)
    assert pdf[:5] == b"%PDF-"
