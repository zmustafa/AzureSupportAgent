"""Derived-result caching and honest progress reporting.

Two things this file protects, both learned from a realistic 5,506-grant tenant where the
escalation graph takes **40 seconds** to build:

1. **A derived cache must not invalidate itself.** The version stamp is compared against a
   counter that every cache write used to bump — including the write that stored the stamp — so
   the cache never hit once, and the 40 seconds was re-paid on every single request.

2. **An estimate is a claim.** A bar that says "8 seconds remaining" for four minutes teaches
   people the number is decorative, and after that no progress indicator in the product is
   believed. So an estimate comes from this tenant's own measured runs or it is not given.
"""
from __future__ import annotations

import time

import pytest

from app.iam import cache, escalation, progress, rightsize, schema

TENANT = "t1"


@pytest.fixture()
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_DATA", tmp_path)
    monkeypatch.setattr(cache, "_INDEX", tmp_path / "iam_cache.json")
    monkeypatch.setattr(cache, "_BLOBS", tmp_path / "iam")
    monkeypatch.setattr(cache, "_migrated", True)
    escalation._GRAPH_CACHE.clear()
    rightsize._ANALYSIS_CACHE.clear()
    return tmp_path


def _row(**kw):
    base = dict(
        surface=schema.SURFACE_AZURE_RBAC, effect=schema.EFFECT_ALLOW,
        assignmentState=schema.STATE_ACTIVE, accessPath=schema.PATH_DIRECT,
        principalId="alice", effectivePrincipalId="alice", effectivePrincipalName="Alice",
        effectivePrincipalType="User", roleName="Owner", roleIsPrivileged=True,
        scope="/subscriptions/s1", scopeType=schema.SCOPE_SUBSCRIPTION, assignmentId="a1",
        principalExists=schema.EXISTS_TRUE,
    )
    base.update(kw)
    return schema.make_row(**base)


# =========================================================================== the version
def test_writing_a_derived_artefact_does_not_change_the_source_version(isolated_cache):
    """The bug that made the cache a no-op.

    `graph_for_tenant` stamps the graph with `cache_version()` and compares on the next read.
    Persisting it bumped that counter, so the stamp was stale the instant it was written and the
    40-second build ran again on every request — a cache that had never once hit."""
    before = cache.cache_version()
    cache.write_escalation(TENANT, {"nodes": [], "edges": [], "paths": []},
                           cache_version=before, min_confidence="low", duration_seconds=1.0)
    assert cache.cache_version() == before

    stored = cache.read_escalation(TENANT)
    assert stored["cache_version"] == cache.cache_version(), "the stamp must still be current"


def test_derived_and_state_writes_do_not_invalidate_the_master_rows_memo(isolated_cache):
    """A scanner sweep writes nine baselines. Each one used to throw away the composed rows,
    forcing a full recompose of a 5,506-row snapshot that had not changed."""
    before = cache.cache_version()
    cache.write_state(TENANT, "scanner_runs", {"a": 1})
    cache.write_rightsizing(TENANT, {"measured": True, "recommendations": []})
    cache.write_drift(TENANT, {"available": True, "changes": []})
    assert cache.cache_version() == before


def test_collecting_rows_DOES_change_the_source_version(isolated_cache):
    """The other half. If a real collection did not bump the version, every derived cache would
    serve results for rows that no longer exist — silently wrong, which is worse than slow."""
    before = cache.cache_version()
    cache.write_scope(TENANT, "/subscriptions/s1", rows=[_row()],
                      meta={"scopeType": schema.SCOPE_SUBSCRIPTION, "displayName": "sub-1"})
    assert cache.cache_version() != before

    mid = cache.cache_version()
    cache.write_directory(TENANT, meta={"status": schema.STATUS_SUCCEEDED},
                          rows=[], role_defs=[], principals=[], groups={})
    assert cache.cache_version() != mid


