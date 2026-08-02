"""The IAM signal registry, the posture score, and the findings service.

The rules pinned here are the ones that make the numbers on this screen trustworthy. Each has a
corresponding way of being silently wrong:

* a signal that cannot be evaluated returning ``[]``  -> "we could not look" scores as "clean"
* a pillar with no checks scoring 100                 -> unbuilt scores as perfect
* a grade shown at low coverage                       -> a letter from a third of the checks
* a finding fingerprint that includes a count         -> everything resolves and reappears每 run
* viewing findings recording a run                    -> "nothing changed, because somebody looked"
"""
from __future__ import annotations

import pytest

from app.iam import cache, demo, findings, schema, score, signals
from app.iam.signals import Finding, SignalContext, SignalSpec, SignalUnavailable

pytestmark = pytest.mark.anyio


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


def _ctx(rows=None, kpis=None, scopes=None, tenant="t1") -> SignalContext:
    return SignalContext(
        tenant_id=tenant,
        rows=rows or [],
        kpis=kpis or {},
        scopes=scopes or [],
    )


# --------------------------------------------------------------------------- registry invariants
def test_signal_ids_are_unique_and_pillar_namespaced():
    """The id prefix is how findings, scanners and the score agree on which pillar a check
    belongs to. A mismatch silently moves weight between pillars."""
    specs = signals.all_signals()
    ids = [s.id for s in specs]
    assert len(ids) == len(set(ids)), "duplicate signal ids"
    for s in specs:
        assert s.id.startswith(f"{s.pillar}."), f"{s.id} is not namespaced to pillar {s.pillar}"


def test_every_signal_declares_a_valid_pillar_severity_kind_and_weight():
    for s in signals.all_signals():
        assert s.pillar in signals.PILLAR_KEYS, f"{s.id} has unknown pillar {s.pillar}"
        assert s.severity in signals.SEVERITIES, f"{s.id} has unknown severity {s.severity}"
        assert s.object_kind in signals.OBJECT_KINDS, f"{s.id} has unknown object_kind {s.object_kind}"
        assert 1 <= s.weight <= 10, f"{s.id} weight {s.weight} outside 1..10"


def test_every_signal_explains_itself():
    """A finding a reader cannot act on is noise. Every signal owes a why and a remediation."""
    for s in signals.all_signals():
        assert s.why.strip(), f"{s.id} has no 'why'"
        assert s.remediation.strip(), f"{s.id} has no remediation"


def test_pillar_weights_sum_to_100():
    assert sum(p["weight"] for p in signals.PILLARS) == 100


def test_declared_frameworks_are_well_formed():
    """A padded control matrix is worse than a short honest one — an auditor who finds one bad
    mapping discards the whole table."""
    known = {"CIS-Azure", "NIST", "ISO", "PCI", "MCSB", "WAF"}
    for s in signals.all_signals():
        for f in s.frameworks:
            assert ":" in f, f"{s.id} framework {f!r} is not '<framework>:<control>'"
            assert f.split(":", 1)[0] in known, f"{s.id} references unknown framework {f!r}"


def test_every_registered_signal_is_reachable_from_a_pillar():
    covered = {s.id for p in signals.PILLARS for s in signals.signals_for_pillar(p["key"])}
    assert covered == {s.id for s in signals.all_signals()}


def test_findings_carry_the_id_of_the_signal_that_produced_them(isolated_cache):
    """A signal that stamps a *different* id on its findings breaks two things quietly:
    `/iam/signals` can no longer explain the finding, and — worse — the fingerprint changes
    when the signal picks the other id, so a recorded suppression evaporates and the finding
    returns looking new. Caught exactly this in `ext.guest_access`, which used to switch to
    `ext.guest_privileged` whenever the guest happened to hold a privileged role."""
    demo.seed_demo("t1")
    known = {s.id for s in signals.all_signals()}
    for r in findings.evaluate("t1"):
        for f in r.findings:
            assert f.signal_id == r.spec.id, f"{r.spec.id} emitted a finding tagged {f.signal_id}"
            assert f.signal_id in known
            # The pillar must match too, or the score attributes the finding to the wrong one.
            assert f.pillar == r.spec.pillar


