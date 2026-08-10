"""Identity Investigate — resolver, capabilities and provenance.

The three behaviours these lock down are the ones the feature is built on:

* an unresolvable principal is a RESULT, not an error — deleted objects and Azure
  Lighthouse principals are often exactly what the reader clicked to ask about;
* capabilities are decided once, on the server, so the UI never switches on ``kind``;
* "unreadable" and "empty" stay distinguishable all the way to the response.
"""
from __future__ import annotations

from typing import Any

from app.entra import investigate as inv
from app.iam import schema as iam_schema


def _snapshot_data(*, users=(), groups=(), sps=()) -> dict[str, Any]:
    return {
        "people": {"users": list(users), "groups": list(groups)},
        "apps": {"service_principals": list(sps)},
    }


def _user(**over: Any) -> dict[str, Any]:
    return {"id": "u-1", "upn": "ada@contoso.com", "mail": "ada@contoso.com",
            "display_name": "Ada", "user_type": "Member", "enabled": True, **over}


def _group(**over: Any) -> dict[str, Any]:
    return {"id": "g-1", "display_name": "Platform Admins", "unified": False,
            "dynamic": False, "is_assignable_to_role": False, "security_enabled": True, **over}


def _sp(**over: Any) -> dict[str, Any]:
    return {"object_id": "s-1", "app_id": "a-1", "display_name": "Deploy SPN",
            "sp_type": "Application", "enabled": True, "credentials": [], **over}


# --------------------------------------------------------------------------- kinds
def test_a_member_and_a_guest_are_different_kinds():
    data = _snapshot_data(users=[_user(), _user(id="u-2", upn="ext@partner.com", user_type="Guest")])
    assert inv.resolve_in_snapshot(data, "u-1")["kind"] == inv.KIND_USER
    assert inv.resolve_in_snapshot(data, "u-2")["kind"] == inv.KIND_GUEST


def test_a_managed_identity_is_told_apart_from_an_app_by_service_principal_type():
    # Managed identities ARE servicePrincipal objects; servicePrincipalType is the only
    # reliable discriminator when the object is in our snapshot.
    data = _snapshot_data(sps=[_sp(), _sp(object_id="s-2", app_id="a-2", sp_type="ManagedIdentity")])
    assert inv.resolve_in_snapshot(data, "s-1")["kind"] == inv.KIND_SP
    assert inv.resolve_in_snapshot(data, "s-2")["kind"] == inv.KIND_MI


def test_a_group_resolves_and_carries_the_facts_that_make_it_interesting():
    data = _snapshot_data(groups=[_group(is_assignable_to_role=True, dynamic=True,
                                         membership_rule="department -eq 'IT'")])
    got = inv.resolve_in_snapshot(data, "g-1")
    assert got["kind"] == inv.KIND_GROUP
    # A role-assignable dynamic group is an escalation path whose membership is a rule.
    assert got["sub_kind"]["role_assignable"] is True
    assert got["sub_kind"]["dynamic"] is True


def test_a_principal_resolves_by_upn_mail_object_id_and_app_id():
    data = _snapshot_data(users=[_user()], sps=[_sp()])
    for needle in ("u-1", "ada@contoso.com", "ADA@CONTOSO.COM"):
        assert inv.resolve_in_snapshot(data, needle)["id"] == "u-1"
    # The Activity Log gives an appId, never an object id — resolving it is what makes the
    # "who changed this?" handoff land.
    assert inv.resolve_in_snapshot(data, "a-1")["id"] == "s-1"


def test_a_managed_identity_names_the_resource_that_owns_it():
    rid = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Web/sites/app"
    data = _snapshot_data(sps=[_sp(sp_type="ManagedIdentity", alternative_names=["x", rid])])
    got = inv.resolve_in_snapshot(data, "s-1")
    assert got["sub_kind"]["assigned_to_resource"] == rid


def test_an_unknown_identifier_resolves_to_nothing_rather_than_guessing():
    assert inv.resolve_in_snapshot(_snapshot_data(users=[_user()]), "nope") is None


# ------------------------------------------------------- unresolvable, but not an error
def _row(**over: Any) -> dict[str, Any]:
    return {"principalId": "p-1", "principalDisplayName": "Ghost", "principalType": "User",
            "principalExists": iam_schema.EXISTS_TRUE, "managingTenantId": "", **over}


def test_a_deleted_principal_is_a_result_not_an_error():
    got = inv.resolve_in_access_rows([_row(principalExists=iam_schema.EXISTS_FALSE)], "p-1")
    assert got["resolution"] == inv.DELETED
    # The assignment outlives the object; that survival IS the audit finding.
    assert got["display_name"] == "Ghost"