# =========================================================================== the graph cache
def _build_count(monkeypatch) -> list[int]:
    calls = [0]
    real = escalation.detect

    def _counting(*a, **k):
        calls[0] += 1
        return real(*a, **k)

    monkeypatch.setattr(escalation, "detect", _counting)
    return calls


def test_the_graph_is_built_once_and_then_served_from_memory(isolated_cache, monkeypatch):
    calls = _build_count(monkeypatch)
    rows = [_row()]
    for _ in range(3):
        escalation.graph_for_tenant(TENANT, rows, {})
    assert calls[0] == 1


def test_the_graph_survives_a_process_restart(isolated_cache, monkeypatch):
    """The in-process memo dies with the process. Without the persisted copy, the first request
    after every deploy or restart pays the full build — 40 seconds here, over a minute on a
    tenant twice this size."""
    calls = _build_count(monkeypatch)
    rows = [_row()]
    escalation.graph_for_tenant(TENANT, rows, {})
    assert calls[0] == 1

    escalation._GRAPH_CACHE.clear()          # what a restart does
    escalation.graph_for_tenant(TENANT, rows, {})
    assert calls[0] == 1, "the graph must come back from disk, not be rebuilt"


def test_switching_connections_and_back_does_not_rebuild(isolated_cache, monkeypatch):
    """The memo used to hold ONE entry and was cleared before every write, so a user with two
    connections re-paid the full build on every switch, in both directions."""
    calls = _build_count(monkeypatch)
    rows = [_row()]
    escalation.graph_for_tenant("tenant-a", rows, {})
    escalation.graph_for_tenant("tenant-b", rows, {})
    escalation.graph_for_tenant("tenant-a", rows, {})
    escalation.graph_for_tenant("tenant-b", rows, {})
    assert calls[0] == 2, "one build per tenant, not one per switch"


def test_both_tenants_stay_in_memory_across_a_switch(isolated_cache):
    """Distinct from the test above, which the DISK cache alone would satisfy.

    Falling back to disk on every switch means re-reading and re-parsing an 8.5 MB graph each
    time. Correct, but the memo exists so that a user flipping between two connections works
    entirely from memory — so it has to hold more than one."""
    rows = [_row()]
    escalation.graph_for_tenant("tenant-a", rows, {})
    escalation.graph_for_tenant("tenant-b", rows, {})
    cached = {key[0] for key in escalation._GRAPH_CACHE}
    assert cached == {"tenant-a", "tenant-b"}


def test_the_disk_copy_is_what_makes_a_switch_cheap_even_when_the_memo_evicts(isolated_cache, monkeypatch):
    """Belt and braces, stated explicitly: with the memo full of other tenants, the graph still
    must not be rebuilt."""
    calls = _build_count(monkeypatch)
    rows = [_row()]
    escalation.graph_for_tenant("tenant-a", rows, {})
    for i in range(escalation.MAX_MEMO_ENTRIES + 2):
        escalation.graph_for_tenant(f"filler-{i}", rows, {})
    assert "tenant-a" not in {key[0] for key in escalation._GRAPH_CACHE}, "evicted, as designed"

    before = calls[0]
    escalation.graph_for_tenant("tenant-a", rows, {})
    assert calls[0] == before, "an evicted tenant comes back from disk, not from a rebuild"


def test_new_rows_invalidate_the_cached_graph(isolated_cache, monkeypatch):
    """Freshness is the source version, not a TTL: a stale graph is silently WRONG, and no
    expiry time is short enough to make that acceptable."""
    calls = _build_count(monkeypatch)
    rows = [_row()]
    escalation.graph_for_tenant(TENANT, rows, {})
    cache.write_scope(TENANT, "/subscriptions/s1", rows=[_row()],
                      meta={"scopeType": schema.SCOPE_SUBSCRIPTION})
    escalation.graph_for_tenant(TENANT, rows, {})
    assert calls[0] == 2


