"""Findings service: evaluate the registry over a tenant's snapshot, then overlay human state.

Two stores, and they must never touch each other:

* **the evaluation** is recomputed on every read from the cached access snapshot. Resolution is
  therefore COMPUTED, never clicked — a fingerprint that stops appearing is resolved, and one
  that reappears is new again.
* **``IamFindingState``** is the human layer: suppressions, accepted risk, assignment. A
  collection run must never modify it, and *viewing* findings must never record anything. If it
  did, everything would be marked seen and the next real run would report "nothing changed"
  because somebody looked.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.core.db import SessionLocal
from app.iam import cache, compose, schema, score as score_mod, signals as sig
from app.models import IamFindingState

log = logging.getLogger("app.iam.findings")

# Bound the payload. One finding per affected row explodes on real data; the signals already
# aggregate, but a hostile or pathological estate should still not be able to return 50k rows.
MAX_FINDINGS = 500

# States a human can put a finding into.
STATES = ("open", "in_progress", "suppressed", "accepted")

#: Evaluated signal results: (tenant, cache fingerprint, utc offset) -> results. Bounded so
#: several connections cannot pin every tenant's findings in memory at once.
_EVAL_CACHE: OrderedDict[tuple[str, tuple[str, int], int], list[sig.SignalResult]] = OrderedDict()
MAX_EVAL_MEMO = 6


def build_context(tenant_id: str, *, utc_offset_minutes: int = 0) -> sig.SignalContext:
    directory = cache.read_directory(tenant_id)
    bypass = cache.read_bypass(tenant_id)
    drift = cache.read_drift(tenant_id)
    usage_payload = cache.read_usage(tenant_id)
    return sig.SignalContext(
        tenant_id=tenant_id,
        rows=compose.build_master_rows(tenant_id),
        kpis=compose.compute_overview(tenant_id)["kpis"],
        scopes=_scopes_with_directory(tenant_id),
        directory=directory,
        identities=directory.get("identities", {}),
        federated=directory.get("federated", []),
        bypass_rows=bypass.get("rows", []),
        bypass_summary=bypass.get("summary", {}),
        bypass_assessed=int((bypass.get("summary") or {}).get("assessed") or 0),
        drift=drift,
        drift_available=bool(drift.get("available")),
        utc_offset_minutes=utc_offset_minutes,
        usage=usage_payload,
        # Data-plane operations are absent from the Activity Log unless diagnostic settings ship
        # them elsewhere, which this product does not read. Until it does, data-plane roles stay
        # out of right-sizing rather than being judged on evidence that cannot exist.
        data_plane_logged=False,
        now=datetime.now(timezone.utc),
    )


def _scopes_with_directory(tenant_id: str) -> list[dict[str, Any]]:
    """Scope metadata PLUS the directory layer's own collector statuses.

    ``ctx.collector_ran`` walks ``ctx.scopes``, but the directory layer is not a scope — it is a
    separate cache slice with its own meta. So every collector that runs there
    (``EntraRoleAssignments``, ``FederatedIdentityCredentials``, ``ArgManagedIdentities``,
    ``ServicePrincipalOwners``, ``GroupExpansion``, ``PrincipalDirectory``) was invisible to
    every signal, and any signal gated on one could never be measured.

    Measured on the live `lu` tenant before this fix: **six signals were permanently unmeasured
    on every real tenant** — `esc.mi_privileged`, `esc.mi_shared`,
    `esc.identity_hijack_available`, `esc.fic_loose_subject`, `esc.fic_unknown_issuer` and
    `gov.drift_out_of_band`. Those are the managed-identity and federated-credential escalation
    checks: the most security-relevant detections in the product, reporting "not collected"
    forever while the data sat in the cache the whole time.

    The directory entry is marked so nothing mistakes it for a scanned scope."""
    scopes = list(cache.list_scope_meta(tenant_id))
    meta = cache.read_directory_meta(tenant_id)
    if meta.get("collectors"):
        scopes.append({
            "scope": cache.DIRECTORY_KEY,
            "displayName": "Directory layer",
            "scopeType": schema.SCOPE_DIRECTORY,
            "collectors": meta.get("collectors") or [],
            "generated_at": meta.get("generated_at", ""),
            "status": meta.get("status", ""),
            # Not a scanned scope: it must not be counted by scope-shape signals.
            "synthetic": True,
        })
    return scopes


def evaluate(tenant_id: str, *, utc_offset_minutes: int = 0) -> list[sig.SignalResult]:
    """Run every signal against the tenant's cached snapshot, memoised per snapshot version.

    `/iam/findings`, `/iam/score`, `/iam/frameworks` and `/iam/scanners` each call this, and a
    single visit to the IAM screen hits several of them — so one page load paid the whole signal
    suite three or four times over. It is ~125 ms on a realistic tenant and scales with the row
    count, so on a tenant twice this size that is most of a second of pure duplication.

    Keyed on the cache version, so it expires exactly when the rows do rather than on a timer.
    The offset is part of the key because after-hours judgement depends on it.

    Safe to memoise because NO signal reads wall-clock time: `ctx.now` is unused by every
    signal_def, and the one time-sensitive check compares the reader's offset against timestamps
    already frozen in the cached drift slice. The results are treated as read-only by every
    caller — each one re-derives its own dicts through `Finding.public()`.
    """
    version = cache.cache_fingerprint()
    key = (tenant_id, version, int(utc_offset_minutes))
    hit = _EVAL_CACHE.get(key)
    if hit is not None:
        _EVAL_CACHE.move_to_end(key)
        return hit
    results = sig.evaluate_all(build_context(tenant_id, utc_offset_minutes=utc_offset_minutes))
    _EVAL_CACHE[key] = results
    _EVAL_CACHE.move_to_end(key)
    while len(_EVAL_CACHE) > MAX_EVAL_MEMO:
        _EVAL_CACHE.popitem(last=False)
    return results


async def _state_map(tenant_id: str) -> dict[str, dict[str, Any]]:
    try:
        async with SessionLocal() as db:
            rows = (await db.execute(select(IamFindingState).where(IamFindingState.tenant_id == tenant_id))).scalars().all()
    except Exception:  # noqa: BLE001 - the findings screen must render without the DB
        log.warning("iam findings: could not read finding state", exc_info=True)
        return {}
    return {
        r.fingerprint: {
            "state": r.state,
            "reason": r.reason,
            "updated_by": r.updated_by,
            "updated_at": r.updated_at.isoformat() if r.updated_at else "",
        }
        for r in rows
    }


async def list_findings(
    tenant_id: str,
    *,
    severity: str = "",
    pillar: str = "",
    signal_id: str = "",
    framework: str = "",
    state: str = "",
    include_suppressed: bool = False,
    limit: int = 200,
    offset: int = 0,
    cap: int | None = MAX_FINDINGS,
) -> dict[str, Any]:
    """Findings for a tenant, with human state overlaid.

    Read-only in every sense: it evaluates the cached snapshot and reads the state table. It
    records nothing — see the module docstring for why that matters.

    ``cap`` bounds the page so a pathological estate cannot return 50k rows to a browser. Pass
    ``None`` to lift it — which the EXPORT does, because a workbook that silently stops at 500
    of 1,167 findings is the same defect in a different wrapper."""
    results = evaluate(tenant_id)
    states = await _state_map(tenant_id)

    items: list[dict[str, Any]] = []
    for r in results:
        for f in r.findings:
            pub = f.public()
            st = states.get(pub["id"], {})
            pub["state"] = st.get("state", "open")
            pub["state_reason"] = st.get("reason", "")
            pub["state_updated_by"] = st.get("updated_by", "")
            pub["state_updated_at"] = st.get("updated_at", "")
            pub["why"] = r.spec.why
            items.append(pub)

    # Suppressed findings are hidden by default but never deleted — a suppression that silently
    # disappears is how a known risk becomes an unknown one.
    if not include_suppressed and not state:
        items = [i for i in items if i["state"] not in ("suppressed", "accepted")]
    if severity:
        items = [i for i in items if i["severity"] == severity]
    if pillar:
        items = [i for i in items if i["pillar"] == pillar]
    if signal_id:
        items = [i for i in items if i["signal_id"] == signal_id]
    if framework:
        fl = framework.lower()
        items = [i for i in items if any(fl in f.lower() for f in i["frameworks"])]
    if state:
        items = [i for i in items if i["state"] == state]

    items.sort(key=lambda i: (sig.SEVERITY_RANK.get(i["severity"], 3), -i["count"], i["signal_id"]))
    total = len(items)
    if cap is None:
        page = items[offset:]
        truncated = False
    else:
        truncated = total > cap
        page = items[offset : offset + max(1, min(limit, cap))]

    counts_by_severity = {s: sum(1 for i in items if i["severity"] == s) for s in sig.SEVERITIES}
    counts_by_pillar = {p["key"]: sum(1 for i in items if i["pillar"] == p["key"]) for p in sig.PILLARS}

    return {
        "findings": page,
        "total": total,
        "offset": offset,
        "limit": limit,
        "truncated": truncated,
        "counts_by_severity": counts_by_severity,
        "counts_by_pillar": counts_by_pillar,
        "unmeasured": [
            {"signal_id": r.spec.id, "title": r.spec.title, "pillar": r.spec.pillar, "reason": r.reason}
            for r in results if not r.measured
        ],
    }


def compute_score(tenant_id: str) -> dict[str, Any]:
    return score_mod.compute(evaluate(tenant_id))


async def set_state(
    tenant_id: str,
    fingerprint: str,
    *,
    state: str,
    reason: str,
    actor: str,
) -> dict[str, Any]:
    """Record a human decision about a finding.

    Stored against the fingerprint rather than a row id, so the decision survives re-evaluation:
    the same finding computed tomorrow keeps the suppression, and a genuinely new one does not
    inherit it."""
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}")
    async with SessionLocal() as db:
        existing = (
            await db.execute(
                select(IamFindingState).where(
                    IamFindingState.tenant_id == tenant_id,
                    IamFindingState.fingerprint == fingerprint,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = IamFindingState(tenant_id=tenant_id, fingerprint=fingerprint)
            db.add(existing)
        existing.state = state
        existing.reason = reason[:1000]
        existing.updated_by = actor
        existing.updated_at = datetime.now(timezone.utc)
        await db.commit()
    return {"fingerprint": fingerprint, "state": state, "reason": reason}
