"""Connection / tenant scope isolation.

This is a known bug class in this codebase, not a hypothetical one: the Estate Graph
shipped with endpoints that derived the tenant from ``principal.tenant_id`` and listed
registries unscoped, which leaked data between Azure connections. Every Entra endpoint
derives its tenant from the *resolved connection*, and these tests hold that line.
"""
from __future__ import annotations

import pytest

from app.api import entra as entra_api
from app.entra import cache, model
from app.entra import snapshot as snapshot_mod


class _Principal:
    def __init__(self, tenant_id="principal-tenant", subject="user-1"):
        self.tenant_id = tenant_id
        self.subject = subject


@pytest.fixture(autouse=True)
def _tmp_root(tmp_path):
    cache.set_root_for_tests(tmp_path / "entra")
    snapshot_mod._analysis_memo.clear()  # noqa: SLF001 - test isolation
    yield
    cache.clear_memo()


def _connections(monkeypatch, mapping):
    import app.core.azure_connections as ac

    monkeypatch.setattr(ac, "resolve_connection", lambda cid: mapping.get(cid or ""))


def test_tenant_comes_from_the_resolved_connection_not_the_principal(monkeypatch):
    _connections(monkeypatch, {"conn-a": {"id": "conn-a", "tenant_id": "tenant-a"}})
    _conn, tenant_id, cid = entra_api._target(_Principal(), "conn-a")  # noqa: SLF001
    assert tenant_id == "tenant-a"        # NOT "principal-tenant"
    assert cid == "conn-a"


def test_unknown_connection_falls_back_to_the_principal_tenant(monkeypatch):
    _connections(monkeypatch, {})
    _conn, tenant_id, _cid = entra_api._target(_Principal(), "missing")  # noqa: SLF001
    assert tenant_id == "principal-tenant"


def test_domain_payloads_are_isolated_per_tenant():
    cache.write_domain("tenant-a", "people", model.domain_payload(
        "people", {"users": [{"id": "a-user", "enabled": True, "user_type": "Member"}]}))
    cache.write_domain("tenant-b", "people", model.domain_payload(
        "people", {"users": [{"id": "b-user", "enabled": True, "user_type": "Member"}]}))

    a = snapshot_mod.load("tenant-a")
    b = snapshot_mod.load("tenant-b")
    assert [u["id"] for u in a["data"]["people"]["users"]] == ["a-user"]
    assert [u["id"] for u in b["data"]["people"]["users"]] == ["b-user"]


def test_analysis_does_not_leak_between_tenants():
    for tenant, uid in (("tenant-a", "a-user"), ("tenant-b", "b-user")):
        cache.write_domain(tenant, "people", model.domain_payload(
            "people", {"users": [{"id": uid, "upn": f"{uid}@x", "enabled": True, "user_type": "Member",
                                  "mfa_registered": False, "signin_known": False}],
                       "groups": [], "capabilities": {"mfa_registration_report": True},
                       "counts": {}}))
    a = snapshot_mod.analyze("tenant-a")
    objects = {f["object_id"] for f in a["_analysis"]["findings"]}
    assert "b-user" not in objects


def test_derived_joins_survive_the_analysis_memo():
    """A memo hit must not silently drop the derived joins.

    ``load`` builds a fresh dict every call, so an analysis served from the memo has to
    re-attach ``_azure_link`` and ``_ca_analysis`` — otherwise the cross-plane join
    disappears from every endpoint the moment the memo warms up."""
    cache.write_domain("tenant-memo", "people", model.domain_payload(
        "people", {"users": [{"id": "u1", "upn": "u1@x", "enabled": True, "user_type": "Member"}],
                   "groups": [], "capabilities": {}, "counts": {}}))

    first = snapshot_mod.analyze("tenant-memo", force=True)
    second = snapshot_mod.analyze("tenant-memo")          # served from the memo

    assert second["_analysis"] is first["_analysis"]
    for snap in (first, second):
        assert "_azure_link" in snap["data"]
        assert "_ca_analysis" in snap["data"]
    assert second["data"]["_azure_link"] == first["data"]["_azure_link"]
    # An unavailable join must still explain itself rather than read as "nothing found".
    link = second["data"]["_azure_link"]
    assert link["available"] or link["reason"]


def test_user_state_is_isolated_per_tenant():
    snapshot_mod.write_state("tenant-a", {"suppressed": ["fp-a"], "breakglass": {}, "findings": {}})
    snapshot_mod.write_state("tenant-b", {"suppressed": ["fp-b"], "breakglass": {}, "findings": {}})
    assert snapshot_mod.read_state("tenant-a")["suppressed"] == ["fp-a"]
    assert snapshot_mod.read_state("tenant-b")["suppressed"] == ["fp-b"]


def test_score_history_is_isolated_per_tenant():
    cache.append_score_history("tenant-a", {"at": "2026-01-01", "score": 10})
    cache.append_score_history("tenant-b", {"at": "2026-01-01", "score": 90})
    assert [h["score"] for h in cache.score_history("tenant-a")] == [10]
    assert [h["score"] for h in cache.score_history("tenant-b")] == [90]


def test_cold_cache_reports_not_loaded_rather_than_failing():
    """Every GET must return a usable envelope on a cold cache — never a 500."""
    snap = snapshot_mod.analyze("never-collected")
    assert snap["loaded"] is False
    meta = snapshot_mod.meta_envelope(snap, "conn-x")
    assert meta["loaded"] is False
    assert meta["stale"] is True
    assert all(d["status"] == model.STATUS_NOT_COLLECTED for d in meta["domains"].values())
    # And the analysis still produces a well-formed (fully unmeasured) score object.
    score = snap["_analysis"]["score"]
    assert score["coverage"] == 0.0
    assert score["grade"] == ""


def test_tenant_id_is_never_used_raw_as_a_path(tmp_path):
    """Tenant ids are GUIDs in practice, but an id is never trusted in a path."""
    root = (tmp_path / "entra").resolve()
    for hostile in ("../../evil", "..", "a/b", "\\\\server\\share"):
        cache.write_domain(hostile, "people", model.domain_payload("people", {}))
        resolved = cache.tenant_dir(hostile).resolve()
        # The only property that matters: the payload lands inside the cache root.
        assert root in resolved.parents or resolved == root
        assert list(resolved.glob("*.json.gz"))
