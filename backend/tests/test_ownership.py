"""Phase 1 — ownership registry + effective-owner resolution engine.

Covers: owner/assignment CRUD + soft-delete Trash + tenant isolation + owner-purge
cascade; and the resolve precedence (direct → tag → workload → inherited ancestor →
unowned) incl. ARM-id ancestor parsing and subscription id-variant matching."""
from __future__ import annotations

import pytest

from app.ownership import registry, resolve

SUB = "11111111-1111-1111-1111-111111111111"
RID = f"/subscriptions/{SUB}/resourceGroups/rg-prod/providers/Microsoft.Web/sites/web1"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "_OWNERS_PATH", tmp_path / "owners.json")
    monkeypatch.setattr(registry, "_ASSIGNMENTS_PATH", tmp_path / "assignments.json")
    yield


def _owner(name="John Doe", tenant="t1", kind="person", **extra):
    return registry.upsert_owner(tenant, {"display_name": name, "kind": kind, **extra})


def _assign(tenant, owner_id, subject_kind, subject_id, **extra):
    return registry.upsert_assignment(tenant, {
        "owner_id": owner_id, "subject_kind": subject_kind, "subject_id": subject_id, **extra,
    })


# ------------------------------------------------------------------- registry CRUD
def test_owner_crud_and_soft_delete_trash():
    o = _owner()
    assert o["display_name"] == "John Doe"
    assert registry.list_owners("t1")[0]["id"] == o["id"]

    assert registry.delete_owner("t1", o["id"], actor="alice") is True
    assert registry.list_owners("t1") == []           # hidden from active
    assert registry.get_owner("t1", o["id"]) is None
    trashed = registry.list_trashed_owners("t1")
    assert [t["id"] for t in trashed] == [o["id"]]
    assert trashed[0]["deleted_by"] == "alice"

    assert registry.restore_owner("t1", o["id"])["deleted_at"] == ""
    assert len(registry.list_owners("t1")) == 1
    # double-delete returns False
    registry.delete_owner("t1", o["id"])
    assert registry.delete_owner("t1", o["id"]) is False


def test_owner_purge_cascades_assignments():
    o = _owner()
    a = _assign("t1", o["id"], "resource", RID)
    assert registry.list_assignments("t1", owner_id=o["id"])
    registry.delete_owner("t1", o["id"])
    assert registry.purge_owner("t1", o["id"]) is True
    # the assignment referencing the purged owner is gone too
    assert registry.get_assignment("t1", a["id"], include_deleted=True) is None


def test_tenant_isolation():
    a = _owner(name="A", tenant="t1")
    _owner(name="B", tenant="t2")
    assert {o["display_name"] for o in registry.list_owners("t1")} == {"A"}
    assert registry.get_owner("t2", a["id"]) is None  # cross-tenant get denied


def test_assignment_trash_lifecycle():
    o = _owner()
    a = _assign("t1", o["id"], "subscription", SUB)
    assert registry.delete_assignment("t1", a["id"]) is True
    assert registry.list_assignments("t1") == []
    assert registry.restore_assignment("t1", a["id"])["deleted_at"] == ""
    registry.delete_assignment("t1", a["id"])
    assert registry.purge_assignment("t1", a["id"]) is True
    assert registry.get_assignment("t1", a["id"], include_deleted=True) is None


# ------------------------------------------------------------------- ARM parsing
def test_parse_arm_scopes_resource():
    chain = resolve.parse_arm_scopes(RID)
    assert [c["kind"] for c in chain] == ["resource_group", "subscription"]
    assert chain[0]["id"].endswith("/resourcegroups/rg-prod")
    assert chain[1]["id"] == f"/subscriptions/{SUB}"


def test_parse_arm_scopes_rg_scope_excludes_self():
    rg_id = f"/subscriptions/{SUB}/resourceGroups/rg-prod"
    chain = resolve.parse_arm_scopes(rg_id)
    assert [c["kind"] for c in chain] == ["subscription"]


def test_sub_guid_and_rg_helpers():
    assert resolve.sub_guid(RID) == SUB
    assert resolve.rg_of(RID) == "rg-prod"
    assert resolve.sub_guid(SUB) == SUB        # bare guid passes through


