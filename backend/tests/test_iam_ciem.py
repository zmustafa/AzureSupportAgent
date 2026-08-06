"""P8 CIEM: usage collection, right-sizing, role hygiene, limits and the what-if simulator.

The tests that matter are the ones about what this refuses to conclude. Comparing granted
actions to used actions is arithmetic; deciding an unused permission is unnecessary is a
judgement that gets people locked out of production. Every guard below is a place where the
convenient answer is wrong:

  - unmeasured usage must never read as "nothing was used";
  - a break-glass account is SUPPOSED to look unused;
  - data-plane roles cannot be judged by a log that does not record data-plane activity;
  - an apparent revocation that changes nothing must be reported as changing nothing;
  - an invalid simulated change must raise, not produce a reassuring empty diff.
"""
from __future__ import annotations

import pytest

from app.iam import cache, effective, rightsize, schema, simulator, usage

SUB = "11111111-1111-1111-1111-111111111111"
SUB2 = "22222222-2222-2222-2222-222222222222"
RG = f"/subscriptions/{SUB}/resourceGroups/rg1"


@pytest.fixture
def anyio_backend():
    return "asyncio"


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
        roleDefinitionId="/rd/owner",
        roleName="Owner",
        roleIsPrivileged=True,
        scope=f"/subscriptions/{SUB}",
        scopeDisplayName="sub-1",
        assignmentId="a1",
        principalExists=schema.EXISTS_TRUE,
    )
    base.update(kw)
    return schema.make_row(**base)


def _role(name, actions, *, data_actions=(), custom=False, not_actions=()):
    return {
        "roleDefinitionId": f"/rd/{name.lower().replace(' ', '-')}",
        "roleName": name,
        "actions": list(actions),
        "notActions": list(not_actions),
        "dataActions": list(data_actions),
        "notDataActions": [],
        "roleType": "CustomRole" if custom else "BuiltInRole",
        "assignableScopes": ["/"],
    }


# A deliberately REALISTIC catalogue. An earlier version declared only wildcards, which made the
# observable action universe nearly empty and every role equally "narrow" — the same blind spot
# that made pattern-counting useless in the first place. Real built-in roles declare hundreds of
# literal actions, and the universe is what makes one role measurably wider than another.
_COMPUTE = [f"Microsoft.Compute/virtualMachines/{v}" for v in
            ("read", "write", "delete", "start/action", "restart/action", "deallocate/action")]
_STORAGE = [f"Microsoft.Storage/storageAccounts/{v}" for v in
            ("read", "write", "delete", "listKeys/action", "regenerateKey/action")]
_NETWORK = [f"Microsoft.Network/virtualNetworks/{v}" for v in ("read", "write", "delete")]
_AUTHZ = ["Microsoft.Authorization/roleAssignments/write", "Microsoft.Authorization/roleAssignments/delete"]
_ALL_LITERAL = [*_COMPUTE, *_STORAGE, *_NETWORK, *_AUTHZ]
_READS = [a for a in _ALL_LITERAL if a.endswith("/read")]

CATALOGUE = [
    _role("Reader", _READS),
    _role("Owner", ["*", *_ALL_LITERAL]),
    _role("Contributor", [*_ALL_LITERAL], not_actions=["Microsoft.Authorization/*/Write"]),
    _role("Virtual Machine Contributor", [*_COMPUTE, *_READS]),
    _role("Storage Blob Data Reader", _READS,
          data_actions=["Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read"]),
]


def _index(defs=None):
    return effective.build_role_index(defs or CATALOGUE)


def _catalogue():
    """Roles ordered narrowest-first against this fixture's observable universe."""
    seeded = rightsize.build_narrowest_index(_index(), ())
    universe = usage.action_universe(seeded, set())
    return rightsize.build_narrowest_index(_index(), universe), universe


def _usage(principals, *, window=90, status=schema.STATUS_SUCCEEDED):
    return {
        "window_days": window, "start": "", "end": "", "source": usage.SOURCE_ACTIVITY_LOG,
        "status": status, "subscriptions": 1,
        "event_count": sum(p.get("events", 1) for p in principals),
        "principals": principals, "notes": [], "limitations": usage.LIMITATIONS,
    }


# =========================================================================== usage honesty
def test_unmeasured_usage_is_distinguishable_from_nothing_being_used(isolated_cache):
    """The single most dangerous confusion in CIEM. An empty action set that means "we have not
    looked" would revoke access on the strength of a blank."""
    payload = cache.read_usage("never-scanned")
    assert payload["measured"] is False
    assert usage.is_measured(payload) is False
    assert payload["principals"] == []
    assert payload["limitations"], "an unmeasured payload must still carry its limitations"


def test_a_failed_usage_collection_is_not_treated_as_measured():
    assert usage.is_measured(_usage([], status=schema.STATUS_FAILED)) is False
    assert usage.is_measured(_usage([], status=schema.STATUS_UNAUTHORIZED)) is False
    # Skipped is different: it means there was legitimately nothing to collect.
    assert usage.is_measured(_usage([], status=schema.STATUS_SKIPPED)) is True


def test_the_window_is_clamped_to_activity_log_retention_and_says_so():
    _s, _e, days, note = usage.clamp_window(365)
    assert days == usage.MAX_WINDOW_DAYS
    assert note and "looks unused here" in note
    _s, _e, days, quiet = usage.clamp_window(30)
    assert days == 30 and quiet == ""


@pytest.mark.parametrize(
    "window,expected",
    [(7, usage.LOW), (14, usage.LOW), (60, usage.MEDIUM), (90, usage.HIGH)],
)
def test_confidence_is_driven_by_the_window_not_the_data_volume(window, expected):
    """A month of dense data still cannot tell you whether a quarterly job needs its
    permissions. Ninety days does not cover an annual DR test."""
    conf, why = usage.confidence_for(window, events=10_000)
    assert conf == expected
    assert why


def test_a_known_cadence_longer_than_the_window_forces_low_confidence():
    conf, why = usage.confidence_for(90, cadence_days=365, events=500)
    assert conf == usage.LOW
    assert "365-day cadence" in why


