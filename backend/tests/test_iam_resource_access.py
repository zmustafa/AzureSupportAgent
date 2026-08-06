"""P4/P6: "who can access this resource" in the Inventory drawer.

This surface is read by somebody looking at one resource and deciding whether it is exposed.
The failure modes are all the same shape — a short list read as a safe resource:

  - inherited access omitted, so a resource anyone can delete reports "nobody";
  - a never-scanned tenant rendering an empty list instead of a wall;
  - deny assignments counted as access;
  - RBAC answered alone, on a resource whose shared keys are wide open.
"""
from __future__ import annotations

import pytest

from app.iam import cache, resource_access, schema

SUB = "11111111-1111-1111-1111-111111111111"
RG = f"/subscriptions/{SUB}/resourceGroups/rg1"
RES = f"{RG}/providers/Microsoft.Storage/storageAccounts/sa1"
MG = "/providers/Microsoft.Management/managementGroups/root"


@pytest.fixture()
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_DATA", tmp_path)
    monkeypatch.setattr(cache, "_INDEX", tmp_path / "iam_cache.json")
    monkeypatch.setattr(cache, "_BLOBS", tmp_path / "iam")
    monkeypatch.setattr(cache, "_migrated", True)
    return tmp_path


def _row(**kw):
    base = dict(
        surface=schema.SURFACE_AZURE_RBAC,
        effect=schema.EFFECT_ALLOW,
        assignmentState=schema.STATE_ACTIVE,
        accessPath=schema.PATH_DIRECT,
        principalId="alice",
        effectivePrincipalId="alice",
        effectivePrincipalName="Alice",
        effectivePrincipalType="User",
        roleDefinitionId="/rd/reader",
        roleName="Reader",
        roleIsPrivileged=False,
        scope=RES,
        assignmentId="a1",
        principalExists=schema.EXISTS_TRUE,
    )
    base.update(kw)
    return schema.make_row(**base)


def _seed(monkeypatch, rows, *, tenant="t1", swept=False, mg_rows=None):
    monkeypatch.setattr(cache, "has_any", lambda t: True)
    monkeypatch.setattr(resource_access.compose, "build_master_rows", lambda t: rows)
    # The raw pre-dedupe rows are where MG ancestry comes from: ARM returns an MG assignment as
    # an inherited copy inside every child subscription's slice.
    monkeypatch.setattr(cache, "all_scope_rows", lambda t: mg_rows or [])
    if not swept:
        monkeypatch.setattr(cache, "read_bypass_meta", lambda t: {})
        monkeypatch.setattr(cache, "read_bypass", lambda t: {"resources": [], "rows": [], "summary": {}})


def _mg_ancestry(mg_scope=MG, sub=SUB):
    """One inherited copy, exactly as ARM reports it under a child subscription."""
    return [{"scopeType": schema.SCOPE_MANAGEMENT_GROUP, "scope": mg_scope, "subscriptionId": sub}]


# =========================================================================== inheritance
def test_access_inherited_from_the_subscription_is_reported(isolated_cache, monkeypatch):
    """The substance of the answer. Almost nobody is assigned at a resource — they are Owner on
    the subscription. A view showing only assignments written AT the resource would report
    "nobody" for a resource anyone can delete."""
    _seed(monkeypatch, [_row(effectivePrincipalId="bob", effectivePrincipalName="Bob",
                             roleName="Owner", roleIsPrivileged=True,
                             scope=f"/subscriptions/{SUB}")])
    out = resource_access.for_resource("t1", RES)
    assert out["total"] == 1
    assert out["principals"][0]["principalName"] == "Bob"
    assert out["principals"][0]["grants"][0]["grantedAt"] == "subscription"


def test_access_inherited_from_a_management_group_is_reported(isolated_cache, monkeypatch):
    """The case pure scope arithmetic gets WRONG, and in the dangerous direction.

    `/providers/Microsoft.Management/managementGroups/root` is not a string prefix of
    `/subscriptions/...`, so `scope_covers` alone reports that an MG Owner cannot reach anything
    inside it. On a real tenant that hid 300 grants — the broadest in the estate — from
    every resource, making each one look less exposed than it is."""
    _seed(monkeypatch, [_row(scope=MG)], mg_rows=_mg_ancestry())
    out = resource_access.for_resource("t1", RES)
    assert out["total"] == 1
    assert out["principals"][0]["grants"][0]["grantedAt"] == "management group"


def test_a_management_group_over_a_different_subscription_does_not_reach_here(isolated_cache, monkeypatch):
    """The ancestry must be a real edge, not "any MG reaches any resource"."""
    other = "99999999-9999-9999-9999-999999999999"
    _seed(monkeypatch, [_row(scope=MG)], mg_rows=_mg_ancestry(sub=other))
    assert resource_access.for_resource("t1", RES)["total"] == 0


