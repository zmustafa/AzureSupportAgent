"""Tests for the CIS Azure Foundations Benchmark v5.0.0 controls + the Phase-0 engine
additions (arg_table, subscription-level scoping, existence/absence mode).

All offline: Resource Graph is monkeypatched, so nothing here touches a live subscription.
"""
from __future__ import annotations

import asyncio

from app.assessments import catalog, runner
from app.exec.command_runner import KqlResult


# --------------------------------------------------------------------------- catalog shape
def _cis():
    return [c for c in catalog.ALL_CHECKS if c.get("source") == "cis-v5"]


def test_cis_v5_version_pinned():
    assert catalog.CIS_VERSION == "v5.0.0"


def test_cis_v5_controls_present_and_tagged():
    cis = _cis()
    assert len(cis) >= 70
    for c in cis:
        assert c["pillar"] == "security", c["id"]
        assert c["profile"] in ("L1", "L2"), c["id"]
        # every CIS control carries its exact v5 recommendation number.
        cis_ids = c["frameworks"].get("cis", [])
        assert cis_ids and all(i.startswith("CIS Azure ") for i in cis_ids), c["id"]


def test_cis_v5_check_ids_unique_and_prefixed():
    ids = [c["id"] for c in _cis()]
    assert len(ids) == len(set(ids))
    assert all(i.startswith("cis_") for i in ids)


def test_cis_existence_checks_well_formed():
    existence = [c for c in _cis() if c.get("expectation") == "present"]
    assert len(existence) >= 20  # Defender plans + activity-log alerts + bastion + app insights
    for c in existence:
        assert "subscriptionId" in c["kql"], c["id"]
        # existence checks are subscription-scoped (no per-resource type gate)
        assert c["resource_types"] == [], c["id"]


def test_cis_defender_plans_use_securityresources():
    defenders = [
        c for c in _cis()
        if c.get("arg_table") == "securityresources" and "pricings" in c["kql"].lower()
    ]
    assert len(defenders) >= 10
    for c in defenders:
        assert c["expectation"] == "present", c["id"]
        assert "microsoft.security/pricings" in c["kql"].lower(), c["id"]


def test_cis_overlap_aliases_renumbered_to_v5():
    # Existing shipped checks that cover a CIS automated rec now speak v5 numbering.
    assert catalog._BY_ID["sec_nsg_mgmt_open"]["frameworks"]["cis"] == ["CIS Azure 7.1", "CIS Azure 7.2"]
    assert catalog._BY_ID["sec_kv_purge_protection"]["frameworks"]["cis"] == ["CIS Azure 8.3.5"]
    assert catalog._BY_ID["sec_storage_public_blob"]["frameworks"]["cis"] == ["CIS Azure 9.3.8"]


def test_public_catalog_exposes_profile():
    pub = catalog.public_catalog()
    sec = pub["checks"]["security"]
    cis = [c for c in sec if c.get("source") == "cis-v5"]
    assert cis and all(c.get("profile") in ("L1", "L2") for c in cis)


def test_cis_automation_majority_is_graph():
    # After converting the ARG-able controls, most CIS checks should be automated graph
    # checks; only genuinely data-plane / Entra controls remain manual attestations.
    cis = _cis()
    graph = [c for c in cis if (c.get("kind") or "graph") == "graph"]
    manual = [c for c in cis if c.get("kind") == "manual"]
    automated = [c for c in cis if c.get("kind") in ("graph", "graph_api", "arm_rest")]
    assert len(graph) >= 55
    assert len(graph) > len(manual)
    # Counting the Microsoft Graph + ARM REST rails, the vast majority are now automated.
    assert len(automated) >= 70
    assert len(manual) <= 12
    # The private-endpoint controls were automated (not manual).
    for cid in ("cis_8_3_8", "cis_9_3_2_1", "cis_2_1_11"):
        assert catalog._BY_ID[cid].get("kind", "graph") == "graph", cid
        assert "privateendpoints" in catalog._BY_ID[cid]["kql"]


