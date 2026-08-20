"""The Entra API handlers, in-process, over the demo snapshot.

The live test tenant has no Conditional Access policies, so the simulator's HTTP path
(validation, save, list, re-run) cannot be exercised against it. These tests drive the
handlers directly over the demo tenant, which has policies, break-glass candidates,
PIM configuration and risky applications.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.api import entra as entra_api
from app.entra import cache, demo
from app.entra import scanners as scanners_mod
from app.entra import snapshot as snapshot_mod


class _FakeDB:
    """Minimal stand-in for the AsyncSession the audit helper touches."""

    def __init__(self) -> None:
        self.rows: list[object] = []

    def add(self, row: object) -> None:
        self.rows.append(row)

    async def commit(self) -> None:
        pass


class _Principal:
    tenant_id = demo.DEMO_TENANT
    subject = "dev"


@pytest.fixture(autouse=True)
def _demo_tenant(tmp_path, monkeypatch):
    cache.set_root_for_tests(tmp_path / "entra")
    snapshot_mod._analysis_memo.clear()  # noqa: SLF001 - test isolation

    import app.core.azure_connections as ac

    monkeypatch.setattr(
        ac, "resolve_connection",
        lambda cid: {"id": "conn-demo", "tenant_id": demo.DEMO_TENANT} if cid == "conn-demo" else None,
    )
    demo.seed()
    yield
    cache.clear_memo()


def _run(coro):
    return asyncio.run(coro)


def _policies():
    body = _run(entra_api.ca_policies(connection_id="conn-demo", principal=_Principal()))
    return body["policies"]


def _simulate(changes, **kw):
    body = entra_api.SimulateBody(changes=changes, **kw)
    return _run(entra_api.ca_simulate(
        body=body, connection_id="conn-demo", principal=_Principal(), db=_FakeDB()))


# ------------------------------------------------------------------------- simulator
def test_disabling_an_enforced_policy_reports_protection_lost():
    enforced = [p for p in _policies() if p["is_enforced"]]
    assert enforced, "the demo tenant must ship an enforced policy"
    out = _simulate([{"kind": "disable", "policy_id": enforced[0]["id"]}])
    result = out["result"]
    assert result["counts"]["protection_lost"] > 0
    assert result["counts"]["newly_blocked"] == 0
    assert result["limitations"], "the model must always publish its limitations"


def test_enabling_a_report_only_policy_reports_new_blocks():
    candidates = [p for p in _policies() if not p["is_enforced"]]
    assert candidates
    out = _simulate([{"kind": "enable", "policy_id": candidates[0]["id"]}])
    counts = out["result"]["counts"]
    assert counts["newly_blocked"] + counts["newly_challenged"] > 0


def test_an_unapplicable_change_is_a_400_not_an_empty_diff():
    with pytest.raises(HTTPException) as exc:
        _simulate([{"kind": "remove_exclusion", "policy_id": "whatever"}])
    assert exc.value.status_code == 400
    assert "remove_exclusion" in str(exc.value.detail)

    with pytest.raises(HTTPException) as exc:
        _simulate([{"kind": "disable", "policy_id": "no-such-policy"}])
    assert exc.value.status_code == 400
    assert "no-such-policy" in str(exc.value.detail)


def test_an_empty_change_set_is_rejected():
    with pytest.raises(HTTPException) as exc:
        _simulate([])
    assert exc.value.status_code == 400


def test_a_simulation_run_is_written_to_the_audit_trail():
    enforced = [p for p in _policies() if p["is_enforced"]][0]
    db = _FakeDB()
    _run(entra_api.ca_simulate(
        body=entra_api.SimulateBody(changes=[{"kind": "disable", "policy_id": enforced["id"]}]),
        connection_id="conn-demo", principal=_Principal(), db=db))
    assert len(db.rows) == 1
    assert getattr(db.rows[0], "action", "") == "entra.ca_simulate"


def test_save_then_list_then_rerun_is_deterministic():
    enforced = [p for p in _policies() if p["is_enforced"]][0]
    first = _simulate([{"kind": "disable", "policy_id": enforced["id"]}],
                      save=True, label="regression")
    saved_id = first["saved_id"]
    assert saved_id

    listing = _run(entra_api.ca_simulations(connection_id="conn-demo", principal=_Principal()))
    assert any(s["id"] == saved_id for s in listing["simulations"])
    assert all("counts" in s for s in listing["simulations"]), "the list must summarise impact"

    again = _run(entra_api.ca_simulation_rerun(
        simulation_id=saved_id, connection_id="conn-demo", principal=_Principal()))
    assert again["result"]["counts"] == first["result"]["counts"]


def test_rerunning_a_simulation_for_a_deleted_policy_conflicts_rather_than_lying():
    enforced = [p for p in _policies() if p["is_enforced"]][0]
    saved_id = _simulate([{"kind": "disable", "policy_id": enforced["id"]}],
                         save=True, label="stale")["saved_id"]

    # The policy is removed from the tenant between the save and the re-run.
    payload = cache.read_domain(demo.DEMO_TENANT, "ca")
    payload["data"]["policies"] = [p for p in payload["data"]["policies"]
                                   if p["id"] != enforced["id"]]
    cache.write_domain(demo.DEMO_TENANT, "ca", payload)
    snapshot_mod._analysis_memo.clear()  # noqa: SLF001
    cache.clear_memo()

    with pytest.raises(HTTPException) as exc:
        _run(entra_api.ca_simulation_rerun(
            simulation_id=saved_id, connection_id="conn-demo", principal=_Principal()))
    assert exc.value.status_code == 409
    assert enforced["id"] in str(exc.value.detail)


def test_unknown_simulation_id_is_a_404():
    with pytest.raises(HTTPException) as exc:
        _run(entra_api.ca_simulation_rerun(
            simulation_id="ghost", connection_id="conn-demo", principal=_Principal()))
    assert exc.value.status_code == 404


# ------------------------------------------------------------ privileged / apps reads
def test_privileged_overview_explains_an_unavailable_azure_join():
    body = _run(entra_api.privileged_overview(connection_id="conn-demo", principal=_Principal()))
    link = body["azure_link"]
    assert link["available"] or link["reason"], "an absent join must say why"
    assert body["counts"]["pim_policies"] > 0


def test_pim_grid_scores_every_privileged_role():
    body = _run(entra_api.privileged_pim_policies(connection_id="conn-demo", principal=_Principal()))
    assert body["policies"]
    assert all(0 <= p["score"] <= 100 for p in body["policies"])
    assert body["policies"] == sorted(body["policies"], key=lambda p: (p["score"], p["role_name"]))


def test_application_inventory_is_risk_ranked_and_explains_the_score():
    body = _run(entra_api.apps_inventory(risk_min=0, limit=200,
                                         connection_id="conn-demo", principal=_Principal()))
    scores = [a["risk_score"] for a in body["apps"]]
    assert scores == sorted(scores, reverse=True)
    assert sum(c["weight"] for c in body["risk_components"]) == 100

    detail = _run(entra_api.app_360(
        object_id=body["apps"][0]["object_id"], connection_id="conn-demo", principal=_Principal()))
    assert detail["risk"]["components"]
    assert "requested_not_granted" in detail


def test_the_focus_picker_is_searchable_and_declares_what_it_is_hiding():
    """The picker used to be a plain dropdown capped at a fixed number. On a 20,000-seat
    tenant that put most of the directory out of reach with nothing on screen saying so, so
    blast radius could not be pointed at the people it exists to analyze."""
    everything = _run(entra_api.graph_targets(connection_id="conn-demo", principal=_Principal()))
    assert everything["principal_total"] >= len(everything["principals"])
    assert len(everything["principals"]) <= entra_api._PICK_LIMIT  # noqa: SLF001

    target = everything["principals"][0]["label"]
    assert target, "the demo tenant must have a named principal to search for"
    hit = _run(entra_api.graph_targets(
        connection_id="conn-demo", q=target[:4], principal=_Principal()))
    assert hit["principals"], "a prefix of a real label must match"
    assert all(target[:4].lower() in p["label"].lower() or target[:4].lower() in p["id"].lower()
               for p in hit["principals"])

    miss = _run(entra_api.graph_targets(
        connection_id="conn-demo", q="zzz-no-such-principal", principal=_Principal()))
    assert miss["principals"] == []
    assert miss["principal_total"] == 0


# ------------------------------------------------------------------ activations (P1-P6)
def test_activation_sessions_span_both_planes_and_declare_their_fidelity():
    body = _run(entra_api.privileged_activations(
        connection_id="conn-demo", days=0, principal=_Principal()))
    sessions = body["sessions"]
    assert sessions, "the demo tenant must ship activation sessions"
    assert {s["plane"] for s in sessions} == {"entra", "azure"}
    # A source that cannot record a justification must be reported as unknown, never as an
    # operator failing to give one.
    unknown = [s for s in sessions if not s["detail_known"]]
    assert unknown and all(s["justification_quality"] == "unknown" for s in unknown)
    blank = [s for s in sessions if s["detail_known"] and not s["justification"]]
    assert all(s["justification_quality"] == "missing" for s in blank)


def test_a_failed_request_is_listed_but_not_counted_as_granted():
    body = _run(entra_api.privileged_activations(
        connection_id="conn-demo", days=0, principal=_Principal()))
    failed = [s for s in body["sessions"] if s["status"] == "Failed"]
    assert failed, "the demo tenant must include an attempt that never provisioned"
    assert all(not s["granted"] for s in failed)


def test_out_of_hours_is_computed_in_the_callers_timezone():
    """The same activation is inside or outside the working day depending on the offset, so
    the server must not assume UTC is the tenant's day."""
    utc = _run(entra_api.privileged_activations(
        connection_id="conn-demo", days=0, utc_offset_hours=0, principal=_Principal()))
    shifted = _run(entra_api.privileged_activations(
        connection_id="conn-demo", days=0, utc_offset_hours=9, principal=_Principal()))
    assert utc["facets"]["out_of_hours"] != shifted["facets"]["out_of_hours"]


