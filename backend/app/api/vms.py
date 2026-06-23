"""Admin endpoints for sandbox troubleshooting VMs.

CRUD over the sandbox-VM registry plus a live ``/test`` (SSH connect, identify the OS,
probe the toolkit, pin the host-key fingerprint) and a debug ``/run`` (execute one
command). All endpoints require the admin role; SSH secrets are never returned.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.sandbox_vms import (
    AUTH_METHODS,
    delete_vm,
    get_vm,
    public_vm,
    public_vms,
    update_environment,
    update_status,
    upsert_vm,
)
from app.core.security import Principal, require_permission
from app.exec.ssh_runner import detect_environment, run_ssh_capture
from app.models import AuditLog, VmRun

router = APIRouter(prefix="/admin/sandbox-vms", tags=["sandbox-vms"])

# Existing `require_admin` call sites now enforce a fine-grained capability (admins always
# pass through require_permission). See app.auth.permissions for the catalog.
require_admin = require_permission("sandbox.exec")
logger = logging.getLogger("app.api.vms")


class VmUpsert(BaseModel):
    id: str | None = None
    display_name: str = Field(max_length=200)
    host: str = Field(max_length=255)
    port: int = 22
    username: str = Field(max_length=128)
    auth_method: str = "ssh_password"
    strict_mode: bool | None = None
    disabled: bool | None = None
    allow_sudo: bool | None = None
    workload_ids: list[str] | None = None
    vnet_label: str | None = None
    # Secrets — blank on update means "keep the stored value".
    ssh_private_key: str | None = None
    ssh_passphrase: str | None = None
    ssh_password: str | None = None


@router.get("")
async def list_vms_endpoint(_: Principal = Depends(require_admin)):
    return {"vms": public_vms(), "auth_methods": list(AUTH_METHODS)}


@router.put("")
async def upsert_vm_endpoint(
    payload: VmUpsert,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if payload.auth_method not in AUTH_METHODS:
        raise HTTPException(status_code=400, detail=f"Unknown auth_method '{payload.auth_method}'.")
    data = payload.model_dump(exclude_none=True)
    if not payload.id:
        data["created_by"] = principal.display_name or principal.email or principal.subject
    saved = upsert_vm(data)
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="sandbox_vm.upsert",
            target=saved["id"],
            metadata_json={
                "display_name": saved.get("display_name"),
                "host": saved.get("host"),
                "auth_method": saved.get("auth_method"),
            },
        )
    )
    await db.commit()
    return {"vm": public_vm(saved)}


@router.delete("/{vm_id}")
async def delete_vm_endpoint(
    vm_id: str,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if not delete_vm(vm_id):
        raise HTTPException(status_code=404, detail="Sandbox VM not found.")
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="sandbox_vm.delete",
            target=vm_id,
        )
    )
    await db.commit()
    return {"ok": True}


@router.post("/{vm_id}/test")
async def test_vm_endpoint(
    vm_id: str,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """SSH in, identify the OS + toolkit, and pin the host-key fingerprint."""
    vm = get_vm(vm_id)
    if not vm:
        raise HTTPException(status_code=404, detail="Sandbox VM not found.")
    env = await detect_environment(vm)
    if not env.get("ok"):
        update_status(vm_id, "error", env.get("error", "Connection failed."))
        return {"ok": False, "detail": env.get("error", "Connection failed.")}
    # Persist detected env + pin the fingerprint (TOFU) if not already pinned.
    update_environment(
        vm_id,
        os_info=env.get("os_info", ""),
        capabilities=env.get("capabilities", []),
        host_key_fingerprint=(vm.get("host_key_fingerprint") or env.get("fingerprint", "")),
        pkg_manager=env.get("pkg_manager", ""),
        can_sudo=env.get("can_sudo", False),
        sudo_mode=env.get("sudo_mode", "none"),
    )
    update_status(vm_id, "ok", f"Connected as {env.get('whoami', '')}")
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action="sandbox_vm.test",
            target=vm_id,
            metadata_json={"os_info": env.get("os_info"), "tools": len(env.get("capabilities", []))},
        )
    )
    await db.commit()
    return {
        "ok": True,
        "whoami": env.get("whoami", ""),
        "os_info": env.get("os_info", ""),
        "capabilities": env.get("capabilities", []),
        "pkg_manager": env.get("pkg_manager", ""),
        "can_sudo": env.get("can_sudo", False),
        "sudo_mode": env.get("sudo_mode", "none"),
        "fingerprint": vm.get("host_key_fingerprint") or env.get("fingerprint", ""),
    }


class VmRunRequest(BaseModel):
    command: str = Field(max_length=8000)
    confirm: bool = False


@router.post("/{vm_id}/run")
async def run_vm_endpoint(
    vm_id: str,
    payload: VmRunRequest,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Run a single command on the sandbox VM (admin debug). Records a VmRun row."""
    vm = get_vm(vm_id)
    if not vm:
        raise HTTPException(status_code=404, detail="Sandbox VM not found.")
    cap = await run_ssh_capture(vm, payload.command, confirm=payload.confirm)
    status = (
        "blocked" if cap.needs_approval
        else "succeeded" if cap.ok
        else "failed"
    )
    run = VmRun(
        vm_id=vm_id,
        vm_name=vm.get("display_name"),
        tenant_id=principal.tenant_id,
        command=payload.command,
        destructive=cap.destructive,
        status=status,
        exit_code=cap.exit_code,
        output=cap.stdout[:200_000] if cap.stdout else None,
        stderr=cap.stderr[:8000] if cap.stderr else None,
        trigger="manual",
        triggered_by=principal.display_name or principal.email or principal.subject,
        error=cap.error or None,
        duration_ms=cap.duration_ms,
    )
    db.add(run)
    await db.commit()
    return {
        "ok": cap.ok,
        "needs_approval": cap.needs_approval,
        "destructive": cap.destructive,
        "exit_code": cap.exit_code,
        "stdout": cap.stdout,
        "stderr": cap.stderr,
        "error": cap.error,
        "duration_ms": cap.duration_ms,
    }


@router.get("/runs")
async def list_vm_runs_endpoint(
    vm_id: str | None = None,
    limit: int = 50,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Recent VM command history (tenant-scoped), newest first."""
    stmt = select(VmRun).where(VmRun.tenant_id == principal.tenant_id)
    if vm_id:
        stmt = stmt.where(VmRun.vm_id == vm_id)
    stmt = stmt.order_by(desc(VmRun.created_at)).limit(min(max(limit, 1), 200))
    rows = (await db.execute(stmt)).scalars().all()
    return {
        "runs": [
            {
                "id": r.id,
                "vm_id": r.vm_id,
                "vm_name": r.vm_name,
                "command": r.command,
                "destructive": r.destructive,
                "status": r.status,
                "exit_code": r.exit_code,
                "output": (r.output or "")[:4000],
                "stderr": (r.stderr or "")[:2000],
                "trigger": r.trigger,
                "chat_id": r.chat_id,
                "triggered_by": r.triggered_by,
                "error": r.error,
                "duration_ms": r.duration_ms,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }
