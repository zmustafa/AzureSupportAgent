"""Continuous SIEM export of the audit log — supports MULTIPLE destinations.

Each destination is an independent forwarder (Splunk HEC or a generic HTTP/webhook
endpoint such as Microsoft Sentinel, Elastic, Datadog, Sumo Logic, …) with its own
encrypted secret, enable flag, and durable delivery cursor + status. The scheduler tick
flushes every enabled destination independently, so one SIEM being down never blocks the
others and each resumes exactly where it left off.

Config persists as JSON under backend/.data with secrets encrypted via app.core.crypto;
the public view masks every secret. A legacy single-destination config is migrated into
the destinations list automatically on first read.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import and_, or_, select

from app.core.crypto import decrypt, encrypt

logger = logging.getLogger("app.siem")

_PATH = Path(__file__).resolve().parents[2] / ".data" / "siem_export.json"

# Supported destination kinds.
DEST_TYPES = ("splunk_hec", "http")

# Editable config fields (per destination) and their defaults.
_FIELD_DEFAULTS: dict[str, Any] = {
    "name": "SIEM destination",
    "enabled": False,
    "type": "splunk_hec",
    "endpoint": "",
    # secret: Splunk HEC token, or the bearer/api-key value for an HTTP destination.
    "token": "",
    # HTTP destination only.
    "auth_header": "Authorization",
    "auth_scheme": "Bearer",
    # Splunk HEC only.
    "splunk_index": "",
    "splunk_sourcetype": "azsupagent:audit",
    "verify_tls": True,
    "batch_size": 100,
}

# Per-destination runtime status (not user-editable).
_STATUS_DEFAULTS: dict[str, Any] = {
    "cursor_ts": None,
    "cursor_id": None,
    "last_success_at": None,
    "last_attempt_at": None,
    "last_error": None,
    "forwarded_total": 0,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    if _PATH.exists():
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _migrate(data: dict[str, Any]) -> dict[str, Any]:
    """Bring any stored shape up to the multi-destination format."""
    if "destinations" in data and isinstance(data["destinations"], list):
        return data
    # Legacy single-destination config (top-level fields) → one destination.
    if data.get("endpoint") or data.get("token") or "enabled" in data:
        legacy = {**_FIELD_DEFAULTS, **_STATUS_DEFAULTS}
        for k in (*_FIELD_DEFAULTS.keys(), *_STATUS_DEFAULTS.keys()):
            if k in data:
                legacy[k] = data[k]
        # The legacy config used 'destination' for the kind.
        legacy["type"] = data.get("destination", legacy.get("type", "splunk_hec"))
        legacy["id"] = uuid.uuid4().hex[:12]
        legacy["name"] = "SIEM destination"
        return {"destinations": [legacy]}
    return {"destinations": []}


def _load() -> list[dict[str, Any]]:
    data = _migrate(_read())
    dests: list[dict[str, Any]] = []
    for d in data.get("destinations", []):
        if isinstance(d, dict):
            dests.append({**_FIELD_DEFAULTS, **_STATUS_DEFAULTS, **d})
    return dests


def _save(dests: list[dict[str, Any]]) -> None:
    _write({"destinations": dests})


def _find(dests: list[dict[str, Any]], dest_id: str) -> dict[str, Any] | None:
    return next((d for d in dests if d.get("id") == dest_id), None)


def _public(dest: dict[str, Any]) -> dict[str, Any]:
    """Masked view of one destination (never leaks the secret)."""
    has_token = bool(decrypt(dest.get("token", "") or ""))
    return {
        "id": dest.get("id"),
        "name": dest.get("name", "SIEM destination"),
        "enabled": bool(dest.get("enabled", False)),
        "type": dest.get("type", "splunk_hec"),
        "endpoint": dest.get("endpoint", ""),
        "token_set": has_token,
        "auth_header": dest.get("auth_header", "Authorization"),
        "auth_scheme": dest.get("auth_scheme", "Bearer"),
        "splunk_index": dest.get("splunk_index", ""),
        "splunk_sourcetype": dest.get("splunk_sourcetype", "azsupagent:audit"),
        "verify_tls": bool(dest.get("verify_tls", True)),
        "batch_size": int(dest.get("batch_size", 100)),
        "status": {
            "last_success_at": dest.get("last_success_at"),
            "last_attempt_at": dest.get("last_attempt_at"),
            "last_error": dest.get("last_error"),
            "forwarded_total": int(dest.get("forwarded_total", 0)),
            "cursor_ts": dest.get("cursor_ts"),
            "configured": bool(dest.get("endpoint")) and (has_token or dest.get("type") == "http"),
        },
    }


def list_destinations() -> dict[str, Any]:
    """Masked list of all destinations for the admin dashboard."""
    return {"destinations": [_public(d) for d in _load()], "types": list(DEST_TYPES)}


def _decrypted(dest: dict[str, Any]) -> dict[str, Any]:
    """A copy of a destination with the secret decrypted — for the forwarder only."""
    out = dict(dest)
    out["token"] = decrypt(dest.get("token", "") or "")
    return out


def _apply_fields(dest: dict[str, Any], values: dict[str, Any]) -> None:
    """Apply admin-editable fields onto a destination in place."""
    if "name" in values and isinstance(values["name"], str):
        dest["name"] = values["name"].strip() or "SIEM destination"
    if "enabled" in values:
        dest["enabled"] = bool(values["enabled"])
    if "type" in values:
        t = str(values["type"])
        dest["type"] = t if t in DEST_TYPES else "splunk_hec"
    for key in ("endpoint", "auth_header", "auth_scheme", "splunk_index", "splunk_sourcetype"):
        if key in values and isinstance(values[key], str):
            dest[key] = values[key].strip()
    if "verify_tls" in values:
        dest["verify_tls"] = bool(values["verify_tls"])
    if "batch_size" in values:
        try:
            dest["batch_size"] = max(1, min(int(values["batch_size"]), 1000))
        except (TypeError, ValueError):
            pass
    # Secret: update only on a non-empty new value, or explicit clear.
    if values.get("clear_token"):
        dest["token"] = ""
    else:
        new_token = values.get("token")
        if isinstance(new_token, str) and new_token.strip():
            dest["token"] = encrypt(new_token.strip())


def add_destination(values: dict[str, Any]) -> dict[str, Any]:
    dests = _load()
    dest = {**_FIELD_DEFAULTS, **_STATUS_DEFAULTS, "id": uuid.uuid4().hex[:12]}
    _apply_fields(dest, values)
    dests.append(dest)
    _save(dests)
    return list_destinations()


def update_destination(dest_id: str, values: dict[str, Any]) -> dict[str, Any] | None:
    dests = _load()
    dest = _find(dests, dest_id)
    if dest is None:
        return None
    _apply_fields(dest, values)
    _save(dests)
    return list_destinations()


def delete_destination(dest_id: str) -> bool:
    dests = _load()
    if _find(dests, dest_id) is None:
        return False
    _save([d for d in dests if d.get("id") != dest_id])
    return True


def reset_cursor(dest_id: str) -> bool:
    """Drop a destination's cursor so the next flush re-sends from the earliest row."""
    dests = _load()
    dest = _find(dests, dest_id)
    if dest is None:
        return False
    dest["cursor_ts"] = None
    dest["cursor_id"] = None
    _save(dests)
    return True