def test_filters_narrow_the_session_list():
    everything = _run(entra_api.privileged_activations(
        connection_id="conn-demo", days=0, principal=_Principal()))
    azure = _run(entra_api.privileged_activations(
        connection_id="conn-demo", days=0, plane="azure", principal=_Principal()))
    assert 0 < azure["total"] < everything["total"]
    assert all(s["plane"] == "azure" for s in azure["sessions"])

    named = _run(entra_api.privileged_activations(
        connection_id="conn-demo", days=0, q="INC-4390", principal=_Principal()))
    assert named["total"] == 1


def test_an_unknown_session_is_a_404_not_an_empty_timeline():
    with pytest.raises(HTTPException) as excinfo:
        _run(entra_api.privileged_activation_actions(
            session_id="entra:req:nope", connection_id="conn-demo", principal=_Principal()))
    assert excinfo.value.status_code == 404


def test_the_evidence_pack_records_where_each_claim_came_from():
    """An auditor needs the provenance, not just the answer."""
    body = _run(entra_api.privileged_activations_export(
        connection_id="conn-demo", days=0, principal=_Principal()))
    assert body["sessions"]
    assert body["generated_at"]
    assert "entra_request" in body["provenance"]
    assert "roleAssignmentScheduleRequests" in body["provenance"]["azure_request"]