def test_force_rebuilds_even_when_the_cache_is_current(isolated_cache, monkeypatch):
    calls = _build_count(monkeypatch)
    rows = [_row()]
    escalation.graph_for_tenant(TENANT, rows, {})
    escalation.graph_for_tenant(TENANT, rows, {}, force=True)
    assert calls[0] == 2


def test_changing_the_confidence_floor_does_not_rebuild(isolated_cache, monkeypatch):
    """The complaint that prompted this: moving the confidence selector re-ran the whole engine.

    Each level used to be its own cache key, so low/medium/high cost 31s, 30s and 19s
    separately, and a refresh left two of the three cold. The floor only ever excludes
    primitives, so one build at `low` answers all three."""
    calls = _build_count(monkeypatch)
    rows = [_row()]
    escalation.graph_for_tenant(TENANT, rows, {}, min_confidence=escalation.CONF_LOW)
    escalation.graph_for_tenant(TENANT, rows, {}, min_confidence=escalation.CONF_MEDIUM)
    escalation.graph_for_tenant(TENANT, rows, {}, min_confidence=escalation.CONF_HIGH)
    escalation.graph_for_tenant(TENANT, rows, {}, min_confidence=escalation.CONF_LOW)
    assert calls[0] == 1


def test_changing_the_confidence_floor_is_served_from_memory(isolated_cache, monkeypatch):
    """Not merely "does not rebuild" — does not even go to disk.

    The disk copy is confidence-independent, so it absorbs a wrong memo key: the rebuild count
    alone stays green while the in-process fast path is silently lost. Pin the memo directly by
    counting disk reads — after the first build, moving the selector must touch neither."""
    calls = _build_count(monkeypatch)
    reads = [0]
    real_read = cache.read_escalation

    def counting_read(tenant_id):
        reads[0] += 1
        return real_read(tenant_id)

    monkeypatch.setattr(cache, "read_escalation", counting_read)

    rows = [_row()]
    escalation.graph_for_tenant(TENANT, rows, {}, min_confidence=escalation.CONF_LOW)
    before = reads[0]
    escalation.graph_for_tenant(TENANT, rows, {}, min_confidence=escalation.CONF_MEDIUM)
    escalation.graph_for_tenant(TENANT, rows, {}, min_confidence=escalation.CONF_HIGH)
    assert calls[0] == 1
    assert reads[0] == before, "a change of floor must be answered from the in-process memo"


def test_a_narrowed_graph_never_contains_a_dangling_edge(isolated_cache):
    """The single most important invariant in this module: Cytoscape rejects the whole batch
    when one edge points at an absent node, which blanks the canvas. Narrowing drops nodes, so
    it is exactly where a dangling edge would be introduced."""
    graph = {
        "nodes": [{"id": "a", "kind": "principal"}, {"id": "b", "kind": "scope"},
                  {"id": "c", "kind": "scope"}],
        "edges": [
            {"id": "e1", "source": "a", "target": "b", "data": {"confidence": "high"}},
            {"id": "e2", "source": "a", "target": "c", "data": {"confidence": "low"}},
        ],
        "paths": [], "limitations": [],
    }
    out = escalation.filter_by_confidence(graph, escalation.CONF_HIGH)
    present = {n["id"] for n in out["nodes"]}
    assert [e["id"] for e in out["edges"]] == ["e1"]
    assert all(e["source"] in present and e["target"] in present for e in out["edges"])
    assert "c" not in present, "a node left isolated by narrowing is dropped, not kept"


def test_narrowing_keeps_only_paths_that_clear_the_floor(isolated_cache):
    graph = {
        "nodes": [], "edges": [],
        "paths": [{"from": "p1", "min_confidence": "low"}, {"from": "p2", "min_confidence": "high"}],
        "limitations": [],
    }
    out = escalation.filter_by_confidence(graph, escalation.CONF_HIGH)
    assert [p["from"] for p in out["paths"]] == ["p2"]


