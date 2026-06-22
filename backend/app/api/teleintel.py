"""Telemetry Intelligence endpoints — AI correlation & triage over App Insights.

NL→KQL (SSE), edited-query execution (validated read-only), AI failure triage, the cross-
signal correlation timeline, the Smart Detection aggregator, transaction reconstruction by
operation_Id, Code Optimizations, finding registration, ticketing, and a War-Room pin.
Admin-gated. Read-only — no telemetry is modified."""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import Principal, require_admin
from app.models import AuditLog
from app.teleintel import demo

router = APIRouter(prefix="/teleintel", tags=["teleintel"])
log = logging.getLogger("app.api.teleintel")


def _settings() -> tuple[int, str, int]:
    from app.core.app_settings import load_settings

    s = load_settings()
    ttl = int(s.get("teleintel_cache_ttl_s", 21600) or 21600)
    ts = str(s.get("teleintel_default_timespan", "P1D") or "P1D")
    rows = int(s.get("teleintel_max_rows", 1000) or 1000)
    return ttl, ts, rows


def _scope(workload_id: str | None, subscription_id: str | None) -> tuple[str, str]:
    if workload_id:
        return "workload", workload_id
    if subscription_id:
        return "subscription", subscription_id
    return "workload", demo.DEMO_WORKLOAD_ID


def _conn_for(scope_kind: str, scope_id: str, connection_id: str | None = None) -> dict[str, Any] | None:
    """Azure connection for a Telemetry-Intelligence scope. An explicit ``connection_id`` (the
    Azure-tenant picker) wins; otherwise a workload's OWN connection (falling back to default
    when it has none), so a workload whose Application Insights / Log Analytics lives in a
    subscription reachable only via a non-default connection still returns rows."""
    from app.core.azure_connections import connection_for_scope
    from app.workloads.registry import get_workload

    workload = get_workload(scope_id) if scope_kind == "workload" else None
    return connection_for_scope(scope_kind, connection_id=connection_id, workload=workload)


async def _resolve(principal: Principal, scope_kind: str, scope_id: str, connection_id: str | None = None) -> dict[str, Any]:
    """Resolve the component set + SLI context for a scope (demo short-circuits)."""
    from app.teleintel.resolver import resolve_components, sli_context_for_workload
    from app.workloads.registry import get_workload

    if demo.is_demo_scope(scope_kind, scope_id):
        return demo.build_overview()

    workload = get_workload(scope_id) if scope_kind == "workload" else None
    connection = _conn_for(scope_kind, scope_id, connection_id)
    res = await resolve_components(connection, scope_kind=scope_kind, scope_id=scope_id, workload=workload)
    return {
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "scope_name": (workload or {}).get("name") if workload else scope_id,
        "components": res.get("components", []),
        "predicate": res.get("predicate", ""),
        "sli_context": sli_context_for_workload(scope_id, principal.tenant_id) if scope_kind == "workload" else "",
        "connection_configured": connection is not None,
        "source": "azure_resource_graph",
        "demo": False,
        "error": res.get("error", ""),
    }


def _pick_component(overview: dict[str, Any], component_id: str | None) -> dict[str, Any] | None:
    comps = overview.get("components", []) or []
    if component_id:
        for c in comps:
            if c.get("id") == component_id:
                return c
    return comps[0] if comps else None


