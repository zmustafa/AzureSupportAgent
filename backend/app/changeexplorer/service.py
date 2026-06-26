"""ChangeAnalysisService — orchestrate the read-only change-analysis pipeline and return a
tab-ready ChangeAnalysisRun. The LLM never queries Azure; collectors are deterministic and any
AI only narrates the already-normalized run summary.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from app.changeexplorer import ai_enrich
from app.changeexplorer import classify as classify_mod
from app.changeexplorer import collectors, deps, demo, explain as explain_mod
from app.changeexplorer import insights as insights_mod
from app.changeexplorer import normalize as normalize_mod
from app.changeexplorer import risk as risk_mod
from app.changeexplorer import scale
from app.changeexplorer.models import ChangeAnalysisRun, label_for_score, new_id, now_iso

log = logging.getLogger("app.changeexplorer.service")


def _is_production(workload: dict[str, Any]) -> bool:
    name = (workload.get("name", "") or "").lower()
    if any(t in name for t in ("prod", "production")):
        return True
    for n in workload.get("nodes", []):
        env = str(((n.get("tags") or {}).get("environment", "")) or "").lower()
        if env in ("prod", "production"):
            return True
    return True if "prod" in name else False


def _detail_paths(event: dict[str, Any]) -> list[str]:
    return [d.get("propertyPath", "") for d in (event.get("details") or []) if d.get("propertyPath")]


def _score_event(event: dict[str, Any], *, production: bool) -> None:
    """(Re)compute dependency role, risk and the deterministic explanation for an event in place,
    from its current category."""
    role = deps.role_for(event.get("resourceType", ""), event.get("resourceName", ""))
    event["dependencyRole"] = role
    event["blastRadius"] = deps.blast_radius(role)
    shared = role == deps.ROLE_SHARED
    scored = risk_mod.score(event, production=production, shared=shared, dependency_role=role)
    event["riskScore"] = scored["score"]
    event["riskLabel"] = scored["label"]
    event["riskFactors"] = scored["factors"]
    ex = explain_mod.explain(event)
    event["plainEnglishSummary"] = ex["plainEnglishSummary"]
    event["possibleImpact"] = ex["possibleImpact"]
    event["whyRisk"] = ex["whyRisk"]
    event["confidence"] = ex["confidence"]
    # Security flags (C1) + rollback hint (C3) — deterministic, recomputed when category changes.
    from app.changeexplorer import security as security_mod

    flags = security_mod.flag_event(event)
    event["securityFlags"] = flags
    event["securitySeverity"] = security_mod.highest_flag_severity(flags)
    event["rollbackHint"] = security_mod.rollback_hint(event)


def _enrich(event: dict[str, Any], *, production: bool) -> dict[str, Any]:
    """Classify (type + operation + property paths) -> dependency role -> risk -> explain."""
    cat = classify_mod.classify(
        event.get("resourceType", ""), event.get("operation", ""), _detail_paths(event))
    # Entra/non-ARM sources carry an explicit category hint; prefer it when the classifier is unsure.
    if cat in ("", "Unknown") and event.get("categoryHint"):
        cat = event["categoryHint"]
    event["category"] = cat
    _score_event(event, production=production)
    return event


def _apply_ai(event: dict[str, Any], ai: dict[str, Any], *, production: bool) -> None:
    """Overlay an AI enrichment result onto an event: fill the category when the deterministic
    pass left it Unknown (then re-score), and use the AI's sharper narrative + risk hint."""
    changed_cat = False
    cat = ai.get("category") or ""
    if cat and event.get("category") in ("", "Unknown"):
        event["category"] = cat
        changed_cat = True
    if changed_cat:
        _score_event(event, production=production)
    if ai.get("summary"):
        event["plainEnglishSummary"] = ai["summary"]
    if ai.get("impact"):
        event["possibleImpact"] = ai["impact"]
    if ai.get("why"):
        event["whyRisk"] = ai["why"]
    # Blend the AI risk hint with the deterministic score (lean toward the higher of the two so an
    # AI-detected dangerous change — e.g. "allows the Internet" — isn't under-rated), then relabel.
    hint = ai.get("risk")
    if isinstance(hint, int):
        blended = max(int(event.get("riskScore", 0)), min(100, max(0, hint)))
        event["riskScore"] = blended
        event["riskLabel"] = label_for_score(blended)
    event["confidence"] = "AI-analyzed"