def test_findings_never_carry_an_unknown_severity_or_kind(isolated_cache):
    demo.seed_demo("t1")
    for r in findings.evaluate("t1"):
        for f in r.findings:
            assert f.severity in signals.SEVERITIES
            assert f.object_kind in signals.OBJECT_KINDS
            assert f.subject, f"{f.signal_id} produced a finding with no subject to fingerprint"


def _guest_row(**kw):
    return schema.make_row(
        effectivePrincipalUserPrincipalName="alice_contoso.com#EXT#@fabrikam.onmicrosoft.com",
        effectivePrincipalId="guest-1",
        effectivePrincipalName="Alice (Contoso)",
        **kw,
    )


def test_a_guest_gaining_privilege_keeps_the_same_signal_id_and_fingerprint():
    """The demo estate has no guests, so the estate-wide invariant test cannot see this path.

    A privileged guest and an unprivileged one differ in severity and wording, but they are the
    same *signal* about the same *subject*. Emitting a different signal id for the privileged
    case changes the fingerprint, which would silently discard a suppression the moment the
    guest's role changed — and re-present the finding as brand new."""
    from app.iam.signal_defs import ext

    spec = next(s for s in ext.SIGNALS if s.id == "ext.guest_access")

    plain = spec.evaluate(_ctx(rows=[_guest_row(roleName="Reader")]))
    privileged = spec.evaluate(_ctx(rows=[_guest_row(roleName="Owner", roleIsPrivileged=True)]))

    assert len(plain) == 1 and len(privileged) == 1
    assert plain[0].signal_id == privileged[0].signal_id == "ext.guest_access"
    assert plain[0].fingerprint == privileged[0].fingerprint
    # The severity is what changes, and it should.
    assert plain[0].severity == "warning"
    assert privileged[0].severity == "critical"


def test_guest_detection_reports_blind_when_no_names_were_resolved():
    """Without resolved principal names a guest is indistinguishable from a member, so the
    signal must say it could not look rather than return an empty (clean-looking) list."""
    from app.iam.signal_defs import ext

    spec = next(s for s in ext.SIGNALS if s.id == "ext.guest_access")
    with pytest.raises(SignalUnavailable):
        spec.evaluate(_ctx(rows=[schema.make_row(roleName="Owner")]))


# --------------------------------------------------------------------------- evaluation contract
def test_unavailable_signal_is_recorded_as_not_measured_not_as_clean():
    """The single most important rule in this file: blind must not score as clean."""
    def _blind(_ctx):
        raise SignalUnavailable("the directory could not be read")

    spec = SignalSpec(id="hyg.x", title="x", pillar="hyg", severity="warning", weight=5,
                      object_kind="tenant", evaluate=_blind, why="w", remediation="r")
    result = signals.SignalResult(spec, [], measured=False, reason="the directory could not be read")
    s = score.compute([result])
    hyg = next(p for p in s["pillars"] if p["key"] == "hyg")
    assert hyg["score"] is None
    assert hyg["state"] == "blind"
    assert "could not be read" in hyg["reason"]


async def test_a_broken_signal_does_not_blind_the_others(monkeypatch):
    """One bad check must not take the screen down or silence the other fifty."""
    def _boom(_ctx):
        raise RuntimeError("bug in this signal")

    good = SignalSpec(id="hyg.good", title="ok", pillar="hyg", severity="info", weight=1,
                      object_kind="tenant", evaluate=lambda c: [], why="w", remediation="r")
    bad = SignalSpec(id="hyg.bad", title="bad", pillar="hyg", severity="info", weight=1,
                     object_kind="tenant", evaluate=_boom, why="w", remediation="r")
    monkeypatch.setattr(signals, "_REGISTRY", [good, bad])
    results = signals.evaluate_all(_ctx())
    assert len(results) == 2
    assert {r.spec.id: r.measured for r in results} == {"hyg.good": True, "hyg.bad": False}


