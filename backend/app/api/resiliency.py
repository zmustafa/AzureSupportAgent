"""Recovery Readiness API — recover from what, in how long, losing how much.

Reads are cache-only. ``POST /analyze`` is the only endpoint that touches Azure, and an
un-analyzed scope returns the empty shell with ``report_exists: false`` rather than a 404 —
"nothing has been analyzed" is a state the UI renders, not an error.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.durable_jobs import (
    DurableJobContext,
    DurableJobExecutor,
    JobOutcome,
    as_utc,
    utcnow,
)
from app.core.security import Principal, require_permission
from app.iam import cpu as iam_cpu
from app.models import AuditLog
from app.resiliency import analyze as analyze_mod
from app.resiliency import history as history_store
from app.resiliency import model, reference, snapshot as snapshot_store

log = logging.getLogger("app.api.resiliency")

router = APIRouter(prefix="/resiliency", tags=["resiliency"])

require_read = require_permission("resiliency.read")
require_admin = require_permission("resiliency.admin")

_job_executor = DurableJobExecutor("resiliency.analyze", event_limit=50)

#: The response NEVER carries the exception's own text. A source that failed for a reason the
#: operator can act on — an expired credential, an unreadable table — is caught inside the
#: collectors and reported as data on `provenance`, so nothing actionable is lost here; what
#: reaches this handler is an unexpected fault whose detail belongs in the server log.
ANALYSIS_FAILED = (
    "The analysis failed before it produced a snapshot. The reason is in the server log. "
    "Re-run it, and if it keeps failing narrow the scope to identify the resource involved."
)


def _public_analysis_job(durable: dict[str, Any]) -> dict[str, Any]:
    messages = [
        dict(event.get("data") or {})
        for event in durable.get("events") or []
        if event.get("event") == "progress"
    ]
    return {
        "key": durable["key"],
        "status": durable["status"],
        "started_at": durable["started_at"],
        "finished_at": durable.get("finished_at"),
        "messages": messages,
        "error": durable.get("error") or "",
    }


async def _load_analysis_job(tenant_id: str, key: str) -> dict[str, Any] | None:
    durable = await _job_executor.store.load_current(
        tenant_id=tenant_id, feature=_job_executor.feature, key=key
    )
    if durable is None:
        return None
    if durable["status"] == "running" and durable.get("lease_expires_at"):
        expires = as_utc(datetime.fromisoformat(durable["lease_expires_at"]))
        if expires is not None and expires <= utcnow():
            # Analysis has no checkpoint. A status read must not silently duplicate Azure
            # collection or overwrite a snapshot produced by a stale owner.
            await _job_executor.store.interrupt_expired(
                tenant_id=tenant_id,
                feature=_job_executor.feature,
                key=key,
                error="Recovery Readiness analysis was interrupted before completion.",
            )
            durable = await _job_executor.store.load_current(
                tenant_id=tenant_id, feature=_job_executor.feature, key=key
            )
            if durable is None:
                return None
    return _public_analysis_job(durable)


def _export_payload(principal: Principal, scope: "ScopeParams") -> tuple[dict, dict, dict]:
    """The snapshot, the objectives registry and the trend behind one export.

    Both formats read exactly this, so a reader cross-checking the PDF against the workbook
    finds the same numbers. The moment each builds its own inputs, they will drift."""
    snap = _read(principal, scope.workload_id, scope.subscription_id,
                 scope.management_group_id, scope.connection_id)
    if not snap.get("report_exists"):
        raise HTTPException(400, "Analyze this scope before exporting.")
    reference_doc = reference.load()
    # The LIVE registry, not the copy frozen into the snapshot: "has a person agreed to
    # these numbers" is a fact about now. Reading the snapshot's copy would make someone who
    # just acknowledged them re-run the whole analysis before they could export.
    if snap.get("breaches") and not reference_doc.get("targets_acknowledged"):
        # Defaults are fine on screen; a number handed to an auditor must have been agreed.
        raise HTTPException(
            409, "The recovery objectives are still the shipped defaults. Acknowledge them "
                 "in Settings before exporting a report that quotes them.")
    scope_kind, scope_id = _scope(scope.workload_id, scope.subscription_id,
                                  scope.management_group_id)
    connection = _connection(scope.connection_id, scope.workload_id) or {}
    trend_doc = history_store.trend(
        principal.tenant_id, str(connection.get("id") or ""), scope_kind, scope_id)
    return snap, reference.load(), trend_doc


def _export_name(snap: dict[str, Any], suffix: str) -> str:
    """A constant filename means three subscriptions collide in one Downloads folder."""
    from datetime import datetime, timezone

    from app.core.coverage_report_helpers import safe_filename

    scope = snap.get("scope") or {}
    label = str(scope.get("scope_name") or scope.get("scope_id") or "scope")
    stamp = str(snap.get("generated_at") or "")[:10].replace("-", "") or \
        datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"recovery-readiness-{safe_filename(label, fallback='scope')}-{stamp}.{suffix}"


# --------------------------------------------------------------------------- scope
def _scope(workload_id: str | None, subscription_id: str | None,
           management_group_id: str | None) -> tuple[str, str]:
    from app.backup_manager import service

    return service.scope_identity(workload_id, subscription_id, management_group_id)


def _connection(connection_id: str | None, workload_id: str | None) -> dict[str, Any]:
    from app.backup_manager import service

    return service.resolve_selected_connection(connection_id, workload_id)


async def _subscriptions_for(
    connection: dict[str, Any], scope: "ScopeParams", scope_kind: str, scope_id: str,
) -> list[str]:
    """Concrete subscriptions in scope. Empty for a demo workload, which has none."""
    from app.backup_manager import service
    from app.demo_catalog import is_demo_workload

    if not connection or (scope.workload_id and is_demo_workload(scope.workload_id)):
        return []
    subs = await service.scope_subscriptions(
        connection,
        workload_id=scope.workload_id,
        subscription_id=scope.subscription_id,
        management_group_id=scope.management_group_id,
    )
    return sorted(subs or [])


def _read(principal: Principal, workload_id: str | None, subscription_id: str | None,
          management_group_id: str | None, connection_id: str | None) -> dict[str, Any]:
    from app.core.azure_portal import portal_host

    scope_kind, scope_id = _scope(workload_id, subscription_id, management_group_id)
    connection = _connection(connection_id, workload_id) or {}
    snap = snapshot_store.read(
        principal.tenant_id, str(connection.get("id") or ""), scope_kind, scope_id)
    # Resolved per read, never stored: a connection can move cloud, and a stale host would
    # send a sovereign customer to the public portal. Blank for demo data — those ids look
    # real enough to build a URL, and the link would open a 404 in the reader's own tenant.
    snap["portal_host"] = "" if snap.get("demo") else portal_host(connection)
    # Same reason as portal_host: resolved per read, never trusted from the snapshot. "Has a
    # person agreed to these numbers" is a fact about NOW, and the snapshot's copy is frozen
    # at analyze time — serving that left the Acknowledge banner on screen, and the export
    # refused, until the operator re-ran the whole analysis.
    snap["targets_acknowledged"] = bool(reference.load().get("targets_acknowledged"))
    return snap


class ScopeParams:
    def __init__(
        self,
        workload_id: str | None = None,
        subscription_id: str | None = None,
        management_group_id: str | None = None,
        connection_id: str | None = None,
    ):
        self.workload_id = workload_id
        self.subscription_id = subscription_id
        self.management_group_id = management_group_id
        self.connection_id = connection_id


# --------------------------------------------------------------------------- meta
@router.get("/meta")
async def meta(_principal: Principal = Depends(require_read)) -> dict[str, Any]:
    """The vocabulary: scenarios, classes and what each means.

    Served from the server so the UI never invents wording for a state — particularly the
    difference between "no recovery path" and "unknown", which must not read alike."""
    return {
        "scenarios": [
            {"id": s, "label": model.SCENARIO_LABEL[s],
             "description": model.SCENARIO_DESCRIPTION[s],
             "redundancy_helps": model.redundancy_helps(s)}
            for s in model.SCENARIOS
        ],
        "rto_classes": [{"id": c, "label": model.RTO_LABEL[c]} for c in model.RTO_CLASSES],
        "rpo_states": list(model.RPO_STATES),
        "confidence_levels": list(model.CONFIDENCE_LEVELS),
    }


# --------------------------------------------------------------------------- analysis
@router.post("/analyze/start")
async def analyze_start(
    scope: ScopeParams = Depends(),
    principal: Principal = Depends(require_read),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.backup_manager import service

    scope_kind, scope_id = _scope(scope.workload_id, scope.subscription_id,
                                  scope.management_group_id)
    if scope_kind == "none":
        raise HTTPException(400, "Choose a workload, subscription or management group first.")
    connection = _connection(scope.connection_id, scope.workload_id) or {}
    cid = str(connection.get("id") or "")
    key = f"{principal.tenant_id}|{cid}|{scope_kind}|{scope_id}"

    running = await _load_analysis_job(principal.tenant_id, key)
    if running and running.get("status") == "running":
        return {"job": running}

    async def _run(context: DurableJobContext) -> JobOutcome:
        async def _progress(level: str, message: str) -> None:
            await context.emit(
                "progress",
                {"level": level, "message": message, "at": service.now_iso()},
            )

        lock = snapshot_store.get_lock(principal.tenant_id, cid, scope_kind, scope_id)
        async with lock:
            try:
                subs = await _subscriptions_for(connection, scope, scope_kind, scope_id)
                snap = await analyze_mod.analyze(
                    connection, tenant_id=principal.tenant_id, scope_kind=scope_kind,
                    scope_id=scope_id, subscriptions=subs,
                    workload_id=scope.workload_id or "", progress=_progress)
                await context.checkpoint()
                snapshot_store.write(principal.tenant_id, cid, scope_kind, scope_id, snap)
                # Counts only, appended per analysis. A trend cannot be reconstructed later
                # because the snapshot it would come from has already been overwritten.
                history_store.record(principal.tenant_id, cid, scope_kind, scope_id, snap)
                return JobOutcome(result={"snapshot_written": True})
            except Exception:  # noqa: BLE001 - recorded for the operator, not swallowed
                log.exception("resiliency: analysis failed")
                return JobOutcome(status="error", error=ANALYSIS_FAILED)

    claim = await _job_executor.start(
        tenant_id=principal.tenant_id,
        key=key,
        metadata={
            "connection_id": cid,
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "workload_id": scope.workload_id or "",
        },
        runner=_run,
    )
    durable = await _job_executor.store.load_current(
        tenant_id=principal.tenant_id, feature=_job_executor.feature, key=key
    )
    job = _public_analysis_job(durable or claim.job)

    db.add(AuditLog(
        tenant_id=principal.tenant_id, actor_id=principal.subject,
        action="resiliency.analyze", target=f"{scope_kind}:{scope_id}",
        metadata_json={"connection_id": cid}))
    await db.commit()
    return {"job": job}


@router.get("/analyze/job")
async def analyze_job(
    scope: ScopeParams = Depends(), principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    scope_kind, scope_id = _scope(scope.workload_id, scope.subscription_id,
                                  scope.management_group_id)
    connection = _connection(scope.connection_id, scope.workload_id) or {}
    key = f"{principal.tenant_id}|{connection.get('id') or ''}|{scope_kind}|{scope_id}"
    return {"job": await _load_analysis_job(principal.tenant_id, key)}


# --------------------------------------------------------------------------- reads
@router.get("/snapshot")
async def get_snapshot(
    scope: ScopeParams = Depends(), principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    return _read(principal, scope.workload_id, scope.subscription_id,
                 scope.management_group_id, scope.connection_id)


@router.get("/summary")
async def summary(
    scope: ScopeParams = Depends(), principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    snap = _read(principal, scope.workload_id, scope.subscription_id,
                 scope.management_group_id, scope.connection_id)
    return {
        "report_exists": snap.get("report_exists", False),
        "generated_at": snap.get("generated_at", ""),
        "demo": snap.get("demo", False),
        "summary": snap.get("summary", {}),
        "breach_summary": snap.get("breach_summary", {}),
        "provenance": snap.get("provenance", {}),
        "targets_acknowledged": snap.get("targets_acknowledged", False),
    }


@router.get("/resources")
async def resources(
    scope: ScopeParams = Depends(),
    scenario: str | None = None,
    state: str | None = Query(default=None, description="met|breached|undetermined|no_path"),
    search: str | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=2000),
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    snap = _read(principal, scope.workload_id, scope.subscription_id,
                 scope.management_group_id, scope.connection_id)
    rows = snap.get("resources") or []

    if scenario and scenario in model.SCENARIOS:
        rows = [r for r in rows if (r["verdicts"].get(scenario) or {}).get("applicable")]
    if state:
        rows = [r for r in rows if _matches_state(r, scenario, state)]
    if search:
        needle = search.strip().lower()
        rows = [r for r in rows
                if needle in r["name"].lower() or needle in r["type"].lower()]

    return {
        "report_exists": snap.get("report_exists", False),
        "generated_at": snap.get("generated_at", ""),
        "total": len(rows),
        "rows": rows[offset:offset + limit],
        "provenance": snap.get("provenance", {}),
    }


def _matches_state(row: dict[str, Any], scenario: str | None, state: str) -> bool:
    verdicts = ([row["verdicts"][scenario]] if scenario and scenario in row["verdicts"]
                else list(row["verdicts"].values()))
    for v in verdicts:
        if not v.get("applicable"):
            continue
        breach = (v.get("breach") or {}).get("state", "")
        if state == "no_path" and v.get("rto_class") == model.RTO_NONE:
            return True
        if state == breach:
            return True
    return False


@router.get("/resources/{resource_id:path}")
async def resource_detail(
    resource_id: str, scope: ScopeParams = Depends(),
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    snap = _read(principal, scope.workload_id, scope.subscription_id,
                 scope.management_group_id, scope.connection_id)
    from app.resiliency import join

    # A path parameter arrives without its leading slash, so restore it before normalising —
    # otherwise every ARM id fails the `/subscriptions/` check and the drawer 404s.
    raw = resource_id if resource_id.startswith("/") else f"/{resource_id}"
    wanted = join.normalize_resource_id(raw)
    for row in snap.get("resources") or []:
        if row["id"] == wanted:
            return {"resource": row, "generated_at": snap.get("generated_at", ""),
                    "provenance": snap.get("provenance", {})}
    raise HTTPException(404, "That resource is not in the current analysis.")


@router.get("/breaches")
async def breaches(
    scope: ScopeParams = Depends(), principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    snap = _read(principal, scope.workload_id, scope.subscription_id,
                 scope.management_group_id, scope.connection_id)
    return {"report_exists": snap.get("report_exists", False),
            "rows": snap.get("breaches") or [],
            "summary": snap.get("breach_summary") or {},
            "targets_acknowledged": snap.get("targets_acknowledged", False)}


@router.get("/workloads")
async def workloads(
    scope: ScopeParams = Depends(), principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    snap = _read(principal, scope.workload_id, scope.subscription_id,
                 scope.management_group_id, scope.connection_id)
    return {"report_exists": snap.get("report_exists", False),
            "rows": snap.get("workloads") or []}


@router.get("/analysis")
async def analysis(
    scope: ScopeParams = Depends(), principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Aggregate view: which resource TYPES are weak, and the handful of reasons why.

    Served from the same function the exports use, so the screen and the workbook cannot
    disagree about a number the reader is about to quote."""
    from app.resiliency import analysis as analysis_mod

    snap = _read(principal, scope.workload_id, scope.subscription_id,
                 scope.management_group_id, scope.connection_id)
    if not snap.get("report_exists"):
        return {"report_exists": False, "resources": 0, "by_type": [], "reasons": [],
                "rto_distribution": {}, "rpo_distribution": {}, "worst_offenders": [],
                "redundant_but_unrecoverable": []}
    facts = await iam_cpu.run(lambda: analysis_mod.analyze(snap), label="resiliency analysis")
    return {"report_exists": True, "generated_at": snap.get("generated_at", ""), **facts}