def _attach_derived(out: dict[str, Any], events: list[dict[str, Any]]) -> None:
    """Attach all derived views to a run dict (so every tab + a later AI re-enrich are consistent):
    headline, resources, actors, operations (A1), narrative (A2), security rollup (C1/C2)."""
    from app.changeexplorer import operations as ops_mod
    from app.changeexplorer import security as security_mod

    out["headline"] = insights_mod.summarize(events)
    out["resources"] = insights_mod.by_resource(events)
    out["actors"] = insights_mod.by_actor(events)
    operations = ops_mod.group_operations(events)
    out["operations"] = operations
    out["narrative"] = ops_mod.build_narrative(events, operations)
    out["security"] = security_mod.summarize_security(events)


def _suspicious_insights(run_id: str, tenant_id: str, workload_id: str,
                         events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """C2 suspicious-pattern detections as ChangeInsight rows. Uses prior runs (when available) to
    power 'first-time actor for a resource type'."""
    from app.changeexplorer import runs as runs_store
    from app.changeexplorer import security as security_mod
    from app.changeexplorer.models import make_insight

    prior: set[tuple[str, str]] = set()
    try:
        for summary in runs_store.list_runs(tenant_id, workload_id)[:5]:
            prev = runs_store.get_run(tenant_id, summary.get("runId", ""))
            if not prev or prev.get("runId") == run_id:
                continue
            for e in prev.get("events", []) or []:
                actor = e.get("actorDisplay") or e.get("actor", "") or "unknown"
                rt = str(e.get("resourceType", "")).lower()
                if rt:
                    prior.add((actor, rt))
    except Exception:  # noqa: BLE001
        prior = set()

    out: list[dict[str, Any]] = []
    for p in security_mod.suspicious_patterns(events, prior_actor_resource_types=prior or None):
        out.append(make_insight(
            run_id, f"suspicious_{p['patternType']}", p["title"], p["summary"],
            p["severity"], p.get("relatedChangeIds", []),
        ))
    return out



async def _collect_raw(workload: dict[str, Any], connection: dict[str, Any] | None,
                       scope_info: dict[str, Any], start_iso: str, end_iso: str) -> tuple[list[dict[str, Any]], list[str], int]:
    """Run the collectors for a real (non-demo) workload. Returns (raw_rows, notes, change_limit).

    ``change_limit`` is the per-scan source cap (``RG_CHANGE_LIMIT``) when the Resource Graph
    ``resourcechanges`` feed returned a full page (i.e. the result was capped and there may be
    more), else 0 — so the UI can clearly tell the user the change list was limited.

    The Resource Graph ``resourcechanges`` feed has no caller, so we backfill the actor onto those
    rows from the Activity Log entries (matched by correlation id) — this is what turns the common
    'unknown' actor into the real service principal / user that made the change.

    The two sources are queried CONCURRENTLY; each collector internally fans out across
    subscriptions with bounded parallelism (>= 5) and 429 backoff/retry."""
    notes: list[str] = []
    from app.changeexplorer import entra as entra_mod

    (rg_rows, rg_note), (al_rows, al_note), (entra_rows, entra_note) = await asyncio.gather(
        collectors.collect_resource_graph_changes(
            scope_info.get("predicate", ""), start_iso, end_iso, connection),
        collectors.collect_activity_log(
            scope_info.get("subscriptions", []), start_iso, end_iso, connection,
            scope_info.get("resource_ids")),
        entra_mod.collect_entra_audits(connection, start_iso, end_iso, max_events=collectors.change_limit()),
    )
    if rg_note:
        notes.append(rg_note)
    if al_note:
        notes.append(al_note)
    if entra_note:
        notes.append(entra_note)
    change_limit = collectors.change_limit() if len(rg_rows) >= collectors.change_limit() else 0

    # ---- Actor attribution --------------------------------------------------------------
    # The Resource Graph ``resourcechanges`` feed has no caller, so we backfill the actor onto
    # those rows from the Activity Log entries. Primary match is correlation id; a secondary
    # proximity match (resourceId + close timestamp) recovers rows whose correlation id is the
    # zero-guid / missing. We copy the FULL identity envelope (object id, kind, ip, on-behalf-of),
    # not just the caller string, so a forensic reviewer sees who + how + from where.
    from app.changeexplorer import identity as identity_mod

    _ID_KEYS = ("actor", "actorType", "actorKind", "actorObjectId", "actorIp",
                "actorOnBehalfOf", "actorAppId", "isPlatformActor")

    def _copy_identity(dst: dict[str, Any], src: dict[str, Any]) -> None:
        for k in _ID_KEYS:
            if src.get(k) not in (None, ""):
                dst[k] = src[k]

    by_corr: dict[str, dict[str, Any]] = {}
    for r in al_rows:
        cid = (r.get("correlationId", "") or "").strip()
        if cid and cid != identity_mod._ZERO_GUID and r.get("actor"):
            by_corr.setdefault(cid, r)

    # Secondary index for proximity matching: resourceId(lower) -> list[(epoch, al_row)].
    by_res: dict[str, list[tuple[float, dict[str, Any]]]] = {}
    for r in al_rows:
        rid = (r.get("resourceId", "") or "").lower()
        if rid and r.get("actor"):
            by_res.setdefault(rid, []).append((_epoch(r.get("eventTime", "")), r))

    proximity_hits = 0
    for r in rg_rows:
        if r.get("actor"):
            continue
        hit = by_corr.get((r.get("correlationId", "") or "").strip())
        if hit:
            _copy_identity(r, hit)
            continue
        # Proximity fallback: a UNIQUE Activity Log event on the same resource within ±5 min.
        rid = (r.get("resourceId", "") or "").lower()
        cands = by_res.get(rid)
        if cands:
            t = _epoch(r.get("eventTime", ""))
            near = [al for (ts, al) in cands if abs(ts - t) <= 300]
            if len(near) == 1:
                _copy_identity(r, near[0])
                proximity_hits += 1

    # Any change still without an actor is an Azure-internal / cascade write with no recorded
    # caller — classify it as the platform (NOT a suspicious "unknown actor") for honesty.
    for r in rg_rows + al_rows:
        if not r.get("actor"):
            kind, _ = identity_mod.classify_actor("", None, r.get("correlationId", ""))
            r["actorKind"] = kind
            r["actorType"] = kind

    if proximity_hits:
        notes.append(f"Attributed {proximity_hits} change(s) to an actor by time-proximity match "
                     "(no correlation id was recorded on the change).")

    raw_rows = rg_rows + al_rows + entra_rows

    # ---- Resolve object-ids -> friendly names via Microsoft Graph (best-effort) ----------
    if connection is not None and _identity_resolution_enabled():
        oids = [r.get("actorObjectId", "") for r in raw_rows if r.get("actorObjectId")]
        # A bare GUID caller with no oid claim is itself the object id.
        oids += [r.get("actor", "") for r in raw_rows
                 if identity_mod.is_guid(r.get("actor", "")) and not r.get("actorObjectId")]
        app_ids = [r.get("actorAppId", "") for r in raw_rows if r.get("actorAppId")]
        if oids or app_ids:
            resolved, rnote = await identity_mod.resolve_display_names(oids, app_ids, connection)
            if rnote:
                notes.append(rnote)
            for r in raw_rows:
                key = r.get("actorObjectId", "") or (r.get("actor", "") if identity_mod.is_guid(r.get("actor", "")) else "")
                rec = resolved.get(key) or resolved.get(r.get("actorAppId", ""))
                if rec and rec.get("display"):
                    r["actorDisplay"] = rec["display"]
                    r["actorResolved"] = True
                    if rec.get("kind"):
                        r["actorKind"] = rec["kind"]

    return raw_rows, notes, change_limit


def _epoch(iso: str) -> float:
    """Parse an ISO timestamp to epoch seconds (0.0 on failure) for proximity matching."""
    if not iso:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _identity_resolution_enabled() -> bool:
    """Whether to resolve actor object-ids to names via Graph (app setting, default on)."""
    try:
        from app.core.app_settings import load_settings

        return bool(load_settings().get("changeexplorer_resolve_identities", True))
    except Exception:  # noqa: BLE001
        return True


async def analyze_stream(*, tenant_id: str, workload: dict[str, Any], connection: dict[str, Any] | None,
                         start_iso: str, end_iso: str, scope_mode: str, requested_by: str,
                         force_demo: bool = False, run_ai: bool = True) -> Any:
    """The change-analysis pipeline as an async generator that yields progress dicts while it
    works (``{"phase","message",...}``) and finally yields the completed run
    (``{"phase":"done","run":<run dict>}``). Used by the SSE endpoint; ``analyze`` drains it."""
    run_id = new_id()
    workload_id = workload.get("id", "")
    workload_name = workload.get("name", "workload")
    created = now_iso()
    notes: list[str] = []
    raw: list[dict[str, Any]] = []
    change_limit = 0
    is_demo = force_demo or demo.is_demo(workload_id)
    is_catalog_demo = demo.is_catalog_demo(workload_id)

    yield {"phase": "scope", "message": f"Resolving scope for {workload_name}…"}

    if is_demo:
        raw = demo.raw_changes()
        scope_info = {"mode": scope_mode, "subscriptions": [demo.DEMO_SUB], "predicate": "", "resource_ids": []}
        notes.append("Demo scenario — synthetic change data (no Azure query).")
    elif is_catalog_demo:
        raw = demo.catalog_changes(workload_id, start_iso, end_iso)
        from app.demo_catalog import DEMO_SUB as _CAT_SUB

        scope_info = {"mode": scope_mode, "subscriptions": [_CAT_SUB], "predicate": "", "resource_ids": []}
        notes.append("Demo workload — synthetic change data derived from its resources (no Azure query).")
        is_demo = True  # render + persist like any other demo run
    else:
        from app.changeexplorer.scope import build_scope

        scope_info = await build_scope(workload, connection, scope_mode)
        if scope_info.get("error"):
            notes.append(scope_info["error"])
        if connection is None:
            notes.append("No Azure connection bound — live change sources were not queried.")
        else:
            yield {"phase": "collect", "message": "Querying Azure Resource Graph & Activity Log for changes…"}
            raw, cnotes, change_limit = await _collect_raw(workload, connection, scope_info, start_iso, end_iso)
            notes.extend(cnotes)

    production = _is_production(workload)

    yield {"phase": "normalize", "message": f"Found {len(raw)} change(s). Dissecting & classifying…", "total": len(raw)}

    # Normalize -> deterministic classify + score.
    events: list[dict[str, Any]] = []
    for r in raw:
        ev = normalize_mod.normalize(r, run_id=run_id, tenant_id=tenant_id, workload_id=workload_id)
        events.append(_enrich(ev, production=production))

    events, truncated = scale.cap_events(events)

    # AI enrichment — resolve Unknowns + sharpen the narrative + risk for the highest-impact
    # changes. The AI pass is the slowest phase, so it's OPTIONAL: when ``run_ai`` is false the
    # run completes with deterministic-only results and ``aiAnalyzed=False``; the user can run the
    # AI pass later (the "Run AI analysis" button or opening a change record), which re-enriches
    # the persisted run via ``ai_enrich_run``.
    ai_analyzed = False
    if run_ai and events and not is_demo:
        ai_result: dict[int, dict[str, Any]] = {}
        async for ev in ai_enrich.enrich_stream(events):
            if "result" in ev:
                ai_result = ev["result"]
            else:
                yield ev
        if ai_result:
            for idx, ai in ai_result.items():
                if 0 <= idx < len(events):
                    _apply_ai(events[idx], ai, production=production)
            notes.append(f"AI analyzed {len(ai_result)} change(s) to infer category, impact and risk.")
        ai_analyzed = True
    elif is_demo:
        ai_analyzed = True  # demo data is pre-narrated; treat as already analyzed

    yield {"phase": "insights", "message": "Building insights & summary…"}

    events.sort(key=lambda e: e.get("eventTime", ""))
    head = insights_mod.summarize(events)
    insights = insights_mod.build_insights(run_id, events)
    # Suspicious-pattern heuristics (C2) become additional insights.
    insights += _suspicious_insights(run_id, tenant_id, workload_id, events)
    facets = insights_mod.facets(events)
    summary = _plain_summary(workload_name, start_iso, end_iso, head, events)
    # Persist the production flag so a later AI re-enrich re-scores consistently.
    scope_info = {**scope_info, "production": production}

    run = ChangeAnalysisRun(
        runId=run_id, tenantId=tenant_id, workloadId=workload_id, workloadName=workload_name,
        startTime=start_iso, endTime=end_iso, scopeMode=scope_mode, requestedBy=requested_by,
        createdAt=created, completedAt=now_iso(), status="succeeded",
        totalChanges=head["total"], criticalCount=head["critical"], highCount=head["high"],
        mediumCount=head["medium"], lowCount=head["low"], informationalCount=head["informational"],
        summary=summary, demo=is_demo, truncated=truncated, notes=notes, scopeInfo=scope_info,
        facets=facets, events=events, insights=insights, changeLimit=change_limit,
        aiAnalyzed=ai_analyzed,
    )
    out = asdict(run)
    _attach_derived(out, events)
    yield {"phase": "done", "run": out}


async def analyze(*, tenant_id: str, workload: dict[str, Any], connection: dict[str, Any] | None,
                  start_iso: str, end_iso: str, scope_mode: str, requested_by: str,
                  force_demo: bool = False, run_ai: bool = True) -> dict[str, Any]:
    """Run the full pipeline and return a serialized ChangeAnalysisRun (dict). Drains the stream."""
    final: dict[str, Any] = {}
    async for ev in analyze_stream(
        tenant_id=tenant_id, workload=workload, connection=connection,
        start_iso=start_iso, end_iso=end_iso, scope_mode=scope_mode,
        requested_by=requested_by, force_demo=force_demo, run_ai=run_ai,
    ):
        if ev.get("phase") == "done":
            final = ev["run"]
    return final


async def ai_enrich_run(run: dict[str, Any]) -> Any:
    """Run the AI enrichment pass over an ALREADY-PERSISTED run that was analyzed without AI.

    Async generator mirroring ``analyze_stream``: yields progress dicts, then a final
    ``{"phase":"done","run":<updated run dict>}``. The run's events are AI-sharpened in place and
    all derived views (headline / insights / facets / summary / actors / resources) are rebuilt so
    every tab reflects the enriched data. ``aiAnalyzed`` is set True. Idempotent-ish: re-running
    simply re-enriches. Demo runs and empty runs short-circuit (already 'analyzed')."""
    events = run.get("events", []) or []
    if run.get("demo") or not events:
        run["aiAnalyzed"] = True
        yield {"phase": "done", "run": run}
        return

    production = bool((run.get("scopeInfo") or {}).get("production", _is_production({})))

    yield {"phase": "ai", "message": "AI analyzing changes…", "total": len(events)}
    ai_result: dict[int, dict[str, Any]] = {}
    async for ev in ai_enrich.enrich_stream(events):
        if "result" in ev:
            ai_result = ev["result"]
        else:
            yield ev
    for idx, ai in ai_result.items():
        if 0 <= idx < len(events):
            _apply_ai(events[idx], ai, production=production)

    yield {"phase": "insights", "message": "Rebuilding insights & summary…"}
    events.sort(key=lambda e: e.get("eventTime", ""))
    head = insights_mod.summarize(events)
    run["events"] = events
    run["insights"] = insights_mod.build_insights(run.get("runId", ""), events)
    run["insights"] += _suspicious_insights(run.get("runId", ""), run.get("tenantId", ""),
                                            run.get("workloadId", ""), events)
    run["facets"] = insights_mod.facets(events)
    run["summary"] = _plain_summary(run.get("workloadName", ""), run.get("startTime", ""),
                                    run.get("endTime", ""), head, events)
    run["totalChanges"] = head["total"]
    run["criticalCount"] = head["critical"]
    run["highCount"] = head["high"]
    run["mediumCount"] = head["medium"]
    run["lowCount"] = head["low"]
    run["informationalCount"] = head["informational"]
    _attach_derived(run, events)
    run["aiAnalyzed"] = True
    notes = list(run.get("notes", []) or [])
    if ai_result and not any("AI analyzed" in n for n in notes):
        notes.append(f"AI analyzed {len(ai_result)} change(s) to infer category, impact and risk.")
    run["notes"] = notes
    yield {"phase": "done", "run": run}


def _plain_summary(workload_name: str, start: str, end: str, head: dict[str, Any], events: list[dict[str, Any]]) -> str:
    if not events:
        return (f"During the selected window, no changes were found for {workload_name} in the "
                f"selected scope. This may mean nothing changed, or that the change sources were "
                f"unavailable (see notes).")
    risky = sorted(events, key=lambda e: -int(e.get("riskScore", 0)))[:4]
    risky_resources = ", ".join(dict.fromkeys(e.get("resourceName", "") for e in risky))
    actor = head.get("most_active_actor", "")
    high_n = head["critical"] + head["high"]
    bits = [
        f"During the selected time range, {workload_name} had {head['total']} change(s) across "
        f"{head['resources_changed']} resource(s).",
    ]
    if high_n:
        bits.append(f"The {high_n} highest-risk change(s) involved {risky_resources}.")
    if actor:
        bits.append(f"Most changes were performed by {actor}.")
    if head.get("most_risky_category"):
        bits.append(f"The most risk-bearing category was {head['most_risky_category']}.")
    return " ".join(bits)
