"""Shared Backup Manager primitives: crypto, scope resolution, ARM/ARG access, LRO writes.

The Azure estate stays the source of truth.  Only approval metadata and encrypted
before/desired payloads are persisted locally, which keeps audit, rollback, and
optimistic-concurrency controls without building a second configuration database.

Unlike Alerts Manager, Azure Backup control-plane mutations are **long-running operations**:
ARM answers 202 with an ``Azure-AsyncOperation``/``Location`` header and the real work
finishes minutes later (often as a job inside the vault).  ``arm_submit`` therefore returns
the response headers so :mod:`app.backup_manager.lro` can poll to completion.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, TypeVar

import httpx

from app.core.crypto import decrypt, encrypt

T = TypeVar("T")

ARG_SUBSCRIPTION_BATCH = 100

ARM_BASE = "https://management.azure.com"

# --- API versions ----------------------------------------------------------------
RSV_API = "2024-04-01"                 # Microsoft.RecoveryServices/vaults
RSV_BACKUP_API = "2024-04-01"          # backupPolicies / protectedItems / backupJobs
RSV_VAULT_CONFIG_API = "2023-01-01"    # backupconfig/vaultconfig (soft delete)
RSV_STORAGE_CONFIG_API = "2023-01-01"  # backupstorageconfig/vaultstorageconfig (redundancy, CRR)
RSV_GUARD_PROXY_API = "2023-01-01"     # backupResourceGuardProxies (MUA)
RSV_ALERTS_API = "2023-01-01"          # alertsConfiguration/defaultAlertSetting
DP_API = "2024-04-01"                  # Microsoft.DataProtection/backupVaults
ASR_API = "2024-10-01"                 # Site Recovery replication resources
DIAG_API = "2021-05-01-preview"        # microsoft.insights/diagnosticSettings
RG_API = "2021-04-01"
POLICY_ASSIGNMENT_API = "2023-04-01"

# Resource types this module manages.
RSV_TYPE = "microsoft.recoveryservices/vaults"
DP_TYPE = "microsoft.dataprotection/backupvaults"

_GUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_ARM_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._()-]{0,88}[A-Za-z0-9_()-]$")
_SUB_ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


# --------------------------------------------------------------------------- time / hashing
def now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now().isoformat()


def canonical_hash(value: Any) -> str:
    """Stable SHA-256 over a JSON-serialisable value (the optimistic-concurrency marker)."""
    text = json.dumps(value or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def encrypted_json(value: Any) -> str:
    return encrypt(json.dumps(value or {}, separators=(",", ":"), ensure_ascii=True, default=str))


def decrypted_json(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(decrypt(value) or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def safe_error(value: str | None) -> str:
    """Strip signed URL query strings and SAS tokens out of an Azure error before display."""
    text = str(value or "")[:1500]
    text = re.sub(r"(https?://[^\s?\"']+)\?[^\s\"']+", r"\1?<redacted>", text)
    return text


# --------------------------------------------------------------------------- ARM id helpers
def subscription_from_id(resource_id: str) -> str:
    parts = str(resource_id or "").strip("/").split("/")
    lower = [p.lower() for p in parts]
    try:
        return parts[lower.index("subscriptions") + 1]
    except (ValueError, IndexError):
        return ""


def resource_group_from_id(resource_id: str) -> str:
    parts = str(resource_id or "").strip("/").split("/")
    lower = [p.lower() for p in parts]
    try:
        return parts[lower.index("resourcegroups") + 1]
    except (ValueError, IndexError):
        return ""


def name_from_id(resource_id: str) -> str:
    return str(resource_id or "").rstrip("/").rsplit("/", 1)[-1]


def vault_from_child_id(resource_id: str) -> str:
    """Parent vault ARM id of a protected item / backup instance / job / policy child id."""
    rid = str(resource_id or "")
    low = rid.lower()
    for marker in ("/backupfabrics/", "/backupinstances/", "/backupjobs/", "/backuppolicies/",
                   "/replicationfabrics/", "/replicationrecoveryplans/", "/backuppolicies"):
        idx = low.find(marker)
        if idx > 0:
            return rid[:idx]
    return ""


def is_rsv(resource_id: str) -> bool:
    return "/providers/microsoft.recoveryservices/vaults/" in f"{resource_id}".lower()


def is_backup_vault(resource_id: str) -> bool:
    return "/providers/microsoft.dataprotection/backupvaults/" in f"{resource_id}".lower()


def canonical_id(resource_id: str) -> str:
    return str(resource_id or "").strip().rstrip("/").lower()


def valid_arm_name(value: str) -> bool:
    return bool(_ARM_NAME_RE.match(str(value or "")))


def valid_subscription_id(value: str) -> bool:
    return bool(_SUB_ID_RE.match(str(value or "")))


def kql_escape(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace("'", "\\'")


def scope_identity(
    workload_id: str | None = None,
    subscription_id: str | None = None,
    management_group_id: str | None = None,
    *,
    required: bool = True,
) -> tuple[str, str]:
    """Validate that exactly one Backup Manager scope was supplied."""
    selected = [
        ("workload", str(workload_id or "").strip()),
        ("subscription", str(subscription_id or "").strip()),
        ("management_group", str(management_group_id or "").strip()),
    ]
    populated = [(kind, value) for kind, value in selected if value]
    if len(populated) > 1:
        raise ValueError("Select exactly one workload, subscription, or management group.")
    if not populated:
        if required:
            raise ValueError("Select a workload, subscription, or management group first.")
        return "none", ""
    kind, value = populated[0]
    if kind == "management_group":
        from app.workloads.discovery import normalize_management_group_id

        value = normalize_management_group_id(value)
    return kind, value


# --------------------------------------------------------------------------- connection scope
def workload_context(workload_id: str | None) -> tuple[dict[str, Any] | None, set[str], set[str]]:
    """Return ``(workload, lowercased node ids, subscription ids)`` for a workload scope."""
    if not workload_id:
        return None, set(), set()
    from app.workloads.registry import get_workload

    workload = get_workload(workload_id)
    ids: set[str] = set()
    subscriptions: set[str] = set()
    for node in (workload or {}).get("nodes", []) or []:
        rid = str(node.get("id") or "")
        if rid:
            ids.add(rid.lower())
        sub = str(node.get("subscription_id") or subscription_from_id(rid))
        if sub:
            subscriptions.add(sub)
    return workload, ids, subscriptions


def resolve_selected_connection(connection_id: str | None, workload_id: str | None = None) -> dict[str, Any]:
    from app.core.azure_connections import connection_for_scope, get_connection, resolve_connection

    workload, _ids, _subs = workload_context(workload_id)
    if connection_id:
        connection = get_connection(connection_id)
        if connection is None:
            raise LookupError("The selected Azure connection was not found.")
    else:
        connection = (
            connection_for_scope("workload", workload=workload)
            if workload_id
            else resolve_connection(None)
        )
    if not connection:
        raise ValueError("No Azure connection is configured for this scope.")
    if connection.get("disabled"):
        raise ValueError("The selected Azure connection is disabled.")
    return connection


def assert_writable(connection: dict[str, Any]) -> None:
    if connection.get("read_only", True):
        raise PermissionError("The selected Azure connection is read-only.")


async def scope_subscriptions(
    connection: dict[str, Any],
    *,
    workload_id: str | None = None,
    subscription_id: str | None = None,
    management_group_id: str | None = None,
) -> set[str]:
    """Resolve the exact concrete subscription set; selected scopes never mean all-visible."""
    return set((await resolve_scope(
        connection,
        workload_id=workload_id,
        subscription_id=subscription_id,
        management_group_id=management_group_id,
    ))["subscriptions"])


async def resolve_scope(
    connection: dict[str, Any],
    *,
    workload_id: str | None = None,
    subscription_id: str | None = None,
    management_group_id: str | None = None,
) -> dict[str, Any]:
    """Resolved scope metadata carried into snapshots, history, and exports."""
    kind, scope_id = scope_identity(workload_id, subscription_id, management_group_id)
    if kind == "subscription":
        return {
            "scope_kind": kind, "scope_id": scope_id, "scope_name": scope_id,
            "subscriptions": [scope_id], "subscription_count": 1,
            "resolution_complete": True, "resolution_warnings": [],
        }
    if kind == "management_group":
        from app.workloads.discovery import resolve_management_group_scope

        result = await resolve_management_group_scope(connection, scope_id)
        return {
            "scope_kind": kind,
            "scope_id": result["management_group_id"],
            "scope_name": result["management_group_name"],
            **result,
        }
    workload, _ids, subscriptions = workload_context(scope_id)
    if workload is None:
        raise LookupError("The selected workload was not found.")
    if not subscriptions:
        raise ValueError("No subscriptions are attached to the selected workload.")
    return {
        "scope_kind": kind, "scope_id": scope_id,
        "scope_name": str(workload.get("name") or scope_id),
        "subscriptions": sorted(subscriptions), "subscription_count": len(subscriptions),
        "resolution_complete": True, "resolution_warnings": [],
    }


# --------------------------------------------------------------------------- Azure access
async def token_for(connection: dict[str, Any]) -> str:
    from app.azure.credentials import get_arm_token

    token, error = await get_arm_token(connection)
    if not token:
        raise ValueError(safe_error(error or "Could not acquire an ARM token."))
    return token


async def arg(
    connection: dict[str, Any],
    query: str,
    subscriptions: Iterable[str] | None = None,
    *,
    max_rows: int = 5000,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run a Resource Graph query. Returns ``(rows, metadata)``; raises on hard failure."""
    from app.azure.arm import query_resource_graph_paged

    token = await token_for(connection)
    subs = sorted({str(s).lower() for s in (subscriptions or ()) if s})
    batches: list[list[str] | None] = (
        [subs[index:index + ARG_SUBSCRIPTION_BATCH] for index in range(0, len(subs), ARG_SUBSCRIPTION_BATCH)]
        if subs else [None]
    )
    retained: list[dict[str, Any]] = []
    errors: list[str] = []
    complete = True
    known_total = 0
    total_known = True
    succeeded = 0
    for index, batch in enumerate(batches, start=1):
        rows, error, batch_complete, total = await query_resource_graph_paged(
            token, query, batch, max_rows=max_rows,
        )
        if error:
            errors.append(f"batch {index}/{len(batches)}: {safe_error(error)}")
            complete = False
            continue
        succeeded += 1
        complete = complete and batch_complete
        if total is None:
            total_known = False
        else:
            known_total += int(total)
        if len(retained) < max_rows:
            retained.extend(rows[: max_rows - len(retained)])
    if not succeeded:
        raise ValueError("; ".join(errors)[:1500] or "Resource Graph query failed.")
    source_total = known_total if total_known else max(len(retained), known_total)
    return retained, {
        "partial": not complete or bool(errors) or source_total > len(retained),
        "source_total": source_total,
        "source_count": len(retained),
        "source_limit": max_rows,
        "subscription_count": len(subs),
        "batch_count": len(batches),
        "successful_batches": succeeded,
        "failed_batches": len(errors),
        "errors": errors,
    }