def test_zero_recorded_events_never_reaches_high_confidence():
    """No activity is consistent with unused access AND with an identity whose activity is not
    logged. Calling that 'high confidence unused' is the claim that causes the outage."""
    conf, _why = usage.confidence_for(365, events=0)
    assert conf != usage.HIGH


@pytest.mark.parametrize(
    "name",
    ["breakglass-admin", "BreakGlass Admin", "svc-emergency-access", "eba-01", "firecall-user"],
)
def test_break_glass_accounts_are_recognised_by_naming_convention(name):
    assert usage.is_break_glass(_row(effectivePrincipalName=name)) is True


def test_a_flagged_principal_is_break_glass_regardless_of_its_name():
    assert usage.is_break_glass(_row(effectivePrincipalName="Alice"), {"alice"}) is True
    assert usage.is_break_glass(_row(effectivePrincipalName="Alice")) is False


def test_usage_limitations_always_name_the_data_plane_gap():
    joined = " ".join(usage.LIMITATIONS)
    assert "NOT in the Activity Log" in joined
    assert "not proof of absence of need" in joined


# =========================================================================== right-sizing
def test_right_sizing_over_unmeasured_usage_returns_unmeasured_not_an_empty_list():
    """An empty recommendation list reads as "nothing is over-privileged". That is the reading
    a tenant that has never run a usage scan would get."""
    out = rightsize.analyse([_row()], _index(), {"measured": False})
    assert out["measured"] is False
    assert out["recommendations"] == []
    assert any("has not been collected" in l for l in out["limitations"])


def test_right_sizing_with_no_resolvable_role_is_not_reported_as_a_clean_result():
    """The failure that produced a false all-clear on a real tenant.

    A directory refresh wiped the collected role definitions, so `build_role_index` was empty,
    every row fell out of the loop on "role actions unknown", and `analyse` returned an empty
    recommendation list with `measured: True` — which the UI rendered as "Nothing crossed the
    over-privilege threshold". 2,185 genuinely over-privileged assignments were reported as
    zero. Losing the ability to measure must never look like a measurement."""
    payload = _usage([{"principalId": "p1", "actions": ["Microsoft.Compute/virtualMachines/read"], "events": 5}])
    out = rightsize.analyse([_row()], {}, payload)

    assert out["assessed"] == 0
    assert out["unresolved_roles"] == 1
    assert out["measured"] is False, "no role could be assessed — this is not a measured result"
    assert any("never collected" in l for l in out["limitations"] + out["excluded"])
    assert any("not a clean result" in l for l in out["limitations"])


def test_rows_skipped_for_an_unknown_role_are_counted_not_silently_dropped():
    """A partial catalogue is the common case — the count is what stops the reader taking the
    denominator for the whole estate."""
    payload = _usage([{"principalId": "p1", "actions": ["Microsoft.Compute/virtualMachines/read"], "events": 5}])
    rows = [
        _row(principalId="p1", effectivePrincipalId="p1",
             roleDefinitionId="/rd/reader", roleName="Reader"),
        # A role this tenant's catalogue does not describe — no GUID key, no name key.
        _row(principalId="p1", effectivePrincipalId="p1",
             roleDefinitionId="/rd/ghost", roleName="Ghost Role", scope="/s/2"),
    ]
    out = rightsize.analyse(rows, _index(), payload)

    assert out["unresolved_roles"] == 1
    assert out["assessed"] == 1
    assert out["measured"] is True, "something WAS assessed, so the result stands"
    assert any("could not be assessed" in l for l in out["excluded"])


def test_the_excluded_field_means_the_same_thing_whether_or_not_usage_was_measured():
    """A field whose semantics change between branches — "roles left out of the analysis" in one
    and "empty, there was no analysis" in the other — is a trap for the next consumer. The
    data-plane caveat is true either way."""
    unmeasured = rightsize.analyse([_row()], _index(), {"measured": False})
    measured = rightsize.analyse([_row()], _index(), _usage([]))
    for out in (unmeasured, measured):
        assert any("Data-plane roles were excluded" in e for e in out["excluded"])


def test_a_break_glass_principal_is_reported_but_never_recommended_for_removal():
    """It is SUPPOSED to look unused. The exclusion is at the point recommendations are built,
    not a filter on the output that a later refactor could drop."""
    rows = [_row(effectivePrincipalId="bg", effectivePrincipalName="breakglass-admin")]
    out = rightsize.analyse(rows, _index(), _usage([]))
    entry = next(r for r in out["recommendations"] if r["principalId"] == "bg")
    assert entry["recommendation"] is None
    assert "SUPPOSED to look unused" in entry["note"]
    assert out["break_glass_excluded"] == 1


def test_data_plane_roles_are_excluded_when_data_plane_logging_is_unavailable():
    """The Activity Log does not record data-plane operations at all. Recommending removal of a
    data-plane role on that evidence is a conclusion drawn from a source that cannot speak."""
    rows = [_row(roleName="Storage Blob Data Reader", roleDefinitionId="/rd/storage-blob-data-reader")]
    out = rightsize.analyse(rows, _index(), _usage([]), data_plane_logged=False)
    assert out["recommendations"] == []
    assert any("Data-plane roles were excluded" in e for e in out["excluded"])

    included = rightsize.analyse(rows, _index(), _usage([]), data_plane_logged=True)
    assert included["recommendations"], "with data-plane logging the role IS assessable"


def test_every_recommendation_publishes_its_denominator_window_and_confidence():
    """"99.8% over-privileged" alone is a number designed to be quoted out of context."""
    out = rightsize.analyse([_row()], _index(), _usage([{"principalId": "alice", "actions": ["microsoft.compute/virtualmachines/read"], "events": 3}]))
    rec = out["recommendations"][0]
    for key in ("usedActionCount", "grantedActionCount", "unusedRatio", "window", "confidence"):
        assert key in rec
    assert rec["window"]["days"] == 90
    assert rec["confidenceWhy"]


def test_a_narrower_proposal_names_what_it_gives_up():
    """"Covers everything you did last quarter" and "safe" are different claims, and the gap
    between them is where a right-sizing recommendation causes an incident."""
    used = [{"principalId": "alice", "actions": ["microsoft.compute/virtualmachines/read"], "events": 5}]
    out = rightsize.analyse([_row()], _index(), _usage(used))
    rec = out["recommendations"][0]
    assert rec["recommendation"] is not None
    assert rec["recommendation"]["residualRisk"]
    assert "not the same as none being needed" in rec["recommendation"]["residualRisk"]