def test_cis_523_tenant_scope_mode():
    c = catalog._BY_ID["cis_5_23"]
    assert c["scope_mode"] == "tenant"
    assert c["arg_table"] == "authorizationresources"
    assert "roledefinitions" in c["kql"]


# --------------------------------------------------------------------------- engine: scope
def test_resolve_scope_effective_subscriptions_from_rg_and_resource():
    sub1 = "11111111-1111-1111-1111-111111111111"
    sub2 = "22222222-2222-2222-2222-222222222222"
    wl = {
        "nodes": [
            {"kind": "resource_group", "subscription_id": f"/subscriptions/{sub1}", "resource_group": "rg1"},
            {"kind": "resource", "id": f"/subscriptions/{sub2}/resourceGroups/rg/providers/x/y/z"},
        ]
    }
    scope = asyncio.run(runner._resolve_scope(wl, None))
    assert set(scope["effective_subscriptions"]) == {sub1, sub2}
    assert "subscriptionId in~" in scope["sub_predicate"]
    assert sub1 in scope["sub_predicate"] and sub2 in scope["sub_predicate"]


# --------------------------------------------------------------------------- engine: existence
def _patch(monkeypatch, rows):
    async def _fake(kql, connection, *, session_config_dir=None, max_rows=5000, page_size=1000):
        return KqlResult(ok=True, rows=rows, complete=True, total=len(rows))
    monkeypatch.setattr(runner, "run_kql_collect", _fake)


def test_existence_fails_subscription_missing_control(monkeypatch):
    chk = catalog._cis_defender("8.1.5.1", "StorageAccounts", "Storage")
    _patch(monkeypatch, [{"subscriptionId": "AAA"}])  # only AAA has the plan
    base = asyncio.run(runner._execute_check(
        chk, "pred", set(), None, None,
        sub_predicate="subscriptionId in~ ('AAA','BBB')", in_scope_subs=["AAA", "BBB"],
    ))
    assert base["status"] == "fail"
    assert base["flagged_count"] == 1
    assert base["flagged_resources"][0]["id"] == "/subscriptions/BBB"
    assert base["flagged_resources"][0]["type"] == "microsoft.resources/subscriptions"


def test_existence_passes_when_all_subscriptions_have_control(monkeypatch):
    chk = catalog._cis_defender("8.1.5.1", "StorageAccounts", "Storage")
    _patch(monkeypatch, [{"subscriptionId": "AAA"}, {"subscriptionId": "BBB"}])
    base = asyncio.run(runner._execute_check(
        chk, "pred", set(), None, None,
        sub_predicate="subscriptionId in~ ('AAA','BBB')", in_scope_subs=["AAA", "BBB"],
    ))
    assert base["status"] == "pass"


def test_existence_not_applicable_without_subscription_scope(monkeypatch):
    chk = catalog._cis_defender("8.1.5.1", "StorageAccounts", "Storage")
    _patch(monkeypatch, [])
    base = asyncio.run(runner._execute_check(chk, "pred", set(), None, None, sub_predicate="", in_scope_subs=[]))
    assert base["status"] == "not_applicable"


def test_existence_query_uses_subscription_predicate(monkeypatch):
    chk = catalog._cis_activity_alert("6.1.2.1", "Microsoft.Authorization/policyAssignments/write", "Create Policy Assignment")
    captured = {}

    async def _fake(kql, connection, *, session_config_dir=None, max_rows=5000, page_size=1000):
        captured["kql"] = kql
        return KqlResult(ok=True, rows=[{"subscriptionId": "AAA"}], complete=True, total=1)

    monkeypatch.setattr(runner, "run_kql_collect", _fake)
    asyncio.run(runner._execute_check(
        chk, "(subscriptionId =~ 'AAA' and resourceGroup =~ 'rg')", set(), None, None,
        sub_predicate="subscriptionId in~ ('AAA')", in_scope_subs=["AAA"],
    ))
    # Activity-log alerts live in Resources but the existence check is subscription-scoped,
    # so it must use the subscription predicate (not the RG-granular resource predicate).
    assert captured["kql"].startswith("Resources | where subscriptionId in~ ('AAA')")