async def arg_safe_detailed(
    connection: dict[str, Any],
    query: str,
    subscriptions: Iterable[str] | None = None,
    *,
    max_rows: int = 5000,
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    try:
        rows, metadata = await arg(connection, query, subscriptions, max_rows=max_rows)
        return rows, metadata, ""
    except (ValueError, KeyError, TypeError) as exc:  # noqa: BLE001 - degraded source
        return [], {
            "partial": True, "source_total": None, "source_count": 0,
            "source_limit": max_rows, "subscription_count": len(set(subscriptions or ())),
            "batch_count": 0, "successful_batches": 0, "failed_batches": 1,
            "errors": [safe_error(str(exc))],
        }, safe_error(str(exc))


async def arg_safe(
    connection: dict[str, Any],
    query: str,
    subscriptions: Iterable[str] | None = None,
    *,
    max_rows: int = 5000,
) -> tuple[list[dict[str, Any]], str]:
    """``arg`` that degrades to ``([], error)`` — one unsupported ARG table must not fail a
    whole inventory sweep, so every collector source is independently fail-soft."""
    rows, _metadata, error = await arg_safe_detailed(
        connection, query, subscriptions, max_rows=max_rows,
    )
    return rows, error


async def arm_get(
    connection: dict[str, Any], path: str, api_version: str, *, query: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, int, str]:
    """ARM GET. Returns ``(body, status, error)`` and never raises for HTTP failures."""
    from app.azure.arm import arm_write

    token = await token_for(connection)
    data, error, status = await arm_write(token, "GET", path, api_version=api_version, query=query)
    return (data if isinstance(data, dict) else None), status, safe_error(error)


async def arm_get_with(
    token: str, path: str, api_version: str, *, query: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, int, str]:
    """``arm_get`` with a pre-acquired token (for bounded fan-out over many vaults)."""
    from app.azure.arm import arm_write

    data, error, status = await arm_write(token, "GET", path, api_version=api_version, query=query)
    return (data if isinstance(data, dict) else None), status, safe_error(error)


class ArmSubmission:
    """Result of an ARM mutation, carrying the LRO tracking headers when ARM answered 202."""

    __slots__ = ("status", "body", "error", "async_operation_url", "location_url", "retry_after")

    def __init__(
        self, *, status: int, body: dict[str, Any] | None, error: str,
        async_operation_url: str = "", location_url: str = "", retry_after: float = 0.0,
    ) -> None:
        self.status = status
        self.body = body
        self.error = error
        self.async_operation_url = async_operation_url
        self.location_url = location_url
        self.retry_after = retry_after

    @property
    def ok(self) -> bool:
        return not self.error and self.status in (200, 201, 202, 204)

    @property
    def is_async(self) -> bool:
        return self.status == 202 or bool(self.async_operation_url or self.location_url)

    def tracking_url(self) -> str:
        return self.async_operation_url or self.location_url

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "error": self.error,
            "async_operation_url": self.async_operation_url,
            "location_url": self.location_url,
            "retry_after": self.retry_after,
        }