def test_narrowing_to_low_is_the_identity(isolated_cache):
    graph = {"nodes": [{"id": "a"}], "edges": [], "paths": [], "limitations": ["x"]}
    assert escalation.filter_by_confidence(graph, escalation.CONF_LOW) is graph


def test_narrowing_preserves_the_limitations(isolated_cache):
    """An escalation map that could not see managed identities must say so at EVERY confidence
    level — dropping the caveat while narrowing would turn a filtered view into a false
    all-clear."""
    graph = {
        "nodes": [], "edges": [], "paths": [],
        "limitations": ["Managed identities were not collected."],
    }
    out = escalation.filter_by_confidence(graph, escalation.CONF_HIGH)
    assert out["limitations"] == ["Managed identities were not collected."]


def test_the_memo_is_bounded(isolated_cache):
    rows = [_row()]
    for i in range(escalation.MAX_MEMO_ENTRIES + 4):
        escalation.graph_for_tenant(f"tenant-{i}", rows, {})
    assert len(escalation._GRAPH_CACHE) <= escalation.MAX_MEMO_ENTRIES


def test_a_cache_write_failure_never_fails_the_request(isolated_cache, monkeypatch):
    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(cache, "write_escalation", _boom)
    graph = escalation.graph_for_tenant(TENANT, [_row()], {})
    assert "nodes" in graph, "the answer is still returned; only the caching failed"


def test_build_duration_is_none_until_measured(isolated_cache):
    assert escalation.build_duration(TENANT) is None
    escalation.graph_for_tenant(TENANT, [_row()], {})
    assert escalation.build_duration(TENANT) is not None


# =========================================================================== estimates
def test_no_estimate_is_given_before_there_is_evidence(isolated_cache):
    """A default constant dressed up as an ETA is worse than no ETA."""
    seconds, basis = progress.estimate(TENANT, "all")
    assert seconds is None
    assert "no previous run" in basis


def test_an_estimate_states_where_it_came_from(isolated_cache):
    progress.record(TENANT, "all", 100.0)
    progress.record(TENANT, "all", 120.0)
    seconds, basis = progress.estimate(TENANT, "all")
    assert seconds == 110.0
    assert "last 2 runs" in basis


def test_the_median_resists_one_pathological_run(isolated_cache):
    """One throttled 429-riddled refresh must not move the estimate for the next ten."""
    for value in (100.0, 110.0, 120.0, 3000.0):
        progress.record(TENANT, "all", value)
    seconds, _ = progress.estimate(TENANT, "all")
    assert seconds == 115.0


def test_only_a_bounded_number_of_samples_is_kept(isolated_cache):
    for i in range(progress.MAX_SAMPLES + 6):
        progress.record(TENANT, "all", float(i + 1))
    raw = cache.read_state(TENANT, progress.STATE_KEY)
    assert len(raw["all"]) == progress.MAX_SAMPLES


def test_an_overdue_run_reports_overdue_not_zero(isolated_cache):
    """"0 seconds remaining" held for a minute is the fastest way to make the whole indicator
    untrustworthy. Overdue is a different fact and is said differently."""
    progress.record(TENANT, "all", 60.0)
    left, basis = progress.remaining(TENANT, "all", elapsed=500.0)
    assert left is None
    assert "longer than usual" in basis


def test_remaining_counts_down(isolated_cache):
    progress.record(TENANT, "all", 100.0)
    assert progress.remaining(TENANT, "all", elapsed=10.0)[0] == 90.0
    assert progress.remaining(TENANT, "all", elapsed=90.0)[0] == 10.0


def test_estimates_are_per_kind(isolated_cache):
    """A single-scope refresh and a full one are different jobs; sharing an estimate would make
    both wrong."""
    progress.record(TENANT, "scope", 5.0)
    progress.record(TENANT, "all", 300.0)
    assert progress.estimate(TENANT, "scope")[0] == 5.0
    assert progress.estimate(TENANT, "all")[0] == 300.0


