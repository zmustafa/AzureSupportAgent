"""Unit tests for the policy exemption manager (plan/validate/build — no live Azure)."""
from __future__ import annotations

from app.policy import exemptions as ex


def _gr(**over):
    g = {"require_justification": True, "max_expiry_days": 180, "block_never_expires": True}
    g.update(over)
    return g


def test_plan_create_valid_builds_arm_and_cli(monkeypatch):
    # Use a wide guardrail so the future date passes regardless of the app's configured window.
    monkeypatch.setattr(ex, "load_guardrails", lambda: _gr(max_expiry_days=0))
    from datetime import datetime, timedelta, timezone
    soon = (datetime.now(timezone.utc) + timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    p = ex.plan({
        "scope": "/subscriptions/abc",
        "policy_assignment_id": "/subscriptions/abc/providers/microsoft.authorization/policyassignments/x",
        "category": "Waiver",
        "display_name": "Temp waiver",
        "description": "ticket 123",
        "expires_on": soon,
    }, "create")
    assert p["valid"] is True and p["errors"] == []
    assert p["arm"]["method"] == "PUT"
    assert p["arm"]["path"].endswith(p["name"])
    assert "/providers/Microsoft.Authorization/policyExemptions/" in p["arm"]["path"]
    assert p["arm"]["body"]["properties"]["exemptionCategory"] == "Waiver"
    assert "az policy exemption create" in p["cli"]


def test_validate_requires_justification_and_blocks_never_expires():
    errs = ex.validate({
        "scope": "/subscriptions/abc", "policy_assignment_id": "/a/x",
        "category": "Waiver", "display_name": "n", "description": "", "expires_on": "",
    }, "create", _gr())
    assert any("justification" in e.lower() for e in errs)
    assert any("never-expiring" in e.lower() for e in errs)


def test_validate_max_expiry_window():
    errs = ex.validate({
        "scope": "/s", "policy_assignment_id": "/a/x", "category": "Waiver",
        "display_name": "n", "description": "ok", "expires_on": "2099-01-01T00:00:00Z",
    }, "create", _gr(max_expiry_days=30))
    assert any("maximum allowed window" in e.lower() for e in errs)


def test_validate_rejects_past_expiry_and_bad_category():
    errs = ex.validate({
        "scope": "/s", "policy_assignment_id": "/a/x", "category": "Nope",
        "display_name": "n", "description": "ok", "expires_on": "2000-01-01T00:00:00Z",
    }, "create", _gr())
    assert any("future" in e.lower() for e in errs)
    assert any("category" in e.lower() for e in errs)


def test_plan_remove_uses_delete_and_ids_cli():
    p = ex.plan({"id": "/subscriptions/abc/providers/microsoft.authorization/policyexemptions/y"}, "remove")
    assert p["valid"] is True
    assert p["arm"]["method"] == "DELETE"
    assert "az policy exemption delete --ids" in p["cli"]


def test_never_expires_allowed_when_guardrail_off():
    errs = ex.validate({
        "scope": "/s", "policy_assignment_id": "/a/x", "category": "Mitigated",
        "display_name": "n", "description": "ok", "expires_on": "",
    }, "create", _gr(block_never_expires=False))
    assert errs == []
