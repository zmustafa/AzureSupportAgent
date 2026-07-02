"""Insight Pack runner — the four-stage loop: gather → reason → gate → deliver.

``run_pack`` executes one run of a pack against a resolved scope and returns a digest dict
(persisted by the caller). It is deterministic about *whether to notify* (the gate) and
delegates interpretation to the LLM (the reason stage). It never raises — any failure is
captured into a ``failed``/``notable`` digest so the scheduler records a clean TaskRun.
"""
from __future__ import annotations

import logging
from typing import Any

from app.insights import reason as reason_mod
from app.insights import runs as runs_store
from app.insights import sources as sources_mod
from app.insights import packfile

log = logging.getLogger("app.insights.runner")


def _gate(*, verdict: str, threshold: str, always_codes: list[str],
          flag_codes: set[str]) -> tuple[bool, str]:
    """Decide whether this run warrants a notification.

    A run notifies when EITHER a deterministic floor fires (any of the pack's
    ``always_notify_if`` flag codes is present in the gathered data) OR the AI verdict
    clears the pack's notify threshold. Returns (should_notify, reason)."""
    floor_hit = sorted(set(always_codes) & set(flag_codes))
    if floor_hit:
        return True, f"floor:{','.join(floor_hit)}"
    if packfile.VERDICT_RANK.get(verdict, 0) >= packfile.VERDICT_RANK.get(threshold, 1):
        return True, f"verdict:{verdict}>={threshold}"
    return False, "below-threshold"


def _severity(verdict: str, notified: bool) -> str:
    if verdict == "urgent":
        return "error"
    if verdict == "notable" and notified:
        return "warning"
    return "info"


async def run_pack(pack: dict[str, Any], scope: dict[str, Any], *, tenant_id: str,
                   overrides: dict[str, Any] | None = None, trigger: str = "schedule",
                   notify: bool = True, notify_connector_ids: list[str] | None = None,
                   task_id: str | None = None) -> dict[str, Any]:
    """Execute one pack run and return the persisted digest."""
    pack = packfile.normalize(pack)
    overrides = overrides or {}
    lookback = int(overrides.get("lookback_hours") or pack["lookback_hours"])
    filters = {**pack["filters"], **(overrides.get("filters") or {})}
    scope_label = sources_mod.scope_label(scope)

    # 1) GATHER
    bundles = await sources_mod.gather(pack["sources"], scope, tenant_id=tenant_id,
                                       lookback_hours=lookback, filters=filters, pack_id=pack["id"])
    flag_codes: set[str] = set()
    total = 0
    for b in bundles:
        flag_codes |= set(b.get("flag_codes") or set())
        total += (b.get("counts") or {}).get("total", 0)

    # 2) REASON
    instructions = reason_mod.fill_placeholders(pack["instructions"], scope_label=scope_label,
                                                lookback_hours=lookback)
    result = await reason_mod.reason(instructions=instructions, bundles=bundles, output=pack["output"])

    # 3) GATE
    should_notify, gate_reason = _gate(
        verdict=result["verdict"],
        threshold=pack["materiality"]["notify_threshold"],
        always_codes=pack["materiality"]["always_notify_if"],
        flag_codes=flag_codes,
    )
    notified = bool(notify and should_notify)

    # Build & persist the digest (always saved, notified or not).
    digest = {
        "id": runs_store.new_id(),
        "pack_id": pack["id"],
        "pack_name": pack["name"],
        "pack_icon": pack["icon"],
        "tenant_id": tenant_id,
        "trigger": trigger,
        "task_id": task_id,
        "scope": scope,
        "scope_label": scope_label,
        "lookback_hours": lookback,
        "verdict": result["verdict"],
        "headline": result["headline"],
        "bullets": result["bullets"],
        "table": result["table"],
        "counts": {"changes": total, "flags": sorted(flag_codes)},
        "sources": [{"source": b["source"], "ok": b.get("ok", False), "note": b.get("note", ""),
                     "counts": b.get("counts", {})} for b in bundles],
        "notified": notified,
        "gate_reason": gate_reason,
        "ai_error": result.get("ai_error"),
        "status": "succeeded",
    }
    runs_store.save_run(tenant_id, digest)

    # 4) DELIVER (only when gated in)
    if notified:
        await _publish(digest, connector_ids=notify_connector_ids or [])
    return digest


async def _publish(digest: dict[str, Any], *, connector_ids: list[str]) -> None:
    from app.notifications.engine import publish

    verdict = digest["verdict"]
    lines = [f"**{digest['headline']}**", ""]
    for b in digest["bullets"][:8]:
        lines.append(f"- {b}")
    body = "\n".join(lines)[:8000]
    try:
        await publish(
            tenant_id=digest["tenant_id"],
            type=f"insight.{verdict}",
            source="insight_pack",
            severity=_severity(verdict, True),
            title=f"{digest['pack_icon']} {digest['pack_name']}: {digest['headline']}"[:200],
            body=body,
            facts={"verdict": verdict, "changes": digest["counts"]["changes"],
                   "scope": digest["scope_label"], "flags": digest["counts"]["flags"]},
            links={"insight_run": digest["id"], "pack_id": digest["pack_id"]},
            fingerprint=f"insight:{digest['pack_id']}:{digest['id']}",
        )
    except Exception:  # noqa: BLE001
        log.warning("Insight pack publish failed", exc_info=True)

    if connector_ids:
        try:
            from app.connectors.notify import deliver_task_result

            await deliver_task_result(
                connector_ids, f"{digest['pack_icon']} {digest['pack_name']}", body[:3000],
                verdict == "urgent",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Insight pack connector delivery failed: %s", exc)
