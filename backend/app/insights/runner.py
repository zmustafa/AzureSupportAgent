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
                   task_id: str | None = None, progress: Any = None) -> dict[str, Any]:
    """Execute one pack run and return the persisted digest.

    ``progress`` (optional) is a callable ``(stage, label, detail, pct)`` used to report
    detailed progress for a background/on-demand run; it is a no-op for scheduled runs.
    """
    def _emit(stage: str, label: str, detail: str = "", pct: int | None = None) -> None:
        if progress is None:
            return
        try:
            progress(stage=stage, label=label, detail=detail, pct=pct)
        except Exception:  # noqa: BLE001 — progress reporting must never break a run
            log.debug("progress callback failed", exc_info=True)

    pack = packfile.normalize(pack)
    overrides = overrides or {}
    lookback = int(overrides.get("lookback_hours") or pack["lookback_hours"])
    filters = {**pack["filters"], **(overrides.get("filters") or {})}
    scope = sources_mod.resolve_scope_names(scope)  # fill workload_names so labels show real names
    scope_label = sources_mod.scope_label(scope)
    _emit("scope", f"Scope resolved · {scope_label}", pct=8)

    # 1) GATHER
    src_labels = [sources_mod.source_label(s) for s in pack["sources"]]
    _emit("gather", f"Gathering signals from {len(src_labels)} source(s)…",
          detail=", ".join(src_labels), pct=15)

    def _on_source(idx: int, total: int, bundle: dict[str, Any]) -> None:
        label = sources_mod.source_label(bundle.get("source", ""))
        cnt = (bundle.get("counts") or {}).get("total", 0)
        if bundle.get("ok"):
            detail = f"{cnt} item(s)" + (f" · {bundle['note']}" if bundle.get("note") else "")
        else:
            detail = bundle.get("note") or "unavailable"
        span = 15 + int(45 * (idx + 1) / max(1, total))  # 15 → 60 across sources
        _emit("gather", f"{label}", detail=detail, pct=span)

    bundles = await sources_mod.gather(pack["sources"], scope, tenant_id=tenant_id,
                                       lookback_hours=lookback, filters=filters, pack_id=pack["id"],
                                       on_source=_on_source)
    flag_codes: set[str] = set()
    total = 0
    for b in bundles:
        flag_codes |= set(b.get("flag_codes") or set())
        total += (b.get("counts") or {}).get("total", 0)
    _emit("gather", f"Collected {total} change(s)",
          detail=(f"{len(flag_codes)} security flag(s)" if flag_codes else "no security flags"), pct=62)

    # 2) REASON
    instructions = reason_mod.fill_placeholders(pack["instructions"], scope_label=scope_label,
                                                lookback_hours=lookback)
    _emit("reason", "Interpreting with AI…", detail="Summarizing what matters for this scope", pct=70)
    result = await reason_mod.reason(instructions=instructions, bundles=bundles, output=pack["output"])
    _emit("reason", f"AI verdict: {result['verdict']}", detail=result.get("headline", "")[:160], pct=86)

    # 3) GATE
    should_notify, gate_reason = _gate(
        verdict=result["verdict"],
        threshold=pack["materiality"]["notify_threshold"],
        always_codes=pack["materiality"]["always_notify_if"],
        flag_codes=flag_codes,
    )
    notified = bool(notify and should_notify)
    # Respect a pack-level snooze: still run + record the digest, just suppress the notify.
    snoozed_until = str(pack.get("snoozed_until") or "")
    if notified and snoozed_until:
        from datetime import datetime, timezone
        try:
            if datetime.fromisoformat(snoozed_until.replace("Z", "+00:00")) > datetime.now(timezone.utc):
                notified = False
                gate_reason = f"snoozed until {snoozed_until}"
        except ValueError:
            pass

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
    _emit("gate", ("Notification will be sent" if notified else "No notification (below threshold)"),
          detail=gate_reason, pct=92)

    # 4) DELIVER (only when gated in)
    if notified:
        _emit("deliver", "Sending notification…", pct=96)
        await _publish(digest, connector_ids=notify_connector_ids or [])
    _emit("done", "Digest ready", detail=result.get("headline", "")[:160], pct=100)
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