def test_two_roles_at_one_scope_are_two_recommendations_with_distinct_ids():
    """One principal, one scope, two over-privileged roles is TWO findings, and each needs its
    own identity.

    Found live: the UI keyed rows on (principalId, scope), which collapsed 243 of 2185
    recommendations on a real tenant — React silently dropped the duplicates while the header
    above the list went on counting all 2185. The identity has to come from the same key the
    grouping used, so publish it rather than letting each consumer guess."""
    rows = [
        _row(roleDefinitionId="/rd/owner", roleName="Owner", assignmentId="a1"),
        _row(roleDefinitionId="/rd/reader", roleName="Reader", assignmentId="a2"),
    ]
    out = rightsize.analyse(rows, _index(), _usage([]))
    recs = out["recommendations"]
    assert len(recs) == 2, "two roles at one scope must produce two recommendations"
    assert {r["principalId"] for r in recs} == {"alice"}
    assert {r["scope"] for r in recs} == {f"/subscriptions/{SUB}"}
    ids = [r["id"] for r in recs]
    assert all(ids), "every recommendation carries its grant identity"
    assert len(set(ids)) == 2, "the identity must distinguish the two roles"


def test_set_cover_never_proposes_owner_as_the_narrower_option():
    """A greedy search that only counts covered actions would pick Owner every time: it covers
    everything. Proposing the role right-sizing exists to move people OFF is absurd output."""
    catalogue, universe = _catalogue()
    chosen, _left = rightsize.cover({"microsoft.compute/virtualmachines/read"}, catalogue, universe)
    assert chosen
    assert all(r.role_name.lower() not in rightsize.NEVER_PROPOSE for r in chosen)


def test_set_cover_prefers_fewer_and_narrower_roles():
    catalogue, universe = _catalogue()
    chosen, left = rightsize.cover({"microsoft.compute/virtualmachines/read"}, catalogue, universe)
    assert not left
    assert len(chosen) == 1
    assert chosen[0].role_name == "Reader", "Reader is the narrowest role granting that read"


def test_an_empty_universe_would_make_every_role_equally_narrow():
    """Guards the reason `universe` is a REQUIRED argument rather than a defaulted one. With no
    universe every breadth is zero, the tie-break falls through to alphabetical order, and the
    search proposes Contributor over Reader — which is the bug the argument exists to prevent."""
    flat = rightsize.build_narrowest_index(_index(), ())
    assert all(usage.breadth(r, ()) == 0 for r in flat)
    chosen, _ = rightsize.cover({"microsoft.compute/virtualmachines/read"}, flat, ())
    assert chosen[0].role_name == "Contributor", (
        "kept as a regression witness: this is the WRONG answer, and it is what an omitted "
        "universe silently produces"
    )


def test_set_cover_is_deterministic():
    """A recommendation whose output changes between identical runs is not usable for a
    decision, and 'run it again' becomes the first thing anybody does when they dislike it."""
    catalogue, universe = _catalogue()
    actions = {"microsoft.compute/virtualmachines/read", "microsoft.storage/storageaccounts/read"}
    first = [r.role_name for r in rightsize.cover(actions, catalogue, universe)[0]]
    second = [r.role_name for r in rightsize.cover(actions, catalogue, universe)[0]]
    assert first == second


def test_a_role_whose_permissions_were_never_collected_cannot_win_the_search():
    """An uncollected role has empty action lists, which makes it the 'narrowest' role in the
    tenant while covering nothing at all."""
    catalogue = rightsize.build_narrowest_index(_index([*CATALOGUE, _role("Ghost", [])]), ())
    assert all(r.role_name != "Ghost" for r in catalogue)


def test_the_narrowest_index_does_not_double_count_roles_keyed_twice():
    """`build_role_index` keys by GUID AND by name, so iterating it directly sees every role
    twice and the greedy search prefers whichever spelling it saw first."""
    catalogue, _universe = _catalogue()
    names = [r.role_name for r in catalogue]
    assert len(names) == len(set(names))


def test_uncovered_actions_mean_no_proposal_rather_than_a_partial_one():
    used = [{"principalId": "alice", "actions": ["contoso.private/thing/write"], "events": 2}]
    out = rightsize.analyse([_row()], _index(), _usage(used))
    rec = out["recommendations"][0]
    assert rec["recommendation"] is None
    assert "No combination of built-in roles covers" in rec["note"]


def test_a_principal_with_no_recorded_activity_is_not_told_no_role_covers_what_it_did():
    """Found on the live tenant: 56 of 56 recommendations said "no combination of roles covers
    everything this principal did" about principals who did NOTHING. That implies they did
    something unusual, and it invites the obvious wrong inference — remove all their access —
    from ninety days of silence that may just mean their activity is not logged."""
    out = rightsize.analyse([_row()], _index(), _usage([]))
    rec = out["recommendations"][0]
    assert rec["recommendation"] is None
    assert "No operation by this principal was recorded" in rec["note"]
    assert "nothing to size against" in rec["note"]
    assert "No combination" not in rec["note"]


def test_eligible_access_is_not_right_sized():
    """Eligible access is not held. Right-sizing it answers a question nobody asked."""
    rows = [_row(assignmentState=schema.STATE_ELIGIBLE)]
    out = rightsize.analyse(rows, _index(), _usage([]))
    assert out["assessed"] == 0