def _set_status(dest_id: str, **fields: Any) -> None:
    dests = _load()
    dest = _find(dests, dest_id)
    if dest is None:
        return
    for k, v in fields.items():
        if k in _STATUS_DEFAULTS:
            dest[k] = v
    _save(dests)


def _event_from_row(row: Any) -> dict[str, Any]:
    created = row.created_at
    if isinstance(created, datetime):
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        created_iso = created.isoformat()
        epoch = created.timestamp()
    else:
        created_iso = str(created)
        epoch = None
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "actor_id": row.actor_id,
        "action": row.action,
        "target": row.target,
        "provider": row.provider,
        "model": row.model,
        "metadata": row.metadata_json or {},
        "created_at": created_iso,
        "_epoch": epoch,
        "source": "azsupagent",
    }


async def _post_splunk(cfg: dict[str, Any], events: list[dict[str, Any]]) -> None:
    base = (cfg.get("endpoint") or "").rstrip("/")
    if not base.startswith("http"):
        base = f"https://{base}"
    url = base if "/services/collector" in base else f"{base}/services/collector/event"
    token = cfg.get("token", "")
    index = cfg.get("splunk_index") or None
    sourcetype = cfg.get("splunk_sourcetype") or "azsupagent:audit"
    lines = []
    for ev in events:
        epoch = ev.pop("_epoch", None)
        envelope: dict[str, Any] = {"event": ev, "source": "azsupagent", "sourcetype": sourcetype}
        if epoch is not None:
            envelope["time"] = epoch
        if index:
            envelope["index"] = index
        lines.append(json.dumps(envelope))
    body = "\n".join(lines)
    async with httpx.AsyncClient(timeout=30, verify=bool(cfg.get("verify_tls", True))) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Splunk {token}", "Content-Type": "application/json"},
            content=body,
        )
    if resp.status_code >= 300:
        raise RuntimeError(f"Splunk HEC {resp.status_code}: {resp.text[:300]}")
    data = resp.json() if resp.content else {}
    if isinstance(data, dict) and data.get("code", 0) not in (0, None):
        raise RuntimeError(f"Splunk HEC error: {data}")


