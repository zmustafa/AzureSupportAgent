"""Change Explorer — security flags, suspicious patterns, operation grouping, compare (A1/C/E2)."""
from app.changeexplorer import compare as compare_mod
from app.changeexplorer import operations as ops_mod
from app.changeexplorer import security as security_mod


def _ev(**kw):
    base = {
        "changeId": "c", "resourceId": "/sub/x/rg/r/res", "resourceName": "res", "resourceType": "",
        "category": "", "operation": "write", "eventTime": "2026-06-25T12:00:00+00:00",
        "actor": "u@x.com", "riskScore": 50, "riskLabel": "Medium", "details": [],
        "securityFlags": [], "correlationId": "",
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------- C1 security flags
def test_flag_public_exposure():
    e = _ev(category="Network", operation="write",
            details=[{"propertyPath": "securityRules[0].sourceAddressPrefix", "beforeValue": "10.0.0.0/8", "afterValue": "0.0.0.0/0"}])
    flags = security_mod.flag_event(e)
    assert any(f["code"] == "public_exposure" for f in flags)
    assert security_mod.highest_flag_severity(flags) == "critical"


def test_flag_rbac_grant_and_owner():
    e = _ev(category="RBAC", operation="write",
            details=[{"propertyPath": "roleDefinitionId", "beforeValue": "", "afterValue": "Owner"}])
    flags = security_mod.flag_event(e)
    codes = {f["code"] for f in flags}
    assert "rbac_grant" in codes and "owner_grant" in codes


def test_flag_logging_disabled_on_delete():
    e = _ev(category="Monitoring", operation="Microsoft.Insights/diagnosticSettings/delete",
            resourceType="microsoft.insights/diagnosticsettings", changeType="Delete")
    flags = security_mod.flag_event(e)
    assert any(f["code"] == "logging_disabled" and f["severity"] == "critical" for f in flags)


def test_no_flags_on_benign():
    e = _ev(category="TagsMetadata", operation="write", resourceType="microsoft.resources/tags")
    assert security_mod.flag_event(e) == []


def test_rollback_hint_present_for_resource():
    assert "az resource show" in security_mod.rollback_hint(_ev())
    assert "activity-log" in security_mod.rollback_hint(_ev(changeType="Delete", operation="delete"))


# --------------------------------------------------------------------------- C2 suspicious patterns
def test_suspicious_mass_delete():
    evs = [_ev(changeId=f"c{i}", operation="delete", changeType="Delete", actor="bad@x.com",
               resourceId=f"/r/{i}") for i in range(6)]
    pats = security_mod.suspicious_patterns(evs)
    assert any(p["patternType"] == "mass_delete" for p in pats)


def test_suspicious_disable_logging_then_change():
    ld = _ev(changeId="ld", eventTime="2026-06-25T12:00:00+00:00", actor="x@x.com",
             securityFlags=[{"code": "logging_disabled", "label": "x", "severity": "high"}])
    after = _ev(changeId="a1", eventTime="2026-06-25T12:05:00+00:00", actor="x@x.com")
    pats = security_mod.suspicious_patterns([ld, after])
    assert any(p["patternType"] == "disable_logging_then_change" and p["severity"] == "Critical" for p in pats)


# --------------------------------------------------------------------------- A1 operation grouping
def test_group_operations_by_correlation():
    evs = [_ev(changeId=f"c{i}", correlationId="corr-1", resourceId=f"/r/{i}", resourceName=f"r{i}")
           for i in range(3)]
    ops = ops_mod.group_operations(evs)
    assert len(ops) == 1
    assert ops[0]["changeCount"] == 3 and ops[0]["resourceCount"] == 3
    assert ops[0]["correlationId"] == "corr-1"


def test_group_operations_burst_without_correlation():
    evs = [
        _ev(changeId="c1", eventTime="2026-06-25T12:00:00+00:00", actor="a@x.com"),
        _ev(changeId="c2", eventTime="2026-06-25T12:00:30+00:00", actor="a@x.com"),
        _ev(changeId="c3", eventTime="2026-06-25T18:00:00+00:00", actor="a@x.com"),  # separate burst
    ]
    ops = ops_mod.group_operations(evs)
    # two bursts for the same actor (first two within 2 min, third far later)
    assert len(ops) == 2


def test_build_narrative_ordered():
    evs = [_ev(changeId="c1", correlationId="x", eventTime="2026-06-25T12:00:00+00:00")]
    ops = ops_mod.group_operations(evs)
    beats = ops_mod.build_narrative(evs, ops)
    assert beats and "performed a" in beats[0]["text"]


# --------------------------------------------------------------------------- E2 compare
def test_compare_added_removed_changed():
    run_a = {"runId": "A", "totalChanges": 2, "criticalCount": 0, "highCount": 1,
             "events": [_ev(resourceId="/r/1", riskScore=80, riskLabel="High"),
                        _ev(resourceId="/r/2", riskScore=30, riskLabel="Low")]}
    run_b = {"runId": "B", "totalChanges": 2, "criticalCount": 1, "highCount": 0,
             "events": [_ev(resourceId="/r/1", riskScore=95, riskLabel="Critical"),
                        _ev(resourceId="/r/3", riskScore=40, riskLabel="Medium")]}
    c = compare_mod.compare_runs(run_a, run_b)
    assert c["summary"]["added"] == 1   # /r/3
    assert c["summary"]["removed"] == 1  # /r/2
    assert c["summary"]["changed"] == 1  # /r/1
    changed = c["changed"][0]
    assert changed["riskDelta"] == 15   # 95 - 80