# =========================================================================== scale
def test_granted_actions_scales_to_a_realistic_catalogue():
    """Performance regression — and it was an OUTAGE, not a slow page.

    The first implementation tested every universe action against every pattern of every role.
    On the live tenant (1,848 roles, a 3,947-action universe) that measured **40 seconds**, ran
    synchronously inside an async handler, and starved the event loop until SQLite began
    reporting "database is locked" on unrelated session writes. Literal patterns are now set
    lookups and wildcards are narrowed to their provider namespace first.

    The bound is deliberately loose: this guards against reintroducing the quadratic scan, it is
    not a benchmark to tune against."""
    import time

    universe = tuple(
        f"microsoft.{ns}/type{i}/{verb}"
        for ns in ("compute", "storage", "network", "keyvault", "sql", "web", "insights")
        for i in range(150)
        for verb in ("read", "write", "delete", "action")
    )
    assert len(universe) > 4000

    roles = [
        effective.role_action_set({
            "roleDefinitionId": f"/rd/r{i}",
            "roleName": f"Role {i}",
            # A realistic mix: literals, a namespaced wildcard, and a broad one.
            "actions": [universe[i], universe[i + 1], f"microsoft.compute/type{i % 40}/*", "*/read"],
        })
        for i in range(400)
    ]

    started = time.perf_counter()
    total = sum(len(usage.granted_actions(r, universe)) for r in roles)
    elapsed = time.perf_counter() - started
    assert total > 0
    assert elapsed < 5.0, f"400 roles x {len(universe)} actions took {elapsed:.1f}s"


def test_a_wildcard_role_still_grants_everything_after_the_fast_path():
    """The optimisation must not change any answer. `*` covers the universe; a namespaced
    wildcard covers only its namespace."""
    universe = ("microsoft.compute/vm/read", "microsoft.storage/sa/read", "microsoft.storage/sa/write")
    star = effective.role_action_set({"roleName": "Owner", "actions": ["*"]})
    scoped = effective.role_action_set({"roleName": "S", "actions": ["microsoft.storage/*/read"]})
    assert usage.granted_actions(star, universe) == set(universe)
    assert usage.granted_actions(scoped, universe) == {"microsoft.storage/sa/read"}


def test_not_actions_are_subtracted_after_the_fast_path():
    universe = ("microsoft.compute/vm/read", "microsoft.compute/vm/write")
    role = effective.role_action_set({
        "roleName": "C", "actions": ["*"], "notActions": ["microsoft.compute/vm/write"],
    })
    assert usage.granted_actions(role, universe) == {"microsoft.compute/vm/read"}


# =========================================================================== narrowest scope
def test_the_action_universe_never_contains_a_wildcard():
    """A wildcard is not a member of the universe; it is a CLAIM over the universe. Admitting
    `*` as a member inflates every wide role's breadth by counting its own claim as something
    it grants, and makes `*` look like an action somebody could have exercised."""
    seeded = rightsize.build_narrowest_index(_index(), ())
    universe = usage.action_universe(seeded, {"microsoft.compute/virtualmachines/read"})
    assert universe, "the fixture catalogue must contribute literal actions"
    assert all("*" not in a for a in universe)


def test_the_universe_includes_actions_that_were_used_but_no_role_declares_literally():
    """A role granting `Microsoft.Storage/*` declares no literal storage action, so an action
    somebody actually performed would be absent from the denominator entirely."""
    universe = usage.action_universe([], {"contoso.private/thing/write"})
    assert "contoso.private/thing/write" in universe


def test_narrowest_scope_tightens_only_when_every_operation_fits_inside():
    inside = {f"{RG}/providers/Microsoft.Compute/virtualMachines/vm1",
              f"{RG}/providers/Microsoft.Compute/virtualMachines/vm2"}
    assert rightsize.narrowest_scope(inside, f"/subscriptions/{SUB}") == RG


def test_narrowest_scope_refuses_when_activity_spans_more_than_one_branch():
    """Proposing a narrower scope that does not contain all the activity is proposing an
    outage."""
    spread = {f"{RG}/providers/Microsoft.Compute/virtualMachines/vm1",
              f"/subscriptions/{SUB}/resourceGroups/rg2/providers/Microsoft.Compute/virtualMachines/vm2"}
    assert rightsize.narrowest_scope(spread, f"/subscriptions/{SUB}") == f"/subscriptions/{SUB}"


def test_narrowest_scope_never_produces_a_character_wise_prefix():
    """A character prefix of /subscriptions/abc and /subscriptions/abd is /subscriptions/ab,
    which is not a scope and covers neither."""
    assert rightsize._common_prefix(["/subscriptions/abc", "/subscriptions/abd"]) == ""


def test_narrowest_scope_ignores_activity_outside_the_current_scope():
    outside = {f"/subscriptions/{SUB2}/resourceGroups/rg9/providers/X/y/z"}
    assert rightsize.narrowest_scope(outside, f"/subscriptions/{SUB}") == f"/subscriptions/{SUB}"


def test_narrowest_scope_refuses_when_only_SOME_activity_is_inside_the_current_scope():
    """The dangerous case, and the one a "did anything match?" guard misses: most operations sit
    in one resource group and one sits in another subscription. Narrowing to the resource group
    covers the majority and breaks the rest — which is an outage proposed with confidence."""
    partly = {
        f"{RG}/providers/Microsoft.Compute/virtualMachines/vm1",
        f"{RG}/providers/Microsoft.Compute/virtualMachines/vm2",
        f"/subscriptions/{SUB2}/resourceGroups/rg9/providers/Microsoft.Compute/virtualMachines/vm3",
    }
    assert rightsize.narrowest_scope(partly, f"/subscriptions/{SUB}") == f"/subscriptions/{SUB}"


# =========================================================================== simulator
def _rows_for_sim():
    return [
        _row(assignmentId="direct", accessPath=schema.PATH_DIRECT, roleName="Owner"),
        _row(assignmentId="viagroup", accessPath=schema.PATH_GROUP, principalId="grp",
             groupChain="Admins", roleName="Owner"),
        _row(assignmentId="bobs", effectivePrincipalId="bob", effectivePrincipalName="Bob",
             principalId="bob", roleName="Reader", roleDefinitionId="/rd/reader",
             roleIsPrivileged=False),
    ]


def test_an_unknown_change_kind_raises_rather_than_being_ignored():
    """The Entra precedent skipped unknown kinds and produced a reassuring 'nothing changes'
    result from a typo — the worst possible output, because it looks like an answer."""
    with pytest.raises(simulator.InvalidChange):
        simulator.simulate(_rows_for_sim(), [{"kind": "delete_everything"}])


def test_a_change_missing_a_required_field_raises():
    with pytest.raises(simulator.InvalidChange):
        simulator.simulate(_rows_for_sim(), [{"kind": simulator.REMOVE_ASSIGNMENT}])