def test_missing_management_group_ancestry_is_declared_not_silently_dropped(isolated_cache, monkeypatch):
    """Under-reporting here removes exactly the broadest grants, so silence is the worst
    possible failure mode."""
    _seed(monkeypatch, [_row(scope=f"/subscriptions/{SUB}")], mg_rows=[])
    out = resource_access.for_resource("t1", RES)
    assert any("management-group ancestry" in l for l in out["limitations"])


def test_an_assignment_at_the_resource_itself_says_so(isolated_cache, monkeypatch):
    _seed(monkeypatch, [_row(scope=RES)])
    out = resource_access.for_resource("t1", RES)
    assert out["principals"][0]["grants"][0]["grantedAt"] == "this resource"


def test_a_sibling_resource_is_not_reported_as_access(isolated_cache, monkeypatch):
    """Segment-wise containment. A raw prefix match would make `.../sa1` cover `.../sa10`."""
    _seed(monkeypatch, [_row(scope=f"{RG}/providers/Microsoft.Storage/storageAccounts/sa10")])
    assert resource_access.for_resource("t1", RES)["total"] == 0


def test_a_neighbouring_subscription_does_not_reach_this_resource(isolated_cache, monkeypatch):
    _seed(monkeypatch, [_row(scope="/subscriptions/22222222-2222-2222-2222-222222222222")])
    assert resource_access.for_resource("t1", RES)["total"] == 0


def test_inheritance_is_called_out_as_a_limitation(isolated_cache, monkeypatch):
    """Removing inherited access means editing the assignment where it was made, which affects
    every other resource under that scope. Somebody acting on this panel has to know that."""
    _seed(monkeypatch, [_row(scope=f"/subscriptions/{SUB}")])
    out = resource_access.for_resource("t1", RES)
    assert any("inherited" in l for l in out["limitations"])


# =========================================================================== blindness
def test_a_never_scanned_tenant_is_a_wall_not_an_empty_list(isolated_cache, monkeypatch):
    """An empty access list on a resource is the most reassuring thing this drawer could
    render, and on an unscanned tenant it would be a lie."""
    monkeypatch.setattr(cache, "has_any", lambda t: False)
    out = resource_access.for_resource("t1", RES)
    assert out["measured"] is False
    assert out["principals"] == []
    assert "not nobody" in out["reason"]


def test_an_empty_resource_id_does_not_match_everything(isolated_cache, monkeypatch):
    """`scope_covers("/", "")` is true for the tenant root, so an empty target would otherwise
    return every root-scoped grant in the tenant as access to a resource that was not named."""
    _seed(monkeypatch, [_row(scope="/")])
    out = resource_access.for_resource("t1", "")
    assert out["measured"] is False
    assert out["principals"] == []


# =========================================================================== deny
def test_a_deny_assignment_is_not_counted_as_access(isolated_cache, monkeypatch):
    _seed(monkeypatch, [
        _row(effectivePrincipalId="bob", effectivePrincipalName="Bob"),
        _row(effectivePrincipalId="eve", effectivePrincipalName="Eve",
             effect=schema.EFFECT_DENY, assignmentId="a2"),
    ])
    out = resource_access.for_resource("t1", RES)
    assert [p["principalName"] for p in out["principals"]] == ["Bob"]
    assert any("deny assignment" in l for l in out["limitations"])


# =========================================================================== grouping
def test_one_principal_with_several_roles_is_one_row_with_several_grants(isolated_cache, monkeypatch):
    _seed(monkeypatch, [
        _row(roleName="Reader"),
        _row(roleName="Contributor", assignmentId="a2", scope=f"/subscriptions/{SUB}"),
    ])
    out = resource_access.for_resource("t1", RES)
    assert out["total"] == 1
    assert {g["roleName"] for g in out["principals"][0]["grants"]} == {"Reader", "Contributor"}


def test_privileged_principals_sort_first(isolated_cache, monkeypatch):
    _seed(monkeypatch, [
        _row(effectivePrincipalId="a", effectivePrincipalName="Aaron"),
        _row(effectivePrincipalId="z", effectivePrincipalName="Zoe",
             roleName="Owner", roleIsPrivileged=True, assignmentId="a2"),
    ])
    out = resource_access.for_resource("t1", RES)
    assert [p["principalName"] for p in out["principals"]] == ["Zoe", "Aaron"]
    assert out["privilegedTotal"] == 1