# ------------------------------------------------------------------- resolution precedence
def test_resolve_direct_assignment_wins():
    o = _owner()
    _assign("t1", o["id"], "resource", RID, primary=True)
    res = resolve.resolve_owner("t1", "resource", RID, tags={"owner": "tagguy@x.com"})
    assert res["source"] == "direct"
    assert res["owners"][0]["display_name"] == "John Doe"
    assert res["unowned"] is False


def test_resolve_tag_when_no_assignment():
    res = resolve.resolve_owner("t1", "resource", RID, tags={"Owner": "ops@contoso.com"})
    assert res["source"] == "tag"
    assert res["owners"][0]["email"] == "ops@contoso.com"


def test_resolve_inherited_from_subscription():
    o = _owner(name="Platform Team", kind="team")
    _assign("t1", o["id"], "subscription", SUB, primary=True)
    # No direct/tag owner on the resource → inherits the subscription owner.
    res = resolve.resolve_owner("t1", "resource", RID)
    assert res["source"] == "inherited"
    assert res["inherited_from"]["kind"] == "subscription"
    assert res["owners"][0]["display_name"] == "Platform Team"


def test_resolve_inherited_prefers_resource_group_over_subscription():
    sub_owner = _owner(name="Sub Owner")
    rg_owner = _owner(name="RG Owner")
    _assign("t1", sub_owner["id"], "subscription", SUB)
    _assign("t1", rg_owner["id"], "resource_group", f"/subscriptions/{SUB}/resourceGroups/rg-prod")
    res = resolve.resolve_owner("t1", "resource", RID)
    assert res["source"] == "inherited"
    assert res["inherited_from"]["kind"] == "resource_group"
    assert res["owners"][0]["display_name"] == "RG Owner"


def test_resolve_unowned():
    res = resolve.resolve_owner("t1", "resource", RID)
    assert res["unowned"] is True
    assert res["source"] == "none"
    assert res["owners"] == []


def test_resolve_via_workload_membership(monkeypatch):
    """A resource that belongs to an OWNED workload inherits the workload's owner — even
    across the dev 'default' vs real-tenant id mismatch (workloads are global)."""
    import app.workloads.registry as wlreg

    wl = {"id": "wl-apricer", "name": "APricer", "tenant_id": "739fb5dd-real-tenant",
          "nodes": [{"kind": "resource", "id": RID}]}
    monkeypatch.setattr(wlreg, "list_workloads", lambda *a, **k: [wl])

    o = _owner(name="App Team", kind="team")
    _assign("t1", o["id"], "workload", "wl-apricer", primary=True)
    res = resolve.resolve_owner("t1", "resource", RID)
    assert res["source"] == "workload"
    assert res["inherited_from"]["kind"] == "workload"
    assert res["owners"][0]["display_name"] == "App Team"


def test_subscription_assignment_matches_bare_guid_and_scope_path():
    o = _owner()
    # Stored as a bare GUID; a /subscriptions/<guid> scope query must still match.
    _assign("t1", o["id"], "subscription", SUB)
    res = resolve.resolve_owner("t1", "subscription", f"/subscriptions/{SUB}")
    assert res["source"] == "direct"
    assert res["owners"][0]["owner_id"] == o["id"]


def test_resolve_label_helper():
    o = _owner(name="Jane")
    _assign("t1", o["id"], "resource", RID, primary=True)
    assert resolve.resolve_label("t1", "resource", RID) == "Jane"
    assert resolve.resolve_label("t1", "resource", "/subscriptions/x/y") == ""


# ------------------------------------------------------------------- API handlers (in-process)
class _FakeDB:
    """Minimal stand-in for the AsyncSession the audit helper touches."""
    def add(self, _row):  # noqa: D401
        pass

    async def commit(self):
        pass


class _Principal:
    def __init__(self, tenant="t1", subject="dev"):
        self.tenant_id = tenant
        self.subject = subject
        self.is_admin = True