def test_a_change_against_an_id_that_does_not_exist_raises_missing_referent():
    """That is the typo case: applied silently it produces an empty diff that looks exactly
    like 'this change is safe'."""
    with pytest.raises(simulator.MissingReferent):
        simulator.simulate(_rows_for_sim(), [{"kind": simulator.REMOVE_ASSIGNMENT, "assignment_id": "nope"}])


def test_access_retained_via_another_path_is_computed_for_every_apparent_revocation():
    """Removing Alice from a group LOOKS like a revocation and frequently is not. A simulator
    that reports only removals encourages revocations that achieve nothing — and those leave a
    false record of remediation behind."""
    out = simulator.simulate(
        _rows_for_sim(),
        [{"kind": simulator.REMOVE_GROUP_MEMBER, "group_id": "grp", "principal_id": "alice"}],
    )
    assert out["access_retained_via_other_path"], "Alice still holds Owner directly"
    assert out["access_lost"] == []


def test_a_genuine_revocation_is_reported_as_lost():
    rows = [r for r in _rows_for_sim() if r["assignmentId"] != "direct"]
    out = simulator.simulate(
        rows, [{"kind": simulator.REMOVE_GROUP_MEMBER, "group_id": "grp", "principal_id": "alice"}]
    )
    assert out["access_lost"]
    assert out["access_retained_via_other_path"] == []


def test_retention_is_checked_against_covering_scopes_not_exact_ones():
    """An assignment at the subscription still covers a resource-group grant that was removed.
    Reporting that as lost access is the error this function exists to prevent."""
    rows = [
        _row(assignmentId="rg", scope=RG),
        _row(assignmentId="sub", scope=f"/subscriptions/{SUB}"),
    ]
    out = simulator.simulate(rows, [{"kind": simulator.REMOVE_ASSIGNMENT, "assignment_id": "rg"}])
    assert out["access_retained_via_other_path"]
    assert out["access_lost"] == []


def test_removing_an_assignment_removes_every_expanded_row_for_it():
    """Removing only the direct row would leave the group members' access in place and report a
    revocation that did not happen."""
    rows = [
        _row(assignmentId="shared", effectivePrincipalId="alice", accessPath=schema.PATH_GROUP),
        _row(assignmentId="shared", effectivePrincipalId="bob", accessPath=schema.PATH_GROUP),
    ]
    after = simulator.apply_changes(rows, [simulator.parse_change(
        {"kind": simulator.REMOVE_ASSIGNMENT, "assignment_id": "shared"})])
    assert after == []


def test_orphaned_resources_are_reported_and_cross_reference_ownership():
    """"After this change, this scope has no owner-level access" is the outcome that gets a
    revocation reverted in a panic two weeks later, and it is knowable in advance."""
    rows = [_row(assignmentId="only-owner")]
    out = simulator.simulate(
        rows, [{"kind": simulator.REMOVE_ASSIGNMENT, "assignment_id": "only-owner"}],
        owned_scopes={f"/subscriptions/{SUB}"},
    )
    orphan = out["orphaned_resources"][0]
    assert orphan["lostAllOwners"] is True
    assert orphan["hasRecordedOwner"] is True


def test_an_orphan_with_no_recorded_owner_is_distinguished_from_one_that_has_a_contact():
    rows = [_row(assignmentId="only-owner")]
    out = simulator.simulate(
        rows, [{"kind": simulator.REMOVE_ASSIGNMENT, "assignment_id": "only-owner"}],
        owned_scopes=set(),
    )
    assert out["orphaned_resources"][0]["hasRecordedOwner"] is False


def test_converting_to_eligible_reduces_standing_privilege_without_removing_access():
    out = simulator.simulate(
        _rows_for_sim(), [{"kind": simulator.CONVERT_TO_ELIGIBLE, "assignment_id": "direct"}]
    )
    assert out["standing_privilege_after"] < out["standing_privilege_before"]


def test_simulation_never_mutates_the_input_rows():
    rows = _rows_for_sim()
    before = [dict(r) for r in rows]
    simulator.simulate(rows, [{"kind": simulator.CONVERT_TO_ELIGIBLE, "assignment_id": "direct"}])
    assert rows == before


def test_sampling_is_seeded_and_reproducible():
    """A simulator whose answer moves between identical runs is not usable for a decision."""
    items = [{"principalId": f"p{i}", "privileged": False} for i in range(50)]
    a = simulator._sample(items, threshold=10)
    b = simulator._sample(items, threshold=10)
    assert [i["principalId"] for i in a["items"]] == [i["principalId"] for i in b["items"]]
    assert a["sampled"] is True


def test_privileged_rows_are_never_sampled_away():
    """A sample that drops the tier-0 holders is answering a different question from the one
    that was asked."""
    items = (
        [{"principalId": f"priv{i}", "privileged": True} for i in range(8)]
        + [{"principalId": f"p{i}", "privileged": False} for i in range(200)]
    )
    out = simulator._sample(items, threshold=10)
    kept = {i["principalId"] for i in out["items"]}
    assert all(f"priv{i}" in kept for i in range(8))
    assert out["always_full"] == 8


def test_sample_size_and_population_are_published_at_the_top_level():
    """So no chart can be rendered without them."""
    rows = [_row(assignmentId=f"a{i}", effectivePrincipalId=f"p{i}", roleIsPrivileged=False,
                 roleName="Reader", roleDefinitionId="/rd/reader") for i in range(30)]
    out = simulator.simulate(
        rows, [{"kind": simulator.REMOVE_GROUP, "group_id": "nothing"}]
    ) if False else simulator.simulate(rows, [{"kind": simulator.ASSUME_PRINCIPAL, "principal_id": "p1"}])
    assert {"sampled", "size", "population", "seed"} <= set(out["sample"])


def test_limitations_are_always_published():
    out = simulator.simulate(_rows_for_sim(), [{"kind": simulator.ASSUME_PRINCIPAL, "principal_id": "alice"}])
    assert out["limitations"]
    assert any("not a prediction" in l for l in out["limitations"])


