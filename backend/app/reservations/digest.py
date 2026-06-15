"""Weekly Reservations digest: selection, rendering, and (gated) delivery.

This replaces the original Logic App's recurrence + Office 365 email. Selection mirrors
the Logic App's ±``window`` filter (expiring within the next ``window`` days OR expired
within the last ``window`` days). Delivery is **off by default** — the scheduler only
sends when ``reservations_digest_enabled`` is set, so nothing leaves the box until the
operator reviews the preview and opts in. When enabled it fans out to the in-app center
plus the configured Email/Outlook connectors."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("app.reservations.digest")

DIGEST_SUBJECT = "Weekly Digest of Azure Reservations (Soon to expire or recently expired)"


# --------------------------------------------------------------------- selection
def select_digest_items(snapshot: dict[str, Any], *, window_days: int = 60) -> dict[str, Any]:
    """Items within ±``window`` days of expiry — the same set the Logic App emailed.

    Returns ``{items, expiring_soon, recently_expired, window_days, count, summary}``."""
    expiring_soon: list[dict[str, Any]] = []
    recently_expired: list[dict[str, Any]] = []
    for e in snapshot.get("items", []) or []:
        d = e.get("days_until")
        if d is None:
            continue
        if 0 <= d <= window_days:
            expiring_soon.append(e)
        elif -window_days <= d < 0:
            recently_expired.append(e)

    items = expiring_soon + recently_expired
    summary = (
        f"{len(expiring_soon)} reservation(s) expiring within {window_days} days"
        f" and {len(recently_expired)} expired in the last {window_days} days."
    )
    return {
        "items": items,
        "expiring_soon": expiring_soon,
        "recently_expired": recently_expired,
        "window_days": window_days,
        "count": len(items),
        "summary": summary,
    }


# --------------------------------------------------------------------- rendering
_STYLE = """<style>
  .reservation-table { font-family: Arial, sans-serif; border-collapse: collapse; width: 100%; margin: 16px 0; box-shadow: 0 2px 3px rgba(0,0,0,0.1); }
  .reservation-table th { background-color: #0078d4; color: white; text-align: left; padding: 10px 12px; }
  .reservation-table td { padding: 8px 12px; border-bottom: 1px solid #ddd; }
  .reservation-table tr:nth-child(even) { background-color: #f2f2f2; }
  .res-red { color: #d83b01; font-weight: bold; }
  .res-amber { color: #b56a00; font-weight: bold; }
</style>"""


def _days_label(d: int | None) -> str:
    if d is None:
        return "TBD"
    if d < 0:
        return f"{abs(d)}d ago"
    return f"{d}d"


def _renew_label(v: Any) -> str:
    if v is True:
        return "Yes"
    if v is False:
        return "No"
    return "—"


def _util_label(v: Any) -> str:
    if isinstance(v, (int, float)):
        return f"{v:g}%"
    return "—"


def render_html(items: list[dict[str, Any]], *, window_days: int = 60, title: str = DIGEST_SUBJECT) -> str:
    """Logic-App-style HTML table for the email body."""
    head = (
        f'<p style="font-size:17px"><b>{escape(title)}</b></p>'
        f"<p>Reservations expired within the <b>last {window_days} days</b> or expiring in the "
        f"<b>next {window_days} days</b>.</p>"
    )
    if not items:
        return head + "<p>No reservations are expiring soon or recently expired. Nothing to action.</p>"
    rows: list[str] = []
    for it in items:
        sev = it.get("severity", "grey")
        cls = "res-red" if sev == "red" else ("res-amber" if sev == "amber" else "")
        rows.append(
            "<tr>"
            f"<td>{escape(str(it.get('display_name', '') or it.get('id', '')))}</td>"
            f"<td>{escape(str(it.get('sku', '') or it.get('reserved_resource_type', '')))}</td>"
            f"<td>{escape(str(it.get('term', '')))}</td>"
            f"<td>{escape(str(it.get('created_date', ''))[:10])}</td>"
            f"<td class=\"{cls}\">{escape(str(it.get('expiry_date', ''))[:10])}</td>"
            f"<td class=\"{cls}\">{escape(_days_label(it.get('days_until')))}</td>"
            f"<td>{escape(_renew_label(it.get('renew')))}</td>"
            f"<td>{escape(_util_label(it.get('utilization_pct')))}</td>"
            f"<td>{escape(str(it.get('provisioning_state', '')))}</td>"
            "</tr>"
        )
    table = (
        '<table class="reservation-table"><thead><tr>'
        "<th>Reservation</th><th>SKU / Type</th><th>Term</th><th>Created</th>"
        "<th>Expires</th><th>Days</th><th>Auto-renew</th><th>Utilization</th><th>Status</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )
    return _STYLE + head + table


def render_text(sel: dict[str, Any]) -> str:
    """Plain-text summary for the in-app notification body."""
    lines = [sel.get("summary", "")]
    for it in sel.get("items", [])[:25]:
        lines.append(
            f"• {it.get('display_name', '') or it.get('id', '')} — "
            f"expires {str(it.get('expiry_date', ''))[:10]} ({_days_label(it.get('days_until'))}), "
            f"renew={_renew_label(it.get('renew'))}, util={_util_label(it.get('utilization_pct'))}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------- scheduling
def _tz(name: str):
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return timezone.utc


def _parse_hhmm(value: str | None) -> tuple[int, int]:
    try:
        h, m = (value or "08:00").split(":")
        return int(h), int(m)
    except (ValueError, AttributeError):
        return 8, 0


def compute_due(settings: dict[str, Any], last: dict[str, Any], *, now: datetime | None = None) -> tuple[bool, str | None]:
    """Decide whether the weekly (or daily) digest is due, returning ``(due, period_key)``.

    ``period_key`` is the date of the most recent scheduled occurrence at/just-before now;
    a digest is due when that occurrence has arrived and differs from the last-sent key —
    so a restart can't double-send, and a missed window catches up on the next tick."""
    tz = _tz(settings.get("reservations_digest_timezone", "America/New_York"))
    now_local = (now or datetime.now(timezone.utc)).astimezone(tz)
    hour, minute = _parse_hhmm(settings.get("reservations_digest_time", "08:00"))
    kind = settings.get("reservations_digest_schedule_kind", "weekly")

    scheduled: datetime | None = None
    if kind == "daily":
        today_at = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        scheduled = today_at if today_at <= now_local else today_at - timedelta(days=1)
    else:  # weekly
        target_wd = int(settings.get("reservations_digest_weekday", 0) or 0) % 7
        for i in range(0, 8):
            day = (now_local - timedelta(days=i)).replace(hour=hour, minute=minute, second=0, microsecond=0)
            if day.weekday() == target_wd and day <= now_local:
                scheduled = day
                break
    if scheduled is None:
        return False, None
    period_key = scheduled.date().isoformat()
    due = last.get("period_key") != period_key
    return due, period_key


# --------------------------------------------------------------------- delivery
async def _send_via_connector(connector_id: str, recipients: list[str], subject: str, html: str) -> dict[str, Any]:
    from app.connectors.registry import CONNECTOR_TYPES, get_connector

    conn = get_connector(connector_id)
    if conn is None or conn.get("disabled"):
        return {"channel": connector_id, "ok": False, "detail": "connector unavailable"}
    type_id = conn.get("type", "")
    ct = CONNECTOR_TYPES.get(type_id)
    if ct is None:
        return {"channel": connector_id, "ok": False, "detail": f"unknown connector type {type_id}"}
    tools = {t.name: t for t in ct.build_tools(conn)}
    tool = tools.get("email_send")
    if tool is None:
        return {"channel": connector_id, "ok": False, "detail": f"{type_id} has no email_send tool"}
    args = {"to": ", ".join(recipients), "subject": subject, "body": html}
    try:
        result = await tool.handler(conn, args)
    except Exception as exc:  # noqa: BLE001
        return {"channel": connector_id, "ok": False, "detail": str(exc)[:200]}
    is_err = bool(result.get("isError"))
    detail = str((result.get("content") or [""])[0])[:200]
    return {"channel": connector_id, "ok": not is_err, "detail": detail}


async def send_digest(
    *,
    tenant_id: str,
    subject: str,
    html: str,
    sel: dict[str, Any],
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    """Fan the digest out to the in-app center + configured Email/Outlook connectors."""
    results: list[dict[str, Any]] = []
    severity = "warning" if any(i.get("severity") == "red" for i in sel.get("items", [])) else "info"

    # In-app center (always records a Notification row).
    try:
        from app.notifications.engine import publish

        await publish(
            tenant_id=tenant_id or "default",
            type="reservations.digest",
            source="reservations",
            severity=severity,
            title=subject,
            body=render_text(sel),
            facts={"count": sel.get("count", 0), "window_days": sel.get("window_days", 60)},
            links={},
            fingerprint=None,
        )
        results.append({"channel": "in_app", "ok": True, "detail": "recorded"})
    except Exception as exc:  # noqa: BLE001
        results.append({"channel": "in_app", "ok": False, "detail": str(exc)[:200]})

    recipients = [r for r in (settings.get("reservations_digest_recipients") or []) if str(r).strip()]
    connector_ids = settings.get("reservations_digest_connector_ids") or []
    if connector_ids and not recipients:
        results.append({"channel": "email", "ok": False, "detail": "No recipients configured."})
    for cid in connector_ids:
        if recipients:
            results.append(await _send_via_connector(cid, recipients, subject, html))
    return results


async def maybe_send_weekly_digest(*, force: bool = False) -> dict[str, Any]:
    """Scheduler entrypoint. No-op unless enabled AND due (or ``force``). Never raises."""
    from app.core.app_settings import load_settings

    settings = load_settings()
    if not force and not settings.get("reservations_digest_enabled"):
        return {"sent": False, "reason": "disabled"}

    from app.core.azure_connections import get_default_connection
    from app.reservations import cache
    from app.reservations.collector import collect_reservations

    connection = get_default_connection()
    tenant_id = (connection or {}).get("tenant_id") or "default"
    last = cache.get_last_digest(tenant_id)
    due, period_key = compute_due(settings, last)
    if not force and not due:
        return {"sent": False, "reason": "not_due", "period_key": period_key}

    window_days = int(settings.get("reservations_window_days", 60) or 60)
    snap = await collect_reservations(connection, window_days=window_days)
    cache.write_snapshot(tenant_id, "tenant", snap)
    sel = select_digest_items(snap, window_days=window_days)
    html = render_html(sel["items"], window_days=window_days)
    results = await send_digest(
        tenant_id=tenant_id, subject=DIGEST_SUBJECT, html=html, sel=sel, settings=settings
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    if period_key:
        cache.set_last_digest(tenant_id, period_key=period_key, sent_at=now_iso, summary=sel["summary"])
    log.info("Reservations digest sent (period=%s): %s", period_key, sel["summary"])
    return {"sent": True, "period_key": period_key, "summary": sel["summary"], "channels": results}
