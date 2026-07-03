"""AI authoring for Monitor: build one widget from natural language, or a whole
dashboard for an Azure workload (grounded in the workload's resources, its linked
Architecture **Memory**, and open assessment findings).

Follows the house AI pattern: ``build_provider_for`` → drain ``provider.stream`` tokens →
``safe_json_parse``. NEVER ``complete_json``. The model only *proposes* JSON config; the
registry's ``_clean_widget`` / resolvers validate and execute it under the usual
read-only + SSRF guards.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.agent.factory import build_provider_for
from app.core.utils import loads_tolerant, safe_json_parse

from .catalog import DATASOURCE_CATALOG, DEFAULT_SIZE, WIDGET_CATALOG
from .playbooks import DASHBOARD_ARCHETYPES, infer_topology, playbooks_for_types

logger = logging.getLogger("app.monitor.ai_author")

_MAX_CTX = 28_000
_MAX_WIDGETS = 12


async def _complete(messages: list[dict[str, Any]]) -> str:
    """One non-streaming completion by draining the provider's token stream.

    A generous ``max_tokens`` is REQUIRED: reasoning models (e.g. Opus) spend part of the
    budget on hidden reasoning tokens, so without headroom the whole budget is consumed by
    reasoning and ZERO visible tokens are emitted — which is what made the Monitor
    "AI-suggest dashboard" flow return an empty completion → 422.
    """
    provider = build_provider_for(None, None)
    parts: list[str] = []
    try:
        async for ev in provider.stream(messages, None, max_tokens=32000):
            if ev.type == "token":
                parts.append(ev.text)
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
    return "".join(parts)


def _parse_ai_json(raw: str) -> dict[str, Any]:
    """Robustly parse a JSON object out of an LLM completion.

    Reasoning models (e.g. Opus) frequently wrap the JSON in a ```json fence, add a prose
    preamble, or hit the output cap mid-object. The strict ``safe_json_parse`` returns ``{}``
    for all of these, which is what made the Monitor "AI-suggest dashboard" flow fail. This
    strips a fence / preamble, extracts the outermost ``{...}`` (repairing a truncated tail by
    closing open brackets at the last complete element), and parses tolerantly.
    """
    obj = safe_json_parse(raw, None)
    if isinstance(obj, dict):
        return obj
    t = (raw or "").strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
        if m:
            t = m.group(1).strip()
    if not t.startswith("{"):
        m = re.search(r"(\{.*\})", t, re.DOTALL)
        if m:
            t = m.group(1)
    parsed = loads_tolerant(t)
    if isinstance(parsed, dict):
        return parsed
    repaired = _repair_truncated_object(t)
    if repaired:
        parsed = loads_tolerant(repaired)
        if isinstance(parsed, dict):
            return parsed
    return {}


def _repair_truncated_object(t: str) -> str | None:
    """Close the open brackets of a truncated JSON object at its last complete element, turning
    a valid-prefix-but-cut-off completion back into parseable JSON (keeps all complete keys)."""
    start = t.find("{")
    if start < 0:
        return None
    s = t[start:]
    stack: list[str] = []
    in_str = False
    esc = False
    last_idx = -1
    last_stack: list[str] | None = None
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
            last_idx = i
            last_stack = list(stack)
    if last_idx < 0 or last_stack is None:
        return None
    head = s[: last_idx + 1]
    closers = "".join("}" if c == "{" else "]" for c in reversed(last_stack))
    return head + closers


def _catalog_brief() -> str:
    ds = "\n".join(
        f"- {d['kind']}: {d['description']} fields={[f['key'] for f in d['fields']]}"
        for d in DATASOURCE_CATALOG
    )
    wt = "\n".join(
        f"- {w['type']}: {w['desc']}" + (f" chartTypes={w.get('chartTypes')}" if w.get("chartTypes") else "")
        for w in WIDGET_CATALOG
    )
    return f"WIDGET TYPES:\n{wt}\n\nDATA SOURCES:\n{ds}"


def _archetype_brief(archetype: str) -> dict[str, Any]:
    return DASHBOARD_ARCHETYPES.get(archetype) or DASHBOARD_ARCHETYPES["full_stack"]


_WIDGET_SCHEMA = """\
A widget is JSON of this exact shape (omit fields you don't need):
{
  "title": "short title",
  "type": "stat|chart|table|list|gauge|availability|map|markdown|clock",
  "dataSource": {"kind":"<one of the data sources>", ...kind-specific fields...},
  "transform": {"x":"<column>","series":["<column>",...],"agg":"sum|avg|max|min|count","topN":10},
  "viz": {"chartType":"line|area|bar|stackedBar|pie|donut|scatter","unit":"%","thresholds":[{"op":">","value":80,"color":"red"}],"stat":{"valueColumn":"<col>"}},
  "refresh": {"mode":"live|manual","intervalSec":60}
}
Rules:
- Pick the simplest type that answers the ask. Use "chart" with chartType for trends/breakdowns,
  "stat" for a single KPI, "table" for row data, "availability" for web/tcp ping, "markdown" for notes.
- For resource_graph/log_analytics put the KQL in dataSource.query.
- For azure_metrics set dataSource.resource_ids (array) and dataSource.metrics (array).
- For web_ping set dataSource.url; for tcp_ping set dataSource.host + dataSource.port.
- NEVER invent resource ids or workspace ids you weren't given; leave them blank for the user to fill.
"""


async def build_widget(prompt: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Natural-language → one widget config (validated by the caller)."""
    ctx = context or {}
    sys = (
        "You design a single Monitor dashboard WIDGET from a user's request. "
        "Respond with ONLY a JSON object (no prose, no code fence).\n\n"
        + _catalog_brief() + "\n\n" + _WIDGET_SCHEMA
    )
    user = f"Request: {prompt}\n"
    if ctx.get("connections"):
        user += f"\nAvailable Azure connections (id → name): {json.dumps(ctx['connections'])[:1000]}\n"
    if ctx.get("workbooks"):
        user += f"\nAvailable workbooks (id → name): {json.dumps(ctx['workbooks'])[:1000]}\n"
    raw = await _complete([{"role": "system", "content": sys}, {"role": "user", "content": user}])
    obj = safe_json_parse(raw, {})
    if not isinstance(obj, dict) or not obj.get("type"):
        return {"error": "The model did not return a valid widget.", "raw": raw[:500]}
    return _with_size(obj)


def _with_size(widget: dict[str, Any]) -> dict[str, Any]:
    size = DEFAULT_SIZE.get(widget.get("type", ""), {"w": 4, "h": 3})
    widget.setdefault("layout", {"x": 0, "y": 0, "w": size["w"], "h": size["h"]})
    return widget


async def _gather_workload_context(workload_id: str, tenant_id: str) -> dict[str, Any]:
    """Curated context bundle for workload dashboard generation.

    This is intentionally richer than Architecture Memory alone: it adds deterministic
    resource playbooks, inferred topology, assessment findings, recent app activity, and
    observability coverage signals so the AI can design like an SRE instead of merely
    making a handful of charts.
    """
    from app.architectures import memory as mem_mod
    from app.architectures import registry as arch_registry
    from app.core.azure_connections import resolve_connection
    from app.workloads.registry import get_workload

    wl = get_workload(workload_id) or {}
    summary = wl.get("summary", {}) or {}
    types = summary.get("types", []) or []
    nodes = wl.get("nodes", []) or []
    type_names = [str(t.get("label") or "") for t in types if t.get("label")]
    node_types = [str(n.get("resource_type") or "") for n in nodes if n.get("resource_type")]
    resource_types = list(dict.fromkeys([*type_names, *node_types]))
    resource_samples = [
        {
            "kind": n.get("kind"),
            "id": n.get("id"),
            "name": n.get("name"),
            "type": n.get("resource_type"),
            "resource_group": n.get("resource_group"),
            "location": n.get("location"),
        }
        for n in nodes[:60]
    ]

    # Architecture Memory linked to this workload (the KEY differentiator).
    memories: list[dict[str, Any]] = []
    archs = {a["id"]: a for a in arch_registry.list_architectures(tenant_id)}
    for m in mem_mod.list_memories(tenant_id):
        if (m.get("workload_id") or "") == workload_id:
            sections = [
                {"label": s.get("label"), "content": (s.get("content") or "").strip()[:1500]}
                for s in m.get("sections", []) if (s.get("content") or "").strip()
            ]
            if sections:
                memories.append({
                    "architecture": archs.get(m.get("architecture_id", ""), {}).get("name", ""),
                    "sections": sections,
                })

    conn = resolve_connection(wl.get("connection_id") or None)
    observability = _observability_coverage(resource_types, conn)

    return {
        "workload": {
            "name": wl.get("name", ""),
            "description": wl.get("description", ""),
            "connection_id": wl.get("connection_id", ""),
            "total_resources": summary.get("total_resources"),
            "resource_types": [{"type": t.get("label"), "count": t.get("count")} for t in types[:25]],
            "resource_samples": resource_samples,
            "tags": wl.get("tags", []),
        },
        "memories": memories,
        "topology": infer_topology(resource_types),
        "resource_playbooks": playbooks_for_types(resource_types),
        "observability_coverage": observability,
        "assessment_findings": await _latest_assessment_findings(workload_id, tenant_id),
        "recent_incidents": await _recent_incident_hints(workload_id, tenant_id, wl.get("name", "")),
        "design_rules": {
            "spine": ["Is it healthy?", "What is broken?", "Where?", "Why?", "What changed?", "What do I do?"],
            "golden_signals": ["latency", "traffic", "errors", "saturation"],
            "layout": "KPIs/SLO first, request path next, time-series together, tables lower, coverage and runbook last.",
            "avoid": ["vanity widgets", "duplicate signals", "CPU-only dashboards", "empty chart soup"],
        },
    }


def _observability_coverage(resource_types: list[str], conn: dict[str, Any] | None) -> dict[str, Any]:
    hay = "\n".join(t.lower() for t in resource_types)
    return {
        "log_analytics_workspace_configured": bool(conn and conn.get("log_analytics_workspace_id")),
        "has_app_insights": "microsoft.insights/components" in hay,
        "has_log_analytics_resource": "microsoft.operationalinsights/workspaces" in hay,
        "has_metric_alerts": "microsoft.insights/metricalerts" in hay,
        "has_action_groups": "microsoft.insights/actiongroups" in hay,
        "suggested_gap_widgets": [
            "diagnostic settings coverage",
            "alerts/action-group coverage",
            "metrics/log query health",
            "last successful refresh",
        ],
    }


async def _latest_assessment_findings(workload_id: str, tenant_id: str) -> dict[str, Any]:
    """Latest assessment findings, reduced to prompt-sized operational signals."""
    try:
        from sqlalchemy import desc, select

        from app.core.db import SessionLocal
        from app.models import AssessmentRun
    except Exception:  # noqa: BLE001
        return {}

    try:
        async with SessionLocal() as db:
            q = (
                select(AssessmentRun)
                .where(
                    AssessmentRun.workload_id == workload_id,
                    AssessmentRun.tenant_id == tenant_id,
                    AssessmentRun.deleted_at.is_(None),
                )
                .order_by(desc(AssessmentRun.started_at))
                .limit(1)
            )
            run = (await db.execute(q)).scalars().first()
            if not run:
                return {}
            findings = [f for f in (run.findings_json or []) if str(f.get("status") or "").lower() in ("fail", "failed", "error")]
            findings.sort(key=lambda f: {"critical": 0, "error": 1, "warning": 2, "info": 3}.get(str(f.get("severity") or "info"), 9))
            return {
                "overall_score": run.overall_score,
                "severity": run.severity,
                "scores": run.scores_json,
                "totals": run.totals_json,
                "summary": (run.summary or "")[:1200],
                "top_findings": [
                    {
                        "title": f.get("title") or f.get("check_title") or f.get("id"),
                        "pillar": f.get("pillar"),
                        "severity": f.get("severity"),
                        "resources": len(f.get("resources") or []),
                        "recommendation": (f.get("recommendation") or "")[:300],
                    }
                    for f in findings[:12]
                ],
            }
    except Exception:  # noqa: BLE001
        return {}


async def _recent_incident_hints(workload_id: str, tenant_id: str, workload_name: str) -> list[dict[str, Any]]:
    """Recent chat/deep-investigation hints related to this workload (best-effort)."""
    try:
        from sqlalchemy import desc, select

        from app.core.db import SessionLocal
        from app.models import Chat
    except Exception:  # noqa: BLE001
        return []

    try:
        terms = [t for t in (workload_id, workload_name) if t]
        async with SessionLocal() as db:
            stmt = select(Chat).where(Chat.tenant_id == tenant_id).order_by(desc(Chat.updated_at)).limit(20)
            rows = (await db.execute(stmt)).scalars().all()
        out: list[dict[str, Any]] = []
        for c in rows:
            title = c.title or ""
            if terms and not any(t.lower() in title.lower() for t in terms):
                continue
            out.append({"title": title[:160], "updated_at": c.updated_at.isoformat() if c.updated_at else ""})
        return out[:6]
    except Exception:  # noqa: BLE001
        return []


async def _design_brief(ctx: dict[str, Any], archetype: str) -> dict[str, Any]:
    """First AI pass: decide the dashboard's purpose, layers, and operating story."""
    sys = (
        "You are a principal SRE creating a concise DESIGN BRIEF before building a Monitor dashboard. "
        "Use workload resources, Architecture Memory, assessment findings, resource playbooks, topology, recent incidents, and observability coverage. "
        "Respond with ONLY JSON.\n"
        "Shape: {\"audience\":\"...\",\"purpose\":\"...\",\"critical_user_journeys\":[...],\"top_risks\":[...],"
        "\"dashboard_layers\":[...],\"required_data_sources\":[...],\"observability_gaps\":[...],\"layout_strategy\":\"...\"}"
    )
    user = json.dumps({"archetype": _archetype_brief(archetype), "context": ctx})[:_MAX_CTX]
    raw = await _complete([{"role": "system", "content": sys}, {"role": "user", "content": user}])
    obj = _parse_ai_json(raw)
    if isinstance(obj, dict) and obj:
        return obj
    return {
        "audience": _archetype_brief(archetype)["label"],
        "purpose": _archetype_brief(archetype)["goal"],
        "dashboard_layers": _archetype_brief(archetype)["layers"],
        "observability_gaps": ctx.get("observability_coverage", {}).get("suggested_gap_widgets", []),
    }


async def suggest_dashboard(workload_id: str, *, tenant_id: str, archetype: str = "full_stack") -> dict[str, Any]:
    """Propose a list of widgets to monitor a workload (for user review before building).

    The workload's Architecture Memory is explicitly included so suggestions reflect what
    actually matters for THIS workload (expected flow, resiliency target, security model,
    known gaps, observability notes) — not just a generic resource list.
    """
    ctx = await _gather_workload_context(workload_id, tenant_id)
    if not ctx["workload"]["name"]:
        return {"error": "Workload not found."}
    brief = await _design_brief(ctx, archetype)
    sys = (
        "You are a principal SRE designing a monitoring dashboard for ONE Azure workload. "
        "Propose an excellent, non-boring dashboard. Use the DESIGN BRIEF as the contract. "
        "Use Architecture Memory heavily, but also apply resource playbooks, golden signals, topology, assessment findings, recent incidents, observability coverage, and data quality. "
        "Every widget must answer a real operational question and include why/source/refresh/confidence. "
        "Include gap widgets when telemetry is missing instead of pretending data exists. Respond with ONLY JSON.\n\n" + _catalog_brief() + "\n\n"
        'Shape: {"widgets":[{"title":"...","type":"...","why":"why it matters","question":"operational question",'
        '"confidence":0.0,"refresh":{"mode":"live","intervalSec":60},"dataSource":{"kind":"...", ...}}], '
        '"summary":"one sentence overview","design_brief":{...}}\n'
        "Prefer 8-12 widgets. Put SLO/golden-signal/dependency/security/coverage/runbook in the mix. "
        "Use actual resource ids from resource_samples where available. Do not invent missing resource ids or URLs."
    )
    user = json.dumps({"archetype": _archetype_brief(archetype), "design_brief": brief, "context": ctx})[:_MAX_CTX]
    raw = await _complete([{"role": "system", "content": sys}, {"role": "user", "content": user}])
    obj = _parse_ai_json(raw)
    if not isinstance(obj, dict) or not isinstance(obj.get("widgets"), list):
        logger.warning("Monitor suggest: no widgets parsed (raw len=%d) head=%r", len(raw or ""), (raw or "")[:200])
        return {"error": "The model did not return valid suggestions.", "raw": raw[:500]}
    obj["workload_name"] = ctx["workload"]["name"]
    obj["used_memory"] = bool(ctx["memories"])
    obj["design_brief"] = obj.get("design_brief") if isinstance(obj.get("design_brief"), dict) else brief
    obj["archetype"] = archetype
    return obj


async def build_dashboard(
    workload_id: str,
    *,
    tenant_id: str,
    selected: list[dict[str, Any]] | None = None,
    archetype: str = "full_stack",
) -> dict[str, Any]:
    """Build a full dashboard for a workload: each suggested widget fleshed out + laid out.

    Returns a dashboard payload (name = workload name) ready to upsert. If ``selected`` is
    provided (user-reviewed subset of suggestions), those are built; else we suggest first.
    """
    ctx = await _gather_workload_context(workload_id, tenant_id)
    if not ctx["workload"]["name"]:
        return {"error": "Workload not found."}

    suggestions = selected
    design_brief: dict[str, Any] | None = None
    if not suggestions:
        sug = await suggest_dashboard(workload_id, tenant_id=tenant_id, archetype=archetype)
        if sug.get("error"):
            return sug
        suggestions = sug.get("widgets", [])
        design_brief = sug.get("design_brief") if isinstance(sug.get("design_brief"), dict) else None
    if design_brief is None:
        design_brief = await _design_brief(ctx, archetype)

    # Flesh each suggestion into a full widget (config + viz), grounded in memory.
    widgets: list[dict[str, Any]] = []
    dry_runs: list[dict[str, Any]] = []
    for s in suggestions[:_MAX_WIDGETS]:
        w = await _build_one_from_suggestion(s, ctx)
        if w and not w.get("error"):
            repaired, dry = await _dry_run_and_repair(w, ctx, tenant_id)
            dry_runs.append(dry)
            widgets.append(repaired)

    # Add a small coverage/runbook widget when the AI omitted one. This keeps dashboards
    # diagnostically useful even if data sources are missing.
    if not any((w.get("type") == "markdown" and "coverage" in json.dumps(w).lower()) for w in widgets):
        widgets.append(_coverage_markdown_widget(ctx, design_brief))

    # Lay them out: KPIs/stat/gauge/availability on a top row, bigger viz below.
    laid = _auto_layout(widgets[:_MAX_WIDGETS])
    critique = await _critic_pass(ctx, design_brief, laid, dry_runs, archetype)
    if int(critique.get("score") or 100) < 70 and not any("critic" in (w.get("title", "").lower()) for w in laid):
        laid = _auto_layout([*laid[: _MAX_WIDGETS - 1], _critic_markdown_widget(critique)])
    return {
        "dashboard": {
            "name": ctx["workload"]["name"],
            "description": f"{_archetype_brief(archetype)['label']} for {ctx['workload']['name']}.",
            "workload_id": workload_id,
            "widgets": laid,
            "ai_design": {
                "archetype": archetype,
                "archetype_label": _archetype_brief(archetype)["label"],
                "design_brief": design_brief,
                "critic": critique,
                "dry_runs": dry_runs,
                "used_memory": bool(ctx["memories"]),
                "context_digest": {
                    "topology": ctx.get("topology", []),
                    "resource_playbooks": [p.get("label") for p in ctx.get("resource_playbooks", [])],
                    "observability_coverage": ctx.get("observability_coverage", {}),
                    "assessment_findings": len(ctx.get("assessment_findings", {}).get("top_findings", [])),
                    "recent_incidents": len(ctx.get("recent_incidents", [])),
                },
            },
        },
        "used_memory": bool(ctx["memories"]),
        "widget_count": len(laid),
        "design_brief": design_brief,
        "critic": critique,
        "dry_runs": dry_runs,
    }


async def _build_one_from_suggestion(suggestion: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Turn a single suggestion into a full, valid widget config."""
    sys = (
        "Flesh out ONE Monitor widget from a brief spec, grounded in the workload context. "
        "Respond with ONLY a JSON widget object.\n\n" + _catalog_brief() + "\n\n" + _WIDGET_SCHEMA
    )
    user = (
        f"Workload context: {json.dumps(ctx['workload'])[:3000]}\n"
        f"Memory highlights: {json.dumps(ctx['memories'])[:4000]}\n"
        f"Resource playbooks: {json.dumps(ctx.get('resource_playbooks', []))[:3000]}\n"
        f"Topology: {json.dumps(ctx.get('topology', []))}\n"
        f"Observability coverage: {json.dumps(ctx.get('observability_coverage', {}))}\n"
        f"Assessment findings: {json.dumps(ctx.get('assessment_findings', {}))[:2500]}\n"
        f"Widget to build: {json.dumps(suggestion)[:2000]}"
    )
    raw = await _complete([{"role": "system", "content": sys}, {"role": "user", "content": user}])
    obj = safe_json_parse(raw, {})
    if not isinstance(obj, dict) or not obj.get("type"):
        # Fall back to the raw suggestion shell so the dashboard still gets a tile.
        if suggestion.get("type"):
            return _with_size(dict(suggestion))
        return {"error": "invalid"}
    if ctx["workload"].get("connection_id") and isinstance(obj.get("dataSource"), dict):
        obj["dataSource"].setdefault("connection_id", ctx["workload"]["connection_id"])
    return _with_size(obj)


async def _dry_run_and_repair(widget: dict[str, Any], ctx: dict[str, Any], tenant_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Dry-run a widget. If it fails, ask AI to repair once; else create a gap widget."""
    from app.monitor.datasources.resolver import resolve_widget

    ds = widget.get("dataSource") if isinstance(widget.get("dataSource"), dict) else {"kind": "none"}
    kind = ds.get("kind", "none")
    if kind in ("none", "static"):
        return widget, {"title": widget.get("title"), "kind": kind, "status": "skipped"}
    first = await resolve_widget(ds, tenant_id=tenant_id, params={}, use_cache=False)
    if not first.get("error"):
        return widget, {"title": widget.get("title"), "kind": kind, "status": "ok", "rows": len(first.get("rows", []))}
    repaired = await _repair_widget(widget, first.get("error", "Unknown error"), ctx)
    if repaired and repaired is not widget:
        ds2 = repaired.get("dataSource") if isinstance(repaired.get("dataSource"), dict) else {"kind": "none"}
        if ds2.get("kind") in ("none", "static"):
            return repaired, {"title": widget.get("title"), "kind": kind, "status": "repaired_to_gap", "error": first.get("error")}
        second = await resolve_widget(ds2, tenant_id=tenant_id, params={}, use_cache=False)
        if not second.get("error"):
            return repaired, {"title": repaired.get("title"), "kind": ds2.get("kind"), "status": "repaired", "rows": len(second.get("rows", []))}
    return _gap_widget(widget, first.get("error", "Unknown error")), {
        "title": widget.get("title"), "kind": kind, "status": "gap", "error": first.get("error")
    }


async def _repair_widget(widget: dict[str, Any], error: str, ctx: dict[str, Any]) -> dict[str, Any] | None:
    sys = (
        "You repair one Monitor widget after a dry-run failed. Respond with ONLY a JSON widget. "
        "If data is genuinely unavailable (missing resource id, workspace id, URL), create a markdown or static gap widget that explains the prerequisite and next step. "
        "Do not invent IDs, URLs, or workspace ids.\n\n" + _catalog_brief() + "\n\n" + _WIDGET_SCHEMA
    )
    user = json.dumps({"widget": widget, "dry_run_error": error, "context": ctx})[:_MAX_CTX]
    raw = await _complete([{"role": "system", "content": sys}, {"role": "user", "content": user}])
    obj = safe_json_parse(raw, {})
    if not isinstance(obj, dict) or not obj.get("type"):
        return None
    return _with_size(obj)


def _gap_widget(widget: dict[str, Any], error: str) -> dict[str, Any]:
    title = widget.get("title") or "Telemetry gap"
    md = (
        f"### {title}\n\n"
        "This widget could not be wired to live data during dashboard generation.\n\n"
        f"**Reason:** {error}\n\n"
        "**Next step:** edit the widget data source, provide the missing resource/workspace/URL, or enable diagnostics."
    )
    return _with_size({
        "title": f"Gap: {title}"[:200],
        "type": "markdown",
        "dataSource": {"kind": "none"},
        "transform": {},
        "viz": {"markdown": md},
        "refresh": {"mode": "manual", "intervalSec": 300},
    })


def _coverage_markdown_widget(ctx: dict[str, Any], brief: dict[str, Any]) -> dict[str, Any]:
    cov = ctx.get("observability_coverage", {})
    gaps = brief.get("observability_gaps") or cov.get("suggested_gap_widgets") or []
    lines = [
        "### Observability coverage",
        "",
        f"- Log Analytics workspace configured: **{bool(cov.get('log_analytics_workspace_configured'))}**",
        f"- Application Insights present: **{bool(cov.get('has_app_insights'))}**",
        f"- Metric alerts present: **{bool(cov.get('has_metric_alerts'))}**",
        f"- Action groups present: **{bool(cov.get('has_action_groups'))}**",
    ]
    if gaps:
        lines += ["", "**Gaps to close**"] + [f"- {g}" for g in gaps[:8]]
    return _with_size({
        "title": "Observability coverage",
        "type": "markdown",
        "dataSource": {"kind": "none"},
        "transform": {},
        "viz": {"markdown": "\n".join(lines)},
        "refresh": {"mode": "manual", "intervalSec": 300},
    })


def _critic_markdown_widget(critique: dict[str, Any]) -> dict[str, Any]:
    gaps = critique.get("gaps") if isinstance(critique.get("gaps"), list) else []
    improvements = critique.get("improvements") if isinstance(critique.get("improvements"), list) else []
    lines = [
        "### Dashboard critic notes",
        "",
        f"Quality score: **{critique.get('score', 'unknown')}**",
        "",
    ]
    if gaps:
        lines += ["**Gaps found**"] + [f"- {g}" for g in gaps[:6]] + [""]
    if improvements:
        lines += ["**Suggested improvements**"] + [f"- {i}" for i in improvements[:6]]
    return _with_size({
        "title": "Dashboard critic notes",
        "type": "markdown",
        "dataSource": {"kind": "none"},
        "transform": {},
        "viz": {"markdown": "\n".join(lines)},
        "refresh": {"mode": "manual", "intervalSec": 300},
    })


async def _critic_pass(
    ctx: dict[str, Any],
    design_brief: dict[str, Any],
    widgets: list[dict[str, Any]],
    dry_runs: list[dict[str, Any]],
    archetype: str,
) -> dict[str, Any]:
    """Second AI pass: senior SRE review of dashboard quality."""
    sys = (
        "You are a Dashboard Critic: a blunt but constructive principal SRE. Review the generated dashboard for gaps. "
        "Check golden signals, topology, dependencies, security/identity, cost/capacity, change awareness, telemetry coverage, and whether every widget answers a real operational question. "
        "Respond with ONLY JSON: {\"score\":0-100,\"strengths\":[...],\"gaps\":[...],\"improvements\":[...],\"ready\":true|false}."
    )
    user = json.dumps({
        "archetype": _archetype_brief(archetype),
        "design_brief": design_brief,
        "context_digest": {
            "topology": ctx.get("topology"),
            "playbooks": [p.get("label") for p in ctx.get("resource_playbooks", [])],
            "coverage": ctx.get("observability_coverage"),
            "assessment": ctx.get("assessment_findings"),
        },
        "widgets": [{"title": w.get("title"), "type": w.get("type"), "kind": (w.get("dataSource") or {}).get("kind")} for w in widgets],
        "dry_runs": dry_runs,
    })[:_MAX_CTX]
    raw = await _complete([{"role": "system", "content": sys}, {"role": "user", "content": user}])
    obj = safe_json_parse(raw, {})
    if isinstance(obj, dict) and obj:
        return obj
    return {"score": 75, "strengths": [], "gaps": ["Critic response was unavailable."], "improvements": [], "ready": True}


def _auto_layout(widgets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Place widgets on a 12-col grid: small KPIs first row, big viz stacked below."""
    small = {"stat", "gauge", "clock"}
    smalls = [w for w in widgets if w.get("type") in small]
    bigs = [w for w in widgets if w.get("type") not in small]
    out: list[dict[str, Any]] = []
    x = 0
    y = 0
    for w in smalls:
        size = DEFAULT_SIZE.get(w.get("type", ""), {"w": 3, "h": 2})
        if x + size["w"] > 12:
            x = 0
            y += size["h"]
        w["layout"] = {"x": x, "y": y, "w": size["w"], "h": size["h"]}
        x += size["w"]
        out.append(w)
    y = (y + 2) if smalls else 0
    x = 0
    row_h = 0
    for w in bigs:
        size = DEFAULT_SIZE.get(w.get("type", ""), {"w": 6, "h": 4})
        if x + size["w"] > 12:
            x = 0
            y += row_h
            row_h = 0
        w["layout"] = {"x": x, "y": y, "w": size["w"], "h": size["h"]}
        x += size["w"]
        row_h = max(row_h, size["h"])
        out.append(w)
    return out