def test_estimates_are_per_tenant(isolated_cache):
    progress.record("small-tenant", "all", 10.0)
    assert progress.estimate("big-tenant", "all")[0] is None


def test_a_zero_or_negative_duration_is_not_recorded(isolated_cache):
    progress.record(TENANT, "all", 0.0)
    progress.record(TENANT, "all", -5.0)
    assert progress.estimate(TENANT, "all")[0] is None


def test_the_public_block_always_carries_elapsed_even_with_no_estimate(isolated_cache):
    block = progress.public(TENANT, "all", elapsed=42.0)
    assert block["elapsed_seconds"] == 42.0
    assert block["elapsed_label"] == "42s"
    assert block["eta_seconds"] is None
    assert block["eta_label"] == "—", "an em dash, never a fabricated number"
    assert block["eta_basis"]


def test_labels_are_human_readable(isolated_cache):
    assert progress.format_seconds(None) == "—"
    assert progress.format_seconds(9.4) == "9s"
    assert progress.format_seconds(60) == "1:00"
    assert progress.format_seconds(605) == "10:05"


# =========================================================================== job wiring
@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_every_progress_line_carries_the_clock(isolated_cache):
    """A progress log without elapsed time reads identically at five seconds and five minutes,
    which is when people reload the page and fire off a second refresh."""
    from app.iam import job

    key = job.job_key(TENANT, "/subscriptions/s1")
    job._jobs[key] = {
        "id": "x", "key": key, "tenant_id": TENANT, "scope": "/subscriptions/s1",
        "mode": "scope", "status": "running", "started_at": "", "started_monotonic": time.monotonic(),
        "finished_at": None, "finished_monotonic": None, "progress": [], "error": "",
    }
    await job._append(key, "info", "Collecting…")
    entry = job._jobs[key]["progress"][0]
    assert "elapsed_seconds" in entry and "eta_label" in entry and entry["eta_basis"]


@pytest.mark.anyio
async def test_a_refresh_rebuilds_the_cache_it_invalidated(isolated_cache, monkeypatch):
    """A refresh changes the rows, which correctly invalidates the escalation graph. Left alone,
    the next person to open Findings pays the 30-to-60-second rebuild as an unexplained spinner
    — the cost simply moves from a screen that reports progress to one that does not."""
    from app.iam import job

    messages: list[str] = []

    async def _progress(_level: str, message: str) -> None:
        messages.append(message)

    calls = _build_count(monkeypatch)
    monkeypatch.setattr(job, "_warm_derived", job._warm_derived)  # keep the real one
    await job._warm_derived(TENANT, _progress)

    assert calls[0] == 1, "the graph is built inside the job"
    assert any("Rebuilding the escalation graph" in m for m in messages)
    assert any("Escalation graph ready" in m for m in messages)


@pytest.mark.anyio
async def test_a_failed_warm_up_does_not_fail_the_refresh(isolated_cache, monkeypatch):
    """The collection succeeded. Failing it because an optimisation failed is a poor trade."""
    from app.iam import escalation as esc_mod
    from app.iam import job

    messages: list[tuple[str, str]] = []

    async def _progress(level: str, message: str) -> None:
        messages.append((level, message))

    def _boom(*_a, **_k):
        raise RuntimeError("out of memory")

    monkeypatch.setattr(esc_mod, "graph_for_tenant", _boom)
    await job._warm_derived(TENANT, _progress)          # must not raise
    assert any(level == "warning" and "could not be pre-built" in msg for level, msg in messages)