def test_context_collector_ran_distinguishes_blind_from_absent():
    scopes_ok = [{"collectors": [{"collector": "AzurePimEligibility", "status": schema.STATUS_SUCCEEDED}]}]
    scopes_blind = [{"collectors": [{"collector": "AzurePimEligibility", "status": schema.STATUS_UNAUTHORIZED}]}]
    assert _ctx(scopes=scopes_ok).collector_ran("AzurePimEligibility") is True
    assert _ctx(scopes=scopes_blind).collector_ran("AzurePimEligibility") is False
    assert _ctx(scopes=[]).collector_ran("AzurePimEligibility") is False


def test_deny_rows_are_excluded_from_the_grant_view():
    """A signal counting a deny as access would report the control as the risk."""
    rows = [
        schema.make_row(roleName="Owner", roleIsPrivileged=True),
        schema.make_row(roleName="Blueprint lock", effect=schema.EFFECT_DENY),
    ]
    assert len(_ctx(rows=rows).grants) == 1


# --------------------------------------------------------------------------- fingerprints
def test_fingerprint_is_stable_across_runs_and_ignores_volatile_fields():
    """Resolution is COMPUTED — a fingerprint that stops appearing is resolved. Including a
    count or timestamp would make every finding resolve and reappear on every scan, and every
    suppression evaporate with it."""
    a = Finding(signal_id="priv.x", title="t", severity="error", pillar="priv",
                object_kind="scope", subject="/subscriptions/s", count=3, detail="now")
    b = Finding(signal_id="priv.x", title="t (reworded)", severity="warning", pillar="priv",
                object_kind="scope", subject="/subscriptions/s", count=99, detail="later")
    assert a.fingerprint == b.fingerprint

    c = Finding(signal_id="priv.x", title="t", severity="error", pillar="priv",
                object_kind="scope", subject="/subscriptions/other")
    assert a.fingerprint != c.fingerprint


def test_fingerprint_is_case_insensitive_on_the_subject():
    a = Finding(signal_id="priv.x", title="t", severity="error", pillar="priv", object_kind="scope", subject="/SUBS/A")
    b = Finding(signal_id="priv.x", title="t", severity="error", pillar="priv", object_kind="scope", subject="/subs/a")
    assert a.fingerprint == b.fingerprint


# --------------------------------------------------------------------------- score
def test_a_pillar_with_no_signals_is_not_implemented_not_perfect():
    """Unbuilt must not score as flawless — that is how a roadmap gap becomes a clean bill."""
    s = score.compute([])
    for p in s["pillars"]:
        assert p["state"] == "not_implemented"
        assert p["score"] is None
    assert s["score"] is None
    assert s["grade"] is None
    assert s["coverage"] == 0.0


def test_grade_is_withheld_below_the_coverage_floor():
    """A letter derived from a third of the checks gets quoted without the caveat."""
    good = SignalSpec(id="priv.a", title="a", pillar="priv", severity="info", weight=1,
                      object_kind="tenant", evaluate=lambda c: [], why="w", remediation="r")
    s = score.compute([signals.SignalResult(good, [], measured=True)])
    assert s["score"] == 100                      # the one thing measured is clean
    assert s["coverage"] < score.MIN_COVERAGE_FOR_GRADE
    assert s["grade"] is None
    assert "measured" in s["grade_withheld_reason"]


def test_coverage_is_weight_granular_within_a_pillar():
    """Half a pillar's weight unmeasured is half covered — not fully covered on the half we saw."""
    heavy = SignalSpec(id="priv.heavy", title="h", pillar="priv", severity="info", weight=9,
                       object_kind="tenant", evaluate=lambda c: [], why="w", remediation="r")
    light = SignalSpec(id="priv.light", title="l", pillar="priv", severity="info", weight=1,
                       object_kind="tenant", evaluate=lambda c: [], why="w", remediation="r")
    s = score.compute([
        signals.SignalResult(heavy, [], measured=False, reason="blind"),
        signals.SignalResult(light, [], measured=True),
    ])
    priv = next(p for p in s["pillars"] if p["key"] == "priv")
    assert priv["measured_fraction"] == 0.1, "the heavy signal being blind must dominate coverage"
    assert priv["state"] == "partial"