def test_unevaluated_abac_conditions_are_named_in_the_limitations():
    rows = [_row(condition="@Resource[tag] StringEquals 'x'")]
    out = simulator.simulate(rows, [{"kind": simulator.ASSUME_PRINCIPAL, "principal_id": "alice"}])
    assert any("ABAC condition which was NOT evaluated" in l for l in out["limitations"])


def test_disable_bypass_states_that_it_models_rbac_only():
    """A bypass credential is not an access row. Implying the simulation covered it would be
    claiming to have modelled the one thing it cannot see."""
    out = simulator.simulate(
        _rows_for_sim(), [{"kind": simulator.DISABLE_BYPASS, "resource_id": "/subscriptions/x/rg/st"}]
    )
    assert any("cannot show who loses" in l for l in out["limitations"])


def test_adding_a_delegation_shows_up_as_gained_access():
    out = simulator.simulate(
        _rows_for_sim(),
        [{"kind": simulator.ADD_DELEGATION, "principal_id": "msp", "scope": f"/subscriptions/{SUB}",
          "role_name": "Contributor", "label": "Managed service provider"}],
    )
    assert out["access_gained"]
    assert out["access_gained"][0]["roleName"] == "Contributor"


def test_every_declared_change_kind_can_actually_be_applied():
    """A kind in the vocabulary that no branch handles is a 400 waiting to happen in the UI's
    own dropdown."""
    fixtures = {
        simulator.REMOVE_ASSIGNMENT: {"assignment_id": "direct"},
        simulator.REMOVE_GROUP_MEMBER: {"group_id": "grp", "principal_id": "alice"},
        simulator.REMOVE_GROUP: {"group_id": "grp"},
        simulator.CONVERT_TO_ELIGIBLE: {"assignment_id": "direct"},
        simulator.RESCOPE_ASSIGNMENT: {"assignment_id": "direct", "to_scope": RG},
        simulator.REPLACE_ROLE: {"assignment_id": "direct", "to_role": "Reader"},
        simulator.DISABLE_BYPASS: {"resource_id": "/x"},
        simulator.ASSUME_PRINCIPAL: {"principal_id": "alice"},
        simulator.ADD_DELEGATION: {"principal_id": "msp", "scope": f"/subscriptions/{SUB}", "role_name": "Reader"},
    }
    assert set(fixtures) == set(simulator.CHANGE_KINDS), "a change kind has no test fixture"
    for kind, extra in fixtures.items():
        out = simulator.simulate(_rows_for_sim(), [{"kind": kind, **extra}])
        assert "limitations" in out, f"{kind} did not produce a result"


def test_the_change_fingerprint_is_stable_and_order_independent():
    a = [{"kind": simulator.REMOVE_ASSIGNMENT, "assignment_id": "x"},
         {"kind": simulator.REMOVE_GROUP, "group_id": "g"}]
    assert simulator.fingerprint(a) == simulator.fingerprint(list(reversed(a)))
    assert simulator.fingerprint(a) != simulator.fingerprint(a[:1])


# =========================================================================== role hygiene
def _ctx(rows=None, role_defs=None, usage_payload=None, rightsizing=None):
    """A signal context.

    `rightsizing` is INJECTED rather than computed: the CIEM signals read a stored analysis
    written by the usage job, because computing it per request cost two seconds of CPU on the
    hottest endpoint in the product."""
    from app.iam import signals as sig

    ctx = sig.SignalContext(
        tenant_id="t1", rows=rows or [], kpis={}, scopes=[],
        directory={"role_defs": role_defs or []},
        usage=usage_payload or {"measured": False},
    )
    if rightsizing is not None:
        ctx._rightsizing = rightsizing
    elif usage_payload is not None:
        # Mirror production: the analysis is derived from the same usage payload.
        ctx._rightsizing = rightsize.analyse(
            rows or [], effective.build_role_index(role_defs or CATALOGUE), usage_payload
        )
    return ctx


def _spec(sid):
    from app.iam import signals as sig

    return next(s for s in sig.all_signals() if s.id == sid)


def test_role_hygiene_reports_not_measured_when_role_definitions_were_not_collected():
    """No findings because nothing was collected is indistinguishable from clean custom roles,
    and the reader will assume the second."""
    from app.iam import signals as sig

    for sid in ("lp.role_wildcard_action", "lp.role_authorization_write", "lp.role_duplicate"):
        with pytest.raises(sig.SignalUnavailable):
            _spec(sid).evaluate(_ctx())


def test_a_custom_role_granting_star_is_flagged():
    defs = [_role("Contoso Admin", ["*"], custom=True)]
    out = _spec("lp.role_wildcard_action").evaluate(_ctx(role_defs=defs))
    assert len(out) == 1
    assert "Owner does" in out[0].detail


def test_a_builtin_role_granting_star_is_not_flagged_as_a_custom_role_problem():
    """Owner legitimately grants `*`. Flagging it would bury the custom role that copied it."""
    out = _spec("lp.role_wildcard_action").evaluate(_ctx(role_defs=[_role("Owner", ["*"])]))
    assert out == []


def test_a_custom_role_that_can_assign_roles_is_critical():
    defs = [_role("Deployer", ["Microsoft.Authorization/roleAssignments/write"], custom=True)]
    out = _spec("lp.role_authorization_write").evaluate(_ctx(role_defs=defs))
    assert len(out) == 1 and out[0].severity == "critical"


def test_wildcard_authorization_actions_are_caught_too():
    defs = [_role("Deployer", ["Microsoft.Authorization/*/write"], custom=True)]
    assert _spec("lp.role_authorization_write").evaluate(_ctx(role_defs=defs))


def test_near_identical_custom_roles_are_clustered():
    common = [f"Microsoft.Compute/virtualMachines/{v}" for v in
              ("read", "write", "delete", "start/action", "restart/action",
               "deallocate/action", "powerOff/action", "redeploy/action",
               "runCommand/action", "reimage/action", "capture/action",
               "convertToManagedDisks/action", "generalize/action",
               "instanceView/read", "performMaintenance/action",
               "simulateEviction/action", "vmSizes/read", "extensions/read",
               "extensions/write", "extensions/delete")]
    defs = [
        _role("VM Ops A", common, custom=True),
        _role("VM Ops B", common, custom=True),
        _role("Unrelated", ["Microsoft.Storage/storageAccounts/read"], custom=True),
    ]
    out = _spec("lp.role_duplicate").evaluate(_ctx(role_defs=defs))
    assert len(out) == 1 and out[0].count == 2