def test_non_resources_table_violation_scoped_by_subscription(monkeypatch):
    chk = catalog._BY_ID["cis_5_27"]
    captured = {}

    async def _fake(kql, connection, *, session_config_dir=None, max_rows=5000, page_size=1000):
        captured["kql"] = kql
        return KqlResult(ok=True, rows=[], complete=True, total=0)

    monkeypatch.setattr(runner, "run_kql_collect", _fake)
    base = asyncio.run(runner._execute_check(
        chk, "pred", set(), None, None,
        sub_predicate="subscriptionId in~ ('AAA')", in_scope_subs=["AAA"],
    ))
    assert captured["kql"].startswith("authorizationresources | where subscriptionId in~ ('AAA')")
    assert base["status"] == "pass"  # no rows → no owner-count violations


def test_tenant_scope_mode_bypasses_scope_predicate(monkeypatch):
    chk = catalog._BY_ID["cis_5_23"]
    captured = {}

    async def _fake(kql, connection, *, session_config_dir=None, max_rows=5000, page_size=1000):
        captured["kql"] = kql
        return KqlResult(ok=True, rows=[], complete=True, total=0)

    monkeypatch.setattr(runner, "run_kql_collect", _fake)
    base = asyncio.run(runner._execute_check(
        chk, "pred", set(), None, None,
        sub_predicate="subscriptionId in~ ('AAA')", in_scope_subs=["AAA"],
    ))
    # scope_mode="tenant" => no subscription filter (custom role definitions have no subscriptionId).
    assert captured["kql"].startswith("authorizationresources | where 1 == 1")
    assert base["status"] == "pass"


# --------------------------------------------------------------- Microsoft Graph (graph_api) kind
def test_graph_api_checks_are_well_formed():
    graph_ids = ["cis_5_1_1", "cis_5_4", "cis_5_14", "cis_5_15", "cis_5_16"]
    for cid in graph_ids:
        chk = catalog._BY_ID[cid]
        assert chk["kind"] == "graph_api", cid
        spec = chk.get("graph_check") or {}
        assert spec.get("path", "").startswith("/policies/"), cid
        assert spec.get("field"), cid
        assert spec.get("op") in ("is_true", "is_false", "equals", "in", "not_equals"), cid


def test_policy_satisfied_comparator():
    assert runner._policy_satisfied(True, "is_true", None)
    assert not runner._policy_satisfied(False, "is_true", None)
    assert runner._policy_satisfied(False, "is_false", None)
    assert runner._policy_satisfied("x", "equals", "x")
    assert runner._policy_satisfied("none", "in", ["none", "adminsAndGuestInviters"])
    assert not runner._policy_satisfied("everyone", "in", ["none"])


def test_drill_dotted_path():
    body = {"defaultUserRolePermissions": {"allowedToCreateApps": True}}
    assert runner._drill(body, "defaultUserRolePermissions.allowedToCreateApps") is True
    assert runner._drill(body, "missing.key") is None


def test_graph_api_pass_when_policy_satisfied(monkeypatch):
    chk = catalog._BY_ID["cis_5_1_1"]  # security defaults: isEnabled is_true

    async def _fake_get(connection, path):
        return True, {"isEnabled": True}, ""

    monkeypatch.setattr(runner, "_graph_get", _fake_get)
    base = asyncio.run(runner._execute_check(chk, "", set(), {"tenant_id": "T"}, None))
    assert base["status"] == "pass"


def test_graph_api_fails_with_tenant_subject(monkeypatch):
    chk = catalog._BY_ID["cis_5_14"]  # allowedToCreateApps is_false

    async def _fake_get(connection, path):
        return True, {"defaultUserRolePermissions": {"allowedToCreateApps": True}}, ""

    monkeypatch.setattr(runner, "_graph_get", _fake_get)
    base = asyncio.run(runner._execute_check(chk, "", set(), {"tenant_id": "T123"}, None))
    assert base["status"] == "fail"
    assert base["flagged_count"] == 1
    subj = base["flagged_resources"][0]
    assert subj["type"] == "microsoft.aad/tenant"
    assert subj["id"] == "/tenants/T123"