async def _post_http(cfg: dict[str, Any], events: list[dict[str, Any]]) -> None:
    url = cfg.get("endpoint") or ""
    if not url:
        raise RuntimeError("No endpoint configured.")
    for ev in events:
        ev.pop("_epoch", None)
    headers = {"Content-Type": "application/json"}
    token = cfg.get("token", "")
    if token:
        header = cfg.get("auth_header") or "Authorization"
        scheme = (cfg.get("auth_scheme") or "").strip()
        headers[header] = f"{scheme} {token}".strip() if scheme else token
    payload = {"source": "azsupagent", "type": "audit", "events": events}
    async with httpx.AsyncClient(timeout=30, verify=bool(cfg.get("verify_tls", True))) as client:
        resp = await client.post(url, headers=headers, content=json.dumps(payload))
    if resp.status_code >= 300:
        raise RuntimeError(f"HTTP destination {resp.status_code}: {resp.text[:300]}")


async def _deliver(cfg: dict[str, Any], events: list[dict[str, Any]]) -> None:
    if cfg.get("type") == "http":
        await _post_http(cfg, events)
    else:
        await _post_splunk(cfg, events)


async def send_test_event(dest_id: str) -> dict[str, Any]:
    """Send a single synthetic event to one destination. Returns {ok, error}."""
    dest = _find(_load(), dest_id)
    if dest is None:
        return {"ok": False, "error": "Unknown destination."}
    cfg = _decrypted(dest)
    if not cfg.get("endpoint"):
        return {"ok": False, "error": "No endpoint configured."}
    now = datetime.now(timezone.utc)
    test_event = {
        "id": "test-" + now.strftime("%Y%m%d%H%M%S"),
        "tenant_id": "—",
        "actor_id": "siem-test",
        "action": "siem.test",
        "target": "connectivity check",
        "provider": None,
        "model": None,
        "metadata": {"note": "Test event from Azure Support Agent SIEM export."},
        "created_at": now.isoformat(),
        "_epoch": now.timestamp(),
        "source": "azsupagent",
    }
    try:
        await _deliver(cfg, [test_event])
        return {"ok": True, "error": None}
    except Exception as exc:  # noqa: BLE001 - surfaced to the admin UI
        return {"ok": False, "error": str(exc)[:500]}