def _header(resp: httpx.Response, name: str) -> str:
    return str(resp.headers.get(name) or resp.headers.get(name.lower()) or "")


async def arm_submit(
    token: str,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    api_version: str = "",
    query: dict[str, str] | None = None,
) -> ArmSubmission:
    """Submit an ARM mutation and capture the long-running-operation tracking headers.

    ``path`` may be an ARM-relative path (``/subscriptions/…``) or a full management.azure.com
    URL. Never raises: transport failures come back as ``status=0`` with an error string."""
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    params = dict(query or {})
    if api_version:
        params["api-version"] = api_version
    url = path if path.lower().startswith("http") else f"{ARM_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.request(method.upper(), url, headers=headers, params=params or None, json=body)
    except httpx.HTTPError as exc:  # noqa: BLE001 - transport failure
        return ArmSubmission(status=0, body=None, error=safe_error(f"ARM request error: {exc}"))

    retry_after = 0.0
    try:
        retry_after = float(_header(resp, "Retry-After") or 0)
    except (TypeError, ValueError):
        retry_after = 0.0
    if resp.status_code not in (200, 201, 202, 204):
        try:
            payload = resp.json()
            detail = (payload.get("error") or {}).get("message") or resp.text
            code = (payload.get("error") or {}).get("code") or ""
        except (ValueError, AttributeError):
            detail, code = resp.text, ""
        message = f"ARM {resp.status_code}: {detail}"
        if code:
            message = f"ARM {resp.status_code} [{code}]: {detail}"
        return ArmSubmission(status=resp.status_code, body=None, error=safe_error(message))
    parsed: dict[str, Any] = {}
    if resp.status_code != 204 and resp.content:
        try:
            raw = resp.json()
            parsed = raw if isinstance(raw, dict) else {"value": raw}
        except (ValueError, AttributeError):
            parsed = {}
    return ArmSubmission(
        status=resp.status_code,
        body=parsed,
        error="",
        async_operation_url=_header(resp, "Azure-AsyncOperation"),
        location_url=_header(resp, "Location"),
        retry_after=retry_after,
    )