def test_graph_api_error_is_fail_closed(monkeypatch):
    chk = catalog._BY_ID["cis_5_4"]

    async def _fake_get(connection, path):
        return False, {}, "Graph token failed (401)."

    monkeypatch.setattr(runner, "_graph_get", _fake_get)
    base = asyncio.run(runner._execute_check(chk, "", set(), {"tenant_id": "T"}, None))
    assert base["status"] == "error"  # never a misleading pass


# ----------------------------------------------------------------- control-plane ARM REST kind
def test_arm_rest_checks_are_well_formed():
    rest = {
        "cis_6_1_1_1": "diag_exists",
        "cis_6_1_1_2": "diag_categories",
        "cis_6_1_1_4": "diag_resource",
        "cis_6_1_1_6": "app_httplogs",
        "cis_2_1_7": "diag_resource",
    }
    for cid, mode in rest.items():
        chk = catalog._BY_ID[cid]
        assert chk["kind"] == "arm_rest", cid
        assert (chk.get("rest_check") or {}).get("mode") == mode, cid


def test_diag_covers_categories():
    settings = [{"properties": {"logs": [
        {"category": "Administrative", "enabled": True},
        {"category": "Security", "enabled": True},
        {"category": "Alert", "enabled": True},
        {"category": "Policy", "enabled": True},
    ]}}]
    assert runner._diag_covers(settings, {"Administrative", "Alert", "Policy", "Security"})
    assert not runner._diag_covers(settings, {"Administrative", "ServiceHealth"})
    # allLogs group satisfies any required set.
    assert runner._diag_covers([{"properties": {"logs": [{"categoryGroup": "allLogs", "enabled": True}]}}], {"Anything"})


def test_diag_resource_ok_existence_and_audit():
    # existence mode (no required): any enabled log suffices.
    assert runner._diag_resource_ok([{"properties": {"logs": [{"category": "dbfs", "enabled": True}]}}], set())
    # audit group satisfies an AuditEvent requirement.
    assert runner._diag_resource_ok([{"properties": {"logs": [{"categoryGroup": "audit", "enabled": True}]}}], {"AuditEvent"})
    # nothing enabled → not ok.
    assert not runner._diag_resource_ok([{"properties": {"logs": [{"category": "x", "enabled": False}]}}], set())


def test_arm_rest_diag_exists_flags_subscriptions_without_settings(monkeypatch):
    chk = catalog._BY_ID["cis_6_1_1_1"]

    async def _fake_arm_token(conn):
        return "tok", None

    async def _fake_get(token, url):
        # AAA has a setting, BBB has none.
        if "/AAA/" in url:
            return True, {"value": [{"id": "s1"}]}, ""
        return True, {"value": []}, ""

    import app.azure.credentials as creds
    monkeypatch.setattr(creds, "get_arm_token", _fake_arm_token)
    monkeypatch.setattr(runner, "_arm_get_token", _fake_get)
    base = asyncio.run(runner._execute_check(
        chk, "pred", set(), {"auth_method": "service_principal"}, None,
        sub_predicate="subscriptionId in~ ('AAA','BBB')", in_scope_subs=["AAA", "BBB"],
    ))
    assert base["status"] == "fail"
    assert base["flagged_count"] == 1
    assert base["flagged_resources"][0]["subscription_id"] == "BBB"


def test_arm_rest_diag_exists_not_applicable_without_subs(monkeypatch):
    chk = catalog._BY_ID["cis_6_1_1_1"]
    import app.azure.credentials as creds

    async def _fake_arm_token(conn):
        return "tok", None

    monkeypatch.setattr(creds, "get_arm_token", _fake_arm_token)
    base = asyncio.run(runner._execute_check(
        chk, "", set(), {"auth_method": "service_principal"}, None,
        sub_predicate="", in_scope_subs=[],
    ))
    assert base["status"] == "not_applicable"


