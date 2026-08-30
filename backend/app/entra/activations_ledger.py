"""Append-only ledger of privileged activation sessions.

Every source this product can read forgets. Graph keeps directory audits for 30 days — the
probe confirmed a 90-day query is rejected outright, not merely empty — and the activation
endpoints are bounded too. So "who activated Global Administrator last quarter", the
question an auditor actually asks, is unanswerable from the live APIs no matter how the
query is written.

The ledger is the answer: every refresh folds what it saw into a durable per-tenant record
keyed by session id. Sessions accumulate past the retention cliff, and a session already
recorded is updated rather than duplicated, because the same activation is seen repeatedly
while it is still live and its end time may firm up between reads.

Deliberately boring on disk: one JSON document per tenant through the same
``cache.read_state``/``write_state`` the findings ledger uses, so backup, restore and tenant
deletion already handle it.
"""
from __future__ import annotations

import logging
from typing import Any

from app.entra import cache

log = logging.getLogger("app.entra.activations_ledger")

STATE_NAME = "activation_ledger"

# Ceiling on retained sessions per tenant. At roughly 700 bytes a session this is a few tens
# of megabytes worst case, and it is ~14 years of a tenant that elevates 20 times a day.
# When it bites, the OLDEST are dropped and the trim is reported rather than done silently.
MAX_SESSIONS = 100_000

# Fields the live source may sharpen after we first record a session. Everything else is
# fixed at activation time, so a later read must not overwrite it with a blanker value.
_REFINABLE = ("end", "status", "granted_hours", "justification", "ticket_number",
              "ticket_system", "role_name", "principal_name", "principal_upn",
              "principal_type", "scope_name", "tier", "detail_known")


def _load(tenant_id: str) -> dict[str, Any]:
    state = cache.read_state(tenant_id, STATE_NAME, default=None) or {}
    if not isinstance(state, dict):
        return {"sessions": {}, "first_seen": "", "trimmed": 0}
    state.setdefault("sessions", {})
    state.setdefault("first_seen", "")
    state.setdefault("trimmed", 0)
    return state


def _merge(known: dict[str, Any], fresh: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Fold a freshly-read session into the one already on record.

    Returns ``(row, changed)``. A live activation is read many times before it expires, and
    each read can carry a firmer end time or a justification the earlier source lacked — but
    a source that simply cannot express justification must never blank one we already hold.
    """
    row = dict(known)
    changed = False
    for field in _REFINABLE:
        new = fresh.get(field)
        if new in (None, "", False) and known.get(field) not in (None, "", False):
            continue                      # never downgrade a known value to an empty one
        if new != known.get(field):
            row[field] = new
            changed = True
    if changed:
        row["last_seen"] = cache.now_iso()
    return row, changed


def append(tenant_id: str, sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """Record sessions. Returns a summary of what changed."""
    if not tenant_id:
        return {"added": 0, "updated": 0, "total": 0, "trimmed": 0}
    now = cache.now_iso()
    added = updated = 0
    trimmed = 0
    total = 0

    def _mutate(stored: Any) -> dict[str, Any]:
        nonlocal added, updated, trimmed, total
        state = stored if isinstance(stored, dict) else {}
        store = state.setdefault("sessions", {})
        state.setdefault("first_seen", "")
        state.setdefault("trimmed", 0)
        for fresh in sessions:
            sid = str(fresh.get("id") or "")
            if not sid:
                continue
            known = store.get(sid)
            if known is None:
                row = dict(fresh)
                row["first_seen"] = now
                row["last_seen"] = now
                store[sid] = row
                added += 1
            else:
                row, changed = _merge(known, fresh)
                if changed:
                    store[sid] = row
                    updated += 1
        if len(store) > MAX_SESSIONS:
            ordered = sorted(store.items(), key=lambda pair: pair[1].get("start") or "")
            for sid, _row in ordered[: len(store) - MAX_SESSIONS]:
                del store[sid]
                trimmed += 1
            state["trimmed"] = int(state.get("trimmed") or 0) + trimmed
        if not state.get("first_seen"):
            state["first_seen"] = now
        state["last_write"] = now
        total = len(store)
        return state

    cache.mutate_state(tenant_id, STATE_NAME, {}, _mutate)
    return {"added": added, "updated": updated, "total": total, "trimmed": trimmed}


def read(tenant_id: str) -> list[dict[str, Any]]:
    """Every session ever recorded for the tenant, newest activation first."""
    store = _load(tenant_id).get("sessions") or {}
    return sorted(store.values(), key=lambda r: r.get("start") or "", reverse=True)


def stats(tenant_id: str) -> dict[str, Any]:
    """What the ledger holds — surfaced so the UI can say how far back history goes."""
    state = _load(tenant_id)
    rows = list((state.get("sessions") or {}).values())
    starts = sorted(r.get("start") or "" for r in rows if r.get("start"))
    return {
        "total": len(rows),
        "earliest": starts[0] if starts else "",
        "latest": starts[-1] if starts else "",
        "first_seen": state.get("first_seen", ""),
        "last_write": state.get("last_write", ""),
        "trimmed": int(state.get("trimmed") or 0),
    }


def merge_with_live(tenant_id: str, live: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Live sessions plus everything the ledger remembers that has since aged out.

    Live wins on conflict: it is the current truth for anything still in the window.
    """
    by_id = {str(r.get("id") or ""): r for r in read(tenant_id)}
    for row in live:
        by_id[str(row.get("id") or "")] = row
    by_id.pop("", None)
    return sorted(by_id.values(), key=lambda r: r.get("start") or "", reverse=True)