def test_deliberately_different_roles_are_not_called_duplicates():
    """Two roles overlapping 80% usually differ on purpose, and telling a platform team to
    merge them is how this feature gets switched off."""
    defs = [
        _role("A", [f"x/{i}" for i in range(10)], custom=True),
        _role("B", [f"x/{i}" for i in range(8)] + ["y/1", "y/2"], custom=True),
    ]
    assert _spec("lp.role_duplicate").evaluate(_ctx(role_defs=defs)) == []


def test_a_custom_role_that_is_a_subset_of_a_much_larger_builtin_is_not_called_equivalent():
    """Reader is a subset of Owner. Recommending Owner as the 'equivalent' built-in would be
    the single worst remediation this product could emit."""
    defs = [_role("Owner", ["*"] + [f"x/{i}" for i in range(50)]),
            _role("Tiny", ["x/1"], custom=True)]
    out = _spec("lp.role_builtin_equivalent").evaluate(_ctx(role_defs=defs))
    assert out == []


def test_a_custom_role_matching_a_builtin_closely_is_flagged():
    actions = [f"Microsoft.Compute/virtualMachines/{i}" for i in range(10)]
    defs = [_role("VM Contributor Builtin", actions), _role("My VM Role", actions, custom=True)]
    out = _spec("lp.role_builtin_equivalent").evaluate(_ctx(role_defs=defs))
    assert len(out) == 1
    assert "will not follow" in out[0].detail


def test_notactions_that_another_role_re_grants_is_reported():
    """notActions subtracts from ITS OWN role; it is not a deny. The person who wrote the
    restriction believes it holds, and it does not."""
    defs = [
        _role("Restricted", ["*"], not_actions=["Microsoft.Authorization/*/write"], custom=True),
        _role("Assigner", ["Microsoft.Authorization/roleAssignments/write"]),
    ]
    rows = [
        _row(roleName="Restricted", roleDefinitionId="/rd/restricted"),
        _row(assignmentId="a2", roleName="Assigner", roleDefinitionId="/rd/assigner"),
    ]
    out = _spec("lp.role_notactions_illusion").evaluate(_ctx(rows=rows, role_defs=defs))
    assert len(out) == 1
    assert "it is not a deny" in out[0].detail


def test_notactions_is_not_reported_when_nothing_re_grants_the_action():
    defs = [
        _role("Restricted", ["*"], not_actions=["Microsoft.Authorization/*/write"], custom=True),
        _role("Reader", ["*/read"]),
    ]
    rows = [
        _row(roleName="Restricted", roleDefinitionId="/rd/restricted"),
        _row(assignmentId="a2", roleName="Reader", roleDefinitionId="/rd/reader"),
    ]
    assert _spec("lp.role_notactions_illusion").evaluate(_ctx(rows=rows, role_defs=defs)) == []


def test_a_role_assignable_at_root_but_used_in_one_subscription_is_flagged():
    defs = [_role("Narrow Use", ["x/read"], custom=True)]
    defs[0]["assignableScopes"] = ["/"]
    rows = [_row(roleName="Narrow Use", scope=f"/subscriptions/{SUB}")]
    out = _spec("lp.role_assignable_root").evaluate(_ctx(rows=rows, role_defs=defs))
    assert len(out) == 1
    assert "blast radius of the next mistake" in out[0].detail


def test_a_role_used_across_several_subscriptions_needs_its_wide_assignable_scope():
    defs = [_role("Wide Use", ["x/read"], custom=True)]
    rows = [
        _row(roleName="Wide Use", scope=f"/subscriptions/{SUB}"),
        _row(assignmentId="a2", roleName="Wide Use", scope=f"/subscriptions/{SUB2}"),
    ]
    assert _spec("lp.role_assignable_root").evaluate(_ctx(rows=rows, role_defs=defs)) == []


# =========================================================================== role catalogue durability
@pytest.mark.anyio
async def test_a_directory_refresh_does_not_delete_the_collected_role_definitions(
    isolated_cache, monkeypatch
):
    """The root cause of the false all-clear on a real tenant.

    Role definitions live in the directory blob but are collected only by `refresh_all`.
    `refresh_directory` rewrites that blob wholesale, and two of its three callers (the
    standalone directory job and the missions system) pass no `role_defs` — so an ordinary
    directory refresh persisted an empty list and destroyed them. Everything that answers "what
    does this role grant" then silently degraded: Effective Access, escalation, the simulator,
    the agent tool, and right-sizing, which reported 2,185 over-privileged assignments as zero.

    A refresh that never looked at role definitions must not be able to delete them."""
    from app.iam import orchestrator

    tenant = "t-roledefs"
    cache.write_directory(
        tenant, meta={"status": schema.STATUS_SUCCEEDED},
        rows=[], role_defs=CATALOGUE, principals=[], groups={},
    )
    assert len(cache.read_directory(tenant)["role_defs"]) == len(CATALOGUE)

    # No Graph token: the early-return path, which is the one the job hits on a connection
    # without directory permissions — and still rewrote the blob.
    monkeypatch.setattr(orchestrator, "_noop", orchestrator._noop)
    await orchestrator.refresh_directory(tenant, None)

    survived = cache.read_directory(tenant)["role_defs"]
    assert len(survived) == len(CATALOGUE), "a directory refresh deleted the role catalogue"
    assert {r["roleName"] for r in survived} == {r["roleName"] for r in CATALOGUE}
    # And the index built from it can still tell Owner from Reader.
    assert effective.build_role_index(survived), "role index must rebuild from the survivors"


@pytest.mark.anyio
async def test_a_refresh_that_did_collect_role_definitions_replaces_them(isolated_cache):
    """The carry-forward must not become a cache that can never be corrected — a role deleted
    in Azure has to be able to disappear here too."""
    from app.iam import orchestrator

    tenant = "t-roledefs-2"
    cache.write_directory(
        tenant, meta={"status": schema.STATUS_SUCCEEDED},
        rows=[], role_defs=CATALOGUE, principals=[], groups={},
    )
    fresh = {"guid-1": _role("Only Survivor", ["Microsoft.Compute/virtualMachines/read"])}
    await orchestrator.refresh_directory(tenant, None, role_defs=fresh)

    survived = cache.read_directory(tenant)["role_defs"]
    assert [r["roleName"] for r in survived] == ["Only Survivor"]