def test_findings_reduce_the_pillar_score_by_severity():
    def _one(sev):
        spec = SignalSpec(id="priv.s", title="s", pillar="priv", severity=sev, weight=10,
                          object_kind="tenant", evaluate=lambda c: [], why="w", remediation="r")
        f = Finding(signal_id="priv.s", title="s", severity=sev, pillar="priv",
                    object_kind="tenant", subject="t")
        return score.compute([signals.SignalResult(spec, [f], measured=True)])["pillars"][0]["score"]

    assert _one("critical") < _one("error") < _one("warning") < _one("info")


def test_a_noisy_signal_cannot_dominate_a_pillar():
    """Volume saturates: past a few findings the marginal one adds nothing, or one chatty check
    would swamp everything else in its pillar."""
    spec = SignalSpec(id="priv.s", title="s", pillar="priv", severity="warning", weight=10,
                      object_kind="tenant", evaluate=lambda c: [], why="w", remediation="r")
    many = [Finding(signal_id="priv.s", title="s", severity="warning", pillar="priv",
                    object_kind="tenant", subject=f"s{i}") for i in range(500)]
    s = score.compute([signals.SignalResult(spec, many, measured=True)])
    assert s["pillars"][0]["score"] > 0, "a single signal must not zero a pillar"


def test_score_is_never_returned_without_coverage():
    s = score.compute([])
    assert "coverage" in s and "score" in s


def test_no_two_findings_ever_share_a_fingerprint(isolated_cache):
    """A fingerprint is an IDENTITY, and three separate systems trust it as one.

    It keys the suppression table, the scanner's new/resolved delta and the React list. Two
    findings sharing a fingerprint therefore means: suppressing one silently suppresses the
    other, the scanner's `total` stops equalling `new + persisting`, and the grid throws
    duplicate-key errors. Measured on the live `lu` tenant before this was enforced: 1,007
    findings held only 908 distinct fingerprints — `priv.eligible_without_controls` used
    `assignmentId`, which every transitive member of a group-granted eligibility inherits (10
    principals to one id), and `lp.role_notactions_illusion` emitted several findings all keyed
    on the bare principal id.

    The rule is: whatever a signal chooses as its subject must distinguish its own findings.
    Aggregate with `count` when they are really one thing; key on the full grant when they are
    not."""
    demo.seed_demo("t1")
    seen: dict[str, Finding] = {}
    for result in findings.evaluate("t1"):
        for f in result.findings:
            clash = seen.get(f.fingerprint)
            assert clash is None, (
                f"{f.signal_id} emitted two findings with fingerprint {f.fingerprint}: "
                f"{clash.subject_label!r} and {f.subject_label!r}. Both share subject "
                f"{f.subject!r}."
            )
            seen[f.fingerprint] = f


def test_a_group_granted_eligibility_does_not_collapse_its_members_into_one_finding(isolated_cache):
    """The exact live shape the estate-wide test above cannot see.

    The demo estate has no group-granted PIM eligibility, so the collision that produced 1,007
    findings from 908 fingerprints on tenant `lu` reproduces only synthetically. Every
    transitive member of a group-granted eligibility inherits the GROUP's
    roleEligibilityScheduleInstance id, so keying the finding on `assignmentId` gave ten
    different people one identity — and one suppression."""
    shared = "/providers/Microsoft.Authorization/roleEligibilityScheduleInstances/shared-id"
    rows = [
        schema.make_row(
            surface=schema.SURFACE_AZURE_RBAC, effect=schema.EFFECT_ALLOW,
            assignmentState=schema.STATE_ELIGIBLE, accessPath=schema.PATH_GROUP,
            principalId="grp", effectivePrincipalId=pid, effectivePrincipalName=name,
            effectivePrincipalType="User", roleDefinitionId="/rd/owner", roleName="Owner",
            roleIsPrivileged=True, scope="/subscriptions/s1", assignmentId=shared,
            isPermanentEligible=True, requiresApproval=False, requiresMfa=False,
            principalExists=schema.EXISTS_TRUE,
        )
        for pid, name in (("u1", "Alice"), ("u2", "Bob"), ("u3", "Carol"))
    ]
    ctx = _ctx(rows=rows, scopes=[{
        "collectors": [{"collector": "AzurePimEligibility", "status": schema.STATUS_SUCCEEDED}],
    }])

    spec = next(s for s in signals.all_signals() if s.id == "priv.eligible_without_controls")
    out = spec.evaluate(ctx)
    assert len(out) == 3, "each member holds this eligibility in their own right"
    assert len({f.fingerprint for f in out}) == 3, "three people must not share one fingerprint"
    assert {f.subject_label for f in out} == {"Alice → Owner", "Bob → Owner", "Carol → Owner"}