async def test_api_owner_assignment_resolve_roundtrip():
    from app.api import ownership as api

    p = _Principal()
    db = _FakeDB()
    owner = await api.upsert_owner(api.OwnerIn(display_name="John Doe", email="john@x.com"), p, db)
    assert owner["display_name"] == "John Doe"

    listed = await api.list_owners(p)
    assert listed["total"] == 1 and listed["owners"][0]["assignment_count"] == 0

    a = await api.upsert_assignment(api.AssignmentIn(
        owner_id=owner["id"], subject_kind="resource", subject_id=RID, primary=True,
    ), p, db)
    assert a["owner_id"] == owner["id"]

    res = await api.resolve_one(p, subject_kind="resource", subject_id=RID)
    assert res["source"] == "direct" and res["owners"][0]["display_name"] == "John Doe"

    # owner now shows an assignment count
    listed2 = await api.list_owners(p)
    assert listed2["owners"][0]["assignment_count"] == 1


async def test_api_assignment_rejects_unknown_owner():
    from fastapi import HTTPException

    from app.api import ownership as api

    p = _Principal()
    db = _FakeDB()
    with pytest.raises(HTTPException):
        await api.upsert_assignment(api.AssignmentIn(
            owner_id="nope", subject_kind="resource", subject_id=RID,
        ), p, db)


async def test_api_transfer_moves_assignments():
    from app.api import ownership as api

    p = _Principal()
    db = _FakeDB()
    a = await api.upsert_owner(api.OwnerIn(display_name="A"), p, db)
    b = await api.upsert_owner(api.OwnerIn(display_name="B"), p, db)
    await api.upsert_assignment(api.AssignmentIn(owner_id=a["id"], subject_kind="subscription", subject_id=SUB), p, db)
    out = await api.transfer(api.TransferIn(from_owner_id=a["id"], to_owner_id=b["id"]), p, db)
    assert out["moved"] == 1
    assert registry.list_assignments("t1", owner_id=b["id"])
    assert registry.list_assignments("t1", owner_id=a["id"]) == []