# ----------------------------------------------------------------------- overview
@router.get("/overview")
async def overview(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    scope_kind, scope_id = _scope(workload_id, subscription_id)
    return await _resolve(principal, scope_kind, scope_id, connection_id)


# ----------------------------------------------------------------------- NL → KQL (SSE)
class AskRequest(BaseModel):
    question: str = Field(min_length=1)
    workload_id: str | None = None
    subscription_id: str | None = None
    connection_id: str | None = None
    component_id: str | None = None


@router.post("/ask")
async def ask(payload: AskRequest, principal: Principal = Depends(require_admin)):
    """NL→KQL over SSE: draft → validate → run → answer (start → kql → rows → answer/error)."""
    from app.teleintel.nlkql import draft_kql, narrate_answer, validate_kql
    from app.teleintel.resolver import run_component_kql

    scope_kind, scope_id = _scope(payload.workload_id, payload.subscription_id)
    _ttl, default_ts, max_rows = _settings()

    async def _gen():
        try:
            overview = await _resolve(principal, scope_kind, scope_id, payload.connection_id)
            sli = overview.get("sli_context", "")
            yield {"event": "start", "data": json.dumps({"question": payload.question})}

            drafted = await draft_kql(payload.question, sli_context=sli, default_timespan=default_ts)
            if drafted.get("error") or not drafted.get("kql"):
                yield {"event": "error", "data": json.dumps({"message": drafted.get("error") or "Could not draft a query."})}
                return
            clean, verr = validate_kql(drafted["kql"], max_rows=max_rows)
            if verr:
                yield {"event": "error", "data": json.dumps({"message": f"Generated query rejected: {verr}"})}
                return
            yield {"event": "kql", "data": json.dumps({"kql": clean, "explanation": drafted.get("explanation", "")})}

            if demo.is_demo_scope(scope_kind, scope_id):
                ex = demo.demo_nlkql_example()
                yield {"event": "rows", "data": json.dumps({"rows": ex["rows"], "path": "demo"})}
                yield {"event": "answer", "data": json.dumps({"answer": ex["answer"]})}
                return

            comp = _pick_component(overview, payload.component_id)
            if comp is None:
                yield {"event": "error", "data": json.dumps({"message": "No Application Insights component in scope."})}
                return
            connection = _conn_for(scope_kind, scope_id, payload.connection_id)
            res = await run_component_kql(comp, clean, connection, timespan=default_ts)
            if not res.get("ok"):
                yield {"event": "error", "data": json.dumps({"message": res.get("error", "Query failed.")})}
                return
            rows = res.get("rows", [])[:max_rows]
            yield {"event": "rows", "data": json.dumps({"rows": rows, "path": res.get("path", "")})}
            answer = await narrate_answer(payload.question, clean, rows)
            yield {"event": "answer", "data": json.dumps({"answer": answer})}
        except Exception as exc:  # noqa: BLE001
            log.exception("teleintel ask failed")
            yield {"event": "error", "data": json.dumps({"message": str(exc)[:300]})}

    return EventSourceResponse(_gen())


# ----------------------------------------------------------------------- edited query
class QueryRequest(BaseModel):
    kql: str = Field(min_length=1)
    workload_id: str | None = None
    subscription_id: str | None = None
    component_id: str | None = None


@router.post("/query")
async def run_query(payload: QueryRequest, principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    """Run an edited KQL directly (validated read-only) for transparency / re-run."""
    from app.teleintel.nlkql import validate_kql
    from app.teleintel.resolver import run_component_kql

    scope_kind, scope_id = _scope(payload.workload_id, payload.subscription_id)
    _ttl, default_ts, max_rows = _settings()
    clean, verr = validate_kql(payload.kql, max_rows=max_rows)
    if verr:
        return {"ok": False, "error": verr, "kql": payload.kql}
    if demo.is_demo_scope(scope_kind, scope_id):
        ex = demo.demo_nlkql_example()
        return {"ok": True, "kql": clean, "rows": ex["rows"], "path": "demo"}
    overview = await _resolve(principal, scope_kind, scope_id)
    comp = _pick_component(overview, payload.component_id)
    if comp is None:
        return {"ok": False, "error": "No Application Insights component in scope.", "kql": clean}
    res = await run_component_kql(comp, clean, _conn_for(scope_kind, scope_id), timespan=default_ts)
    return {"ok": res.get("ok"), "kql": clean, "rows": res.get("rows", []), "error": res.get("error", ""), "path": res.get("path", "")}


# ----------------------------------------------------------------------- triage
@router.get("/triage")
async def triage(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    connection_id: str | None = Query(default=None),
    component_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    scope_kind, scope_id = _scope(workload_id, subscription_id)
    if demo.is_demo_scope(scope_kind, scope_id):
        return demo.demo_triage()
    from app.teleintel.triage import run_triage

    _ttl, default_ts, _rows = _settings()
    overview = await _resolve(principal, scope_kind, scope_id, connection_id)
    comp = _pick_component(overview, component_id)
    if comp is None:
        return {"error": "No Application Insights component in scope.", "has_spike": False, "evidence": []}
    return await run_triage(
        comp, _conn_for(scope_kind, scope_id, connection_id),
        predicate=overview.get("predicate", ""), timespan=default_ts, sli_context=overview.get("sli_context", ""),
    )


# ----------------------------------------------------------------------- timeline
@router.get("/timeline")
async def timeline(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    connection_id: str | None = Query(default=None),
    component_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    scope_kind, scope_id = _scope(workload_id, subscription_id)
    if demo.is_demo_scope(scope_kind, scope_id):
        return demo.demo_timeline()
    from app.teleintel.timeline import build_timeline

    _ttl, default_ts, _rows = _settings()
    overview = await _resolve(principal, scope_kind, scope_id, connection_id)
    comp = _pick_component(overview, component_id)
    if comp is None:
        return {"series_keys": [], "points": [], "change_events": [], "signal_count": 0, "notes": "No component in scope."}
    return await build_timeline(comp, _conn_for(scope_kind, scope_id, connection_id), predicate=overview.get("predicate", ""), timespan=default_ts)


# ----------------------------------------------------------------------- smart detection
@router.get("/smart-detection")
async def smart_detection(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    scope_kind, scope_id = _scope(workload_id, subscription_id)
    if demo.is_demo_scope(scope_kind, scope_id):
        return demo.demo_smart_detection()
    from app.teleintel.smartdetect import aggregate

    overview = await _resolve(principal, scope_kind, scope_id, connection_id)
    return await aggregate(overview.get("components", []), _conn_for(scope_kind, scope_id, connection_id))


# ----------------------------------------------------------------------- transaction
class TransactionRequest(BaseModel):
    operation_id: str = Field(min_length=1)
    workload_id: str | None = None
    subscription_id: str | None = None
    component_id: str | None = None


@router.post("/transaction")
async def transaction(payload: TransactionRequest, principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    scope_kind, scope_id = _scope(payload.workload_id, payload.subscription_id)
    if demo.is_demo_scope(scope_kind, scope_id):
        return demo.demo_transaction()
    from app.teleintel.transaction import explain_transaction

    _ttl, default_ts, _rows = _settings()
    overview = await _resolve(principal, scope_kind, scope_id)
    comp = _pick_component(overview, payload.component_id)
    if comp is None:
        return {"ok": False, "error": "No Application Insights component in scope.", "spans": []}
    return await explain_transaction(comp, payload.operation_id, _conn_for(scope_kind, scope_id), timespan=default_ts)


# ----------------------------------------------------------------------- code optimizations
@router.get("/code-optimizations")
async def code_optimizations(
    workload_id: str | None = Query(default=None),
    subscription_id: str | None = Query(default=None),
    connection_id: str | None = Query(default=None),
    component_id: str | None = Query(default=None),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    scope_kind, scope_id = _scope(workload_id, subscription_id)
    if demo.is_demo_scope(scope_kind, scope_id):
        return demo.demo_code_optimizations()
    from app.teleintel.code_opt import code_optimizations as fetch

    overview = await _resolve(principal, scope_kind, scope_id, connection_id)
    comp = _pick_component(overview, component_id)
    if comp is None:
        return {"items": [], "note": "No component in scope."}
    return await fetch(comp, _conn_for(scope_kind, scope_id, connection_id))


# ----------------------------------------------------------------------- findings
class RegisterFindingRequest(BaseModel):
    workload_id: str
    workload_name: str = ""
    triage: dict[str, Any]


@router.post("/findings/register")
async def register_finding(
    payload: RegisterFindingRequest, principal: Principal = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Register a triage conclusion as Performance+Reliability findings via a lightweight
    AssessmentRun, so it feeds existing scoring / finding-state / waivers."""
    from datetime import datetime, timezone

    from app.models import AssessmentRun

    s = payload.triage.get("summary", {}) if isinstance(payload.triage, dict) else {}
    op = s.get("operation", "operation")
    rate = s.get("failure_rate_pct", 0)
    dep = s.get("top_dependency", "")
    sev = "critical" if (rate or 0) >= 25 else "error" if (rate or 0) >= 5 else "warning"
    finding = {
        "check_id": f"teleintel_{str(op).lower().replace(' ', '_').replace('/', '_')}"[:64],
        "pillar": "reliability",
        "title": f"Failure spike on {op} ({rate}%)",
        "description": payload.triage.get("hypothesis", "") if isinstance(payload.triage, dict) else "",
        "severity": sev,
        "weight": 0,
        "frameworks": {},
        "remediation": f"Investigate dependency {dep}; review recent deploys ({s.get('probable_trigger', 'n/a')}).",
        "remediation_command": "",
        "resource_types": [],
        "status": "fail",
        "flagged_count": 1,
        "flagged_resources": [{"id": s.get("trigger_target", ""), "name": op, "type": "operation"}],
        "ai_rationale": payload.triage.get("hypothesis", "") if isinstance(payload.triage, dict) else "",
    }
    now = datetime.now(timezone.utc)
    run = AssessmentRun(
        workload_id=payload.workload_id, workload_name=payload.workload_name or payload.workload_id,
        tenant_id=principal.tenant_id, pillars=["performance", "reliability"], status="succeeded", overall_score=None,
        scores_json={}, totals_json={"passed": 0, "failed": 1, "na": 0, "waived": 0, "by_severity": {sev: 1}},
        severity=sev, findings_json=[finding], resource_count=1, resources_json=[],
        summary=f"Telemetry Intelligence: failure-triage finding on {op}.",
        used_ai=True, triggered_by=principal.subject, trigger="teleintel", started_at=now, ended_at=now,
    )
    db.add(run)
    db.add(AuditLog(tenant_id=principal.tenant_id, actor_id=principal.subject, action="teleintel.findings.register", target=payload.workload_id, metadata_json={"operation": op}))
    await db.commit()
    await db.refresh(run)
    return {"ok": True, "run_id": run.id, "finding_count": 1}


# ----------------------------------------------------------------------- ticketing
class TicketRequest(BaseModel):
    connector_id: str = Field(min_length=1)
    triage: dict[str, Any]


@router.post("/ticket")
async def create_teleintel_ticket(
    payload: TicketRequest, principal: Principal = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    from app.assessments.tickets import create_ticket

    s = payload.triage.get("summary", {}) if isinstance(payload.triage, dict) else {}
    op = s.get("operation", "operation")
    evidence = payload.triage.get("evidence", []) if isinstance(payload.triage, dict) else []
    kql_block = "\n\n".join(f"-- {e.get('label', '')}\n{e.get('kql', '')}" for e in evidence[:3])
    finding = {
        "severity": "critical" if (s.get("failure_rate_pct", 0) or 0) >= 25 else "error",
        "title": f"Failure spike: {op} ({s.get('failure_rate_pct', 0)}%)",
        "check_id": f"teleintel_{op}",
        "pillar": "reliability",
        "description": (payload.triage.get("hypothesis", "") if isinstance(payload.triage, dict) else "")
        + (f"\n\nEvidence queries:\n{kql_block}" if kql_block else ""),
        "remediation": f"Investigate dependency {s.get('top_dependency', '')}; review {s.get('probable_trigger', 'recent deploys')}.",
    }
    result = await create_ticket(connector_id=payload.connector_id, finding=finding, workload_name=op)
    if result.get("ok"):
        db.add(AuditLog(tenant_id=principal.tenant_id, actor_id=principal.subject, action="teleintel.ticket.create", target=str(op)[:512], metadata_json={"ticket": result.get("ticket_id", "")}))
        await db.commit()
    return result


# ----------------------------------------------------------------------- demo
@router.post("/demo/seed")
async def seed_demo_endpoint(principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    demo.ensure_demo()
    return {"ok": True, "workload_id": demo.DEMO_WORKLOAD_ID, "component": demo.DEMO_COMPONENT["name"]}