# =========================================================================== limits
def test_assignment_limit_pressure_fires_only_near_the_cap():
    from app.iam.signal_defs import rolehygiene

    few = [_row(assignmentId=f"a{i}", effectivePrincipalId=f"p{i}") for i in range(10)]
    assert rolehygiene._limit_pressure(_ctx(rows=few)) == []

    many = [_row(assignmentId=f"a{i}", effectivePrincipalId=f"p{i}") for i in
            range(int(rolehygiene.LIMIT_ASSIGNMENTS_PER_SUBSCRIPTION * 0.85))]
    out = rolehygiene._limit_pressure(_ctx(rows=many))
    assert len(out) == 1 and out[0].severity == "warning"


def test_group_expanded_rows_do_not_count_against_the_assignment_limit():
    """Azure counts assignments, not expanded members. Counting members would report a tenant as
    at its cap when it is nowhere near it — and the remediation for that is to create groups,
    which would make the number worse."""
    from app.iam.signal_defs import rolehygiene

    rows = [_row(assignmentId="one", effectivePrincipalId=f"p{i}", accessPath=schema.PATH_GROUP)
            for i in range(int(rolehygiene.LIMIT_ASSIGNMENTS_PER_SUBSCRIPTION * 0.9))]
    assert rolehygiene._limit_pressure(_ctx(rows=rows)) == []


def test_a_management_group_uses_the_much_lower_mg_limit():
    from app.iam.signal_defs import rolehygiene

    mg = "/providers/Microsoft.Management/managementGroups/mg1"
    rows = [_row(assignmentId=f"a{i}", effectivePrincipalId=f"p{i}", scope=mg)
            for i in range(int(rolehygiene.LIMIT_ASSIGNMENTS_PER_MG * 0.85))]
    out = rolehygiene._limit_pressure(_ctx(rows=rows))
    assert len(out) == 1
    assert out[0].evidence["limit"] == rolehygiene.LIMIT_ASSIGNMENTS_PER_MG


# =========================================================================== CIEM signals
def test_ciem_signals_report_not_measured_when_usage_was_never_collected():
    """Without this gate a tenant that has never run a usage scan sees "0 over-privileged
    principals" — the most reassuring possible rendering of "we have not looked"."""
    from app.iam import signals as sig
    from app.iam.signal_defs import ciem

    ctx = _ctx(rows=[_row()])
    for spec in ciem.SIGNALS:
        with pytest.raises(sig.SignalUnavailable):
            spec.evaluate(ctx)


def test_overprivileged_publishes_both_numbers_in_its_detail():
    used = [{"principalId": "alice", "actions": ["microsoft.compute/virtualmachines/read"], "events": 9}]
    ctx = _ctx(rows=[_row()], role_defs=CATALOGUE, usage_payload=_usage(used))
    out = _spec("lp.overprivileged").evaluate(ctx)
    assert out
    assert "of the" in out[0].detail and "action patterns" in out[0].detail
    assert out[0].evidence["grantedActionCount"] > 0


def test_a_low_confidence_finding_is_not_raised_at_all():
    """A low-confidence 'unused' is a prompt to collect more data, not a finding somebody should
    act on."""
    used = [{"principalId": "alice", "actions": ["x/read"], "events": 1}]
    ctx = _ctx(rows=[_row()], role_defs=CATALOGUE, usage_payload=_usage(used, window=7))
    assert _spec("lp.overprivileged").evaluate(ctx) == []


def test_owner_used_as_reader_needs_recorded_activity_not_an_empty_log():
    """No recorded activity is the BLIND case, not the read-only case. Asserting read-only from
    an empty log invents a behaviour profile."""
    ctx = _ctx(rows=[_row()], role_defs=CATALOGUE, usage_payload=_usage([]))
    assert _spec("lp.owner_used_as_reader").evaluate(ctx) == []


def test_owner_used_as_reader_fires_when_every_recorded_action_was_a_read():
    used = [{"principalId": "alice", "actions": ["microsoft.compute/virtualmachines/read",
                                                 "microsoft.storage/storageaccounts/read"], "events": 40}]
    ctx = _ctx(rows=[_row()], role_defs=CATALOGUE, usage_payload=_usage(used))
    out = _spec("lp.owner_used_as_reader").evaluate(ctx)
    assert len(out) == 1
    assert "have been looking at it" in out[0].detail


def test_owner_used_as_reader_does_not_fire_when_a_write_was_recorded():
    used = [{"principalId": "alice", "actions": ["microsoft.compute/virtualmachines/read",
                                                 "microsoft.compute/virtualmachines/write"], "events": 40}]
    ctx = _ctx(rows=[_row()], role_defs=CATALOGUE, usage_payload=_usage(used))
    assert _spec("lp.owner_used_as_reader").evaluate(ctx) == []


def test_a_break_glass_owner_is_never_reported_as_used_only_for_reading():
    used = [{"principalId": "bg", "actions": ["x/read"], "events": 2}]
    rows = [_row(effectivePrincipalId="bg", effectivePrincipalName="breakglass-admin")]
    ctx = _ctx(rows=rows, role_defs=CATALOGUE, usage_payload=_usage(used))
    assert _spec("lp.owner_used_as_reader").evaluate(ctx) == []


def test_every_ciem_and_hygiene_signal_is_discoverable_through_the_registry():
    from app.iam import signals as sig

    ids = {s.id for s in sig.all_signals()}
    for expected in ("lp.overprivileged", "lp.owner_used_as_reader", "lp.scope_too_broad",
                     "lp.role_wildcard_action", "lp.role_authorization_write", "lp.role_unused",
                     "lp.role_duplicate", "lp.role_builtin_equivalent", "lp.role_assignable_root",
                     "lp.role_notactions_illusion", "lp.assignment_limit_pressure"):
        assert expected in ids, f"{expected} never reached the registry"