async def _flush_destination(dest: dict[str, Any], *, force: bool) -> dict[str, Any]:
    """Forward the next batch for ONE destination. Never raises."""
    dest_id = dest.get("id", "")
    cfg = _decrypted(dest)
    if not cfg.get("enabled") and not force:
        return {"id": dest_id, "forwarded": 0, "error": None, "pending_more": False}
    if not cfg.get("endpoint"):
        return {"id": dest_id, "forwarded": 0, "error": "No endpoint configured.", "pending_more": False}

    from app.core.db import SessionLocal
    from app.models import AuditLog

    batch_size = int(cfg.get("batch_size", 100))
    try:
        cursor_dt = datetime.fromisoformat(cfg["cursor_ts"]) if cfg.get("cursor_ts") else None
    except (TypeError, ValueError):
        cursor_dt = None
    cursor_id = cfg.get("cursor_id")

    async with SessionLocal() as db:
        stmt = select(AuditLog)
        if cursor_dt is not None:
            stmt = stmt.where(
                or_(
                    AuditLog.created_at > cursor_dt,
                    and_(AuditLog.created_at == cursor_dt, AuditLog.id > (cursor_id or "")),
                )
            )
        stmt = stmt.order_by(AuditLog.created_at.asc(), AuditLog.id.asc()).limit(batch_size + 1)
        rows = (await db.execute(stmt)).scalars().all()

    if not rows:
        _set_status(dest_id, last_attempt_at=_now_iso(), last_error=None)
        return {"id": dest_id, "forwarded": 0, "error": None, "pending_more": False}

    pending_more = len(rows) > batch_size
    batch = rows[:batch_size]
    events = [_event_from_row(r) for r in batch]

    _set_status(dest_id, last_attempt_at=_now_iso())
    try:
        await _deliver(cfg, events)
    except Exception as exc:  # noqa: BLE001 - record, keep cursor, retry next tick
        msg = str(exc)[:500]
        logger.warning("SIEM export to %s failed: %s", dest.get("name", dest_id), msg)
        _set_status(dest_id, last_error=msg)
        return {"id": dest_id, "forwarded": 0, "error": msg, "pending_more": True}

    last = batch[-1]
    last_ts = last.created_at
    if isinstance(last_ts, datetime):
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        cursor_ts_new = last_ts.isoformat()
    else:
        cursor_ts_new = str(last_ts)

    # Re-load + update only this destination so concurrent status writes don't clobber.
    dests = _load()
    target = _find(dests, dest_id)
    if target is not None:
        target["cursor_ts"] = cursor_ts_new
        target["cursor_id"] = last.id
        target["last_success_at"] = _now_iso()
        target["last_error"] = None
        target["forwarded_total"] = int(target.get("forwarded_total", 0)) + len(batch)
        _save(dests)

    return {"id": dest_id, "forwarded": len(batch), "error": None, "pending_more": pending_more}


async def flush_destination(dest_id: str, *, force: bool = False, max_batches: int = 5) -> dict[str, Any]:
    """Drain up to ``max_batches`` for a single destination (manual flush)."""
    total = 0
    last_error = None
    for _ in range(max_batches):
        dest = _find(_load(), dest_id)
        if dest is None:
            return {"forwarded": total, "error": "Unknown destination."}
        res = await _flush_destination(dest, force=force)
        total += int(res.get("forwarded", 0))
        last_error = res.get("error")
        if last_error or not res.get("pending_more"):
            break
    return {"forwarded": total, "error": last_error}


async def flush_once() -> dict[str, Any]:
    """Forward the next batch for EVERY enabled destination. Safe for the scheduler tick.

    Each destination drains up to a few batches per call so bursts catch up quickly.
    Never raises. Returns a small summary {forwarded, destinations}.
    """
    dests = _load()
    total = 0
    per: list[dict[str, Any]] = []
    for d in dests:
        if not d.get("enabled"):
            continue
        sub_total = 0
        last_error = None
        for _ in range(5):
            cur = _find(_load(), d.get("id", ""))
            if cur is None:
                break
            res = await _flush_destination(cur, force=False)
            sub_total += int(res.get("forwarded", 0))
            last_error = res.get("error")
            if last_error or not res.get("pending_more"):
                break
        total += sub_total
        per.append({"id": d.get("id"), "forwarded": sub_total, "error": last_error})
    return {"forwarded": total, "destinations": per}