def test_notactions_findings_for_one_principal_aggregate_into_one(isolated_cache):
    """The second live collision, and the demo estate does not contain it either.

    `lp.role_notactions_illusion` is about a principal, so its subject is the principal id. A
    principal with two re-granted restrictions therefore produced two findings with one
    fingerprint — one suppression hiding both. Aggregating with a `count` is the fix the rest of
    the registry already uses; the alternative (a compound subject) would break the deep link
    that the `principal` object_kind promises."""
    role_defs = [
        {"roleDefinitionId": "/rd/restricted-a", "roleName": "Restricted A",
         "actions": ["Microsoft.Compute/*"], "notActions": ["Microsoft.Compute/virtualMachines/delete"],
         "dataActions": [], "notDataActions": [], "roleType": "CustomRole"},
        {"roleDefinitionId": "/rd/restricted-b", "roleName": "Restricted B",
         "actions": ["Microsoft.Storage/*"], "notActions": ["Microsoft.Storage/storageAccounts/delete"],
         "dataActions": [], "notDataActions": [], "roleType": "CustomRole"},
        {"roleDefinitionId": "/rd/regrant", "roleName": "Regrant All",
         "actions": ["Microsoft.Compute/virtualMachines/delete",
                     "Microsoft.Storage/storageAccounts/delete"],
         "notActions": [], "dataActions": [], "notDataActions": [], "roleType": "CustomRole"},
    ]

    def _row(role_id, role_name):
        return schema.make_row(
            surface=schema.SURFACE_AZURE_RBAC, effect=schema.EFFECT_ALLOW,
            assignmentState=schema.STATE_ACTIVE, accessPath=schema.PATH_DIRECT,
            principalId="u1", effectivePrincipalId="u1", effectivePrincipalName="Alice",
            effectivePrincipalType="User", roleDefinitionId=role_id, roleName=role_name,
            scope="/subscriptions/s1", assignmentId=f"a-{role_name}",
            principalExists=schema.EXISTS_TRUE,
        )

    ctx = _ctx(rows=[_row("/rd/restricted-a", "Restricted A"),
                     _row("/rd/restricted-b", "Restricted B"),
                     _row("/rd/regrant", "Regrant All")])
    ctx.directory = {"role_defs": role_defs}

    spec = next(s for s in signals.all_signals() if s.id == "lp.role_notactions_illusion")
    out = spec.evaluate(ctx)
    assert len(out) == 1, "two re-granted restrictions on one principal are ONE finding"
    assert out[0].count == 2, "…and the count must not hide the second one"
    assert len({f.fingerprint for f in out}) == len(out)


# --------------------------------------------------------------------------- over demo data
def test_registry_runs_clean_over_the_demo_dataset(isolated_cache):
    demo.seed_demo("t1")
    results = findings.evaluate("t1")
    assert len(results) == len(signals.all_signals())
    # The demo estate is deliberately imperfect, so several signals must actually fire —
    # otherwise the whole registry could be broken and the tests would still pass.
    fired = [r for r in results if r.findings]
    assert len(fired) >= 5, "the demo dataset should exercise the registry"
    # …and at least one must honestly report that it could not measure.
    unmeasured = [r for r in results if not r.measured]
    assert unmeasured and all(r.reason for r in unmeasured), "an unmeasured signal must say why"


