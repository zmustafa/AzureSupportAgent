"""Notification engine: publish → match rules → deliver (in-app + connectors).

A single ``publish()`` entrypoint any feature can call. It records the event as a
``Notification``, evaluates the global ``NotificationRule`` set, and fans out to the
matched channels (the in-app center and/or connectors), recording each attempt as a
``NotificationDelivery`` row (an outbox/delivery log). Delivery failures never raise back
to the producer.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import Notification, NotificationDelivery, NotificationRule

logger = logging.getLogger("app.notifications.engine")

SEV_RANK = {"info": 0, "warning": 1, "error": 2, "critical": 3}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _matches(rule: NotificationRule, *, type: str, source: str, severity: str) -> bool:
    if not rule.enabled:
        return False
    if SEV_RANK.get(severity, 0) < SEV_RANK.get(rule.min_severity or "info", 0):
        return False
    if rule.event_types and type not in rule.event_types:
        return False
    if rule.sources and source not in rule.sources:
        return False
    return True


async def publish(
    *,
    tenant_id: str,
    type: str,
    source: str,
    severity: str,
    title: str,
    body: str,
    facts: dict[str, Any] | None = None,
    links: dict[str, Any] | None = None,
    fingerprint: str | None = None,
) -> str:
    """Record an event and deliver it to all matching channels. Returns notification id."""
    severity = severity if severity in SEV_RANK else "info"
    async with SessionLocal() as db:
        note = Notification(
            tenant_id=tenant_id,
            type=type,
            source=source,
            severity=severity,
            title=title[:512],
            body=body[:8000],
            facts_json=facts or {},
            links_json=links or {},
            fingerprint=fingerprint,
        )
        db.add(note)
        await db.commit()
        await db.refresh(note)
        note_id = note.id

        rules = (
            await db.execute(select(NotificationRule).where(NotificationRule.enabled.is_(True)))
        ).scalars().all()

    # Determine target channels from matching rules.
    want_in_app = False
    connector_ids: set[str] = set()
    matched_any = False
    for r in rules:
        if _matches(r, type=type, source=source, severity=severity):
            matched_any = True
            if r.in_app:
                want_in_app = True
            for cid in r.connector_ids or []:
                connector_ids.add(cid)

    # Zero-config baseline: if there are no rules at all, still record it in-app so
    # nothing is silently lost ("everything shows in the bell" until rules are added).
    if not rules:
        want_in_app = True

    deliveries: list[NotificationDelivery] = []
    if want_in_app:
        deliveries.append(
            NotificationDelivery(
                notification_id=note_id,
                tenant_id=tenant_id,
                channel="in_app",
                channel_label="In-app",
                status="sent",
                attempts=1,
                sent_at=_now(),
            )
        )

    for cid in connector_ids:
        ok, detail, label = await _deliver_connector(cid, title, body, severity)
        deliveries.append(
            NotificationDelivery(
                notification_id=note_id,
                tenant_id=tenant_id,
                channel=cid,
                channel_label=label,
                status="sent" if ok else "failed",
                detail=detail,
                attempts=1,
                sent_at=_now() if ok else None,
            )
        )

    if deliveries:
        async with SessionLocal() as db:
            for d in deliveries:
                db.add(d)
            await db.commit()
    _ = matched_any
    return note_id


async def _deliver_connector(
    connector_id: str, title: str, body: str, severity: str
) -> tuple[bool, str, str]:
    """Deliver to one connector; returns (ok, detail, label)."""
    try:
        from app.connectors.notify import deliver_to_connector
        from app.connectors.registry import get_connector

        conn = get_connector(connector_id)
        label = (conn or {}).get("name", connector_id)
        ok, detail = await deliver_to_connector(connector_id, title, body, severity)
        return ok, detail, label
    except Exception as exc:  # noqa: BLE001
        logger.warning("Connector delivery %s failed: %s", connector_id, exc)
        return False, str(exc)[:200], connector_id
