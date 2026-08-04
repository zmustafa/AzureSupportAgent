"""Network access control (IP allowlist) admin API.

Backs the /admin/firewall screen. See ``app.core.netaccess`` for the enforcement semantics and
``docs/improvement-plans/network-access-control/`` for the design.

Everything here is guarded: reading needs ``firewall.read`` (so an auditor can evidence the
policy), changing needs ``firewall.manage``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import netaccess, netaccess_events
from app.core.clientip import client_ip, describe as describe_client_ip
from app.core.db import get_db
from app.core.security import Principal, require_permission
from app.models import AuditLog, IpBlockEvent

router = APIRouter(prefix="/admin/firewall", tags=["firewall"])

require_read = require_permission("firewall.read")
require_manage = require_permission("firewall.manage")


class RuleIn(BaseModel):
    cidr: str = Field(max_length=64)
    label: str = Field(max_length=128)
    enabled: bool = True


class ConfigIn(BaseModel):
    mode: str
    rules: list[RuleIn]


async def _audit(db: AsyncSession, principal: Principal, action: str, meta: dict[str, Any]) -> None:
    db.add(
        AuditLog(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            action=action,
            target="network_access",
            metadata_json=meta,
        )
    )
    await db.commit()


def _decorate(cfg: dict[str, Any], caller_ip: str | None, request: Request | None = None) -> dict[str, Any]:
    """Add derived, display-only fields the UI needs to be safe to operate."""
    rules = []
    for rule in cfg.get("rules", []):
        item = dict(rule)
        try:
            net = netaccess.parse_cidr(str(rule.get("cidr", "")))
            item["scope"] = netaccess.describe_scope(net)
            item["valid"] = True
        except netaccess.NetAccessError:
            item["scope"] = "invalid"
            item["valid"] = False
        rules.append(item)
    match = netaccess.matching_rule(caller_ip, cfg.get("rules", []))
    return {
        "mode": cfg.get("mode", "off"),
        "effective_mode": netaccess.effective_mode(cfg),
        "rules": rules,
        "confirm_by": cfg.get("confirm_by"),
        "your_ip": caller_ip,
        "your_ip_covered": match is not None,
        "your_ip_rule": match.get("label") if match else None,
        "break_glass_active": netaccess.break_glass_active(),
        "confirm_window_minutes": netaccess.CONFIRM_WINDOW_MINUTES,
        # How the address above was arrived at. Without this, a mis-attributed address is only
        # discoverable by noticing the number looks wrong and asking someone.
        "resolution": describe_client_ip(request) if request is not None else None,
    }


@router.get("")
async def get_config(request: Request, _: Principal = Depends(require_read)):
    # Evaluate the commit-confirm timer on read so the screen never shows a stale "Enforcing"
    # after the window has lapsed.
    netaccess.revert_if_expired()
    return _decorate(netaccess.load_config(), client_ip(request), request)


@router.put("")
async def update_config(
    payload: ConfigIn,
    request: Request,
    principal: Principal = Depends(require_manage),
    db: AsyncSession = Depends(get_db),
):
    if payload.mode not in netaccess.MODES:
        raise HTTPException(status_code=400, detail=f"Unknown mode '{payload.mode}'.")

    caller_ip = client_ip(request)
    seen: set[str] = set()
    rules: list[dict[str, Any]] = []
    for rule in payload.rules:
        try:
            net = netaccess.parse_cidr(rule.cidr)
        except netaccess.NetAccessError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if payload.mode == "enforce" and rule.enabled and net.prefixlen == 0:
            # A rule that permits everything is a misconfiguration wearing a policy costume:
            # it silently turns enforcement into a no-op while the UI still reads "Enforcing".
            raise HTTPException(
                status_code=400,
                detail=f"'{rule.cidr}' allows every address, which disables enforcement. "
                "Remove it or use Off mode.",
            )
        if not rule.label.strip():
            raise HTTPException(status_code=400, detail=f"'{rule.cidr}' needs a label.")
        key = str(net)
        if key in seen:
            raise HTTPException(status_code=400, detail=f"'{key}' is listed more than once.")
        seen.add(key)
        rules.append(
            {
                "cidr": key,
                "label": rule.label.strip()[:128],
                "enabled": bool(rule.enabled),
                "created_by": principal.subject,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )

    # SAFETY: never let an operator save an enforcing policy that excludes themselves. This is
    # the single most likely way to lose access to the application, and it is entirely
    # preventable at the point of the mistake.
    if payload.mode == "enforce" and not netaccess.matches(caller_ip, rules):
        raise HTTPException(
            status_code=400,
            detail=f"No enabled rule covers your current address ({caller_ip or 'unknown'}). "
            "Add one before enforcing, or you will lose access immediately.",
        )

    previous = netaccess.load_config()
    cfg = {
        "mode": payload.mode,
        "rules": rules,
        # Switching INTO enforce is provisional and must be confirmed from a still-permitted
        # address; anything else clears the timer.
        "confirm_by": (
            netaccess.confirm_deadline()
            if payload.mode == "enforce" and previous.get("mode") != "enforce"
            else (previous.get("confirm_by") if payload.mode == "enforce" else None)
        ),
    }
    netaccess.write_config(cfg)
    netaccess.reset_cache()
    await _audit(
        db,
        principal,
        "firewall.update",
        {
            "from_mode": previous.get("mode"),
            "to_mode": payload.mode,
            "rule_count": len(rules),
            "rules": [r["cidr"] for r in rules],
            "actor_ip": caller_ip,
        },
    )
    return _decorate(cfg, caller_ip, request)


@router.post("/confirm")
async def confirm_enforcement(
    request: Request,
    principal: Principal = Depends(require_manage),
    db: AsyncSession = Depends(get_db),
):
    """Clear the commit-confirm timer — proof the operator still has access while enforcing."""
    cfg = netaccess.load_config()
    if cfg.get("mode") != "enforce":
        raise HTTPException(status_code=400, detail="Enforcement is not active.")
    cfg["confirm_by"] = None
    netaccess.write_config(cfg)
    await _audit(db, principal, "firewall.confirmed", {"actor_ip": client_ip(request)})
    return _decorate(cfg, client_ip(request), request)


@router.get("/blocks")
async def list_blocks(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_read),
):
    """Aggregated block records, newest activity first."""
    # Flush anything buffered so the operator sees the last minute of activity, not a stale view.
    await netaccess_events.flush(db)
    limit = max(1, min(200, limit))
    total = (await db.execute(select(func.count(IpBlockEvent.id)))).scalar() or 0
    rows = (
        await db.execute(
            select(IpBlockEvent).order_by(desc(IpBlockEvent.last_seen)).limit(limit).offset(offset)
        )
    ).scalars().all()
    return {
        "total": total,
        "items": [
            {
                "id": r.id,
                "ip": r.ip,
                "mode": r.mode,
                "hits": r.hits,
                "last_path": r.last_path,
                "first_seen": r.first_seen.isoformat() if r.first_seen else None,
                "last_seen": r.last_seen.isoformat() if r.last_seen else None,
            }
            for r in rows
        ],
    }


@router.delete("/blocks")
async def clear_blocks(
    request: Request,
    principal: Principal = Depends(require_manage),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(delete(IpBlockEvent))
    await db.commit()
    netaccess_events.reset()
    await _audit(db, principal, "firewall.blocks_cleared", {"actor_ip": client_ip(request)})
    return {"ok": True}