async def arm_poll(token: str, url: str) -> tuple[str, dict[str, Any], str, float]:
    """Poll one LRO tracking URL.

    Returns ``(state, body, error, retry_after)`` where ``state`` is one of
    ``running`` | ``succeeded`` | ``failed``.  A 200 with no recognisable status field means
    the operation finished (the Location pattern returns the final resource body)."""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:  # noqa: BLE001
        return "running", {}, safe_error(f"ARM poll error: {exc}"), 15.0

    try:
        retry_after = float(_header(resp, "Retry-After") or 0)
    except (TypeError, ValueError):
        retry_after = 0.0
    if resp.status_code == 202:
        return "running", {}, "", retry_after or 15.0
    if resp.status_code == 204:
        return "succeeded", {}, "", 0.0
    if resp.status_code >= 400:
        try:
            payload = resp.json()
            detail = (payload.get("error") or {}).get("message") or resp.text
        except (ValueError, AttributeError):
            detail = resp.text
        # 404 on a Location URL after completion is a benign race; treat as success.
        if resp.status_code == 404:
            return "succeeded", {}, "", 0.0
        return "failed", {}, safe_error(f"ARM {resp.status_code}: {detail}"), 0.0
    try:
        payload = resp.json()
    except (ValueError, AttributeError):
        payload = {}
    body = payload if isinstance(payload, dict) else {}
    state = str(
        body.get("status")
        or (body.get("properties") or {}).get("provisioningState")
        or ""
    ).lower()
    if state in ("inprogress", "in progress", "running", "accepted", "creating", "updating", "pending"):
        return "running", body, "", retry_after or 15.0
    if state in ("failed", "canceled", "cancelled"):
        err = (body.get("error") or {}).get("message") or (
            (body.get("properties") or {}).get("errorMessage") or "The Azure operation failed."
        )
        return "failed", body, safe_error(str(err)), 0.0
    return "succeeded", body, "", 0.0