# --------------------------------------------------------------- scanner results
def _scanner_rows():
    return _run(entra_api.scanners_list(
        connection_id="conn-demo", principal=_Principal()))["scanners"]


def _run_scanner(scanner_id):
    body = entra_api.ScannerRunBody(scanner_ids=[scanner_id], force=True, notify=False)
    return _run(entra_api.scanners_run(
        body=body, connection_id="conn-demo", principal=_Principal(), db=_FakeDB()))["ran"][0]


def _view(scanner_id, **kw):
    return _run(entra_api.scanner_findings(
        scanner_id=scanner_id, connection_id="conn-demo", principal=_Principal(), **kw))


def _productive_scanner():
    """A scanner the demo tenant actually produces findings for."""
    for row in _scanner_rows():
        if not row["blocked"] and _view(row["id"])["total"] > 0:
            return row["id"]
    raise AssertionError("the demo tenant must give at least one scanner something to find")


def test_a_scanner_that_has_already_run_still_shows_its_findings():
    """The reported defect: re-running a scanner appeared to find nothing.

    The second run legitimately has an empty delta, and the screen used to render only the
    delta — so a scanner holding hundreds of open findings displayed as silence.
    """
    scanner_id = _productive_scanner()
    first = _run_scanner(scanner_id)
    second = _run_scanner(scanner_id)

    assert second["counts"]["new"] == 0, "nothing changed between the two runs"
    assert second["counts"]["total"] == first["counts"]["total"] > 0

    view = _view(scanner_id)
    assert view["total"] == second["counts"]["total"]
    assert view["findings"], "the operator must still be shown what the scanner reports"


def test_viewing_findings_does_not_record_a_run():
    """Looking must not consume the delta.

    If opening the screen recorded a run, everything would be marked as already-seen and
    the next real run would report 'nothing changed' precisely because someone looked.
    """
    scanner_id = _productive_scanner()
    _run_scanner(scanner_id)
    before = next(r for r in _scanner_rows() if r["id"] == scanner_id)["last_run"]

    _view(scanner_id)
    _view(scanner_id)

    after = next(r for r in _scanner_rows() if r["id"] == scanner_id)["last_run"]
    assert after == before


def test_findings_are_returned_worst_first():
    ranks = {s: i for i, s in enumerate(scanners_mod.SEVERITY_ORDER)}
    view = _view(_productive_scanner())
    seen = [ranks.get(f["severity"], 0) for f in view["findings"]]
    assert seen == sorted(seen, reverse=True)


def test_a_scanner_that_never_ran_calls_nothing_new():
    """Without a baseline there is no honest way to call anything new."""
    view = _view(_productive_scanner())
    assert view["last_run"] == ""
    assert not any(f["is_new"] for f in view["findings"])


