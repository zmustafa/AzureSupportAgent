"""Know-Me generation context: known-fact auto-fill (A1) and evidence gathering (A3).

Two concerns, both best-effort and fail-silent (they only enrich generation — never block it):

* ``gather_known_facts`` — resolves things the platform already knows (subscription friendly
  names, regions, the assigned owner, workload tags/criticality) so the generator fills them
  as facts and ``autofill_todos`` can complete the matching ⟦TODO⟧ fields outright instead of
  leaving them for a human.
* ``gather_evidence`` — pulls real posture signals (latest assessment failures, monitoring /
  telemetry / backup-DR coverage gaps, the performance profiler's top bottleneck, idle/orphaned
  resources) so the Diagnostics / Known-issues / Thresholds / Resiliency sections are grounded
  in measured data rather than inference.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("app.knowme.context")


# ============================================================ A1 — known facts + auto-fill
async def gather_known_facts(
    workload: dict[str, Any] | None,
    arch: dict[str, Any] | None,
    tenant_id: str,
    connection_id: str,
    facts: dict[str, Any],
) -> dict[str, Any]:
    """Resolve platform-known values for this workload. Returns::

        {subscriptions: {guid: name}, regions: [..], owner: {display_name,email,team}|None,
         tags: {key: value}, criticality, environment, block: "<prompt text>"}
    """
    subs: dict[str, str] = {}
    for s in facts.get("subscriptions") or []:
        gid = str(s.get("id") or "").lower()
        nm = str(s.get("name") or "")
        if gid and nm and nm != gid:
            subs[gid] = nm

    # Fill any still-unnamed subscription GUIDs from the configured connections.
    try:
        from app.core.azure_connections import list_connections

        unresolved = [s.get("id") for s in (facts.get("subscriptions") or []) if str(s.get("id", "")).lower() not in subs]
        if unresolved:
            for c in list_connections():
                nm = c.get("display_name") or ""
                ds = (c.get("default_subscription") or "").lower()
                if ds and nm and ds not in subs:
                    subs[ds] = nm
    except Exception:  # noqa: BLE001
        pass

    regions = list(facts.get("regions") or [])

    owner: dict[str, Any] | None = None
    try:
        from app.ownership.resolve import resolve_owner

        wl_id = (workload or {}).get("id") or (arch or {}).get("workload_id") or ""
        if wl_id:
            res = resolve_owner(tenant_id, "workload", wl_id)
            owners = (res or {}).get("owners") or []
            primary = next((o for o in owners if o.get("primary")), owners[0] if owners else None)
            if primary and primary.get("display_name"):
                owner = {
                    "display_name": primary.get("display_name", ""),
                    "email": primary.get("email", ""),
                    "team": primary.get("role", "") or "",
                }
    except Exception:  # noqa: BLE001
        pass

    wl = workload or {}
    tags: dict[str, str] = {}
    raw_tags = wl.get("tags")
    if isinstance(raw_tags, dict):
        tags = {str(k): str(v) for k, v in raw_tags.items()}
    criticality = str(wl.get("criticality") or "")
    environment = str(wl.get("environment") or "")

    block = _known_facts_block(subs, regions, owner, criticality, environment, tags)
    return {
        "subscriptions": subs, "regions": regions, "owner": owner,
        "tags": tags, "criticality": criticality, "environment": environment, "block": block,
    }


def _known_facts_block(
    subs: dict[str, str], regions: list[str], owner: dict[str, Any] | None,
    criticality: str, environment: str, tags: dict[str, str],
) -> str:
    lines = ["PLATFORM-KNOWN VALUES (authoritative — state these as facts, do NOT mark ⟦TODO⟧):"]
    if subs:
        lines.append("Subscription names:")
        lines += [f"  - {g} → {n}" for g, n in subs.items()]
    if regions:
        lines.append(f"Region(s): {', '.join(regions)}")
    if owner:
        who = owner.get("display_name", "")
        em = f" <{owner['email']}>" if owner.get("email") else ""
        lines.append(f"Assigned workload owner (from Ownership): {who}{em} — this is the verified owner hint.")
    if criticality:
        lines.append(f"Business criticality (from workload profile): {criticality}")
    if environment:
        lines.append(f"Environment: {environment}")
    if tags:
        shown = ", ".join(f"{k}={v}" for k, v in list(tags.items())[:12])
        lines.append(f"Workload tags: {shown}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def autofill_todos(todos: list[dict[str, Any]], known: dict[str, Any]) -> int:
    """Auto-complete or suggest ⟦TODO⟧ fields from known facts. Mutates ``todos`` in place;
    returns the number of fields auto-FILLED (status→done, source=auto). Ambiguous fields
    (on-call / escalation / customer contacts) get one-click ``suggestions`` instead.
    Already-filled fields are never overwritten."""
    subs = known.get("subscriptions") or {}
    regions = known.get("regions") or []
    owner = known.get("owner") or None
    filled = 0
    for t in todos:
        if t.get("value") or t.get("status") == "done":
            continue
        group = t.get("group") or ""
        blob = f"{t.get('field_key','')} {t.get('label','')}".lower()
        # Subscription friendly name → auto-fill when exactly one is known.
        if group == "scope" and ("friendly" in blob or "sub" in blob and "name" in blob):
            names = list(subs.values())
            if len(names) == 1:
                _fill(t, names[0]); filled += 1
            elif names:
                _offer(t, names)
            continue
        # Region → auto-fill single region, else suggest.
        if group == "scope" and "region" in blob:
            if len(regions) == 1:
                _fill(t, regions[0]); filled += 1
            elif regions:
                _offer(t, regions, multi=True)
            continue
        # Ownership / cost-center / department → auto-fill the resolved owner.
        if group == "ownership" and owner:
            val = owner["email"] if (t.get("type") == "email" and owner.get("email")) else owner.get("display_name", "")
            if val:
                _fill(t, val); filled += 1
            continue
        # Escalation / on-call / customer contacts → suggest the owner (do NOT auto-fill;
        # the on-call group or customer contact is often distinct from the Azure owner).
        if group in ("escalation", "contacts") and owner:
            cand = []
            if owner.get("email"):
                cand.append(owner["email"])
            if owner.get("display_name"):
                cand.append(owner["display_name"])
            if cand:
                _offer(t, cand)
            continue
    return filled


def _offer(todo: dict[str, Any], values: list[str], *, multi: bool = False) -> None:
    """Attach platform-resolved candidate values as a choice set (a picker, free text still
    allowed). Platform facts are the most authoritative source, so they replace any rule/AI
    choices already on the field. Also kept in ``suggestions`` for backward-compat."""
    clean = [str(v) for v in values if str(v).strip()]
    if not clean:
        return
    todo["suggestions"] = clean
    todo["choices"] = clean
    todo["allow_custom"] = True
    todo["choice_source"] = "platform"
    if multi:
        todo["multi"] = True


def _fill(todo: dict[str, Any], value: str) -> None:
    todo["value"] = value
    todo["status"] = "done"
    todo["source"] = "auto"
    todo["confidence"] = 0.9


# ============================================================ A3 — evidence
async def gather_evidence(
    architecture_id: str, workload_id: str, tenant_id: str, connection_id: str,
) -> dict[str, Any]:
    """Best-effort posture evidence for grounding. Returns::

        {assessment: {...}|None, coverage: {amba,telemetry,backupdr}, performance: {...}|None,
         idle: [..], block: "<prompt text>"}
    """
    ev: dict[str, Any] = {"assessment": None, "coverage": {}, "performance": None, "idle": []}

    # 1) Latest succeeded assessment run → top failing findings + score.
    try:
        from sqlalchemy import select

        from app.core.db import SessionLocal
        from app.models import AssessmentRun

        async with SessionLocal() as db:
            run = (await db.execute(
                select(AssessmentRun).where(
                    AssessmentRun.tenant_id == tenant_id,
                    AssessmentRun.workload_id == workload_id,
                    AssessmentRun.status == "succeeded",
                    AssessmentRun.deleted_at.is_(None),
                ).order_by(AssessmentRun.started_at.desc()).limit(1)
            )).scalars().first()
        if run is not None:
            fails = [f for f in (run.findings_json or []) if isinstance(f, dict) and f.get("status") == "fail"]
            ranked = sorted(fails, key=lambda f: _sev_rank(f.get("severity", "")), reverse=True)[:12]
            ev["assessment"] = {
                "score": run.overall_score,
                "findings": [
                    {"title": f.get("check_title") or f.get("check_id") or "finding",
                     "severity": f.get("severity", ""), "pillar": f.get("pillar", "")}
                    for f in ranked
                ],
            }
    except Exception:  # noqa: BLE001
        pass

    # 2) Coverage snapshots (cached) for monitoring / telemetry / backup-DR.
    for name, mod in (("amba", "app.amba.cache"), ("telemetry", "app.telemetry.cache"), ("backupdr", "app.backupdr.cache")):
        try:
            cache = __import__(mod, fromlist=["read_snapshot"])
            snap = cache.read_snapshot(tenant_id, "workload", workload_id)
            if isinstance(snap, dict) and snap.get("kpis"):
                kpis = snap["kpis"]
                gaps = [
                    g.get("resource_name") or g.get("resource_id") or ""
                    for g in (snap.get("gaps") or [])[:8]
                ]
                ev["coverage"][name] = {
                    "coverage_pct": kpis.get("coverage_pct"),
                    "gaps": [g for g in gaps if g],
                }
        except Exception:  # noqa: BLE001
            pass

    # 3) Performance profiler — latest run's bottleneck + scorecard.
    try:
        from app.perfprofile.runs import latest_run

        run = latest_run(tenant_id, "workload", workload_id)
        if isinstance(run, dict):
            ev["performance"] = {
                "score": (run.get("scorecard") or {}).get("workload_score"),
                "breaching": (run.get("scorecard") or {}).get("breaching"),
                "top_bottleneck": run.get("top_bottleneck") or None,
            }
    except Exception:  # noqa: BLE001
        pass

    # 4) Idle / orphaned resources (cost + red-herring callouts).
    try:
        from app.inventory import cache as inv_cache
        from app.inventory import cost as inv_cost
        from app.inventory.optimization import analyze_resources

        hit = inv_cache.get(tenant_id, connection_id or "")
        if hit:
            resources = (hit.get("payload") or {}).get("resources") or []
            report = analyze_resources(resources, inv_cost.peek_cost(tenant_id, connection_id or ""))
            ev["idle"] = [
                f"{it.get('category_label')} — {it.get('name')}"
                for it in report.get("items", [])[:12]
            ]
    except Exception:  # noqa: BLE001
        pass

    ev["block"] = _evidence_block(ev)
    return ev


_SEV_ORDER = {"critical": 4, "error": 3, "high": 3, "warning": 2, "medium": 2, "info": 1, "low": 1}


def _sev_rank(sev: str) -> int:
    return _SEV_ORDER.get((sev or "").lower(), 0)


def _evidence_block(ev: dict[str, Any]) -> str:
    lines: list[str] = []
    a = ev.get("assessment")
    if a:
        score = f" (overall score {a['score']}/100)" if a.get("score") is not None else ""
        lines.append(f"WELL-ARCHITECTED ASSESSMENT{score} — top failing findings:")
        lines += [f"  - ❌ {f['title']}" + (f" [{f['severity']}/{f['pillar']}]" if f.get("severity") else "")
                  for f in a.get("findings", [])]
    cov = ev.get("coverage") or {}
    cov_labels = {"amba": "Monitoring (AMBA) alerts", "telemetry": "Diagnostic telemetry", "backupdr": "Backup & DR"}
    for k, label in cov_labels.items():
        c = cov.get(k)
        if c and c.get("coverage_pct") is not None:
            gaps = f" — gaps: {', '.join(c['gaps'][:6])}" if c.get("gaps") else ""
            lines.append(f"{label} coverage: {round(float(c['coverage_pct']))}%{gaps}")
    p = ev.get("performance")
    if p:
        tb = p.get("top_bottleneck") or {}
        sc = f"Performance health {p['score']}/100" if p.get("score") is not None else "Performance profiler"
        # pct_of_threshold can be None (a bottleneck without a numeric threshold ratio), so guard
        # before float() — tb.get(k, 0) still returns None when the key exists with a None value.
        pct = tb.get("pct_of_threshold")
        if tb and pct is not None:
            lines.append(f"{sc}; top bottleneck: {tb.get('resource_name','?')} {tb.get('metric_name','')} "
                         f"at {round(float(pct))}% of threshold ({tb.get('state','')}).")
        elif tb:
            lines.append(f"{sc}; top bottleneck: {tb.get('resource_name','?')} {tb.get('metric_name','')} "
                         f"({tb.get('state','')}).")
        else:
            lines.append(f"{sc}.")
    idle = ev.get("idle") or []
    if idle:
        lines.append("Idle / orphaned resources (cost + verify-scope red herrings): " + "; ".join(idle[:8]))
    if not lines:
        return ""
    return ("MEASURED POSTURE EVIDENCE (ground the Diagnostics, Known issues, Thresholds and "
            "Resiliency sections in THIS — cite specific findings/resources):\n" + "\n".join(lines))
