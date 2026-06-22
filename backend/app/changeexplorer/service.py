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


def _enrich(event: dict[str, Any], *, production: bool) -> dict[str, Any]:
    """Classify (type + operation + property paths) -> dependency role -> risk -> explain."""
    event["category"] = classify_mod.classify(
        event.get("resourceType", ""), event.get("operation", ""), _detail_paths(event))
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
    (rg_rows, rg_note), (al_rows, al_note) = await asyncio.gather(
        collectors.collect_resource_graph_changes(
            scope_info.get("predicate", ""), start_iso, end_iso, connection),
        collectors.collect_activity_log(
            scope_info.get("subscriptions", []), start_iso, end_iso, connection,
            scope_info.get("resource_ids")),
    )
    if rg_note:
        notes.append(rg_note)
    if al_note:
        notes.append(al_note)
    change_limit = collectors.RG_CHANGE_LIMIT if len(rg_rows) >= collectors.RG_CHANGE_LIMIT else 0

    # Build a correlation-id -> (actor, actorType) map from Activity Log and backfill RG rows.
    by_corr: dict[str, tuple[str, str]] = {}
    for r in al_rows:
        cid = r.get("correlationId", "")
        if cid and r.get("actor"):
            by_corr.setdefault(cid, (r["actor"], r.get("actorType", "Unknown")))
    for r in rg_rows:
        if not r.get("actor"):
            hit = by_corr.get(r.get("correlationId", ""))
            if hit:
                r["actor"], r["actorType"] = hit

    return rg_rows + al_rows, notes, change_limit


async def analyze_stream(*, tenant_id: str, workload: dict[str, Any], connection: dict[str, Any] | None,
                         start_iso: str, end_iso: str, scope_mode: str, requested_by: str,
                         force_demo: bool = False) -> Any:
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

    # AI enrichment — resolve Unknowns + sharpen the narrative + risk for the highest-impact changes.
    ai_result: dict[int, dict[str, Any]] = {}
    if events and not is_demo:
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

    yield {"phase": "insights", "message": "Building insights & summary…"}

    events.sort(key=lambda e: e.get("eventTime", ""))
    head = insights_mod.summarize(events)
    insights = insights_mod.build_insights(run_id, events)
    facets = insights_mod.facets(events)
    summary = _plain_summary(workload_name, start_iso, end_iso, head, events)

    run = ChangeAnalysisRun(
        runId=run_id, tenantId=tenant_id, workloadId=workload_id, workloadName=workload_name,
        startTime=start_iso, endTime=end_iso, scopeMode=scope_mode, requestedBy=requested_by,
        createdAt=created, completedAt=now_iso(), status="succeeded",
        totalChanges=head["total"], criticalCount=head["critical"], highCount=head["high"],
        mediumCount=head["medium"], lowCount=head["low"], informationalCount=head["informational"],
        summary=summary, demo=is_demo, truncated=truncated, notes=notes, scopeInfo=scope_info,
        facets=facets, events=events, insights=insights, changeLimit=change_limit,
    )
    out = asdict(run)
    out["headline"] = head
    out["resources"] = insights_mod.by_resource(events)
    out["actors"] = insights_mod.by_actor(events)
    yield {"phase": "done", "run": out}


async def analyze(*, tenant_id: str, workload: dict[str, Any], connection: dict[str, Any] | None,
                  start_iso: str, end_iso: str, scope_mode: str, requested_by: str,
                  force_demo: bool = False) -> dict[str, Any]:
    """Run the full pipeline and return a serialized ChangeAnalysisRun (dict). Drains the stream."""
    final: dict[str, Any] = {}
    async for ev in analyze_stream(
        tenant_id=tenant_id, workload=workload, connection=connection,
        start_iso=start_iso, end_iso=end_iso, scope_mode=scope_mode,
        requested_by=requested_by, force_demo=force_demo,
    ):
        if ev.get("phase") == "done":
            final = ev["run"]
    return final


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