def test_a_principal_we_could_not_look_up_is_not_reported_as_deleted():
    # principalExists is a string precisely so these two stay different facts.
    got = inv.resolve_in_access_rows([_row(principalExists=iam_schema.EXISTS_UNKNOWN)], "p-1")
    assert got["resolution"] == inv.UNREADABLE
    assert got["resolution"] != inv.DELETED


def test_a_lighthouse_principal_names_the_organisation_that_holds_the_access():
    got = inv.resolve_in_access_rows(
        [_row(managingTenantId="t-9", managingTenantName="Contoso MSP")], "p-1")
    assert got["resolution"] == inv.CROSS_TENANT
    assert got["managing_tenant"] == {"id": "t-9", "name": "Contoso MSP"}


def test_cross_tenant_wins_over_a_missing_object():
    # A managing tenant explains the non-resolution; "deleted" would be a wrong claim.
    got = inv.resolve_in_access_rows(
        [_row(principalExists=iam_schema.EXISTS_FALSE, managingTenantId="t-9")], "p-1")
    assert got["resolution"] == inv.CROSS_TENANT


def test_an_identifier_nothing_knows_about_says_so_plainly():
    env = inv.envelope(inv.unresolved("who?"))
    assert env["principal"]["resolution"] == inv.NOT_FOUND
    assert env["capabilities"] == []
    assert env["notes"] and "tenant" in env["notes"][0].lower()


# --------------------------------------------------------------------------- capabilities
def test_a_group_has_no_signin_audit_or_risk_sections():
    caps, notes = inv.capabilities_for(inv.KIND_GROUP, inv.RESOLVED)
    for absent in (inv.CAP_SIGNINS, inv.CAP_AUDIT, inv.CAP_RISK, inv.CAP_REGISTRATION):
        assert absent not in caps
    assert inv.CAP_MEMBERS in caps
    # Absence must be EXPLAINED, or the reader reads "no sign-ins" as "never signed in".
    assert notes


def test_a_workload_identity_has_credentials_but_no_mfa_or_risk():
    caps, _ = inv.capabilities_for(inv.KIND_SP, inv.RESOLVED)
    assert inv.CAP_CREDENTIALS in caps
    assert inv.CAP_REGISTRATION not in caps
    assert inv.CAP_RISK not in caps


def test_only_a_managed_identity_claims_an_owning_resource():
    assert inv.CAP_OWNING_RESOURCE in inv.capabilities_for(inv.KIND_MI, inv.RESOLVED)[0]
    assert inv.CAP_OWNING_RESOURCE not in inv.capabilities_for(inv.KIND_SP, inv.RESOLVED)[0]


def test_a_user_keeps_the_behavioural_sections():
    caps, _ = inv.capabilities_for(inv.KIND_USER, inv.RESOLVED)
    for expected in (inv.CAP_SIGNINS, inv.CAP_AUDIT, inv.CAP_AZURE_ACTIVITY, inv.CAP_RISK):
        assert expected in caps


def test_an_unresolvable_principal_keeps_only_the_structural_sections():
    for state in (inv.DELETED, inv.CROSS_TENANT, inv.UNREADABLE):
        caps, notes = inv.capabilities_for(inv.KIND_USER, state)
        assert set(caps) == {inv.CAP_ACCESS, inv.CAP_FINDINGS, inv.CAP_TIMELINE}
        # There are no logs to read for an object that is gone or lives elsewhere.
        assert inv.CAP_SIGNINS not in caps
        assert notes


def test_every_kind_has_a_capability_entry():
    # A kind with no entry silently renders nothing; that must be a deliberate empty tuple.
    for kind in inv.ALL_KINDS:
        assert kind in inv._CAPABILITIES_BY_KIND


# --------------------------------------------------------------------------- provenance
def test_unreadable_and_empty_are_distinguishable():
    empty = inv.section([], inv.provenance("cache", collected_at="t0"))
    blind = inv.section([], inv.provenance("cache", unreadable=True, reason="no permission"))
    assert empty["data"] == blind["data"] == []
    # Same payload, opposite facts — only provenance can tell them apart.
    assert empty["provenance"]["unreadable"] is False
    assert blind["provenance"]["unreadable"] is True
    assert blind["provenance"]["reason"]


def test_truncation_is_reported_rather_than_silently_shortening_a_list():
    prov = inv.provenance("cache", truncated=True)
    assert prov["truncated"] is True


# --------------------------------------------------------------------------- search
def test_search_ranks_an_exact_identifier_above_a_partial_name():
    data = _snapshot_data(users=[
        _user(id="u-1", upn="ada@contoso.com", display_name="Ada Lovelace"),
        _user(id="u-2", upn="adamson@contoso.com", display_name="Adam Son"),
    ])
    hits = inv.search(data, "ada@contoso.com")
    assert hits[0]["id"] == "u-1"