# --------------------------------------------------------------------------- concurrency
async def bounded_gather(
    factories: Iterable[Callable[[], Awaitable[T]]], *, limit: int = 6,
) -> list[T | BaseException]:
    """Run awaitables with a hard concurrency ceiling, preserving input order.

    Azure Backup control-plane endpoints throttle aggressively, so every fan-out in this
    module (per-vault config reads, per-change applies) goes through here."""
    semaphore = asyncio.Semaphore(max(1, int(limit)))

    async def run(factory: Callable[[], Awaitable[T]]) -> T:
        async with semaphore:
            return await factory()

    return list(await asyncio.gather(*(run(f) for f in factories), return_exceptions=True))


def unwrap(results: Iterable[Any], default: Any = None) -> list[Any]:
    """Replace exceptions from ``bounded_gather`` with ``default`` so callers degrade."""
    return [default if isinstance(r, BaseException) else r for r in results]


# --------------------------------------------------------------------------- misc parsing
def parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def age_hours(value: Any, *, reference: datetime | None = None) -> float | None:
    parsed = parse_iso(value)
    if parsed is None:
        return None
    return max(0.0, ((reference or now()) - parsed).total_seconds() / 3600.0)


def age_days(value: Any, *, reference: datetime | None = None) -> float | None:
    hours = age_hours(value, reference=reference)
    return None if hours is None else hours / 24.0


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def redact_guids(value: str) -> str:
    return _GUID_RE.sub("<guid>", str(value or ""))