@router.get("/trend")
async def trend(
    scope: ScopeParams = Depends(), principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    scope_kind, scope_id = _scope(scope.workload_id, scope.subscription_id,
                                  scope.management_group_id)
    connection = _connection(scope.connection_id, scope.workload_id) or {}
    return history_store.trend(
        principal.tenant_id, str(connection.get("id") or ""), scope_kind, scope_id)


# --------------------------------------------------------------------------- registry
@router.get("/reference")
async def get_reference(_principal: Principal = Depends(require_read)) -> dict[str, Any]:
    """Restore rates and objectives. Visible to every reader, because a band derived from a
    constant nobody can see is not reviewable."""
    return reference.load()


class ReferenceBody(BaseModel):
    restore_rates: dict[str, int] | None = None
    mechanism_minutes: dict[str, int] | None = None
    tiers: list[dict[str, Any]] | None = Field(default=None, max_length=20)
    targets_acknowledged: bool | None = None


@router.put("/reference")
async def put_reference(
    body: ReferenceBody, principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    incoming = {k: v for k, v in body.model_dump().items() if v is not None}
    doc, rejected = reference.save(incoming, actor=principal.subject)
    db.add(AuditLog(
        tenant_id=principal.tenant_id, actor_id=principal.subject,
        action="resiliency.reference_write", target="reference",
        metadata_json={"version": doc.get("version"), "rejected": rejected}))
    await db.commit()
    return {"reference": doc, "rejected": rejected}


# --------------------------------------------------------------------------- export
XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/export")
async def export(
    scope: ScopeParams = Depends(),
    format: str = Query("xlsx", pattern="^(xlsx|pdf)$"),
    principal: Principal = Depends(require_read),
    db: AsyncSession = Depends(get_db),
):
    """One endpoint for both formats, because the objectives gate must apply to both and a
    duplicated refusal is how one copy drifts."""
    from fastapi import Response

    snap, reference_doc, trend_doc = _export_payload(principal, scope)

    if format == "pdf":
        from app.resiliency import pdf_report

        content = await iam_cpu.run(
            lambda: pdf_report.build(snap, reference_doc=reference_doc, trend=trend_doc),
            label="resiliency pdf")
        media, suffix = "application/pdf", "pdf"
    else:
        from app.resiliency import export as export_mod

        content = await iam_cpu.run(
            lambda: export_mod.build(snap, reference_doc=reference_doc, trend=trend_doc),
            label="resiliency workbook")
        media, suffix = XLSX_MEDIA, "xlsx"

    db.add(AuditLog(
        tenant_id=principal.tenant_id, actor_id=principal.subject,
        action="resiliency.export", target=str(snap.get("scope", {}).get("scope_id", "")),
        metadata_json={"format": suffix, "bytes": len(content)}))
    await db.commit()
    return Response(
        content=content,
        media_type=media,
        headers={"Content-Disposition":
                 f'attachment; filename="{_export_name(snap, suffix)}"'},
    )


class EvidenceBody(BaseModel):
    name: str = Field(default="", max_length=200)
    retention_class: str = Field(default="standard", pattern="^(standard|audit)$")


@router.post("/evidence")
async def save_evidence(
    body: EvidenceBody, scope: ScopeParams = Depends(),
    principal: Principal = Depends(require_read),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Freeze this analysis as an immutable Evidence Locker snapshot.

    Stored as content, not as a rendered PDF: a PDF's hash proves only that the file has not
    changed, while the content can be diffed against a later capture and re-rendered. Same
    objectives gate as the export — an artifact that outlives the screen must not quote
    numbers nobody agreed to."""
    from app.evidence import registry as evidence_registry
    from app.resiliency import evidence as evidence_mod

    snap, reference_doc, _trend = _export_payload(principal, scope)
    name, ev_scope, included, tags, content = await iam_cpu.run(
        lambda: evidence_mod.build_evidence_content(snap, reference_doc=reference_doc),
        label="resiliency evidence")
    meta = evidence_registry.create_snapshot(
        tenant_id=principal.tenant_id or "default",
        name=body.name or name,
        scope=ev_scope,
        included=included,
        retention_class=body.retention_class,
        tags=tags,
        content=content,
        created_by=principal.subject or "system",
        demo=bool(snap.get("demo")),
    )
    db.add(AuditLog(
        tenant_id=principal.tenant_id, actor_id=principal.subject,
        action="resiliency.evidence", target=meta["id"],
        metadata_json={"sha256": meta["sha256"],
                       "scope": str(ev_scope.get("id", "")), "name": meta["name"]}))
    await db.commit()
    return {"ok": True, "evidence": meta}