# ------------------------------------------------------------------- directory people-picker
async def test_directory_search_app_users(tmp_path):
    """search_app_users finds SSO-provisioned + local users via a temp sqlite engine."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.db import Base
    from app.models.auth import User
    from app.ownership import directory

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'd.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        db.add(User(email="alice@corp.com", username="alice", display_name="Alice Lane",
                    auth_source="oidc", external_idp="idp1", external_id="sub-1", tenant_id="t1"))
        db.add(User(email="bob@corp.com", username="bob", display_name="Bob",
                    auth_source="local", tenant_id="t1"))
        await db.commit()

        all_hits = await directory.search_app_users(db, "t1", "")
        assert {h["display_name"] for h in all_hits} == {"Alice Lane", "Bob"}

        sso = await directory.search_app_users(db, "t1", "", sso_only=True)
        assert [h["display_name"] for h in sso] == ["Alice Lane"]
        assert sso[0]["source"] == "app_user"
        assert sso[0]["link"]["user_id"] and sso[0]["link"]["external_id"] == "sub-1"

        # query filter + Entra disabled => combined picker returns just the app user
        combined = await directory.search_directory(db, None, "t1", "alice", include_entra=False)
        assert combined["counts"]["app_users"] == 1
        assert combined["results"][0]["email"] == "alice@corp.com"
    await engine.dispose()


# ------------------------------------------------------------------- coverage + policy
def test_coverage_compute_buckets_and_policy():
    from app.ownership import coverage

    o = _owner(name="Platform")
    _assign("t1", o["id"], "subscription", SUB)  # whole-sub owner → resources inherit
    resources = [
        {"id": RID, "name": "web1", "type": "microsoft.web/sites", "subscriptionId": SUB,
         "resourceGroup": "rg-prod", "tags": {}},
        {"id": f"/subscriptions/{SUB}/resourceGroups/rg-prod/providers/Microsoft.Storage/storageAccounts/st1",
         "name": "st1", "type": "microsoft.storage/storageaccounts", "subscriptionId": SUB,
         "resourceGroup": "rg-prod", "tags": {"owner": "tagonly@x.com", "environment": "production"}},
        {"id": "/subscriptions/99999999-9999-9999-9999-999999999999/resourceGroups/rg-x/providers/Microsoft.Web/sites/orphan",
         "name": "orphan", "type": "microsoft.web/sites", "subscriptionId": "99999999-9999-9999-9999-999999999999",
         "resourceGroup": "rg-x", "tags": {"environment": "production"}},
    ]
    snap = coverage.compute_coverage(resources, tenant_id="t1", scope_kind="subscription", scope_id=SUB)
    # web1 inherits the sub owner; st1 owned via tag; orphan (other sub) is unowned + production.
    assert snap["kpis"]["total"] == 3
    assert snap["kpis"]["owned"] == 2
    assert snap["kpis"]["unowned"] == 1
    assert snap["by_source"]["inherited"] == 1
    assert snap["by_source"]["tag"] == 1
    assert snap["kpis"]["orphan_owners"] == 1   # st1 tag owner not in directory
    assert snap["kpis"]["prod_unowned"] == 1    # the orphan resource
    ids = {f["id"] for f in snap["findings"]}
    assert "prod_unowned" in ids and "unowned_resources" in ids and "orphan_tag_owners" in ids


def test_coverage_empty_snapshot():
    from app.ownership import coverage

    snap = coverage.empty_snapshot("workload", "w1", never_loaded=True)
    assert snap["never_loaded"] is True and snap["coverage_pct"] is None and snap["kpis"]["total"] == 0


# ------------------------------------------------------------------- agent tools
async def test_agent_tool_who_owns_and_what_owns():
    from app.ownership.agent_tool import build_ownership_tools

    o = _owner(name="Jane Ops", email="jane@x.com")
    _assign("t1", o["id"], "resource", RID, primary=True, subject_name="web1")
    tools = {t.name: t for t in build_ownership_tools("t1")}

    who = await tools["who_owns"].handler({}, {"subject_id": RID})
    assert "Jane Ops" in who["content"][0] and "direct" in who["content"][0].lower()

    unowned = await tools["who_owns"].handler({}, {"subject_id": "/subscriptions/zzz/x"})
    assert "UNOWNED" in unowned["content"][0]

    what = await tools["what_does_owner_own"].handler({}, {"owner": "jane"})
    assert "Jane Ops" in what["content"][0] and "web1" in what["content"][0]


# ------------------------------------------------------------------- demo seed
def test_demo_seed_and_purge():
    from app.ownership import demo

    res = demo.seed_demo("t1")
    assert res["owners"] >= 4 and res["assignments"] >= 4
    assert demo.is_seeded("t1") is True
    # idempotent — re-seed doesn't duplicate
    demo.seed_demo("t1")
    owners = registry.list_owners("t1")
    assert len([o for o in owners if o["id"].startswith("own-demo-")]) == res["owners"]
    # the Contoso workload resolves to the seeded Platform Team (primary)
    r = resolve.resolve_owner("t1", "workload", "demo-amba-coverage")
    assert any(o["display_name"] == "Platform Team" for o in r["owners"])
    removed = demo.purge_demo("t1")
    assert removed >= 8  # 4 owners + 4 assignments
    assert demo.is_seeded("t1") is False


# ------------------------------------------------------------------- suggestions
def test_suggestions_from_rbac(monkeypatch):
    import app.workloads.registry as wlreg
    from app.ownership import suggest

    wl = {"id": "wl1", "name": "Shop", "nodes": [{"kind": "subscription", "id": SUB, "subscription_id": SUB}]}
    monkeypatch.setattr(wlreg, "list_workloads", lambda *a, **k: [wl])
    # Fake an RBAC cached scan: Grace has Owner on the subscription.
    monkeypatch.setattr(
        "app.rbac.compose.build_master_rows",
        lambda tid: [{
            "subscriptionId": SUB, "roleName": "Owner", "roleIsPrivileged": True,
            "effectivePrincipalName": "Grace Hopper", "effectivePrincipalId": "p-grace",
            "effectivePrincipalType": "user", "effectivePrincipalUserPrincipalName": "grace@x.com",
        }],
    )
    sugg = suggest.suggest_for_tenant("t1")
    assert sugg and sugg[0]["candidate"]["display_name"] == "Grace Hopper"
    assert sugg[0]["subject_id"] == "wl1"
    assert sugg[0]["confidence"] >= 0.85
    assert any("Owner" in e for e in sugg[0]["evidence"])


def test_suggestions_from_owner_tags(monkeypatch):
    """Owner-tag suggestions read the cached inventory and propose the dominant owner for an
    unowned workload (no RBAC scan needed)."""
    import app.inventory.cache as invcache
    from app.ownership import suggest

    wl_id = "wl-shop"
    # Fake a cached inventory snapshot with owner-tagged resources linked to the workload.
    fake = {
        "default|conn1": {"payload": {"resources": [
            {"id": "/r/a", "name": "a", "tags": {"owner": "platform-team"}, "workloads": [{"id": wl_id, "name": "Shop"}]},
            {"id": "/r/b", "name": "b", "tags": {"owner": "platform-team"}, "workloads": [{"id": wl_id, "name": "Shop"}]},
            {"id": "/r/c", "name": "c", "tags": {"Owner": "someone-else"}, "workloads": [{"id": wl_id, "name": "Shop"}]},
            {"id": "/r/url", "name": "u", "tags": {"owner": "https://aka.ms/x"}, "workloads": [{"id": wl_id, "name": "Shop"}]},
        ]}},
    }
    monkeypatch.setattr(invcache, "_load", lambda: fake)
    out = suggest.inventory_tag_suggestions("default")
    shop = [s for s in out if s["subject_id"] == wl_id]
    assert shop, "expected a suggestion for the unowned workload"
    assert shop[0]["candidate"]["display_name"] == "platform-team"  # dominant (2 of 3)
    assert shop[0]["candidate"]["kind"] == "team"                    # 'team' in the name
    assert shop[0]["signal"] == "inventory_tag"
    # the URL owner tag was filtered out (not suggested)
    assert not any(s["candidate"]["display_name"].startswith("http") for s in out)


# ------------------------------------------------------------------- attestation
async def test_attest_and_status():
    o = _owner()
    a = _assign("t1", o["id"], "subscription", SUB)
    assert a["attested_at"] == ""
    attested = registry.attest_assignment("t1", a["id"], actor="dev")
    assert attested["attested_at"] and attested["attested_by"] == "dev"


# ------------------------------------------------------------------- tag write-back
async def test_writeback_disabled_by_default(monkeypatch):
    from app.ownership import writeback

    # Default setting is off → fail-closed, never calls Azure.
    res = await writeback.apply_owner_tag({"id": "c"}, resource_id=RID, owner="John")
    assert res["ok"] is False and "disabled" in res["error"].lower()


def test_writeback_bicep_and_policy():
    from app.ownership import writeback

    b = writeback.bicep_for(RID, "Platform Team", "team@x.com")
    assert "Microsoft.Resources/tags" in b and "Platform Team" in b and "owner-email" in b
    p = writeback.policy_for("Platform Team")
    assert "modify" in p and "tags['owner']" in p


# ------------------------------------------------------------------- section scope
def test_scope_predicate_workload_and_subscription(monkeypatch):
    from app.api import ownership as api

    import app.workloads.registry as wlreg
    wl_in = {"id": "wl-in", "name": "In", "nodes": [{"kind": "subscription", "id": SUB, "subscription_id": SUB}]}
    wl_out = {"id": "wl-out", "name": "Out", "nodes": [{"kind": "subscription", "id": "99999999-9999-9999-9999-999999999999"}]}
    monkeypatch.setattr(wlreg, "list_workloads", lambda *a, **k: [wl_in, wl_out])
    monkeypatch.setattr("app.architectures.registry.list_architectures", lambda *a, **k: [])

    # workload scope → only that workload subject matches
    pred = api._scope_predicate("t1", "workload", "wl-in", "")
    assert pred("workload", "wl-in") is True
    assert pred("workload", "wl-out") is False
    assert pred("architecture", "anything") is False

    # subscription scope → workloads touching the sub + resources in the sub
    pred = api._scope_predicate("t1", "subscription", "", SUB)
    assert pred("workload", "wl-in") is True
    assert pred("workload", "wl-out") is False
    assert pred("resource", RID) is True                       # RID is in SUB
    assert pred("resource", "/subscriptions/zzz/x") is False

    # tenant → everything
    pred = api._scope_predicate("t1", "tenant", "", "")
    assert pred("workload", "wl-out") is True and pred("resource", "/x") is True


# ------------------------------------------------------------------- delegation (RACI)
def test_active_delegation_surfaces_in_resolution():
    from datetime import date, timedelta

    deleg = _owner(name="Cover Person")
    o = _owner(name="Primary", delegate={"owner_id": deleg["id"], "until": (date.today() + timedelta(days=5)).isoformat(), "reason": "vacation"})
    _assign("t1", o["id"], "resource", RID, primary=True)
    res = resolve.resolve_owner("t1", "resource", RID)
    assert res["owners"][0]["delegate"]["owner_id"] == deleg["id"]


def test_expired_delegation_ignored():
    from datetime import date, timedelta

    deleg = _owner(name="Old Cover")
    o = _owner(name="Primary2", delegate={"owner_id": deleg["id"], "until": (date.today() - timedelta(days=1)).isoformat()})
    _assign("t1", o["id"], "subscription", SUB, primary=True)
    res = resolve.resolve_owner("t1", "subscription", SUB)
    assert res["owners"][0]["delegate"] is None


# ------------------------------------------------------------------- connection scoping (Option A)
def _principal(tenant="t1"):
    from app.core.security import Principal
    return Principal(subject="u", email="u@local", tenant_id=tenant, role="admin")


def test_own_seam_keeps_owner_registry_on_app_tenant(monkeypatch):
    """The connection picker NEVER repartitions the owner registry — owner_tenant is always
    the app principal's tenant regardless of which connection is selected (Option A)."""
    from app.api import ownership as api
    import app.core.azure_connections as az

    conn_a = {"id": "ca", "tenant_id": "azure-A"}
    conn_b = {"id": "cb", "tenant_id": "azure-B"}
    monkeypatch.setattr(az, "get_connection", lambda cid: {"ca": conn_a, "cb": conn_b}.get(cid))
    monkeypatch.setattr(az, "get_default_connection", lambda: conn_a)

    _conn, owner_tenant_a, cid_a = api._own(_principal("t1"), "ca")
    _conn, owner_tenant_b, cid_b = api._own(_principal("t1"), "cb")
    assert owner_tenant_a == "t1" and owner_tenant_b == "t1"   # registry shared across connections
    assert cid_a == "ca" and cid_b == "cb"