def test_search_spans_users_groups_and_service_principals():
    data = _snapshot_data(users=[_user(display_name="Platform Bot")],
                          groups=[_group(display_name="Platform Admins")],
                          sps=[_sp(display_name="Platform Deploy")])
    kinds = {h["kind"] for h in inv.search(data, "platform")}
    assert kinds == {inv.KIND_USER, inv.KIND_GROUP, inv.KIND_SP}


def test_search_refuses_a_needle_too_short_to_mean_anything():
    assert inv.search(_snapshot_data(users=[_user()]), "a") == []


def test_search_respects_its_limit():
    users = [_user(id=f"u-{i}", upn=f"user{i}@contoso.com", display_name=f"User {i}")
             for i in range(50)]
    assert len(inv.search(_snapshot_data(users=users), "user", limit=10)) == 10


# --------------------------------------------------------------------------- recents
def _view(pid: str, *, at: str, conn: str = "c-1", name: str = "", kind: str = inv.KIND_USER,
          resolution: str = inv.RESOLVED) -> dict[str, Any]:
    return {"target": pid, "at": at,
            "metadata": {"connection_id": conn, "name": name or pid, "kind": kind,
                         "resolution": resolution}}


def test_recents_keep_one_entry_per_principal_however_often_it_was_opened():
    rows = [_view("p-1", at="T5"), _view("p-1", at="T4"), _view("p-2", at="T3"),
            _view("p-1", at="T2"), _view("p-3", at="T1")]
    got = inv.recent_entries(rows, connection_id="c-1")
    assert [e["id"] for e in got] == ["p-1", "p-2", "p-3"]
    # The latest visit wins, because the strip is ordered by recency.
    assert got[0]["at"] == "T5"


def test_recents_are_scoped_to_the_connection_being_viewed():
    # A principal id from another tenant resolves to nothing here, so a chip for it could
    # never be opened — worse than no chip at all.
    rows = [_view("p-1", at="T2", conn="c-9"), _view("p-2", at="T1", conn="c-1")]
    assert [e["id"] for e in inv.recent_entries(rows, connection_id="c-1")] == ["p-2"]


def test_recents_without_a_connection_filter_keep_everything():
    rows = [_view("p-1", at="T2", conn="c-9"), _view("p-2", at="T1", conn="c-1")]
    assert len(inv.recent_entries(rows, connection_id="")) == 2


def test_recents_stop_at_the_limit():
    rows = [_view(f"p-{i}", at=f"T{100 - i}") for i in range(60)]
    assert len(inv.recent_entries(rows, connection_id="c-1")) == inv.RECENT_LIMIT
    assert len(inv.recent_entries(rows, connection_id="c-1", limit=5)) == 5


def test_recents_carry_the_name_recorded_at_view_time():
    rows = [_view("p-1", at="T1", name="Ada Lovelace")]
    assert inv.recent_entries(rows, connection_id="c-1")[0]["display_name"] == "Ada Lovelace"


def test_a_row_with_no_target_is_skipped_rather_than_shown_blank():
    rows = [{"target": "", "at": "T2", "metadata": {"connection_id": "c-1"}},
            _view("p-1", at="T1")]
    assert [e["id"] for e in inv.recent_entries(rows, connection_id="c-1")] == ["p-1"]


def test_an_identifier_that_never_resolved_is_not_offered_to_return_to():
    # A mistyped id — or a route segment that reached the dossier endpoint — is junk.
    rows = [_view("recent", at="T2", resolution=inv.NOT_FOUND), _view("p-1", at="T1")]
    assert [e["id"] for e in inv.recent_entries(rows, connection_id="c-1")] == ["p-1"]


def test_a_deleted_or_cross_tenant_principal_IS_offered_to_return_to():
    # These resolved to a real answer, and that answer is usually the finding.
    rows = [_view("p-1", at="T2", resolution=inv.DELETED),
            _view("p-2", at="T1", resolution=inv.CROSS_TENANT)]
    got = inv.recent_entries(rows, connection_id="c-1")
    assert [e["id"] for e in got] == ["p-1", "p-2"]
    assert got[0]["resolution"] == inv.DELETED


def test_the_live_directory_name_beats_the_one_recorded_at_view_time():
    # Someone renamed after being investigated should show their CURRENT name.
    data = _snapshot_data(users=[_user(id="p-1", display_name="Ada Byron")])
    entries = inv.recent_entries([_view("p-1", at="T1", name="Ada Lovelace")], connection_id="c-1")
    got = inv.refresh_recent_names(data, entries)
    assert got[0]["display_name"] == "Ada Byron"


def test_a_principal_the_directory_no_longer_holds_keeps_its_recorded_name():
    # Falling back to the object id would make a deleted principal unrecognisable — and a
    # deleted principal is exactly the one worth returning to.
    entries = inv.recent_entries([_view("gone-1", at="T1", name="Retired SPN")], connection_id="c-1")
    got = inv.refresh_recent_names(_snapshot_data(), entries)
    assert got[0]["display_name"] == "Retired SPN"


