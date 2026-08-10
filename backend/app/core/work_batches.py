"""Generic durable control plane for non-profiler fleet and background work.

A browser submits one batch and may disappear. SQL item rows own admission, progress, retries,
cancellation and restart recovery; feature-specific executors keep results in their existing
native stores. Performance Profiler deliberately remains on its proven dedicated tables.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, or_, select, update

from app.models import WorkBatch, WorkBatchItem

log = logging.getLogger("app.core.work_batches")

TERMINAL = {"succeeded", "partial", "failed", "cancelled"}
ACTIVE = {"queued", "running"}
FEATURES = {
    "assessment",
    "changeexplorer",
    "coverage_amba",
    "coverage_telemetry",
    "coverage_backupdr",
    "backup_manager",
    "architecture",
    "mission",
    "inventory_cost",
    "deep_review",
    "nightly",
}
_FEATURE_LIMITS = {
    "assessment": 2,
    "changeexplorer": 2,
    "coverage_amba": 3,
    "coverage_telemetry": 3,
    "coverage_backupdr": 3,
    "backup_manager": 2,
    "architecture": 2,
    "mission": 2,
    "inventory_cost": 1,
    "deep_review": 2,
    "nightly": 1,
}
_TRANSIENT_MARKERS = (
    "429",
    "rate limit",
    "too many requests",
    "throttl",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "connection reset",
    "server busy",
    "502",
    "503",
    "504",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _transient(message: str) -> bool:
    low = (message or "").lower()
    return any(marker in low for marker in _TRANSIENT_MARKERS)


@dataclass
class ItemResult:
    status: str = "succeeded"
    message: str = "Complete."
    result_ref: dict[str, Any] | None = None
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    retryable: bool = False


def item_public(item: WorkBatchItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "batch_id": item.batch_id,
        "item_key": item.item_key,
        "workload_id": item.workload_id or "",
        "workload_name": item.workload_name,
        "connection_id": item.connection_id or "",
        "status": item.status,
        "attempt": item.attempt,
        "max_attempts": item.max_attempts,
        "progress_current": item.progress_current,
        "progress_total": item.progress_total,
        "message": item.message,
        "result_ref": item.result_ref,
        "result": item.result_json or {},
        "error": item.error or "",
        "retryable": item.retryable,
        "available_at": _iso(item.available_at),
        "started_at": _iso(item.started_at),
        "ended_at": _iso(item.ended_at),
        "duration_ms": item.duration_ms,
    }


def batch_public(batch: WorkBatch, items: list[WorkBatchItem]) -> dict[str, Any]:
    return {
        "id": batch.id,
        "feature": batch.feature,
        "status": batch.status,
        "config": batch.config_json or {},
        "total": batch.total,
        "completed": batch.completed,
        "succeeded": batch.succeeded,
        "partial": batch.partial,
        "failed": batch.failed,
        "cancelled": batch.cancelled,
        "cancel_requested": batch.cancel_requested,
        "error": batch.error or "",
        "triggered_by": batch.triggered_by,
        "trigger": batch.trigger,
        "created_at": _iso(batch.created_at),
        "started_at": _iso(batch.started_at),
        "ended_at": _iso(batch.ended_at),
        "items": [item_public(item) for item in items],
    }


async def refresh_batch(db, batch_id: str) -> WorkBatch | None:
    batch = await db.get(WorkBatch, batch_id)
    if batch is None:
        return None
    items = list(
        (
            await db.execute(select(WorkBatchItem).where(WorkBatchItem.batch_id == batch_id))
        ).scalars().all()
    )
    counts = {status: 0 for status in TERMINAL}
    for item in items:
        if item.status in counts:
            counts[item.status] += 1
    batch.total = len(items)
    batch.completed = sum(counts.values())
    batch.succeeded = counts["succeeded"]
    batch.partial = counts["partial"]
    batch.failed = counts["failed"]
    batch.cancelled = counts["cancelled"]
    if items and batch.completed == len(items):
        if counts["cancelled"] == len(items):
            batch.status = "cancelled"
        elif counts["failed"] == len(items):
            batch.status = "failed"
        elif counts["failed"] or counts["partial"] or counts["cancelled"]:
            batch.status = "partial"
        else:
            batch.status = "succeeded"
        batch.ended_at = batch.ended_at or _now()
        batch.error = next((item.error for item in items if item.error), None)
    elif any(item.status == "running" for item in items):
        batch.status = "running"
        batch.started_at = batch.started_at or _now()
    else:
        batch.status = "queued"
    return batch


async def create_batch(
    *,
    tenant_id: str,
    feature: str,
    actor: str,
    idempotency_key: str,
    items: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
    trigger: str = "manual",
    start_worker: bool = True,
) -> tuple[dict[str, Any], bool]:
    """Create a durable batch or return the existing idempotent request."""
    from app.core.db import SessionLocal

    if feature not in FEATURES:
        raise ValueError(f"Unsupported batch feature: {feature}")
    if not items:
        raise ValueError("A work batch needs at least one item.")
    clean_key = (idempotency_key or "").strip()
    if not clean_key or len(clean_key) > 128:
        raise ValueError("idempotency_key must contain 1-128 characters.")

    async with SessionLocal() as db:
        existing = (
            await db.execute(
                select(WorkBatch).where(
                    WorkBatch.tenant_id == tenant_id,
                    WorkBatch.feature == feature,
                    WorkBatch.idempotency_key == clean_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            rows = list(
                (
                    await db.execute(
                        select(WorkBatchItem)
                        .where(WorkBatchItem.batch_id == existing.id)
                        .order_by(WorkBatchItem.workload_name, WorkBatchItem.item_key)
                    )
                ).scalars().all()
            )
            return batch_public(existing, rows), False

        batch = WorkBatch(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id or "default",
            feature=feature,
            idempotency_key=clean_key,
            status="queued",
            config_json=dict(config or {}),
            total=len(items),
            triggered_by=actor,
            trigger=trigger,
        )
        db.add(batch)
        seen: set[str] = set()
        for descriptor in items:
            item_key = str(descriptor.get("item_key") or descriptor.get("workload_id") or "").strip()
            if not item_key or item_key in seen:
                continue
            seen.add(item_key)
            db.add(
                WorkBatchItem(
                    id=str(uuid.uuid4()),
                    batch_id=batch.id,
                    tenant_id=batch.tenant_id,
                    item_key=item_key[:256],
                    workload_id=str(descriptor.get("workload_id") or "")[:128] or None,
                    workload_name=str(descriptor.get("workload_name") or descriptor.get("name") or item_key)[:256],
                    connection_id=str(descriptor.get("connection_id") or "")[:128] or None,
                    status="queued",
                    max_attempts=max(1, min(int(descriptor.get("max_attempts") or 3), 5)),
                    result_json=dict(descriptor.get("result") or {}),
                )
            )
        await db.commit()
        await db.refresh(batch)
        rows = list(
            (
                await db.execute(
                    select(WorkBatchItem)
                    .where(WorkBatchItem.batch_id == batch.id)
                    .order_by(WorkBatchItem.workload_name, WorkBatchItem.item_key)
                )
            ).scalars().all()
        )
        if not rows:
            await db.delete(batch)
            await db.commit()
            raise ValueError("A work batch needs at least one unique item.")
        batch.total = len(rows)
        await db.commit()
        response = batch_public(batch, rows)
    if start_worker:
        await worker.ensure_running()
    return response, True


async def get_batch(batch_id: str, tenant_id: str) -> dict[str, Any] | None:
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        batch = await db.get(WorkBatch, batch_id)
        if batch is None or batch.tenant_id != tenant_id:
            return None
        rows = list(
            (
                await db.execute(
                    select(WorkBatchItem)
                    .where(WorkBatchItem.batch_id == batch.id)
                    .order_by(WorkBatchItem.started_at, WorkBatchItem.workload_name, WorkBatchItem.item_key)
                )
            ).scalars().all()
        )
        return batch_public(batch, rows)


async def latest_batch(tenant_id: str, feature: str, *, active_only: bool = False) -> dict[str, Any] | None:
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        stmt = select(WorkBatch).where(WorkBatch.tenant_id == tenant_id, WorkBatch.feature == feature)
        if active_only:
            stmt = stmt.where(WorkBatch.status.in_(ACTIVE))
        batch = (
            await db.execute(stmt.order_by(WorkBatch.created_at.desc()).limit(1))
        ).scalar_one_or_none()
        if batch is None:
            return None
        rows = list(
            (
                await db.execute(
                    select(WorkBatchItem)
                    .where(WorkBatchItem.batch_id == batch.id)
                    .order_by(WorkBatchItem.started_at, WorkBatchItem.workload_name, WorkBatchItem.item_key)
                )
            ).scalars().all()
        )
        return batch_public(batch, rows)


async def latest_batch_for_config(
    tenant_id: str,
    feature: str,
    expected: dict[str, Any],
    *,
    active_only: bool = False,
) -> dict[str, Any] | None:
    """Newest feature batch whose config contains the requested exact key/value pairs."""
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        stmt = select(WorkBatch).where(WorkBatch.tenant_id == tenant_id, WorkBatch.feature == feature)
        if active_only:
            stmt = stmt.where(WorkBatch.status.in_(ACTIVE))
        batches = list(
            (
                await db.execute(stmt.order_by(WorkBatch.created_at.desc()).limit(100))
            ).scalars().all()
        )
        batch = next(
            (
                row for row in batches
                if all((row.config_json or {}).get(key) == value for key, value in expected.items())
            ),
            None,
        )
        if batch is None:
            return None
        rows = list(
            (
                await db.execute(
                    select(WorkBatchItem)
                    .where(WorkBatchItem.batch_id == batch.id)
                    .order_by(WorkBatchItem.started_at, WorkBatchItem.workload_name, WorkBatchItem.item_key)
                )
            ).scalars().all()
        )
        return batch_public(batch, rows)


async def cancel_batch(batch_id: str, tenant_id: str) -> bool:
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        batch = await db.get(WorkBatch, batch_id)
        if batch is None or batch.tenant_id != tenant_id or batch.status in TERMINAL:
            return False
        batch.cancel_requested = True
        await db.execute(
            update(WorkBatchItem)
            .where(WorkBatchItem.batch_id == batch_id, WorkBatchItem.status == "queued")
            .values(status="cancelled", ended_at=_now(), message="Cancelled before start.", error="Cancelled before start.")
        )
        await refresh_batch(db, batch_id)
        await db.commit()
    worker.wake()
    return True


async def delete_batch(batch_id: str, tenant_id: str) -> bool:
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        batch = await db.get(WorkBatch, batch_id)
        if batch is None or batch.tenant_id != tenant_id or batch.status not in TERMINAL:
            return False
        await db.execute(delete(WorkBatchItem).where(WorkBatchItem.batch_id == batch_id))
        await db.delete(batch)
        await db.commit()
        return True


async def retry_batch(batch_id: str, tenant_id: str, actor: str, idempotency_key: str) -> dict[str, Any] | None:
    original = await get_batch(batch_id, tenant_id)
    if original is None:
        return None
    retry_items = [
        {
            "item_key": item["item_key"],
            "workload_id": item["workload_id"],
            "workload_name": item["workload_name"],
            "connection_id": item["connection_id"],
            "max_attempts": item["max_attempts"],
            "result": item.get("result") or {},
        }
        for item in original["items"]
        if item["status"] in {"failed", "partial", "cancelled"}
    ]
    if not retry_items:
        return original
    created, _ = await create_batch(
        tenant_id=tenant_id,
        feature=original["feature"],
        actor=actor,
        idempotency_key=idempotency_key,
        items=retry_items,
        config=original["config"],
        trigger="retry",
    )
    return created


async def recover_interrupted() -> int:
    """Requeue running items; terminal siblings remain untouched."""
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        running_ids = list(
            (
                await db.execute(select(WorkBatchItem.id).where(WorkBatchItem.status == "running"))
            ).scalars().all()
        )
        if running_ids:
            await db.execute(
                update(WorkBatchItem)
                .where(WorkBatchItem.id.in_(running_ids))
                .values(
                    status="queued",
                    started_at=None,
                    available_at=None,
                    retryable=True,
                    message="Requeued after server restart.",
                    error="Requeued after server restart.",
                )
            )
        batch_ids = list(
            (
                await db.execute(select(WorkBatch.id).where(WorkBatch.status.in_(ACTIVE)))
            ).scalars().all()
        )
        for batch_id in batch_ids:
            batch = await db.get(WorkBatch, batch_id)
            if batch and batch.cancel_requested:
                await db.execute(
                    update(WorkBatchItem)
                    .where(WorkBatchItem.batch_id == batch_id, WorkBatchItem.status == "queued")
                    .values(status="cancelled", ended_at=_now(), error="Cancelled before restart.")
                )
            await refresh_batch(db, batch_id)
        await db.commit()
        return len(running_ids)


async def update_item_progress(
    item_id: str,
    *,
    current: int | None = None,
    total: int | None = None,
    message: str | None = None,
    status: str | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        item = await db.get(WorkBatchItem, item_id)
        if item is None:
            return
        if current is not None:
            item.progress_current = max(0, current)
        if total is not None:
            item.progress_total = max(0, total)
        if message is not None:
            item.message = message[:2000]
        if status is not None:
            item.status = status
        if result is not None:
            item.result_json = result
        await db.commit()


async def _set_result_ref(item_id: str, result_ref: dict[str, Any]) -> None:
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        item = await db.get(WorkBatchItem, item_id)
        if item is not None:
            item.result_ref = result_ref
            await db.commit()


async def _load_item_batch(item_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        item = await db.get(WorkBatchItem, item_id)
        if item is None:
            return None
        batch = await db.get(WorkBatch, item.batch_id)
        if batch is None:
            return None
        return item_public(item), batch_public(batch, [])


def _principal(batch: dict[str, Any]):
    from app.core.security import Principal

    return Principal(
        subject=batch["triggered_by"] or "batch-worker",
        email="",
        tenant_id=batch.get("tenant_id") or "default",
        role="admin",
        display_name=batch["triggered_by"] or "Batch worker",
    )


async def _execute_assessment(item: dict[str, Any], batch: dict[str, Any]) -> ItemResult:
    from app.assessments import catalog
    from app.assessments.runner import run_assessment_to_completion
    from app.core.db import SessionLocal
    from app.models import AssessmentRun

    config = batch["config"]
    pillars = config.get("pillars") or list(catalog.PILLARS)
    pack = str(config.get("pack") or "")
    if pack:
        pillars = catalog.pack_pillars(pack) or pillars
    run_id = str((item.get("result_ref") or {}).get("id") or "")
    if not run_id:
        run_id = item["id"]
        async with SessionLocal() as db:
            if await db.get(AssessmentRun, run_id) is None:
                db.add(AssessmentRun(
                    id=run_id,
                    workload_id=item["workload_id"],
                    workload_name=item["workload_name"],
                    tenant_id=batch["tenant_id"],
                    connection_id=item["connection_id"] or None,
                    pillars=pillars,
                    status="queued",
                    triggered_by=batch["triggered_by"],
                    trigger=f"batch:{batch['id']}:{item['id']}",
                ))
                await db.commit()
        await _set_result_ref(item["id"], {"kind": "assessment", "id": run_id})
    async with SessionLocal() as db:
        existing = await db.get(AssessmentRun, run_id)
        if existing is not None and existing.status == "succeeded":
            return ItemResult(result_ref={"kind": "assessment", "id": run_id}, message="Assessment already complete.")
    await run_assessment_to_completion(
        run_id=run_id,
        workload_id=item["workload_id"],
        pillars=pillars,
        tenant_id=batch["tenant_id"],
        connection_id=item["connection_id"] or None,
        actor=batch["triggered_by"],
        trigger=f"batch:{batch['id']}:{item['id']}",
        use_ai=bool(config.get("use_ai", True)),
    )
    async with SessionLocal() as db:
        row = await db.get(AssessmentRun, run_id)
        if row is None:
            raise RuntimeError("Assessment result row disappeared.")
        status = row.status if row.status in {"succeeded", "failed", "cancelled"} else "failed"
        return ItemResult(
            status=status,
            message=f"Assessment {status}.",
            result_ref={"kind": "assessment", "id": run_id},
            result={"score": row.overall_score, "completeness_pct": row.completeness_pct},
            error=str(row.error or "") if status != "succeeded" else "",
            retryable=_transient(str(row.error or "")),
        )


async def _execute_changeexplorer(item: dict[str, Any], batch: dict[str, Any]) -> ItemResult:
    from app.changeexplorer import demo, runs as runs_store, service
    from app.core.azure_connections import connection_for_workload, resolve_connection
    from app.workloads.registry import get_workload

    run_id = item["id"]
    stored = runs_store.get_run(batch["tenant_id"], run_id)
    if stored is not None:
        return ItemResult(message="Change analysis already complete.", result_ref={"kind": "changeexplorer", "id": run_id})
    workload = get_workload(item["workload_id"])
    if workload is None:
        raise RuntimeError("Workload no longer exists.")
    connection = resolve_connection(item["connection_id"]) if item["connection_id"] else connection_for_workload(workload)
    config = batch["config"]
    run = await service.analyze(
        tenant_id=batch["tenant_id"],
        workload=workload,
        connection=connection,
        start_iso=str(config.get("start_time") or ""),
        end_iso=str(config.get("end_time") or ""),
        scope_mode=str(config.get("scope_mode") or "workload"),
        requested_by=batch["triggered_by"],
        force_demo=demo.is_demo(item["workload_id"]),
        run_ai=bool(config.get("run_ai", True)),
        run_id=run_id,
    )
    runs_store.save_run(batch["tenant_id"], item["workload_id"], run)
    return ItemResult(
        status="succeeded",
        message=f"Analyzed {int(run.get('totalChanges') or 0)} change(s).",
        result_ref={"kind": "changeexplorer", "id": run_id},
        result={"total_changes": int(run.get("totalChanges") or 0)},
    )


async def _execute_coverage(item: dict[str, Any], batch: dict[str, Any]) -> ItemResult:
    from app.core.db import SessionLocal

    feature = batch["feature"]
    principal = _principal(batch)
    kwargs = {
        "workload_id": item["workload_id"],
        "subscription_id": None,
        "connection_id": item["connection_id"] or None,
        "principal": principal,
    }
    async with SessionLocal() as db:
        if feature == "coverage_amba":
            from app.api.amba import refresh

            snapshot = await refresh(**kwargs, db=db)
            headline = snapshot.get("coverage_pct")
        elif feature == "coverage_telemetry":
            from app.api.telemetry import refresh

            snapshot = await refresh(**kwargs, db=db)
            headline = snapshot.get("coverage_pct")
        else:
            from app.api.backupdr import refresh

            snapshot = await refresh(**kwargs, db=db)
            headline = (snapshot.get("scorecard") or {}).get("pct_protected")
    error = str(snapshot.get("scan_error") or snapshot.get("error") or "")
    errors = snapshot.get("errors") or {}
    status = "failed" if error else "partial" if errors else "succeeded"
    return ItemResult(
        status=status,
        message=f"Coverage scan {status}.",
        result_ref={"kind": feature, "id": item["workload_id"]},
        result={"headline": headline, "errors": errors},
        error=error or ("; ".join(map(str, errors.values())) if isinstance(errors, dict) else ""),
        retryable=_transient(error),
    )


async def _execute_backup_manager(item: dict[str, Any], batch: dict[str, Any]) -> ItemResult:
    from app.api.backup_manager import run_refresh_analysis

    snapshot = await run_refresh_analysis(
        principal=_principal(batch),
        connection_id=item["connection_id"],
        workload_id=item["workload_id"],
        progress=lambda level, message: update_item_progress(item["id"], message=message),
    )
    errors = snapshot.get("errors") or {}
    status = "partial" if snapshot.get("partial") or errors else "succeeded"
    return ItemResult(
        status=status,
        message="Backup Manager analysis complete.",
        result_ref={"kind": "backup_manager", "id": item["workload_id"]},
        result={"counts": snapshot.get("counts") or {}, "errors": errors},
        error="; ".join(map(str, errors.values())) if isinstance(errors, dict) else "",
        retryable=any(_transient(str(value)) for value in errors.values()) if isinstance(errors, dict) else False,
    )


async def _execute_architecture(item: dict[str, Any], batch: dict[str, Any]) -> ItemResult:
    from app.architectures import registry
    from app.architectures.jobs import manager

    target_id = str(batch["config"].get("target_architecture_id") or item["id"])
    existing = registry.get_architecture(item["id"])
    if target_id == item["id"] and existing is not None:
        return ItemResult(message="Architecture already generated.", result_ref={"kind": "architecture", "id": item["id"]})
    job = manager.create(
        tenant_id=batch["tenant_id"],
        workload_id=item["workload_id"],
        workload_name=item["workload_name"],
        connection_id=item["connection_id"],
        created_by=batch["triggered_by"],
        target_architecture_id=target_id,
    )
    while True:
        current = manager.get(job["id"], batch["tenant_id"])
        if current is None:
            raise RuntimeError("Architecture generation job disappeared.")
        await update_item_progress(item["id"], current=current.get("progress", 0), total=100, message=current.get("message", ""))
        if current["status"] in {"done", "error", "canceled"}:
            if current["status"] != "done":
                return ItemResult(status="cancelled" if current["status"] == "canceled" else "failed", error=current.get("error") or current.get("message") or "Architecture generation failed.", retryable=_transient(current.get("error") or ""))
            return ItemResult(
                message="Architecture generated.",
                result_ref={"kind": "architecture", "id": current.get("architecture_id") or target_id},
                result={"resource_count": current.get("resource_count", 0)},
            )
        await asyncio.sleep(0.5)


async def _execute_mission(item: dict[str, Any], batch: dict[str, Any]) -> ItemResult:
    from app.missions import orchestrator

    config = batch["config"]
    mission = orchestrator.manager.create(
        tenant_id=batch["tenant_id"],
        workload_id=item["workload_id"],
        workload_name=item["workload_name"],
        connection_id=item["connection_id"],
        actor=batch["triggered_by"],
        force=bool(config.get("force", False)),
        trigger="fleet",
        system_keys=list(config.get("systems") or []),
        mission_id=item["id"],
    )
    await _set_result_ref(item["id"], {"kind": "mission", "id": mission["id"]})
    while True:
        current = await orchestrator.get_mission(mission["id"], batch["tenant_id"])
        if current is None:
            raise RuntimeError("Mission result disappeared.")
        await update_item_progress(
            item["id"],
            current=int(current.get("systems_done") or 0),
            total=int(current.get("systems_total") or 0),
            message=f"Mission {current.get('status', 'running')}.",
        )
        if current.get("status") in TERMINAL:
            status = str(current["status"])
            return ItemResult(
                status=status,
                message=f"Mission {status}.",
                result_ref={"kind": "mission", "id": mission["id"]},
                result={"readiness": current.get("readiness"), "systems_attention": current.get("systems_attention", 0)},
                error=str(current.get("error") or "") if status in {"failed", "partial"} else "",
                retryable=_transient(str(current.get("error") or "")),
            )
        await asyncio.sleep(0.75)


async def _execute_deep_review(item: dict[str, Any], batch: dict[str, Any]) -> ItemResult:
    from app.api.chats import run_deep_review_workload

    result = await run_deep_review_workload(
        workload_id=item["workload_id"],
        principal=_principal(batch),
        chat_id=item["id"],
    )
    return ItemResult(
        status="succeeded" if result.get("ok") else "failed",
        message="Deep review complete." if result.get("ok") else "Deep review failed.",
        result_ref={"kind": "chat", "id": result.get("chat_id") or item["id"]},
        error=str(result.get("error") or ""),
        retryable=_transient(str(result.get("error") or "")),
    )


async def _execute_nightly(item: dict[str, Any], batch: dict[str, Any]) -> ItemResult:
    from app.core.app_settings import load_settings
    from app.workloads import nightly, profile
    from app.workloads.registry import get_workload

    principal = nightly._admin_principal(batch["tenant_id"])
    signals = await nightly._refresh_one(principal, item["workload_id"], item["connection_id"])
    workload = get_workload(item["workload_id"])
    if workload is not None:
        profile.record_trend(workload, batch["tenant_id"], load_settings())
    status = "succeeded" if signals >= 5 else "partial" if signals else "failed"
    return ItemResult(status=status, message=f"Nightly refresh warmed {signals}/5 signals.", result={"signals": signals})


async def _execute_inventory_cost(item: dict[str, Any], batch: dict[str, Any]) -> ItemResult:
    """Run one aggregate cost refresh while persisting each subscription as a batch item."""
    from app.core.azure_connections import resolve_connection
    from app.core.db import SessionLocal
    from app.inventory import cost

    config = batch["config"]
    connection = resolve_connection(str(config.get("connection_id") or "") or None)
    async with SessionLocal() as db:
        rows = list(
            (
                await db.execute(
                    select(WorkBatchItem).where(WorkBatchItem.batch_id == batch["id"])
                )
            ).scalars().all()
        )
        subscriptions = [row.item_key for row in rows]
        ids = {row.item_key: row.id for row in rows}

    async def progress(event: dict[str, Any]) -> None:
        sub = str(event.get("subscription_id") or "")
        target_id = ids.get(sub)
        if not target_id:
            return
        event_type = str(event.get("type") or "")
        if event_type == "subscription_started":
            await update_item_progress(target_id, status="running", message=str(event.get("message") or "Querying subscription."))
        elif event_type in {"subscription_done", "subscription_error"}:
            status = "succeeded" if event_type == "subscription_done" else "failed"
            await _complete_external_item(
                target_id,
                ItemResult(
                    status=status,
                    message=str(event.get("message") or status),
                    result={"subscription_total": event.get("subscription_total"), "currency": event.get("currency"), "resource_cost_rows": event.get("resource_cost_rows")},
                    error=str(event.get("error") or ""),
                    retryable=_transient(str(event.get("error") or "")),
                ),
            )
        elif event_type == "subscription_retry":
            await update_item_progress(target_id, message=str(event.get("message") or "Retrying subscription."))

    result = await cost.get_cost(
        connection,
        subscriptions,
        batch["tenant_id"],
        str(config.get("connection_id") or ""),
        force=bool(config.get("force", True)),
        scope=str(config.get("scope") or ""),
        progress=progress,
    )
    async with SessionLocal() as db:
        aggregate_item = await db.get(WorkBatchItem, item["id"])
        if aggregate_item is not None:
            aggregate_item.result_json = {**(aggregate_item.result_json or {}), "aggregate": result}
            aggregate_item.result_ref = {"kind": "inventory_cost", "id": batch["id"]}
            await db.commit()
    # Defensive completion for a collector that returned without an item terminal event.
    async with SessionLocal() as db:
        pending = list(
            (
                await db.execute(
                    select(WorkBatchItem).where(
                        WorkBatchItem.batch_id == batch["id"], WorkBatchItem.status.in_(ACTIVE)
                    )
                )
            ).scalars().all()
        )
    for row in pending:
        await _complete_external_item(
            row.id,
            ItemResult(status="partial" if result.get("errors") else "succeeded", message="Cost refresh complete.", result_ref={"kind": "inventory_cost", "id": batch["id"]}),
        )
    return ItemResult(
        status="partial" if result.get("errors") else "succeeded",
        message="Cost refresh complete.",
        result_ref={"kind": "inventory_cost", "id": batch["id"]},
        result={"total": result.get("total"), "currency": result.get("currency"), "errors": result.get("errors") or []},
        error="; ".join(map(str, result.get("errors") or [])),
    )


_EXECUTORS = {
    "assessment": _execute_assessment,
    "changeexplorer": _execute_changeexplorer,
    "coverage_amba": _execute_coverage,
    "coverage_telemetry": _execute_coverage,
    "coverage_backupdr": _execute_coverage,
    "backup_manager": _execute_backup_manager,
    "architecture": _execute_architecture,
    "mission": _execute_mission,
    "inventory_cost": _execute_inventory_cost,
    "deep_review": _execute_deep_review,
    "nightly": _execute_nightly,
}


async def _complete_external_item(item_id: str, result: ItemResult) -> None:
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        item = await db.get(WorkBatchItem, item_id)
        if item is None:
            return
        _apply_result(item, result)
        await refresh_batch(db, item.batch_id)
        await db.commit()


def _apply_result(item: WorkBatchItem, result: ItemResult) -> None:
    status = result.status if result.status in TERMINAL else "failed"
    item.status = status
    item.message = result.message[:2000]
    item.result_ref = result.result_ref or item.result_ref
    item.result_json = result.result or item.result_json or {}
    item.error = (result.error or "")[:4000] or None
    item.retryable = bool(result.retryable)
    item.available_at = None
    item.ended_at = _now()
    started = _aware(item.started_at)
    if started is not None:
        item.duration_ms = max(0, int((_now() - started).total_seconds() * 1000))


class WorkBatchWorker:
    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []
        self._wake: asyncio.Event | None = None
        self._claim_lock: asyncio.Lock | None = None

    @property
    def running(self) -> bool:
        return any(not task.done() for task in self._tasks)

    async def start(self) -> None:
        if self.running:
            return
        await recover_interrupted()
        self._wake = asyncio.Event()
        self._claim_lock = asyncio.Lock()
        self._tasks = [
            asyncio.create_task(self._loop(index), name=f"work-batch-{index}")
            for index in range(4)
        ]
        self.wake()

    async def ensure_running(self) -> None:
        if not self.running:
            await self.start()
        self.wake()

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            await recover_interrupted()
        self._wake = None
        self._claim_lock = None

    def wake(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def _loop(self, _index: int) -> None:
        while True:
            try:
                item_id = await self._claim_next()
                if item_id:
                    await self._run_item(item_id)
                    continue
                assert self._wake is not None
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Durable work-batch loop failed; retrying")
                await asyncio.sleep(1.0)

    async def _claim_next(self) -> str | None:
        from app.core.db import SessionLocal

        assert self._claim_lock is not None
        async with self._claim_lock:
            async with SessionLocal() as db:
                now = _now()
                candidates = list(
                    (
                        await db.execute(
                            select(WorkBatchItem, WorkBatch)
                            .join(WorkBatch, WorkBatch.id == WorkBatchItem.batch_id)
                            .where(
                                WorkBatchItem.status == "queued",
                                or_(WorkBatchItem.available_at.is_(None), WorkBatchItem.available_at <= now),
                                WorkBatch.status.in_(ACTIVE),
                                WorkBatch.cancel_requested.is_(False),
                            )
                            .order_by(WorkBatch.created_at, WorkBatchItem.workload_name, WorkBatchItem.item_key)
                            .limit(100)
                        )
                    ).all()
                )
                if not candidates:
                    return None
                running = list(
                    (
                        await db.execute(
                            select(WorkBatchItem, WorkBatch)
                            .join(WorkBatch, WorkBatch.id == WorkBatchItem.batch_id)
                            .where(WorkBatchItem.status == "running")
                        )
                    ).all()
                )
                lane_counts: dict[tuple[str, str], int] = {}
                feature_counts: dict[str, int] = {}
                for running_item, running_batch in running:
                    lane = (running_item.tenant_id, running_item.connection_id or "no-connection")
                    lane_counts[lane] = lane_counts.get(lane, 0) + 1
                    feature_counts[running_batch.feature] = feature_counts.get(running_batch.feature, 0) + 1
                chosen: tuple[WorkBatchItem, WorkBatch] | None = None
                for candidate, batch in candidates:
                    lane = (candidate.tenant_id, candidate.connection_id or "no-connection")
                    if lane_counts.get(lane, 0) >= 1:
                        continue
                    if feature_counts.get(batch.feature, 0) >= _FEATURE_LIMITS.get(batch.feature, 1):
                        continue
                    chosen = (candidate, batch)
                    break
                if chosen is None:
                    return None
                item, batch = chosen
                item.status = "running"
                item.attempt += 1
                item.started_at = _now()
                item.ended_at = None
                item.available_at = None
                item.error = None
                item.retryable = False
                item.message = f"Starting {batch.feature.replace('_', ' ')}."
                batch.status = "running"
                batch.started_at = batch.started_at or _now()
                await db.commit()
                return item.id

    async def _run_item(self, item_id: str) -> None:
        loaded = await _load_item_batch(item_id)
        if loaded is None:
            return
        item, batch = loaded
        # Include private fields needed by executors without exposing them in HTTP responses.
        batch["tenant_id"] = item.get("tenant_id") or ""
        # item_public intentionally omits tenant_id; recover it from the batch row.
        from app.core.db import SessionLocal

        async with SessionLocal() as db:
            batch_row = await db.get(WorkBatch, item["batch_id"])
            item_row = await db.get(WorkBatchItem, item_id)
            if batch_row is None or item_row is None:
                return
            batch["tenant_id"] = batch_row.tenant_id
            item["tenant_id"] = item_row.tenant_id
        executor = _EXECUTORS[batch["feature"]]
        try:
            result = await executor(item, batch)
            await self._finish_item(item_id, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("Work-batch item failed: %s", item_id)
            await self._fail_or_retry(item_id, str(exc)[:4000])

    async def _finish_item(self, item_id: str, result: ItemResult) -> None:
        from app.core.db import SessionLocal

        async with SessionLocal() as db:
            item = await db.get(WorkBatchItem, item_id)
            if item is None:
                return
            batch = await db.get(WorkBatch, item.batch_id)
            # Coordinating executors (Inventory Cost) may already have completed this item.
            if item.status not in TERMINAL:
                if (
                    result.retryable
                    and result.status in {"failed", "partial"}
                    and item.attempt < item.max_attempts
                    and batch is not None
                    and not batch.cancel_requested
                ):
                    delay = min(60, 2 ** max(1, item.attempt))
                    item.status = "queued"
                    item.retryable = True
                    item.error = (result.error or result.message)[:4000] or None
                    item.message = f"Transient outcome; retrying in {delay}s."
                    item.available_at = _now() + timedelta(seconds=delay)
                    item.started_at = None
                else:
                    _apply_result(item, result)
            await refresh_batch(db, item.batch_id)
            await db.commit()
        self.wake()

    async def _fail_or_retry(self, item_id: str, message: str) -> None:
        from app.core.db import SessionLocal

        async with SessionLocal() as db:
            item = await db.get(WorkBatchItem, item_id)
            if item is None:
                return
            transient = _transient(message)
            batch = await db.get(WorkBatch, item.batch_id)
            if transient and item.attempt < item.max_attempts and batch is not None and not batch.cancel_requested:
                delay = min(60, 2 ** max(1, item.attempt))
                item.status = "queued"
                item.retryable = True
                item.error = message
                item.message = f"Transient failure; retrying in {delay}s."
                item.available_at = _now() + timedelta(seconds=delay)
                item.started_at = None
            else:
                _apply_result(item, ItemResult(status="failed", message="Failed.", error=message, retryable=transient))
            await refresh_batch(db, item.batch_id)
            await db.commit()
        self.wake()


worker = WorkBatchWorker()