@pytest.mark.anyio
async def test_the_done_event_reports_elapsed_and_no_estimate(isolated_cache):
    """Once a job has finished there is nothing remaining, and "0s left" on a completed run is
    at best noise and at worst reads as though something is still pending."""
    import json as _json

    from app.iam import job

    key = job.job_key(TENANT, "s")
    job._jobs[key] = {
        "id": "x", "key": key, "tenant_id": TENANT, "scope": "s", "mode": "scope",
        "status": "done", "started_at": "", "started_monotonic": time.monotonic() - 7,
        "finished_at": "", "finished_monotonic": time.monotonic(), "progress": [], "error": "",
    }
    events = [e async for e in job.stream(key)]
    done = _json.loads(next(e["data"] for e in events if e["event"] == "done"))
    assert done["eta_seconds"] is None
    assert done["eta_label"] == "—"
    assert done["elapsed_seconds"] >= 6
    assert "completed in" in done["eta_basis"]


@pytest.mark.anyio
async def test_only_a_successful_run_teaches_the_estimator(isolated_cache):
    """A refresh that died after four seconds would otherwise teach the estimator that this
    tenant takes four seconds."""
    from app.iam import job

    key = job.job_key(TENANT, "s")
    for status in ("error", "done"):
        job._jobs[key] = {
            "id": "x", "key": key, "tenant_id": TENANT, "scope": "s", "mode": "all",
            "status": "running", "started_at": "", "started_monotonic": time.monotonic() - 12,
            "finished_at": None, "finished_monotonic": None, "progress": [], "error": "",
        }
        await job._finish(key, status=status)
    samples = cache.read_state(TENANT, progress.STATE_KEY).get("all") or []
    assert len(samples) == 1, "the failed run must not be a sample"


# =========================================================================== the rightsizing cache
def _analyse_count(monkeypatch) -> list[int]:
    calls = [0]
    real = rightsize.analyze

    def _counting(*a, **k):
        calls[0] += 1
        return real(*a, **k)

    monkeypatch.setattr(rightsize, "analyze", _counting)
    return calls


def _seed_rows(monkeypatch):
    """Give analyse_for_tenant something to chew on without a real snapshot."""
    from app.iam import compose, effective
    monkeypatch.setattr(compose, "build_master_rows", lambda t: [_row()])
    monkeypatch.setattr(effective, "build_role_index", lambda defs: {})


def test_rightsizing_is_not_recomputed_on_every_request(isolated_cache, monkeypatch):
    """The single worst number in the IAM benchmark: 7.3 seconds per page load, cold AND warm.

    The refresh path was already writing this analysis to disk; the endpoint ignored it and
    recomputed from scratch every time."""
    _seed_rows(monkeypatch)
    calls = _analyse_count(monkeypatch)
    rightsize.analyze_for_tenant(TENANT)
    rightsize.analyze_for_tenant(TENANT)
    rightsize.analyze_for_tenant(TENANT)
    assert calls[0] == 1


def test_rightsizing_survives_a_process_restart(isolated_cache, monkeypatch):
    """The memo dies with the process; the disk copy is what makes the SECOND visit fast."""
    _seed_rows(monkeypatch)
    calls = _analyse_count(monkeypatch)
    rightsize.analyze_for_tenant(TENANT)
    rightsize._ANALYSIS_CACHE.clear()  # a restart
    rightsize.analyze_for_tenant(TENANT)
    assert calls[0] == 1


def test_repeat_rightsizing_reads_are_served_from_memory(isolated_cache, monkeypatch):
    """Not merely "does not recompute" — does not even re-read and re-parse the blob.

    The disk copy alone keeps the recompute count at one, so it masks a dead memo entirely.
    Counting disk reads is what actually pins the in-process fast path."""
    _seed_rows(monkeypatch)
    reads = [0]
    real_read = cache.read_rightsizing

    def counting_read(tenant_id):
        reads[0] += 1
        return real_read(tenant_id)

    monkeypatch.setattr(cache, "read_rightsizing", counting_read)
    rightsize.analyze_for_tenant(TENANT)
    before = reads[0]
    rightsize.analyze_for_tenant(TENANT)
    rightsize.analyze_for_tenant(TENANT)
    assert reads[0] == before, "a repeat read must not touch the blob at all"