# ======================================================= PIM eligibility is not standing access
# `build_dossier` used to concatenate `assignments` + `group_derived` + `eligible` into one
# flat `directory_assignments` list with nothing saying which bucket a row came from. An
# eligibility row carries `permanent: True` when the ELIGIBILITY never lapses, so a correctly
# governed PIM-eligible Global Administrator was reported as a "permanent, direct" Global
# Administrator — the opposite of the truth, and it invited the reader to remove a standing
# assignment that does not exist.
import pytest


def _roles_data() -> dict[str, Any]:
    common = {
        "principal_id": "u1", "principal_type": "User", "principal_name": "P - Alex",
        "role_privileged": True, "role_tier": "tier0",
    }
    return {
        "assignments": [
            # A live PIM ACTIVATION: held right now, but time-boxed.
            {**common, "id": "a1", "role_name": "Global Administrator", "scope": "/",
             "assignment_kind": "active", "source": "direct", "activated": True,
             "permanent": False, "end": "2026-08-03T23:10:05Z", "permanence_known": True},
            # Genuinely standing.
            {**common, "id": "a2", "role_name": "Global Reader", "scope": "/",
             "assignment_kind": "active", "source": "direct", "activated": False,
             "permanent": True, "end": "", "permanence_known": True},
        ],
        "group_derived": [],
        "eligible": [
            {**common, "id": "e1", "role_name": "Global Administrator",
             "start": "2025-02-26T14:33:36Z", "end": "", "permanent": True, "status": "Provisioned"},
            {**common, "id": "e2", "role_name": "SharePoint Administrator",
             "start": "2025-04-09T19:40:52Z", "end": "", "permanent": True, "status": "Provisioned"},
        ],
    }


async def _access(monkeypatch) -> dict[str, Any]:
    async def _no_rows(_tenant):
        return []

    monkeypatch.setattr(inv, "access_rows", _no_rows)

    async def _resolve(_data, _tenant, needle):
        return {"id": "u1", "kind": "user", "display_name": "P - Alex",
            "resolution": "resolved", "upn": "p-alex@example.com"}

    monkeypatch.setattr(inv, "resolve", _resolve)
    snapshot = {"data": {"roles": _roles_data()}, "_analysis": {}, "domains": {}}
    _env, sections = await inv.build_dossier(snapshot, "t", "u1")
    return sections["access"]["data"]


@pytest.mark.asyncio
async def test_an_eligible_role_is_never_reported_as_permanent(monkeypatch):
    """The exact defect: `permanent: True` on an eligibility row read as standing access."""
    data = await _access(monkeypatch)
    eligible = [r for r in data["directory_assignments"] if r["assignment_kind"] == "eligible"]
    assert len(eligible) == 2
    for row in eligible:
        assert "permanent" not in row, f"{row['role_name']} still carries the ambiguous key"
        assert row["standing_access"] is False
        assert row["activated"] is False
        # The eligibility not lapsing is a different fact, and it keeps its own name.
        assert row["eligibility_permanent"] is True


@pytest.mark.asyncio
async def test_every_assignment_row_says_what_kind_it_is(monkeypatch):
    """Flattening the buckets is only safe if the rows are self-describing."""
    data = await _access(monkeypatch)
    kinds = {r["role_name"]: r["assignment_kind"] for r in data["directory_assignments"]}
    assert kinds["SharePoint Administrator"] == "eligible"
    assert kinds["Global Reader"] == "active"
    assert all(r.get("assignment_kind") for r in data["directory_assignments"])


@pytest.mark.asyncio
async def test_held_now_is_separable_from_privileged_by_any_path(monkeypatch):
    """`directory_roles` stays 'privileged by any path' (the score depends on it), but a
    reader deciding whether someone HOLDS a role needs the split."""
    data = await _access(monkeypatch)
    assert "sharepoint administrator" in data["directory_roles"]
    assert "sharepoint administrator" not in data["directory_roles_active"]
    assert data["directory_roles_eligible_only"] == ["sharepoint administrator"]
    # GA is both eligible AND currently activated, so it is held now and must not be
    # listed as eligible-only.
    assert "global administrator" in data["directory_roles_active"]


@pytest.mark.asyncio
async def test_an_activated_pim_role_is_still_reported_as_held(monkeypatch):
    """The fix must not swing the other way: an ACTIVATED role really is held right now."""
    data = await _access(monkeypatch)
    active_ga = [r for r in data["directory_assignments"]
                 if r["role_name"] == "Global Administrator" and r["assignment_kind"] == "active"]
    assert len(active_ga) == 1
    assert active_ga[0]["activated"] is True
    assert active_ga[0]["end"], "an activation must keep its expiry"