def test_findings_absent_from_the_last_run_are_flagged_new():
    """The flag has to be able to turn on, not merely stay off.

    Rewriting the recorded baseline as if the previous run had seen nothing is the only way
    to move it without a second snapshot.
    """
    scanner_id = _productive_scanner()
    _run_scanner(scanner_id)
    runs = scanners_mod.read_runs(demo.DEMO_TENANT)
    runs[scanner_id]["fingerprints"] = []
    scanners_mod.write_runs(demo.DEMO_TENANT, runs)

    view = _view(scanner_id)
    assert view["findings"]
    assert all(f["is_new"] for f in view["findings"])


def test_the_severity_breakdown_matches_the_findings_returned():
    view = _view(_productive_scanner(), limit=1000)
    assert sum(view["by_severity"].values()) == view["total"]
    assert view["truncated"] is (view["total"] > len(view["findings"]))


def test_an_unknown_scanner_is_a_404_rather_than_an_empty_result():
    """Silence and 'no such scanner' must not look the same."""
    with pytest.raises(HTTPException) as excinfo:
        _view("entra.does_not_exist")
    assert excinfo.value.status_code == 404


# ------------------------------------------------------- live permission re-check
class _FakeGraphClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_permissions(granted, domains):
    async def _build(_client, *, live=False):
        assert live is True, "the re-check must probe, not trust the cached claim"
        return {"token_ok": True, "token_error": "", "granted": sorted(granted),
                "granted_known": True, "claim_error": "", "domains": domains,
                "tiers": [], "probed": True}
    return _build


def _recheck(monkeypatch, granted, domains):
    monkeypatch.setattr(entra_api, "GraphClient", lambda _conn: _FakeGraphClient())
    monkeypatch.setattr(entra_api.permissions_probe, "build", _fake_permissions(granted, domains))
    return _run(entra_api.permissions_recheck(
        connection_id="conn-demo", principal=_Principal()))


def test_the_recheck_names_the_scope_granted_since_the_last_refresh(monkeypatch):
    """The whole point: 'I already granted that' has to be checkable without a collection."""
    before = _run(entra_api.setup_checklist(
        connection_id="conn-demo", principal=_Principal()))["granted"]
    # A scope the demo tenant genuinely does not hold, so "gained" cannot be vacuous.
    extra = next(s for s in entra_api.permissions_probe.ALL_SCOPES if s not in before)
    body = _recheck(monkeypatch, [*before, extra],
                    {"risk": {"ok": True, "missing": [], "reason": ""}})
    assert body["gained"] == [extra]
    assert body["revoked"] == []
    assert body["needs_refresh"] is True, "consent alone does not backfill the data"


def test_the_recheck_reports_a_revoked_scope(monkeypatch):
    before = _run(entra_api.setup_checklist(
        connection_id="conn-demo", principal=_Principal()))["granted"]
    assert before, "the demo tenant must start with some permissions"
    body = _recheck(monkeypatch, before[1:], {})
    assert body["revoked"] == [before[0]]


def test_the_recheck_separates_a_licence_gap_from_a_missing_scope(monkeypatch):
    """Two different problems with two different fixes; one of them consent cannot solve."""
    body = _recheck(monkeypatch, ["RoleManagement.Read.Directory"], {
        "risk": {"ok": False, "missing": ["IdentityRiskyUser.Read.All"], "reason": "Missing"},
        "pim": {"ok": True, "missing": [], "reason": "", "licence_blocked": True,
                "licence_reason": "Requires Entra ID P2."},
    })
    assert body["blind_domains"] == ["risk"]
    assert body["licence_blocked"] == ["pim"]


def test_the_recheck_persists_so_the_setup_screen_agrees(monkeypatch):
    _recheck(monkeypatch, ["RoleManagement.Read.Directory"], {})
    setup = _run(entra_api.setup_checklist(connection_id="conn-demo", principal=_Principal()))
    assert setup["granted"] == ["RoleManagement.Read.Directory"]


def test_a_token_failure_is_reported_rather_than_written_as_zero_permissions(monkeypatch):
    """Recording 'no permissions' because auth broke would blank the whole product."""
    async def _broken(_client, *, live=False):
        return {"token_ok": False, "token_error": "AADSTS700016", "granted": [],
                "granted_known": False, "claim_error": "", "domains": {}, "tiers": [],
                "probed": False}
    monkeypatch.setattr(entra_api, "GraphClient", lambda _conn: _FakeGraphClient())
    monkeypatch.setattr(entra_api.permissions_probe, "build", _broken)

    before = _run(entra_api.setup_checklist(
        connection_id="conn-demo", principal=_Principal()))["granted"]
    with pytest.raises(HTTPException) as excinfo:
        _run(entra_api.permissions_recheck(connection_id="conn-demo", principal=_Principal()))
    assert excinfo.value.status_code == 502

    after = _run(entra_api.setup_checklist(
        connection_id="conn-demo", principal=_Principal()))["granted"]
    assert after == before, "a failed probe must not overwrite known permissions"
