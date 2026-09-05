"""Managed change lifecycle: create, approve, apply, track, roll back.

The ledger is deliberately narrow.  Every mutation Backup Manager can perform is declared in
:data:`TARGET_SPECS`; anything not declared cannot be created, which is how the product's two
hard refusals are enforced structurally rather than by a runtime check that could be missed:

* there is no restore target type at all;
* there is no target type that deletes backup data, purges soft-deleted items, locks vault
  immutability, or weakens soft delete.

Applying is a two-phase operation.  ``apply_change`` re-reads live Azure state, enforces the
optimistic-concurrency hash captured at request time, and submits.  Azure Backup answers 202,
so the row moves to ``applying`` and :mod:`app.backup_manager.lro` drives it to a terminal
state.  Synchronous successes short-circuit straight to ``applied``.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.backup_manager import builtin_seed, dr, service
from app.models import BackupManagerChange

log = logging.getLogger("app.backup_manager.changes")

TERMINAL_STATUSES = {"applied", "failed", "rejected", "rolled_back"}
ACTIONABLE_STATUSES = {"pending", "approved"}
# How long a long-running Azure Backup operation may stay in `applying` before the poller
# gives up. Configure-protection jobs routinely take several minutes; two hours is generous
# without letting a row hang indefinitely.
POLL_DEADLINE_MINUTES = 120
MAX_POLL_ATTEMPTS = 240


class RefusedOperation(PermissionError):
    """Raised for an operation Backup Manager deliberately does not implement."""

    def __init__(self, operation_id: str, message: str) -> None:
        super().__init__(message)
        self.operation_id = operation_id


_REFUSALS = {item["id"]: item for item in builtin_seed.PORTAL_ONLY_OPERATIONS}


def refuse(operation_id: str) -> None:
    """Raise the documented refusal for a portal-only operation."""
    entry = _REFUSALS.get(operation_id)
    raise RefusedOperation(
        operation_id,
        (entry or {}).get("reason") or "This operation is not available in Backup Manager.",
    )


# --------------------------------------------------------------------------- target registry
# ``permission`` is the capability required to *draft* the change; approving and applying
# always additionally require ``backup_manager.approve``.
TARGET_SPECS: dict[str, dict[str, Any]] = {
    "vault": {
        "label": "Backup vault",
        "operations": {"create"},
        "risk": "low",
        "permission": "backup_manager.vault_write",
        "dual_approval": False,
    },
    "vault_security": {
        "label": "Vault security setting",
        "operations": {"update"},
        "risk": "medium",
        "permission": "backup_manager.vault_write",
        "dual_approval": False,
    },
    "vault_alerts": {
        "label": "Vault built-in alerts",
        "operations": {"update"},
        "risk": "low",
        "permission": "backup_manager.vault_write",
        "dual_approval": False,
    },
    "vault_diagnostics": {
        "label": "Vault diagnostic settings",
        "operations": {"create", "update"},
        "risk": "low",
        "permission": "backup_manager.vault_write",
        "dual_approval": False,
    },
    "backup_policy": {
        "label": "Backup policy",
        "operations": {"create", "update", "delete"},
        "risk": "medium",
        "permission": "backup_manager.policy_write",
        "dual_approval": False,
    },
    "protection": {
        # create = enable protection, update = change policy / resume, delete = STOP protection
        # while RETAINING data. Deleting backup data is not reachable from here.
        "label": "Protected item",
        "operations": {"create", "update", "delete"},
        "risk": "low",
        "permission": "backup_manager.protect_write",
        "dual_approval": False,
    },
    "adhoc_backup": {
        "label": "On-demand backup",
        "operations": {"invoke"},
        "risk": "low",
        "permission": "backup_manager.ondemand",
        "dual_approval": False,
    },
    "job_cancel": {
        "label": "Cancel running job",
        "operations": {"invoke"},
        "risk": "low",
        "permission": "backup_manager.ondemand",
        "dual_approval": False,
    },
    "policy_assignment": {
        "label": "Auto-protect policy assignment",
        "operations": {"create", "delete"},
        "risk": "medium",
        "permission": "backup_manager.policy_write",
        "dual_approval": False,
    },
    "asr_test_failover": {
        "label": "Site Recovery test failover",
        "operations": {"invoke"},
        "risk": "high",
        "permission": "backup_manager.drill_write",
        "dual_approval": True,
    },
    "asr_cleanup": {
        "label": "Site Recovery test-failover cleanup",
        "operations": {"invoke"},
        "risk": "medium",
        "permission": "backup_manager.drill_write",
        "dual_approval": False,
    },
}

# Stop-protection variants. Only the data-retaining variant exists as an operation.
STOP_PROTECTION_RETAIN = "stop_retain_data"


def spec_for(target_type: str) -> dict[str, Any]:
    spec = TARGET_SPECS.get(target_type)
    if not spec:
        raise ValueError(f"Unsupported Backup Manager change target '{target_type}'.")
    return spec


def validate_operation(target_type: str, operation: str) -> dict[str, Any]:
    spec = spec_for(target_type)
    if operation not in spec["operations"]:
        raise ValueError(f"{spec['label']} does not support the '{operation}' operation.")
    return spec


# --------------------------------------------------------------------------- creation
def build_change(
    *,
    tenant_id: str,
    connection_id: str,
    target_type: str,
    target_id: str,
    operation: str,
    requested_by: str,
    desired: dict[str, Any],
    before: dict[str, Any] | None = None,
    summary: dict[str, Any] | None = None,
    risk: str | None = None,
    plan_id: str | None = None,
    depends_on: list[str] | None = None,
    requires_dual_approval: bool | None = None,
) -> BackupManagerChange:
    """Construct (but do not persist) a pending managed change."""
    spec = validate_operation(target_type, operation)
    before = before or {}
    return BackupManagerChange(
        tenant_id=tenant_id,
        connection_id=connection_id,
        target_type=target_type,
        target_id=target_id,
        operation=operation,
        status="pending",
        risk=risk or spec["risk"],
        summary_json=dict(summary or {}),
        desired_encrypted=service.encrypted_json(desired),
        before_encrypted=service.encrypted_json(before),
        after_encrypted="",
        expected_state_hash=service.canonical_hash(before) if before else "",
        requested_by=requested_by,
        requested_at=service.now(),
        plan_id=plan_id,
        depends_on=list(depends_on or []),
        requires_dual_approval=(
            spec["dual_approval"] if requires_dual_approval is None else bool(requires_dual_approval)
        ),
    )


def public_change(change: BackupManagerChange) -> dict[str, Any]:
    """The browser-safe projection. Encrypted payloads never leave the server."""
    return {
        "id": change.id,
        "connection_id": change.connection_id,
        "target_type": change.target_type,
        "target_label": TARGET_SPECS.get(change.target_type, {}).get("label", change.target_type),
        "target_id": change.target_id,
        "target_name": service.name_from_id(change.target_id),
        "operation": change.operation,
        "status": change.status,
        "risk": change.risk,
        "summary": dict(change.summary_json or {}),
        "requested_by": change.requested_by,
        "requested_at": change.requested_at.isoformat() if change.requested_at else "",
        "decided_by": change.decided_by or "",
        "decided_at": change.decided_at.isoformat() if change.decided_at else "",
        "decision_reason": change.decision_reason or "",
        "requires_dual_approval": bool(change.requires_dual_approval),
        "second_approver": change.second_approver or "",
        "second_approved_at": change.second_approved_at.isoformat() if change.second_approved_at else "",
        "applied_by": change.applied_by or "",
        "applied_at": change.applied_at.isoformat() if change.applied_at else "",
        "error_code": change.error_code or "",
        "error_message": service.safe_error(change.error_message),
        "rollback_of": change.rollback_of or "",
        "evidence_id": change.evidence_id or "",
        "plan_id": change.plan_id or "",
        "depends_on": list(change.depends_on or []),
        "azure_job_id": change.azure_job_id or "",
        "poll_attempts": int(change.poll_attempts or 0),
        "is_async": bool(change.operation_url),
        "can_rollback": change.status == "applied" and _rollback_spec(change) is not None,
    }


# --------------------------------------------------------------------------- apply
def _dp_stop_path(instance_id: str) -> str:
    return f"{instance_id.rstrip('/')}/stopProtection"


def _dp_resume_path(instance_id: str) -> str:
    return f"{instance_id.rstrip('/')}/resumeProtection"


async def _current_state(connection: dict[str, Any], change: BackupManagerChange) -> tuple[dict[str, Any] | None, int, str]:
    api = str((change.summary_json or {}).get("api_version") or service.RSV_BACKUP_API)
    return await service.arm_get(connection, change.target_id, api)


def concurrency_body(live: dict[str, Any]) -> dict[str, Any]:
    """The subset compared for optimistic concurrency (ARM adds volatile fields to reads)."""
    return {
        "location": live.get("location") or "",
        "properties": service.as_dict(live.get("properties")),
        "sku": service.as_dict(live.get("sku")),
    }


# Backwards-compatible alias used inside this module.
_concurrency_body = concurrency_body


async def apply_change(
    connection: dict[str, Any], change: BackupManagerChange,
) -> tuple[service.ArmSubmission, dict[str, Any]]:
    """Submit one approved change to Azure.

    Returns ``(submission, context)``. The caller persists the resulting status; nothing in
    here mutates the database, so a failed apply leaves an accurate, auditable row."""
    service.assert_writable(connection)
    desired = service.decrypted_json(change.desired_encrypted)
    summary = dict(change.summary_json or {})
    token = await service.token_for(connection)
    api_version = str(summary.get("api_version") or service.RSV_BACKUP_API)
    body = service.as_dict(desired.get("body"))
    context: dict[str, Any] = {"before": {}}

    # Optimistic concurrency: an update must still be operating on the state that was reviewed.
    if change.operation == "update" and change.expected_state_hash:
        live, status, error = await service.arm_get(connection, change.target_id, api_version)
        if error or not live:
            return service.ArmSubmission(
                status=status or 404, body=None,
                error=error or "The target no longer exists in Azure.",
            ), context
        if service.canonical_hash(_concurrency_body(live)) != change.expected_state_hash:
            return service.ArmSubmission(
                status=409, body=None,
                error="Azure state changed after this request was reviewed. Refresh and create a new change.",
            ), context
        context["before"] = live
    elif change.operation == "create" and change.target_type in ("vault", "backup_policy", "protection"):
        live, status, _error = await service.arm_get(connection, change.target_id, api_version)
        if live and change.target_type != "protection":
            return service.ArmSubmission(
                status=409, body=None, error=f"{service.name_from_id(change.target_id)} already exists.",
            ), context
        context["before"] = live or {}

    handler = _HANDLERS.get(change.target_type)
    if handler is None:
        return service.ArmSubmission(status=0, body=None, error="No apply handler for this change type."), context
    submission = await handler(token, connection, change, body, summary, api_version)
    return submission, context


async def _apply_put(token, _connection, change, body, _summary, api_version) -> service.ArmSubmission:
    return await service.arm_submit(token, "PUT", change.target_id, body=body, api_version=api_version)


async def _apply_vault(token, _connection, change, body, _summary, api_version) -> service.ArmSubmission:
    return await service.arm_submit(token, "PUT", change.target_id, body=body, api_version=api_version)


async def _apply_vault_security(token, _connection, change, body, summary, api_version) -> service.ArmSubmission:
    """Soft delete, redundancy, and Cross Region Restore live on different sub-resources."""
    path = str(summary.get("arm_path") or change.target_id)
    method = str(summary.get("arm_method") or "PATCH").upper()
    return await service.arm_submit(token, method, path, body=body, api_version=api_version)


async def _apply_vault_alerts(token, _connection, change, body, summary, api_version) -> service.ArmSubmission:
    path = str(summary.get("arm_path") or change.target_id)
    method = str(summary.get("arm_method") or "PUT").upper()
    return await service.arm_submit(token, method, path, body=body, api_version=api_version)


async def _apply_diagnostics(token, _connection, change, body, _summary, _api_version) -> service.ArmSubmission:
    return await service.arm_submit(token, "PUT", change.target_id, body=body, api_version=service.DIAG_API)


async def _apply_policy(token, _connection, change, body, _summary, api_version) -> service.ArmSubmission:
    if change.operation == "delete":
        return await service.arm_submit(token, "DELETE", change.target_id, api_version=api_version)
    return await service.arm_submit(token, "PUT", change.target_id, body=body, api_version=api_version)


async def _apply_protection(token, _connection, change, body, summary, api_version) -> service.ArmSubmission:
    mechanism = str(summary.get("mechanism") or "rsv_vm")
    if change.operation == "delete":
        # The ONLY stop-protection variant that exists: retain data. Anything that would
        # delete recovery points is refused before a change row is created.
        if str(summary.get("stop_mode") or STOP_PROTECTION_RETAIN) != STOP_PROTECTION_RETAIN:
            return service.ArmSubmission(
                status=0, body=None,
                error="Only stop-protection-with-data-retained is supported. Deleting backup data must be done in the Azure portal.",
            )
        if mechanism == "dataprotection":
            return await service.arm_submit(
                token, "POST", _dp_stop_path(change.target_id), body={}, api_version=service.DP_API,
            )
        return await service.arm_submit(token, "PUT", change.target_id, body=body, api_version=api_version)
    if change.operation == "update" and str(summary.get("intent") or "") == "resume" and mechanism == "dataprotection":
        return await service.arm_submit(
            token, "POST", _dp_resume_path(change.target_id),
            body=body or {}, api_version=service.DP_API,
        )
    return await service.arm_submit(token, "PUT", change.target_id, body=body, api_version=api_version)


async def _apply_adhoc_backup(token, _connection, change, body, summary, _api_version) -> service.ArmSubmission:
    mechanism = str(summary.get("mechanism") or "rsv_vm")
    if mechanism == "dataprotection":
        return await service.arm_submit(
            token, "POST", f"{change.target_id.rstrip('/')}/backup", body=body, api_version=service.DP_API,
        )
    return await service.arm_submit(
        token, "POST", f"{change.target_id.rstrip('/')}/backup", body=body, api_version=service.RSV_BACKUP_API,
    )


async def _apply_job_cancel(token, _connection, change, _body, _summary, _api_version) -> service.ArmSubmission:
    return await service.arm_submit(
        token, "POST", f"{change.target_id.rstrip('/')}/cancel", body=None, api_version=service.RSV_BACKUP_API,
    )


async def _apply_policy_assignment(token, _connection, change, body, _summary, _api_version) -> service.ArmSubmission:
    if change.operation == "delete":
        return await service.arm_submit(
            token, "DELETE", change.target_id, api_version=service.POLICY_ASSIGNMENT_API,
        )
    return await service.arm_submit(
        token, "PUT", change.target_id, body=body, api_version=service.POLICY_ASSIGNMENT_API,
    )


async def _apply_test_failover(token, _connection, change, body, summary, _api_version) -> service.ArmSubmission:
    path = (
        dr.recovery_plan_test_failover_path(change.target_id)
        if str(summary.get("drill_target") or "item") == "recovery_plan"
        else dr.test_failover_path(change.target_id)
    )
    return await service.arm_submit(token, "POST", path, body=body, api_version=service.ASR_API)


async def _apply_cleanup(token, _connection, change, body, summary, _api_version) -> service.ArmSubmission:
    path = (
        dr.recovery_plan_cleanup_path(change.target_id)
        if str(summary.get("drill_target") or "item") == "recovery_plan"
        else dr.cleanup_path(change.target_id)
    )
    return await service.arm_submit(token, "POST", path, body=body, api_version=service.ASR_API)


_HANDLERS = {
    "vault": _apply_vault,
    "vault_security": _apply_vault_security,
    "vault_alerts": _apply_vault_alerts,
    "vault_diagnostics": _apply_diagnostics,
    "backup_policy": _apply_policy,
    "protection": _apply_protection,
    "adhoc_backup": _apply_adhoc_backup,
    "job_cancel": _apply_job_cancel,
    "policy_assignment": _apply_policy_assignment,
    "asr_test_failover": _apply_test_failover,
    "asr_cleanup": _apply_cleanup,
}


# --------------------------------------------------------------------------- status transitions
def mark_submitted(change: BackupManagerChange, submission: service.ArmSubmission, actor: str) -> None:
    """Move a change into its post-submit state (``applying`` for an LRO, else terminal)."""
    change.applied_by = actor
    change.applied_at = service.now()
    if not submission.ok:
        change.status = "failed"
        change.error_code = str(submission.status or "")[:64]
        change.error_message = submission.error
        return
    tracking = submission.tracking_url()
    if submission.is_async and tracking:
        change.status = "applying"
        change.operation_url = tracking
        change.poll_attempts = 0
        change.poll_after = service.now() + timedelta(seconds=max(5.0, submission.retry_after or 10.0))
        change.poll_deadline = service.now() + timedelta(minutes=POLL_DEADLINE_MINUTES)
        job_id = str(service.as_dict(submission.body).get("id") or "")
        if job_id:
            change.azure_job_id = job_id[:1024]
        return
    change.status = "applied"
    change.after_encrypted = service.encrypted_json(submission.body or {})
    change.error_code = None
    change.error_message = None


def mark_polled(change: BackupManagerChange, state: str, body: dict[str, Any], error: str, retry_after: float) -> None:
    change.poll_attempts = int(change.poll_attempts or 0) + 1
    if state == "succeeded":
        change.status = "applied"
        change.after_encrypted = service.encrypted_json(body or {})
        change.error_code = None
        change.error_message = None
        change.poll_after = None
        return
    if state == "failed":
        change.status = "failed"
        change.error_code = "AzureOperationFailed"
        change.error_message = error or "The Azure operation failed."
        change.poll_after = None
        return
    change.poll_after = service.now() + timedelta(seconds=max(5.0, retry_after or 15.0))


def mark_timed_out(change: BackupManagerChange) -> None:
    change.status = "failed"
    change.error_code = "OperationTimeout"
    change.error_message = (
        f"The Azure operation did not complete within {POLL_DEADLINE_MINUTES} minutes. "
        "Check the vault's job list in Azure before retrying."
    )
    change.poll_after = None


# --------------------------------------------------------------------------- rollback
def _rollback_spec(change: BackupManagerChange) -> dict[str, Any] | None:
    """The inverse of an applied change, or ``None`` when it cannot be reversed.

    Deliberately conservative: only changes whose inverse is itself a non-destructive,
    already-supported operation can be rolled back."""
    before = service.decrypted_json(change.before_encrypted)
    summary = dict(change.summary_json or {})
    if change.target_type == "protection" and change.operation == "create":
        return {
            "target_type": "protection",
            "operation": "delete",
            "summary": {**summary, "stop_mode": STOP_PROTECTION_RETAIN, "intent": "rollback"},
            "desired": {"body": _stop_protection_body(summary, before)},
        }
    if change.target_type in ("vault_security", "vault_alerts", "backup_policy") and change.operation == "update" and before:
        return {
            "target_type": change.target_type,
            "operation": "update",
            "summary": {**summary, "intent": "rollback"},
            "desired": {"body": _restore_body(change.target_type, before, summary)},
        }
    if change.target_type == "policy_assignment" and change.operation == "create":
        return {
            "target_type": "policy_assignment",
            "operation": "delete",
            "summary": {**summary, "intent": "rollback"},
            "desired": {"body": {}},
        }
    return None


def _stop_protection_body(summary: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
    if str(summary.get("mechanism") or "") == "dataprotection":
        return {}
    props = service.as_dict(before.get("properties"))
    return {
        "properties": {
            "protectedItemType": str(props.get("protectedItemType") or "Microsoft.Compute/virtualMachines"),
            "sourceResourceId": str(props.get("sourceResourceId") or summary.get("resource_id") or ""),
            "protectionState": "ProtectionStopped",
        }
    }


def _restore_body(target_type: str, before: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    if target_type == "backup_policy":
        return {"properties": service.as_dict(before.get("properties"))}
    key = str(summary.get("setting") or "")
    props = service.as_dict(before.get("properties"))
    if key:
        return {"properties": {key: props.get(key)}} if key in props else {"properties": props}
    return {"properties": props}


def build_rollback(change: BackupManagerChange, *, requested_by: str) -> BackupManagerChange:
    spec = _rollback_spec(change)
    if spec is None:
        raise ValueError("This change cannot be rolled back automatically. Reverse it in the Azure portal.")
    row = build_change(
        tenant_id=change.tenant_id,
        connection_id=change.connection_id,
        target_type=str(spec["target_type"]),
        target_id=change.target_id,
        operation=str(spec["operation"]),
        requested_by=requested_by,
        desired=spec["desired"],
        before=service.decrypted_json(change.after_encrypted),
        summary={**spec["summary"], "rollback_of": change.id},
        risk="medium",
    )
    row.rollback_of = change.id
    return row


# --------------------------------------------------------------------------- helpers
def summary_for_protection(gap_item: dict[str, Any]) -> dict[str, Any]:
    """Public summary for a remediation change (no payload, no secrets)."""
    return {
        "kind": "enable_protection",
        "resource_id": gap_item.get("resource_id", ""),
        "resource_name": gap_item.get("resource_name", ""),
        "resource_type": gap_item.get("resource_type", ""),
        "display_type": gap_item.get("display_type", ""),
        "vault_id": gap_item.get("vault_id", ""),
        "vault_name": gap_item.get("vault_name", ""),
        "policy_id": gap_item.get("policy_id", ""),
        "policy_name": gap_item.get("policy_name", ""),
        "mechanism": gap_item.get("mechanism", ""),
        "api_version": gap_item.get("api_version", ""),
        "gap_id": gap_item.get("gap_id", ""),
        "description": gap_item.get("summary", ""),
    }


async def load_change(db: AsyncSession, change_id: str, *, tenant_id: str) -> BackupManagerChange | None:
    row = await db.get(BackupManagerChange, change_id)
    if row is None or row.tenant_id != tenant_id:
        return None
    return row


def order_changes(rows: list[BackupManagerChange]) -> list[BackupManagerChange]:
    """Topologically order a batch so prerequisites (vault, then policy, then protection) run
    first. Cycles fall back to declaration order rather than deadlocking the batch."""
    priority = {"vault": 0, "backup_policy": 1, "vault_security": 2, "vault_alerts": 2,
                "vault_diagnostics": 2, "protection": 3, "policy_assignment": 3,
                "adhoc_backup": 4, "job_cancel": 4, "asr_test_failover": 5, "asr_cleanup": 6}
    index = {row.id: row for row in rows}
    indegree = {row.id: 0 for row in rows}
    dependents: dict[str, list[str]] = {row.id: [] for row in rows}
    for row in rows:
        for dep in row.depends_on or []:
            if dep in index:
                indegree[row.id] += 1
                dependents[dep].append(row.id)

    def sort_key(change_id: str) -> tuple[Any, ...]:
        row = index[change_id]
        return (priority.get(row.target_type, 9), row.requested_at or service.now(), row.id)

    ready = sorted([cid for cid, deg in indegree.items() if deg == 0], key=sort_key)
    ordered: list[BackupManagerChange] = []
    while ready:
        current = ready.pop(0)
        ordered.append(index[current])
        for dependent in dependents[current]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort(key=sort_key)
    if len(ordered) != len(rows):
        seen = {row.id for row in ordered}
        ordered.extend(row for row in rows if row.id not in seen)
    return ordered