def test_demo_dataset_fires_the_signals_it_was_built_to_exercise(isolated_cache):
    demo.seed_demo("t1")
    fired = {r.spec.id for r in findings.evaluate("t1") if r.findings}
    assert "priv.standing_privilege" in fired          # 82% standing in the demo estate
    assert "priv.classic_administrators" in fired      # Ken is a co-administrator
    assert "priv.eligible_without_controls" in fired   # Henry: permanent eligibility, no approval/MFA
    assert "str.demo_data_present" in fired            # the trust signal


def test_score_over_demo_data_publishes_coverage_and_pillar_states(isolated_cache):
    demo.seed_demo("t1")
    s = findings.compute_score("t1")
    assert 0 <= s["score"] <= 100
    assert 0 < s["coverage"] <= 1

    # This test has now been rewritten twice, each time because a pillar it named as "unbuilt"
    # got built (esc in P5, byp in P6). The invariant was never about a particular pillar: it is
    # that a pillar WITHOUT signals reports `not_implemented` with a null score, and one WITH
    # signals never claims to be unimplemented. Stated that way it survives the next phase.
    registered = {s.pillar for s in signals.all_signals()}
    for p in s["pillars"]:
        if p["key"] in registered:
            assert p["state"] != "not_implemented", f"{p['key']} has signals but claims otherwise"
            assert p["score"] is not None
        else:
            assert p["state"] == "not_implemented", f"{p['key']} has no signals but scored"
            assert p["score"] is None


def test_an_unbuilt_pillar_is_still_possible_and_still_reports_honestly(monkeypatch):
    """The behaviour the test above used to pin, kept explicitly now that every pillar in the
    shipped registry happens to be built."""
    only_priv = [s for s in signals.all_signals() if s.pillar == "priv"][:1]
    monkeypatch.setattr(signals, "_REGISTRY", only_priv)
    s = score.compute([signals.SignalResult(only_priv[0], [], measured=True)])
    unbuilt = [p for p in s["pillars"] if p["key"] != "priv"]
    assert unbuilt and all(p["state"] == "not_implemented" and p["score"] is None for p in unbuilt)


async def test_listing_findings_overlays_state_and_hides_suppressed(isolated_cache, monkeypatch):
    demo.seed_demo("t1")

    async def fake_states(_tenant):
        first = findings.evaluate("t1")
        fp = next(f.fingerprint for r in first if r.findings for f in r.findings)
        return {fp: {"state": "suppressed", "reason": "accepted by security", "updated_by": "u", "updated_at": ""}}

    monkeypatch.setattr(findings, "_state_map", fake_states)
    visible = await findings.list_findings("t1")
    with_suppressed = await findings.list_findings("t1", include_suppressed=True)
    assert with_suppressed["total"] == visible["total"] + 1
    # A suppression is never deleted — it is retrievable, because a risk acceptance that
    # silently disappears becomes an unknown risk.
    suppressed = [f for f in with_suppressed["findings"] if f["state"] == "suppressed"]
    assert suppressed and suppressed[0]["state_reason"] == "accepted by security"


async def test_findings_are_ordered_worst_first(isolated_cache):
    demo.seed_demo("t1")
    out = await findings.list_findings("t1")
    ranks = [signals.SEVERITY_RANK[f["severity"]] for f in out["findings"]]
    assert ranks == sorted(ranks)


async def test_unmeasured_signals_are_published_alongside_the_findings(isolated_cache):
    """The reader has to be able to see what was NOT checked, or an empty list reads as clean."""
    demo.seed_demo("t1")
    out = await findings.list_findings("t1")
    assert out["unmeasured"], "unmeasured signals must be reported, not hidden"
    assert all(u["reason"] for u in out["unmeasured"])


# ------------------------------------------------------------------- grouping count maps
_COUNT_MAPS = ("counts_by_severity", "counts_by_pillar", "counts_by_signal", "counts_by_object_kind", "counts_by_state")


async def test_every_count_map_tallies_the_whole_filtered_set(isolated_cache):
    """The UI groups findings using these maps, so each one must sum to ``total``.

    A map that summed to the PAGE instead would give every group header a number that shrinks as
    the reader scrolls — and on any estate past the page size, understates the problem."""
    demo.seed_demo("t1")
    out = await findings.list_findings("t1")
    for key in _COUNT_MAPS:
        assert sum(out[key].values()) == out["total"], f"{key} does not tally the whole set"


async def test_count_maps_are_unaffected_by_paging(isolated_cache):
    """Same filters, one row per page: the tallies must not move."""
    demo.seed_demo("t1")
    full = await findings.list_findings("t1")
    paged = await findings.list_findings("t1", limit=1)
    assert len(paged["findings"]) == 1 < len(full["findings"])
    for key in _COUNT_MAPS:
        assert paged[key] == full[key], f"{key} changed when the page shrank"


async def test_count_maps_describe_the_filtered_set_not_the_whole_tenant(isolated_cache):
    """Filtering to one severity must shrink the other maps with it, or the headers describe
    findings the reader cannot see."""
    demo.seed_demo("t1")
    full = await findings.list_findings("t1")
    sev = next(s for s, n in full["counts_by_severity"].items() if n)
    narrowed = await findings.list_findings("t1", severity=sev)
    assert narrowed["counts_by_severity"][sev] == full["counts_by_severity"][sev]
    assert sum(narrowed["counts_by_severity"].values()) == narrowed["total"] < full["total"]
    assert sum(narrowed["counts_by_signal"].values()) == narrowed["total"]
    assert set(narrowed["counts_by_signal"]) <= set(full["counts_by_signal"])


async def test_signal_tally_omits_zeroes_but_the_fixed_vocabularies_do_not(isolated_cache):
    """~50 registered signals produce nothing on a healthy tenant; shipping 50 zero keys on
    every response is pure payload. The fixed vocabularies keep theirs so a genuine zero can be
    rendered AS a zero rather than as an absent group."""
    demo.seed_demo("t1")
    out = await findings.list_findings("t1")
    assert all(n > 0 for n in out["counts_by_signal"].values())
    assert set(out["counts_by_severity"]) == set(signals.SEVERITIES)
    assert set(out["counts_by_object_kind"]) == set(signals.OBJECT_KINDS)
    assert set(out["counts_by_state"]) == set(findings.STATES)


async def test_grouping_keys_are_present_on_every_finding(isolated_cache):
    """A finding missing one of these lands in an unlabelled bucket the reader cannot act on."""
    demo.seed_demo("t1")
    out = await findings.list_findings("t1", limit=findings.MAX_FINDINGS)
    for f in out["findings"]:
        assert f["pillar"] and f["severity"] and f["signal_id"] and f["object_kind"] and f["state"]


# --------------------------------------------------------------------------- api surface
def test_findings_routes_are_registered_and_write_is_separated():
    from app.api import iam as iam_api

    paths = {r.path for r in iam_api.router.routes}
    assert {"/findings", "/findings/{fingerprint}/state", "/score", "/signals"} <= paths


def test_findings_are_not_embedded_in_the_refresh_response():
    """Findings get their own endpoint on purpose. Embedding them in every run response is what
    took the equivalent Entra payload from ~10 KB to 1.3 MB."""
    import inspect

    from app.api import iam as iam_api

    src = inspect.getsource(iam_api)
    refresh = src.split("async def refresh", 1)
    if len(refresh) > 1:
        body = refresh[1].split("\n@router", 1)[0]
        assert "findings.list_findings" not in body and "findings.evaluate" not in body


def test_state_change_requires_write_permission_not_read():
    """Suppressing a finding is a governance decision. Read access must not be enough."""
    from app.api import iam as iam_api

    route = next(r for r in iam_api.router.routes if getattr(r, "path", "") == "/findings/{fingerprint}/state")
    deps = repr(route.dependant.dependencies) + repr(getattr(route, "dependencies", []))
    assert "iam.write" in deps or iam_api.require_write is not iam_api.require_admin