def test_scan_tenant_partitions_cache_by_connection_tenant():
    """The coverage CACHE key is partitioned by the connection's Azure tenant so the same
    subscription id under two connections can't collide; falls back to the app tenant when a
    connection has no tenant_id."""
    from app.api import ownership as api

    assert api._scan_tenant("t1", {"tenant_id": "azure-A"}) == "t1::azure-A"
    assert api._scan_tenant("t1", {"tenant_id": "azure-B"}) == "t1::azure-B"
    assert api._scan_tenant("t1", {"tenant_id": ""}) == "t1"   # tenant-less (pasted-token) connection
    assert api._scan_tenant("t1", None) == "t1"                # no connection configured


def test_resolve_scope_inputs_honors_explicit_connection(monkeypatch):
    """An explicit connection_id ALWAYS wins for subscription scope (the bug this fixes:
    subscription scope used to silently use the default connection)."""
    from app.api import ownership as api
    import app.core.azure_connections as az

    conn_a = {"id": "ca", "tenant_id": "azure-A"}
    conn_b = {"id": "cb", "tenant_id": "azure-B"}
    monkeypatch.setattr(az, "get_connection", lambda cid: {"ca": conn_a, "cb": conn_b}.get(cid))
    monkeypatch.setattr(az, "get_default_connection", lambda: conn_a)

    sid, workload, connection = api._resolve_scope_inputs("subscription", "", SUB, "cb")
    assert sid == SUB and workload is None
    assert connection["id"] == "cb"          # explicit override, not the default (ca)