def test_new_rows_invalidate_the_rightsizing_cache(isolated_cache, monkeypatch):
    """Serving an analysis of rows that no longer exist is worse than being slow: the screen
    would recommend revoking access that was already revoked, and miss what has since appeared."""
    _seed_rows(monkeypatch)
    calls = _analyse_count(monkeypatch)
    rightsize.analyze_for_tenant(TENANT)
    cache.write_scope(TENANT, "/subscriptions/s2", rows=[_row()],
                      meta={"scopeType": schema.SCOPE_SUBSCRIPTION, "displayName": "sub-2"})
    rightsize.analyze_for_tenant(TENANT)
    assert calls[0] == 2


def test_forcing_a_rightsizing_rebuild_recomputes(isolated_cache, monkeypatch):
    _seed_rows(monkeypatch)
    calls = _analyse_count(monkeypatch)
    rightsize.analyze_for_tenant(TENANT)
    rightsize.analyze_for_tenant(TENANT, force=True)
    assert calls[0] == 2


def test_the_breadth_memo_is_shared_across_one_analysis(isolated_cache, monkeypatch):
    """`cover()` built a memo and then never consulted it, sorting on an UNCACHED breadth call.

    Breadth depends only on (role, universe) and the universe is fixed for a run, so the same
    roles were re-measured for every one of 2,185 recommendations. That single line was 5 of the
    7.3 seconds."""
    from app.iam import effective, usage
    seen = []
    real = usage.breadth

    def _counting(role, universe):
        seen.append(role.role_name if role else None)
        return real(role, universe)

    monkeypatch.setattr(usage, "breadth", _counting)
    reader = effective.RoleActionSet(
        role_definition_id="r1", role_name="Reader", actions=("*/read",),
        not_actions=(), data_actions=(), not_data_actions=(), is_custom=False,
    )
    catalogue = [reader]
    universe = ("microsoft.compute/x/read", "microsoft.compute/x/write")
    measure = rightsize._breadth_memo(universe)
    for _ in range(10):
        rightsize.cover({"microsoft.compute/x/read"}, catalogue, universe, measure=measure)
    assert len(seen) == 1, f"breadth recomputed {len(seen)} times for one role and one universe"


# =========================================================================== memo identity
def test_two_cache_roots_do_not_share_a_memo(tmp_path, monkeypatch):
    """A memo keyed on the version alone is not merely stale — it is WRONG.

    `_write_seq` is a process-global counter starting at zero that only counts writes made
    through this module, so a second cache root reads as version 0 too. Keyed on the version
    alone, the same tenant id in two different stores collides and one store's findings are
    served for the other."""
    from app.iam import findings

    findings._EVAL_CACHE.clear()
    seen = []

    def _fake_eval(ctx):
        seen.append(cache._INDEX)
        return []

    monkeypatch.setattr("app.iam.signals.evaluate_all", _fake_eval)

    first, second = tmp_path / "a", tmp_path / "b"
    versions = []
    for root in (first, second):
        monkeypatch.setattr(cache, "_DATA", root)
        monkeypatch.setattr(cache, "_INDEX", root / "iam_cache.json")
        monkeypatch.setattr(cache, "_BLOBS", root / "iam")
        monkeypatch.setattr(cache, "_migrated", True)
        versions.append(cache.cache_version())
        findings.evaluate("t1")

    assert versions[0] == versions[1], "both roots really are at the same version"
    assert len(seen) == 2, "the second cache root must be evaluated, not served from the first"


def test_the_fingerprint_separates_stores_at_the_same_version(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_INDEX", tmp_path / "a.json")
    a = cache.cache_fingerprint()
    monkeypatch.setattr(cache, "_INDEX", tmp_path / "b.json")
    b = cache.cache_fingerprint()
    assert a != b
    assert a[1] == b[1], "the version component is identical; the store is what differs"
