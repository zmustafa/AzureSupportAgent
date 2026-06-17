"""Unit tests for the assessment PDF report builder (``app.assessments.pdf_report``).

These exercise the renderer end-to-end on a synthetic-but-realistic run payload — no DB
or HTTP needed — and assert the produced bytes are a valid, multi-section PDF.
"""
from __future__ import annotations

import io

from pypdf import PdfReader

from app.assessments import catalog
from app.assessments.pdf_report import build_pdf


def _payload() -> dict:
    payload = {
        "id": "run-123",
        "workload_name": "Contoso Payments PROD",
        "pillars": ["security", "reliability", "cost"],
        "status": "succeeded",
        "overall_score": 72,
        "trigger": "waf",
        "triggered_by": "admin@contoso.com",
        "duration_ms": 45000,
        "catalog_version": "2026.06.3",
        "confidence": "high",
        "completeness_pct": 95,
        "used_ai": True,
        "resource_count": 2,
        "is_baseline": True,
        "started_at": "2026-06-16T10:30:00+00:00",
        "ended_at": "2026-06-16T10:30:45+00:00",
        "totals": {"passed": 80, "failed": 15, "not_applicable": 10, "error": 2, "manual": 5, "waived": 3},
        "scores": {
            "security": {"score": 65, "passed": 20, "failed": 8, "na": 2, "waived": 1},
            "reliability": {"score": 84, "passed": 30, "failed": 3, "na": 4},
            "cost": {"score": 50, "passed": 10, "failed": 4, "na": 1},
        },
        "summary": "Overall posture is moderate.\n\nThe most urgent risks are unencrypted storage.",
        "findings": [
            {
                "check_id": "sec_storage_https", "pillar": "security",
                "title": "Storage accounts allow insecure HTTP",
                "description": "Secure transfer is disabled.", "severity": "critical", "status": "fail",
                "frameworks": {"cis": ["3.1"], "nist": ["SC-8"]}, "flagged_count": 1,
                "remediation": "Enable 'Secure transfer required'.",
                "remediation_command": "az storage account update --https-only true",
                "flagged_resources": [
                    {"id": "/subscriptions/s1/x/sa1", "name": "sa1",
                     "type": "microsoft.storage/storageaccounts", "resource_group": "rg1",
                     "subscription_id": "s1", "subscription_name": "Payments Prod",
                     "portal_url": "https://portal.azure.com/#@/resource/x/overview"},
                ],
            },
            {
                "check_id": "sec_mfa", "pillar": "security", "title": "MFA enforced for admins",
                "severity": "error", "status": "pass", "flagged_count": 0,
                "frameworks": {"cis": ["1.1"]}, "flagged_resources": [],
            },
        ],
        "resources": [
            {"id": "x1", "name": "sa1", "type": "microsoft.storage/storageaccounts",
             "resource_group": "rg1", "location": "eastus"},
        ],
    }
    payload["compliance"] = catalog.compliance_coverage(payload["findings"])
    return payload


def test_build_pdf_returns_valid_pdf_bytes():
    pdf = build_pdf(_payload())
    assert isinstance(pdf, bytes)
    assert pdf[:5] == b"%PDF-"
    reader = PdfReader(io.BytesIO(pdf))
    # Cover + TOC + exec + visual snapshot + scores + findings + 4 appendices => at least 8 pages.
    assert len(reader.pages) >= 8


def test_build_pdf_contains_key_sections_and_branding():
    pdf = build_pdf(_payload())
    reader = PdfReader(io.BytesIO(pdf))
    text = "\n".join((p.extract_text() or "") for p in reader.pages)
    assert "Azure Support Agent" in text
    assert "Contoso Payments PROD" in text
    assert "Executive summary" in text
    assert "Visual snapshot" in text
    assert "Control outcome mix" in text
    assert "Pillar score bars" in text
    assert "Findings & recommendations" in text
    assert "Storage accounts allow insecure HTTP" in text
    # Appendices with full detail.
    assert "Appendix A" in text
    assert "Appendix D" in text
    # Branded outline / bookmarks expose the sections for navigation.
    titles = [b.title for b in reader.outline if hasattr(b, "title")]
    assert any("Executive summary" in t for t in titles)


def test_build_pdf_handles_empty_run():
    """A minimal/empty run (no findings, no scores) must still render without error."""
    pdf = build_pdf({"workload_name": "Empty", "overall_score": None, "findings": [],
                     "resources": [], "scores": {}, "totals": {}, "compliance": {}})
    assert pdf[:5] == b"%PDF-"
    assert len(PdfReader(io.BytesIO(pdf)).pages) >= 1
