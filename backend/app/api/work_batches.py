"""Generic durable work-batch API for non-profiler fleet/background features."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import accepted_permission_keys
from app.core import work_batches
from app.core.db import get_db
from app.core.security import Principal, get_principal
from app.models import AuditLog

router = APIRouter(prefix="/work-batches", tags=["work-batches"])

_FEATURE_PERMISSION = {
    "assessment": "assessments.run",
    "changeexplorer": "changeexplorer.read",
    "coverage_amba": "coverage.read",
    "coverage_telemetry": "coverage.read",
    "coverage_backupdr": "coverage.read",
    "backup_manager": "backup_manager.read",
    "architecture": "architectures.write",
    "mission": "missions.run",
    "inventory_cost": "inventory.read",
    "deep_review": None,
    "nightly": "settings.write",
}


class WorkBatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feature: str = Field(min_length=1, max_length=48)
    workload_ids: list[str] = Field(default_factory=list, max_length=500)
    connection_id: str = Field(default="", max_length=128)
    config: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=128)


class WorkBatchRetry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)


def _authorize(principal: Principal, feature: str) -> None:
    if feature not in _FEATURE_PERMISSION:
        raise HTTPException(status_code=422, detail="Unsupported batch feature.")
    permission = _FEATURE_PERMISSION[feature]
    if permission is None:
        return
    accepted = accepted_permission_keys(permission)
    if not principal.is_admin and not any(principal.has(key) for key in accepted):
        raise HTTPException(status_code=403, detail=f"Missing permission: {permission}")


async def _items_for_request(payload: WorkBatchCreate, principal: Principal) -> list[dict[str, Any]]:
    if payload.feature == "inventory_cost":
        from app.api.inventory import _conn, _scope_subscriptions

        connection_id = str(payload.config.get("connection_id") or payload.connection_id or "")
        subscriptions = await _scope_subscriptions(
            _conn(connection_id or None), str(payload.config.get("scope") or "")
        )
        return [
            {
                "item_key": subscription_id,
                "workload_name": f"Subscription {subscription_id[:8]}…",
                "connection_id": connection_id,
            }
            for subscription_id in subscriptions
        ]

    from app.workloads.registry import get_workload

    unique_ids = list(dict.fromkeys(value.strip() for value in payload.workload_ids if value.strip()))
    if not unique_ids:
        raise HTTPException(status_code=422, detail="Select at least one workload.")
    items: list[dict[str, Any]] = []
    missing: list[str] = []
    for workload_id in unique_ids:
        workload = get_workload(workload_id)
        if workload is None:
            missing.append(workload_id)
            continue
        items.append(
            {
                "item_key": workload_id,
                "workload_id": workload_id,
                "workload_name": str(workload.get("name") or workload_id),
                "connection_id": payload.connection_id or str(workload.get("connection_id") or ""),
            }
        )
    if missing:
        raise HTTPException(
            status_code=404,
            detail={"message": "One or more workloads were not found.", "workload_ids": missing},
        )
    return items


@router.post("", status_code=202)
async def create_work_batch(
    payload: WorkBatchCreate,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _authorize(principal, payload.feature)
    items = await _items_for_request(payload, principal)
    if not items:
        raise HTTPException(status_code=422, detail="No executable batch items were resolved.")
    config = dict(payload.config)
    if payload.connection_id and "connection_id" not in config:
        config["connection_id"] = payload.connection_id
    try:
        batch, created = await work_batches.create_batch(
            tenant_id=principal.tenant_id,
            feature=payload.feature,
            actor=principal.subject,
            idempotency_key=payload.idempotency_key,
            items=items,
            config=config,
            trigger="manual",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if created:
        db.add(
            AuditLog(
                tenant_id=principal.tenant_id,
                actor_id=principal.subject,
                action="work_batch.launch",
                target=batch["id"],
                metadata_json={"feature": payload.feature, "count": len(items)},
            )
        )
        await db.commit()
    return {"batch": batch}


@router.get("/latest")
async def get_latest_work_batch(
    feature: str = Query(..., min_length=1, max_length=48),
    active_only: bool = Query(default=False),
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    _authorize(principal, feature)
    return {"batch": await work_batches.latest_batch(principal.tenant_id, feature, active_only=active_only)}


@router.get("/{batch_id}")
async def get_work_batch(
    batch_id: str,
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    batch = await work_batches.get_batch(batch_id, principal.tenant_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Work batch not found.")
    _authorize(principal, batch["feature"])
    return {"batch": batch}


@router.post("/{batch_id}/cancel")
async def cancel_work_batch(
    batch_id: str,
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    batch = await work_batches.get_batch(batch_id, principal.tenant_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Work batch not found.")
    _authorize(principal, batch["feature"])
    if not await work_batches.cancel_batch(batch_id, principal.tenant_id):
        raise HTTPException(status_code=409, detail="Only an active batch can be cancelled.")
    return {"batch": await work_batches.get_batch(batch_id, principal.tenant_id)}


@router.post("/{batch_id}/retry", status_code=202)
async def retry_work_batch(
    batch_id: str,
    payload: WorkBatchRetry,
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    batch = await work_batches.get_batch(batch_id, principal.tenant_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Work batch not found.")
    _authorize(principal, batch["feature"])
    retried = await work_batches.retry_batch(
        batch_id, principal.tenant_id, principal.subject, payload.idempotency_key
    )
    if retried is None:
        raise HTTPException(status_code=404, detail="Work batch not found.")
    return {"batch": retried}


@router.delete("/{batch_id}")
async def delete_work_batch(
    batch_id: str,
    principal: Principal = Depends(get_principal),
) -> dict[str, Any]:
    batch = await work_batches.get_batch(batch_id, principal.tenant_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Work batch not found.")
    _authorize(principal, batch["feature"])
    if not await work_batches.delete_batch(batch_id, principal.tenant_id):
        raise HTTPException(status_code=409, detail="Only a terminal batch can be deleted.")
    return {"ok": True}