def test_the_list_is_capped_and_says_so(isolated_cache, monkeypatch):
    rows = [_row(effectivePrincipalId=f"p{i}", effectivePrincipalName=f"P{i}", assignmentId=f"a{i}")
            for i in range(resource_access.MAX_PRINCIPALS + 5)]
    _seed(monkeypatch, rows)
    out = resource_access.for_resource("t1", RES)
    assert out["total"] == resource_access.MAX_PRINCIPALS + 5
    assert len(out["principals"]) == resource_access.MAX_PRINCIPALS
    assert any("Showing the first" in l for l in out["limitations"])


# =========================================================================== bypass
def test_a_resource_with_no_bypass_sweep_reports_unknown_not_none(isolated_cache, monkeypatch):
    """RBAC is not the only door. A resource whose access list is short but whose shared keys
    are on is not locked down, and "we never swept" must not read as "no other way in"."""
    _seed(monkeypatch, [_row()])
    out = resource_access.for_resource("t1", RES)
    assert out["bypass"]["measured"] is False
    assert "unknown" in out["bypass"]["reason"]


def test_an_enabled_bypass_on_this_resource_is_returned(isolated_cache, monkeypatch):
    _seed(monkeypatch, [_row()], swept=True)
    monkeypatch.setattr(cache, "read_bypass_meta", lambda t: {"generated_at": "now"})
    monkeypatch.setattr(cache, "read_bypass", lambda t: {"resources": [], "summary": {}, "rows": [
        {"resourceId": RES, "key": "storage.shared_key", "title": "Shared key enabled",
         "enabled": True, "severity": "error", "bypassKind": "SharedKey",
         "credentialAction": "Microsoft.Storage/storageAccounts/listKeys/action",
         "reachableCount": 9, "reachabilityAvailable": True},
        {"resourceId": RES, "key": "storage.public_blob", "title": "Anonymous blob",
         "enabled": False, "severity": "error"},
    ]})
    out = resource_access.for_resource("t1", RES)
    assert out["bypass"]["measured"] is True
    assert out["bypass"]["checked"] == 2
    assert [d["key"] for d in out["bypass"]["openDoors"]] == ["storage.shared_key"]
    assert out["bypass"]["openDoors"][0]["reachableCount"] == 9


def test_a_bypass_on_a_different_resource_is_not_attributed_here(isolated_cache, monkeypatch):
    _seed(monkeypatch, [_row()], swept=True)
    monkeypatch.setattr(cache, "read_bypass_meta", lambda t: {"generated_at": "now"})
    monkeypatch.setattr(cache, "read_bypass", lambda t: {"resources": [], "summary": {}, "rows": [
        {"resourceId": f"{RG}/providers/Microsoft.Storage/storageAccounts/other",
         "key": "storage.shared_key", "enabled": True, "severity": "error"},
    ]})
    out = resource_access.for_resource("t1", RES)
    assert out["bypass"]["checked"] == 0
    assert out["bypass"]["openDoors"] == []


def test_a_swept_resource_with_every_door_shut_is_measured_and_empty(isolated_cache, monkeypatch):
    """The one case where an empty bypass list IS good news — and it is only distinguishable
    from the unmeasured case because the sweep left a meta entry behind."""
    _seed(monkeypatch, [_row()], swept=True)
    monkeypatch.setattr(cache, "read_bypass_meta", lambda t: {"generated_at": "now"})
    monkeypatch.setattr(cache, "read_bypass", lambda t: {"resources": [], "summary": {}, "rows": [
        {"resourceId": RES, "key": "storage.shared_key", "enabled": False, "severity": "error"},
    ]})
    out = resource_access.for_resource("t1", RES)
    assert out["bypass"]["measured"] is True
    assert out["bypass"]["openDoors"] == []
    assert out["bypass"]["reason"] == ""


# =========================================================================== routing
def test_no_two_iam_routes_share_a_path_and_method():
    """FastAPI accepts a duplicate route silently and serves whichever registered first.

    This surface was first mounted on `/iam/resource-access`, which already answers the narrower
    "who can perform this ACTION here". Both handlers were even named `resource_access`. The
    action-level pivot would have started returning this payload instead, with nothing failing
    anywhere — no import error, no startup warning, and a passing test suite."""
    from app.main import app

    seen: dict[tuple[str, str], str] = {}
    for route in app.routes:
        path = getattr(route, "path", "")
        if "/iam/" not in path:
            continue
        for method in getattr(route, "methods", set()) or set():
            key = (method, path)
            clash = seen.get(key)
            assert clash is None, (
                f"{method} {path} is registered twice: {clash} and "
                f"{getattr(route, 'name', '?')}. The first one wins and the second is dead."
            )
            seen[key] = getattr(route, "name", "?")
