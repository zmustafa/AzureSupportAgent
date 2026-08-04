"""Entra ID Support Agent API — posture, findings, Conditional Access and refresh.

Contract (see docs/improvement-plans/entra-support-agent/20-api-contract.md):

* **Every GET reads the cache only.** A cold cache returns HTTP 200 with ``meta.loaded=false``
  and a prompt to refresh — never a 500, never a silent live tenant scan.
* **Every response carries ``meta``** with freshness, coverage, per-domain status, licences
  and the permission summary. The frontend renders ``meta`` before it renders data, which is
  what makes blindness impossible to hide.
* **A blind domain is never an HTTP error.** It is ``meta.domains[d].status == "blind"`` with
  the exact missing permission named.
* ``POST /entra/refresh`` is the only path that calls Microsoft Graph.

Read-only: no endpoint here writes to the directory, and no Graph write scope is requested.
"""
from __future__ import annotations

import asyncio
import csv
import datetime as _dt
import io
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.core.db import get_db
from app.core.security import Principal, require_permission
from app.entra import DOMAINS, blastradius, ca_engine, ca_simulator, cache, demo as demo_mod, job, model, permissions_probe
from app.entra import investigate
from app.entra import guests as guests_mod
from app.entra import investigate_activity as inv_activity
from app.entra import scanners as scanners_mod
from app.entra import export as entra_export
from app.entra import ca_exposure as ca_exposure_mod
from app.entra import signals as sig
from app.entra import snapshot as snapshot_mod
from app.entra import score as score_mod
from app.entra.graphclient import GraphClient
from app.models import AuditLog

router = APIRouter(prefix="/entra", tags=["entra"])

# How many options a picker returns. Enough that a browser <datalist> stays responsive,
# small enough that the payload is not a second copy of the directory. Pickers pair this
# with a server-side query and a total, so nothing is silently out of reach.
_PICK_LIMIT = 300

require_read = require_permission("entra.read")
require_admin = require_permission("entra.admin")
log = logging.getLogger("app.api.entra")


def _target(principal: Principal, connection_id: str | None) -> tuple[dict[str, Any] | None, str, str]:
    """Resolve (connection, tenant_id, connection_id).

    The tenant id comes from the RESOLVED CONNECTION, never from ``principal.tenant_id``
    alone — deriving it from the principal is exactly the bug that leaked data across
    connections in the Estate Graph."""
    from app.core.azure_connections import resolve_connection

    connection = resolve_connection(connection_id)
    tenant_id = (connection or {}).get("tenant_id") or principal.tenant_id or "default"
    cid = connection_id or (connection or {}).get("id") or ""
    return connection, str(tenant_id), cid


def _snapshot(principal: Principal, connection_id: str | None) -> tuple[dict[str, Any], str, str]:
    _conn, tenant_id, cid = _target(principal, connection_id)
    return snapshot_mod.analyse(tenant_id), tenant_id, cid


def _envelope(snapshot: dict[str, Any], cid: str, **body: Any) -> dict[str, Any]:
    return {"meta": snapshot_mod.meta_envelope(snapshot, cid), **body}


def _analysis(snapshot: dict[str, Any]) -> dict[str, Any]:
    return snapshot.get("_analysis") or {}


# ------------------------------------------------------------------------- sorting
# Grids that page or cap server-side have to sort server-side too. Sorting a capped page
# in the browser reorders the rows that survived the cap and calls the result "the top by
# this column", which is a different claim and a wrong one.
#
# The rule is the same one the client uses: a row with no value for the sorted column goes
# LAST whichever way the arrow points. "Not recorded" is not "oldest" and not "zero".
_FINDING_STATE_RANK = {"open": 4, "acknowledged": 3, "snoozed": 2, "suppressed": 1, "resolved": 0}


def _text_key(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text or None


def _apply_sort(
    rows: list[dict[str, Any]],
    keyfns: dict[str, Any],
    sort: str | None,
    direction: str,
) -> list[dict[str, Any]]:
    """Sort in place-ish by a named column, missing values last, stably."""
    keyfn = keyfns.get(sort or "")
    if keyfn is None:
        return rows
    present = [r for r in rows if keyfn(r) is not None]
    missing = [r for r in rows if keyfn(r) is None]
    present.sort(key=keyfn, reverse=direction != "asc")
    return present + missing


# =============================================================== lifecycle / status
@router.get("/status")
async def status(
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Snapshot freshness, per-domain state, licences and granted permissions."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    running = job.is_running(job.job_key(tenant_id))
    return _envelope(
        snapshot, cid,
        domains=[{**meta, "name": name} for name, meta in (snapshot.get("domains") or {}).items()],
        collectable=list(DOMAINS),
        job=job.public_job(job.get_job(job.job_key(tenant_id))),
        refreshing=running,
    )


class RefreshBody(BaseModel):
    domains: list[str] = Field(default_factory=list)
    force: bool = True


@router.post("/refresh")
async def refresh(
    body: RefreshBody | None = None,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Start (or re-attach to) the background collection. The only path that calls Graph."""
    connection, tenant_id, cid = _target(principal, connection_id)
    if connection is None:
        raise HTTPException(status_code=400, detail="No Azure connection is configured.")
    wanted = [d for d in (body.domains if body else []) if d in DOMAINS] or None
    running = job.start_job(tenant_id=tenant_id, connection=connection, domains=wanted, connection_id=cid)
    db.add(AuditLog(
        tenant_id=principal.tenant_id, actor_id=principal.subject,
        action="entra.refresh", target=tenant_id,
        metadata_json={"domains": wanted or list(DOMAINS), "connection_id": cid},
    ))
    await db.commit()
    return {"job": job.public_job(running), "key": running["key"]}


@router.get("/refresh/stream")
async def refresh_stream(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
):
    """SSE progress for the in-flight refresh. Replays the log, then tails."""
    _conn, tenant_id, _cid = _target(principal, connection_id)
    return EventSourceResponse(job.stream(job.job_key(tenant_id)))


@router.get("/diagnostics")
async def diagnostics(
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Collector statistics: Graph requests, batches, throttle events, timings, errors."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    index = cache.tenant_index(tenant_id)
    analysis = _analysis(snapshot)
    return _envelope(
        snapshot, cid,
        graph=(index.get("domains") or {}).get("_graph") or {},
        auth=(index.get("domains") or {}).get("_auth") or {},
        signal_errors=analysis.get("errors") or {},
        not_measured=analysis.get("not_measured") or {},
        permissions=snapshot.get("permissions") or {},
    )


@router.get("/setup/checklist")
async def setup_checklist(
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Which consent tier is granted, what each adds, and what is currently blind."""
    connection, tenant_id, cid = _target(principal, connection_id)
    snapshot = snapshot_mod.analyse(tenant_id)
    permissions = snapshot.get("permissions") or {}
    granted = set(permissions.get("granted") or [])
    tiers = []
    for tier in permissions_probe.TIERS:
        scopes = tier["scopes"]
        have = [s for s in scopes if s in granted]
        tiers.append({
            **tier,
            "granted": have,
            "missing": [s for s in scopes if s not in granted],
            "complete": bool(granted) and len(have) == len(scopes),
        })
    from app.entra.licences import TIER_VALUE

    client_id = str((connection or {}).get("client_id") or "")
    return _envelope(
        snapshot, cid,
        tiers=tiers,
        granted=sorted(granted),
        granted_known=bool(permissions.get("granted_known")),
        claim_error=permissions.get("claim_error", ""),
        domains=permissions.get("domains") or {},
        licence_value=TIER_VALUE,
        # How the tenant actually authenticates: which domains are federated, to whom, and
        # what the on-premises bridge is doing. It belongs on this screen because it decides
        # whether the rest of the product's authentication numbers can be read at face value.
        identity_fabric=_identity_fabric(snapshot),
        # Which app registration to edit. Without it an operator can grant the permission on
        # the wrong app and see no change — the exact failure this screen exists to prevent.
        app_registration={
            "client_id": client_id,
            "tenant_id": tenant_id,
            "portal_url": _app_registration_url(tenant_id, client_id),
        },
        consent_url=_consent_url(tenant_id, client_id),
    )


def _identity_fabric(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The authentication perimeter, shaped for the screen.

    The raw signing certificate never leaves the collector — only the derived facts
    (subject, issuer, thumbprint, expiry) are carried here, the same rule application
    credentials already follow.
    """
    tenant = (snapshot.get("data") or {}).get("tenant") or {}
    fabric = dict(tenant.get("identity_fabric") or {})
    hybrid = dict(tenant.get("hybrid") or {})
    trusts = fabric.get("federation") or []
    return {
        **fabric,
        "hybrid": hybrid,
        "federated": bool(trusts),
        # One line the header can render without re-deriving it in three places.
        "summary": _fabric_summary(fabric, trusts),
    }


def _fabric_summary(fabric: dict[str, Any], trusts: list[dict[str, Any]]) -> str:
    if not fabric.get("readable"):
        return ""
    if not trusts:
        total = len(fabric.get("domains") or [])
        return f"All {total} domain(s) authenticate in Entra ID. No external provider is federated."
    vendors = sorted({(t.get("vendor") or {}).get("label") or "an external provider" for t in trusts})
    share = sum(t.get("user_share") or 0 for t in trusts)
    who = ", ".join(vendors)
    tail = f", {round(share * 100)}% of users" if share else ""
    return (f"{len(trusts)} of {len(fabric.get('domains') or [])} domain(s) federated to "
            f"{who}{tail}.")


def _fabric_brief(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The one-line version, for screens that only need to qualify their own numbers.

    Kept separate from the full block on purpose: the posture header and the auth-methods
    banner have no use for endpoints or certificate facts, and shipping them everywhere
    would spread the authentication perimeter across payloads that do not need it.
    """
    tenant = (snapshot.get("data") or {}).get("tenant") or {}
    fabric = tenant.get("identity_fabric") or {}
    hybrid = tenant.get("hybrid") or {}
    trusts = fabric.get("federation") or []
    return {
        "readable": bool(fabric.get("readable")),
        "federated": bool(trusts),
        "federated_count": fabric.get("federated_count", 0),
        "managed_count": fabric.get("managed_count", 0),
        "vendors": sorted({(t.get("vendor") or {}).get("label") or "" for t in trusts} - {""}),
        "domains": [t.get("domain", "") for t in trusts],
        "user_count": sum(t.get("user_count") or 0 for t in trusts) or None,
        "user_share": round(sum(t.get("user_share") or 0 for t in trusts), 4) or None,
        "sync_enabled": bool(hybrid.get("sync_enabled")),
        "password_sync": hybrid.get("password_sync"),
        "summary": _fabric_summary(fabric, trusts),
    }


def _consent_url(tenant_id: str, client_id: str) -> str:
    """Admin-consent link for THIS connection's app registration.

    This used to return a literal ``<your-app-registration-client-id>`` placeholder, so the
    one action the coverage banner kept telling people to take led to a broken link.

    Consent only grants what the app manifest already requests, so the UI must say "add the
    permission to the app, then consent" — consenting first is a no-op and looks like the
    product ignoring you.
    """
    if not client_id:
        return ""
    return (f"https://login.microsoftonline.com/{tenant_id}/adminconsent"
            f"?client_id={client_id}")


def _app_registration_url(tenant_id: str, client_id: str) -> str:
    """Deep link to the API-permissions blade of the app registration."""
    if not client_id:
        return ""
    return ("https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/"
            f"ApplicationMenuBlade/~/CallAnAPI/appId/{client_id}")


@router.post("/permissions/recheck")
async def permissions_recheck(
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Re-read this connection's permissions from Microsoft, without collecting anything.

    Every screen reports the permissions held *when the snapshot was taken*, so a scope
    granted afterwards stays invisible until the next refresh — which is why "I already
    granted that" and "still missing" can both be true at once. This is the cheap way to
    settle it: one token read plus one $batch of probes, no collection.

    It deliberately does NOT mark any domain as measured. Holding a permission and having
    collected the data are different facts, and a screen that conflated them would claim
    coverage it does not have.
    """
    connection, tenant_id, cid = _target(principal, connection_id)
    if connection is None:
        raise HTTPException(status_code=400, detail="No Azure connection is configured.")

    before = set((cache.tenant_index(tenant_id).get("permissions") or {}).get("granted") or [])
    async with GraphClient(connection) as client:
        permissions = await permissions_probe.build(client, live=True)
    if not permissions.get("token_ok"):
        raise HTTPException(
            status_code=502,
            detail=permissions.get("token_error") or "Could not acquire a Microsoft Graph token.")

    cache.set_tenant_meta(tenant_id, permissions=permissions)
    snapshot_mod.invalidate(tenant_id)

    after = set(permissions.get("granted") or [])
    domains = permissions.get("domains") or {}
    return _envelope(
        snapshot_mod.analyse(tenant_id), cid,
        granted=sorted(after),
        gained=sorted(after - before),
        revoked=sorted(before - after),
        blind_domains=sorted(d for d, s in domains.items() if not s.get("ok")),
        licence_blocked=sorted(d for d, s in domains.items() if s.get("licence_blocked")),
        domains=domains,
        # Permissions are now current; the DATA still reflects the last collection.
        needs_refresh=bool(after - before),
    )


# ========================================================================== posture
@router.get("/posture")
async def posture(
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Identity Posture Score with pillar breakdown, coverage and recoverable points."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    analysis = _analysis(snapshot)
    tenant = (snapshot.get("data", {}).get("tenant") or {}).get("tenant") or {}
    history = cache.score_history(tenant_id)
    previous = history[-2] if len(history) >= 2 else None
    current = analysis.get("score") or {}
    return _envelope(
        snapshot, cid,
        score=current,
        tenant=tenant,
        counts=_counts(snapshot),
        # One line under the tenant name: a federated tenant does not authenticate its own
        # users, which changes how every authentication figure below should be read.
        identity_fabric=_fabric_brief(snapshot),
        trend={
            "previous_score": (previous or {}).get("score"),
            "previous_at": (previous or {}).get("at"),
            "delta": (current.get("score") - previous["score"]) if previous and current else None,
            # Per-pillar movement since the previous FULL refresh. A pillar that was blind on
            # either side has no delta rather than a delta of zero: "unchanged" and "we could
            # not see it" are different answers and the UI must be able to tell them apart.
            "pillar_delta": _pillar_delta(current, previous),
            "points": [
                {
                    "at": h["at"],
                    "score": h["score"],
                    "coverage": h["coverage"],
                    # Written by score.history_entry since the first release, but never served
                    # until the posture screen grew per-pillar sparklines.
                    "pillars": h.get("pillars") or {},
                }
                for h in history[-90:]
            ],
        },
    )


def _pillar_delta(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, int]:
    if not previous:
        return {}
    before = previous.get("pillars") or {}
    out: dict[str, int] = {}
    for pillar in current.get("pillars") or []:
        now, then = pillar.get("score"), before.get(pillar.get("key"))
        if now is not None and then is not None:
            out[pillar["key"]] = now - then
    return out


def _counts(snapshot: dict[str, Any]) -> dict[str, Any]:
    data = snapshot.get("data") or {}
    return {
        "people": (data.get("people") or {}).get("counts") or {},
        "apps": (data.get("apps") or {}).get("counts") or {},
        "roles": (data.get("roles") or {}).get("counts") or {},
        "ca": (data.get("ca") or {}).get("counts") or {},
    }


@router.get("/posture/history")
async def posture_history(
    days: int = Query(default=90, ge=1, le=365),
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    return _envelope(snapshot, cid, history=cache.score_history(tenant_id)[-days:])


@router.get("/posture/pillar/{pillar}")
async def posture_pillar(
    pillar: str,
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """One pillar: its signals, what was measured, and why the rest was not."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    analysis = _analysis(snapshot)
    score = analysis.get("score") or {}
    row = next((p for p in score.get("pillars") or [] if p["key"] == pillar), None)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown pillar.")
    by_signal = analysis.get("by_signal") or {}
    not_measured = analysis.get("not_measured") or {}
    specs = [s for s in sig.registry() if s.pillar == pillar]
    return _envelope(
        snapshot, cid,
        pillar=row,
        signals=[{
            **spec.public(),
            "findings": by_signal.get(spec.id, 0),
            "measured": spec.id in by_signal,
            "not_measured_reason": not_measured.get(spec.id, ""),
        } for spec in specs],
        findings=[f for f in analysis.get("findings") or [] if f.get("pillar") == pillar][:500],
    )


@router.get("/posture/diff")
async def posture_diff(
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """New / resolved / persisting findings versus the previous completed refresh."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    analysis = _analysis(snapshot)
    diff = score_mod.diff_findings(analysis.get("findings") or [], snapshot_mod.previous_findings(tenant_id))
    return _envelope(snapshot, cid, **diff)


# ========================================================================= findings
@router.get("/findings")
async def findings(
    severity: str | None = None,
    pillar: str | None = None,
    signal: str | None = None,
    state: str | None = None,
    search: str | None = None,
    sort: str | None = Query(default=None, pattern="^(severity|title|object|signal|state)$"),
    dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    offset: int = 0,
    limit: int = Query(default=200, ge=1, le=2000),
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Filtered findings with their persisted workflow state."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    analysis = _analysis(snapshot)
    user_state = snapshot_mod.read_state(tenant_id)
    per_finding = user_state.get("findings") or {}

    rows = list(analysis.get("findings") or [])
    if severity:
        wanted = {s.strip() for s in severity.split(",") if s.strip()}
        rows = [f for f in rows if f.get("severity") in wanted]
    if pillar:
        rows = [f for f in rows if f.get("pillar") == pillar]
    if signal:
        rows = [f for f in rows if f.get("signal_id") == signal]
    if search:
        needle = search.lower()
        rows = [f for f in rows if needle in f"{f.get('object_name','')} {f.get('title','')}".lower()]

    def _decorate(f: dict[str, Any]) -> dict[str, Any]:
        st = per_finding.get(f["fingerprint"]) or {}
        return {**f, "state": st.get("state", "open"), "assignee": st.get("assignee", ""),
                "note": st.get("note", ""), "ticket": st.get("ticket", ""),
                "first_seen": st.get("first_seen", "")}

    rows = [_decorate(f) for f in rows]
    if state:
        rows = [f for f in rows if f["state"] == state]

    rows = _apply_sort(rows, {
        "severity": lambda r: model.SEVERITY_RANK.get(r.get("severity"), None),
        "title": lambda r: _text_key(r.get("title")),
        "object": lambda r: _text_key(r.get("object_name")),
        "signal": lambda r: _text_key(r.get("signal_id")),
        "state": lambda r: _FINDING_STATE_RANK.get(r.get("state"), None),
    }, sort, dir)

    total = len(rows)
    page = rows[offset: offset + limit]
    spec_index = {s.id: s for s in sig.registry()}
    return _envelope(
        snapshot, cid,
        findings=page, total=total, offset=offset, limit=limit,
        sort=sort or "", dir=dir,
        by_severity=model.count_by_severity(rows),
        signals={sid: spec_index[sid].public() for sid in {f["signal_id"] for f in page} if sid in spec_index},
        suppressed_count=len(user_state.get("suppressed") or []),
    )


@router.get("/findings/{fingerprint}")
async def finding_detail(
    fingerprint: str,
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    analysis = _analysis(snapshot)
    found = next((f for f in analysis.get("findings") or [] if f["fingerprint"] == fingerprint), None)
    if found is None:
        raise HTTPException(status_code=404, detail="Finding not found in the current snapshot.")
    spec = sig.by_id(found["signal_id"])
    user_state = (snapshot_mod.read_state(tenant_id).get("findings") or {}).get(fingerprint) or {}
    return _envelope(snapshot, cid, finding={**found, **user_state},
                     signal=spec.public() if spec else None)


class FindingStateBody(BaseModel):
    state: str = "open"
    reason: str = ""
    assignee: str = ""
    note: str = ""


@router.post("/findings/{fingerprint}/state")
async def set_finding_state(
    fingerprint: str,
    body: FindingStateBody,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Acknowledge / snooze / suppress a finding.

    This state lives in ``findings_state.json`` and is NEVER rewritten by a collection run —
    a suppression that disappears on the next refresh is worse than no suppression."""
    _conn, tenant_id, cid = _target(principal, connection_id)
    valid = {"open", "acknowledged", "snoozed", "suppressed"}
    if body.state not in valid:
        raise HTTPException(status_code=400, detail=f"state must be one of {sorted(valid)}")
    if body.state == "suppressed" and not body.reason.strip():
        raise HTTPException(status_code=400, detail="A suppression requires a reason.")

    state = snapshot_mod.read_state(tenant_id)
    per = state.setdefault("findings", {})
    entry = per.setdefault(fingerprint, {"first_seen": model.now_iso()})
    entry.update({"state": body.state, "reason": body.reason, "assignee": body.assignee,
                  "note": body.note, "updated_at": model.now_iso()})
    suppressed = set(state.get("suppressed") or [])
    suppressed.discard(fingerprint)
    if body.state == "suppressed":
        suppressed.add(fingerprint)
    state["suppressed"] = sorted(suppressed)
    snapshot_mod.write_state(tenant_id, state)

    db.add(AuditLog(
        tenant_id=principal.tenant_id, actor_id=principal.subject,
        action="entra.finding_state", target=fingerprint,
        metadata_json={"state": body.state, "reason": body.reason[:400]},
    ))
    await db.commit()
    return {"ok": True, "fingerprint": fingerprint, "state": body.state}


@router.get("/signals")
async def signals_catalogue(principal: Principal = Depends(require_read)) -> dict[str, Any]:
    """The Signal Registry itself — every check, its pillar, weight and remediation."""
    return {
        "pillars": sig.PILLARS,
        "signals": [s.public() for s in sig.registry()],
        "registry_version": sig.registry_version(),
    }


# ============================================================== Conditional Access
def _require_ca(snapshot: dict[str, Any]) -> dict[str, Any]:
    analysis = snapshot.get("_ca_analysis") or {}
    if not analysis:
        return {}
    return analysis


@router.get("/ca/policies")
async def ca_policies(
    state: str | None = None,
    search: str | None = None,
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Normalized policies: resolved user sets, decoded controls and derived flags."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    analysis = _require_ca(snapshot)
    rows = list(analysis.get("policies") or [])
    if state:
        rows = [p for p in rows if p.get("state") == state]
    if search:
        needle = search.lower()
        rows = [p for p in rows if needle in str(p.get("display_name", "")).lower()]
    # Effective id lists are large; the grid never needs them.
    slim = [{k: v for k, v in p.items() if k not in ("effective_ids", "included_ids", "excluded_ids")}
            for p in rows]
    return _envelope(snapshot, cid, policies=slim, counts=analysis.get("counts") or {},
                     named_locations=(snapshot.get("data", {}).get("ca") or {}).get("named_locations") or [],
                     auth_strengths=(snapshot.get("data", {}).get("ca") or {}).get("auth_strengths") or [])


@router.get("/ca/policy/{policy_id}")
async def ca_policy(
    policy_id: str,
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    analysis = _require_ca(snapshot)
    found = next((p for p in analysis.get("policies") or [] if p["id"] == policy_id), None)
    if found is None:
        raise HTTPException(status_code=404, detail="Policy not found in the current snapshot.")
    users = {str(u["id"]): u for u in (snapshot.get("data", {}).get("people") or {}).get("users") or []}

    def _names(ids: list[str], cap: int = 100) -> list[dict[str, str]]:
        return [{"id": i, "name": (users.get(i) or {}).get("upn") or (users.get(i) or {}).get("display_name") or i}
                for i in ids[:cap]]

    conflicts = [c for c in analysis.get("conflicts") or []
                 if c["policy_id"] == policy_id or c.get("other_id") == policy_id]
    return _envelope(
        snapshot, cid,
        policy={k: v for k, v in found.items() if k not in ("effective_ids", "included_ids", "excluded_ids")},
        effective_sample=_names(found.get("effective_ids") or []),
        excluded_sample=_names(found.get("excluded_ids") or []),
        conflicts=conflicts,
    )


@router.get("/ca/coverage")
async def ca_coverage(
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """The coverage matrix and the headline sentence the page exists for."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    analysis = _require_ca(snapshot)
    coverage = analysis.get("coverage") or {}
    users = {str(u["id"]): u for u in (snapshot.get("data", {}).get("people") or {}).get("users") or []}
    headline = dict(coverage.get("headline") or {})
    if headline:
        headline["uncovered_user_sample"] = [
            {"id": i, "name": (users.get(i) or {}).get("upn") or i}
            for i in headline.get("uncovered_user_sample") or []
        ]
        headline["privileged_uncovered_sample"] = [
            {"id": i, "name": (users.get(i) or {}).get("upn") or i}
            for i in headline.get("privileged_uncovered_sample") or []
        ]
    return _envelope(
        snapshot, cid,
        cohorts=coverage.get("cohorts") or [],
        app_classes=coverage.get("app_classes") or [],
        derived_classes=coverage.get("derived_classes") or [],
        controls=coverage.get("controls") or [],
        taxonomy_version=coverage.get("taxonomy_version") or "",
        app_index=coverage.get("app_index") or {},
        derived=coverage.get("derived") or {},
        matrix=[{**row, "cells": {k: {kk: vv for kk, vv in cell.items() if kk != "uncovered_sample"}
                                  for k, cell in (row.get("cells") or {}).items()}}
                for row in coverage.get("matrix") or []],
        headline=headline,
    )


@router.get("/ca/coverage/cell")
async def ca_coverage_cell(
    cohort: str,
    app_class: str,
    control: str,
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Drill-down for one matrix cell: which policies produced it and who is left out."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    analysis = _require_ca(snapshot)
    row = next((r for r in (analysis.get("coverage") or {}).get("matrix") or []
                if r.get("cohort") == cohort), None)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown cohort.")
    cell = (row.get("cells") or {}).get(f"{app_class}|{control}")
    if cell is None:
        raise HTTPException(status_code=404, detail="Unknown cell.")
    users = {str(u["id"]): u for u in (snapshot.get("data", {}).get("people") or {}).get("users") or []}
    # Resolve the missing application ids to names. A drawer listing raw GUIDs tells the reader
    # a number is wrong but not which application to go and fix.
    sps = (snapshot.get("data", {}).get("apps") or {}).get("service_principals") or []
    app_names = {str(s.get("app_id") or "").lower(): str(s.get("display_name") or "") for s in sps}
    return _envelope(
        snapshot, cid,
        cohort=row.get("label"), app_class=app_class, control=control, cell=cell,
        apps_missing=[{"app_id": a, "name": app_names.get(str(a).lower()) or a}
                      for a in cell.get("apps_missing") or []],
        uncovered=[{"id": i, "name": (users.get(i) or {}).get("upn") or i,
                    "mfa_registered": (users.get(i) or {}).get("mfa_registered")}
                   for i in cell.get("uncovered_sample") or []],
    )


def _exposure_findings(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Only the application-class exposure detectors.

    Filtering on ``object_kind in ("app_class", "app")`` looks equivalent and is not: every
    app-hygiene signal in the product emits ``object_kind="app"``, so a real tenant dragged in
    277 expired-certificate and multi-tenant-consent findings. They key off an application GUID
    rather than a class id, so they landed in no row while still inflating every count on the
    page.
    """
    from app.entra.signal_defs import ca_appclass

    wanted = {s.id for s in ca_appclass.SPECS}
    return [f for f in _analysis(snapshot).get("findings") or [] if f.get("signal_id") in wanted]


@router.get("/ca/exposure")
async def ca_exposure(
    cohort: str = "members",
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """One row per application class, ordered by what is actually exposed.

    The matrix is the audit view; this is the work queue. It collapses the control axis and
    joins the findings that fired so the first row is the one worth acting on."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    analysis = _require_ca(snapshot)
    coverage = analysis.get("coverage") or {}
    return _envelope(snapshot, cid,
                     **ca_exposure_mod.build(coverage, _exposure_findings(snapshot), cohort=cohort))


@router.get("/ca/exposure/export")
async def ca_exposure_export(
    fmt: str = Query(default="csv", pattern="^(csv|json)$"),
    cohort: str = "members",
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
):
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    analysis = _require_ca(snapshot)
    coverage = analysis.get("coverage") or {}
    exposure = ca_exposure_mod.build(coverage, _exposure_findings(snapshot), cohort=cohort)
    if fmt == "json":
        return _envelope(snapshot, cid, **exposure)

    rows = ca_exposure_mod.to_csv_rows(exposure)
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="ca-exposure.csv"'},
    )


@router.get("/ca/conflicts")
async def ca_conflicts(
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    analysis = _require_ca(snapshot)
    conflicts = analysis.get("conflicts") or []
    kinds: dict[str, int] = {}
    for c in conflicts:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    return _envelope(snapshot, cid, conflicts=conflicts, by_kind=kinds)


@router.get("/ca/breakglass")
async def ca_breakglass(
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    analysis = _require_ca(snapshot)
    return _envelope(snapshot, cid, **(analysis.get("breakglass") or {}))


class BreakGlassBody(BaseModel):
    user_id: str
    confirmed: bool
    note: str = ""


@router.post("/ca/breakglass/confirm")
async def ca_breakglass_confirm(
    body: BreakGlassBody,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Confirm or reject a break-glass candidate.

    Detection is heuristic on purpose. Auto-classifying an account as emergency access and
    then excluding it from findings would be dangerous, so the decision is always the
    operator's and it persists in ``findings_state``."""
    _conn, tenant_id, cid = _target(principal, connection_id)
    state = snapshot_mod.read_state(tenant_id)
    state.setdefault("breakglass", {})[body.user_id] = {
        "confirmed": bool(body.confirmed), "note": body.note,
        "by": principal.subject, "at": model.now_iso(),
    }
    snapshot_mod.write_state(tenant_id, state)
    db.add(AuditLog(
        tenant_id=principal.tenant_id, actor_id=principal.subject,
        action="entra.breakglass_confirm", target=body.user_id,
        metadata_json={"confirmed": bool(body.confirmed), "note": body.note[:400]},
    ))
    await db.commit()
    return {"ok": True, "user_id": body.user_id, "confirmed": bool(body.confirmed)}


@router.get("/ca/export")
async def ca_export(
    format: str = Query(default="json", pattern="^(json|markdown)$"),  # noqa: A002 - matches the query name
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Policy-as-code export. Resolves GUIDs to names alongside the raw ids so the artifact
    is both readable and re-applyable."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    analysis = _require_ca(snapshot)
    policies = analysis.get("policies") or []
    if format == "markdown":
        return _envelope(snapshot, cid, format="markdown", content=_ca_markdown(policies, snapshot))
    return _envelope(snapshot, cid, format="json", content=[
        {k: v for k, v in p.items() if k not in ("effective_ids", "included_ids", "excluded_ids")}
        for p in policies
    ])


def _ca_markdown(policies: list[dict[str, Any]], snapshot: dict[str, Any]) -> str:
    lines = ["# Conditional Access policy book", ""]
    lines.append(f"Snapshot: {snapshot.get('generated_at') or 'unknown'}")
    lines.append("")
    for p in policies:
        c = p.get("conditions") or {}
        lines += [
            f"## {p.get('display_name')} — `{p.get('state')}`",
            "",
            f"- **Effective users:** {p.get('effective_user_count')} "
            f"(excluded {p.get('excluded_user_count')})",
            f"- **Applications:** {', '.join(c.get('include_apps') or []) or 'none'}"
            + (f" (excluding {', '.join(c.get('exclude_apps'))})" if c.get("exclude_apps") else ""),
            f"- **Client apps:** {', '.join(c.get('client_app_types') or []) or 'all'}",
            f"- **Controls:** {', '.join(p.get('controls') or []) or 'none'} "
            f"({p.get('grant', {}).get('operator', 'OR')})",
            f"- **Fingerprint:** `{p.get('fingerprint')}`",
            "",
        ]
    return "\n".join(lines)


# ============================================================================ demo
@router.post("/demo/seed")
async def seed_demo(
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Populate a synthetic tenant so the area works offline. Local/demo use only."""
    return demo_mod.seed()


# ============================================================ privileged access (P3)
@router.get("/privileged/overview")
async def privileged_overview(
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Privilege KPIs: standing vs eligible, PIM configuration health, cross-plane."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    data = snapshot.get("data") or {}
    roles = data.get("roles") or {}
    pim = data.get("pim") or {}
    link = data.get("_azure_link") or {}
    analysis = _analysis(snapshot)

    from app.entra.collectors.pim import privileged_policies
    from app.entra.collectors.roles import global_admin_ids, privileged_principal_ids

    policies = privileged_policies(pim, roles)
    standing = [a for a in roles.get("assignments") or []
                if a.get("role_privileged") and not a.get("activated")]
    return _envelope(
        snapshot, cid,
        counts={
            **(roles.get("counts") or {}),
            "global_admins": len(global_admin_ids(roles)),
            "privileged_principals": len(privileged_principal_ids(roles)),
            "standing_privileged": len(standing),
            "eligible": len(roles.get("eligible") or []),
            "pim_policies": len(policies),
            "pim_fully_configured": sum(1 for p in policies if p["score"] == 100),
            "cross_plane": sum(1 for f in analysis.get("findings") or []
                               if f["signal_id"] == "priv.cross_plane_power"),
        },
        capabilities={**(roles.get("capabilities") or {}), **(pim.get("capabilities") or {})},
        azure_link={
            "available": link.get("available", False),
            "reason": link.get("reason", ""),
            "generated_at": link.get("generated_at", ""),
            "stale": link.get("stale", False),
            "counts": link.get("counts") or {},
        },
        findings=[f for f in analysis.get("findings") or [] if f.get("pillar") == "priv"][:200],
    )


@router.get("/privileged/assignments")
async def privileged_assignments(
    kind: str = Query(default="standing", pattern="^(standing|eligible|all)$"),
    tier: str | None = None,
    principal_type: str | None = None,
    privileged: bool = False,
    search: str | None = None,
    sort: str | None = Query(default=None, pattern="^(principal|type|role|tier|kind|permanent|activation)$"),
    dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Role assignments with permanence, path and PIM context."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    roles = (snapshot.get("data") or {}).get("roles") or {}
    pim = (snapshot.get("data") or {}).get("pim") or {}

    from app.entra.collectors.pim import last_activation

    rows: list[dict[str, Any]] = []
    if kind in ("standing", "all"):
        rows += [{**a, "assignment_kind": "active"} for a in roles.get("assignments") or []]
        rows += [{**a, "assignment_kind": "group-derived"} for a in roles.get("group_derived") or []]
    if kind in ("eligible", "all"):
        rows += [{**e, "assignment_kind": "eligible"} for e in roles.get("eligible") or []]
    if tier:
        rows = [r for r in rows if r.get("role_tier") == tier]
    if principal_type:
        rows = [r for r in rows if r.get("principal_type") == principal_type]
    # Directory role assignments include plenty of unprivileged roles. The overview tiles
    # are about privileged access specifically, so they need to be able to say so rather
    # than sending the reader to a grid where most rows are beside the point.
    if privileged:
        rows = [r for r in rows if r.get("role_privileged")]
    if search:
        needle = search.lower()
        rows = [r for r in rows
                if needle in f"{r.get('principal_name','')} {r.get('principal_upn','')} "
                             f"{r.get('role_name','')}".lower()]
    for r in rows:
        r["last_activation"] = last_activation(pim, str(r.get("principal_id") or ""),
                                               str(r.get("role_id") or ""))
    rows.sort(key=lambda r: (not r.get("role_privileged"), r.get("role_tier", "z"),
                             r.get("role_name", ""), r.get("principal_name", "")))
    rows = _apply_sort(rows, {
        "principal": lambda r: _text_key(r.get("principal_name") or r.get("principal_upn")),
        "type": lambda r: _text_key(r.get("principal_type")),
        "role": lambda r: _text_key(r.get("role_name")),
        "tier": lambda r: {"tier0": 3, "tier1": 2, "tier2": 1}.get(str(r.get("role_tier") or ""), None),
        "kind": lambda r: _text_key(r.get("assignment_kind")),
        "permanent": lambda r: None if r.get("permanent") is None else (1 if r.get("permanent") else 0),
        "activation": lambda r: r.get("last_activation") or None,
    }, sort, dir)
    return _envelope(snapshot, cid, assignments=rows[:2000], total=len(rows),
                     sort=sort or "", dir=dir,
                     capabilities=roles.get("capabilities") or {})


@router.get("/privileged/pim-policies")
async def privileged_pim_policies(
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Per-role PIM configuration health grid — the dataset nothing else exposes."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    data = snapshot.get("data") or {}
    pim = data.get("pim") or {}

    from app.entra.collectors.pim import privileged_policies

    controls = [
        {"key": "mfa_on_activation", "label": "MFA on activation"},
        {"key": "approval_required", "label": "Approval"},
        {"key": "justification_required", "label": "Justification"},
        {"key": "ticket_required", "label": "Ticket"},
        {"key": "duration_bounded", "label": "Bounded duration"},
        {"key": "notifications", "label": "Notifications"},
    ]
    return _envelope(
        snapshot, cid,
        controls=controls,
        policies=privileged_policies(pim, data.get("roles") or {}),
        capabilities=pim.get("capabilities") or {},
        domain=(snapshot.get("domains") or {}).get("pim") or {},
    )


@router.get("/privileged/activity")
async def privileged_activity(
    days: int = Query(default=90, ge=1, le=365),
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """PIM activation history with justification-quality analysis."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    pim = (snapshot.get("data") or {}).get("pim") or {}
    roles = (snapshot.get("data") or {}).get("roles") or {}
    role_names = {d.get("id"): d.get("display_name") for d in roles.get("definitions") or []}
    users = {str(u["id"]): u for u in ((snapshot.get("data") or {}).get("people") or {}).get("users") or []}

    cutoff = cache.age_seconds
    rows = []
    for a in pim.get("activations") or []:
        age = cutoff(a.get("created_at", ""))
        if age is not None and age > days * 86400:
            continue
        u = users.get(a.get("principal_id")) or {}
        rows.append({
            **a,
            "role_name": role_names.get(a.get("role_id"), ""),
            "principal_name": u.get("upn") or u.get("display_name") or a.get("principal_id"),
            "justification_length": len(str(a.get("justification") or "").strip()),
        })
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return _envelope(snapshot, cid, activations=rows[:1000], total=len(rows),
                     counts=pim.get("counts") or {},
                     capabilities=pim.get("capabilities") or {})


# ------------------------------------------------------------ activation sessions (P1-P6)
_ACTIVATION_PAGE = 500


def _business_hours(iso: str, start_hour: int, end_hour: int) -> bool | None:
    """Is this timestamp inside the tenant's working day? None when unparseable.

    Activations are recorded in UTC. Comparing a UTC hour against a local working day is the
    obvious bug here and it produces confidently wrong findings, so the caller supplies the
    tenant's offset and this only ever sees local time.
    """
    from app.entra.collectors.activations import parse_time

    when = parse_time(iso)
    if when is None:
        return None
    if when.weekday() >= 5:
        return False
    return start_hour <= when.hour < end_hour


def _activation_sessions(snapshot: dict[str, Any], tenant_id: str,
                         *, history: bool = True) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Live sessions merged with the durable ledger, newest first."""
    from app.entra import activations_ledger

    domain = (snapshot.get("data") or {}).get("activations") or {}
    live = list(domain.get("sessions") or [])
    rows = activations_ledger.merge_with_live(tenant_id, live) if history else live
    return rows, domain


def _decorate(rows: list[dict[str, Any]], snapshot: dict[str, Any],
              offset_hours: float, day: tuple[int, int]) -> list[dict[str, Any]]:
    """Resolve names the source did not carry and add the derived judgements."""
    data = snapshot.get("data") or {}
    roles = data.get("roles") or {}
    role_names = {str(d.get("id")): str(d.get("display_name") or "")
                  for d in roles.get("definitions") or []}
    users = {str(u.get("id")): u for u in (data.get("people") or {}).get("users") or []}
    from datetime import timedelta

    from app.entra.collectors.activations import parse_time
    from app.entra.collectors.roles import tier_of

    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if not item.get("role_name"):
            item["role_name"] = role_names.get(str(item.get("role_id") or ""), "")
            if item["role_name"]:
                item["tier"] = tier_of(item["role_name"])
        if not item.get("principal_name"):
            user = users.get(str(item.get("principal_id") or "")) or {}
            item["principal_name"] = str(user.get("display_name") or "")
            item["principal_upn"] = str(user.get("upn") or "")
        item["label"] = (item.get("principal_upn") or item.get("principal_name")
                         or f"unresolved principal {item.get('principal_id', '')}")
        justification = str(item.get("justification") or "").strip()
        item["justification_length"] = len(justification)
        # Only judge a justification when the source can actually carry one; scoring a blank
        # from the instances fallback would blame the operator for our own blind spot.
        item["justification_quality"] = (
            "unknown" if not item.get("detail_known")
            else "missing" if not justification
            else "weak" if len(justification) < 15
            else "ok")
        start_local = parse_time(item.get("start") or "")
        if start_local is not None:
            local = (start_local + timedelta(hours=offset_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
            item["start_local"] = local
            item["in_business_hours"] = _business_hours(local, day[0], day[1])
        else:
            item["start_local"] = ""
            item["in_business_hours"] = None
        out.append(item)
    return out


@router.get("/privileged/activations")
async def privileged_activations(
    connection_id: str | None = None,
    days: int = 90,
    plane: str = "",
    tier: str = "",
    q: str = "",
    principal_id: str = "",
    history: bool = True,
    utc_offset_hours: float = 0.0,
    business_start: int = 8,
    business_end: int = 18,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Privileged activation sessions across Entra ID and Azure subscriptions.

    Merges what the current snapshot can see with the durable ledger, so history reaches
    past the 30 days Graph retains. Read-only and cache-only: this never calls Microsoft.
    """
    from app.entra import activations_ledger

    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    rows, domain = _activation_sessions(snapshot, tenant_id, history=history)
    rows = _decorate(rows, snapshot, utc_offset_hours, (business_start, business_end))

    cutoff = cache.age_seconds
    scoped: list[dict[str, Any]] = []
    for row in rows:
        age = cutoff(row.get("start") or "")
        if days and age is not None and age > days * 86400:
            continue
        if plane and row.get("plane") != plane:
            continue
        if tier and row.get("tier") != tier:
            continue
        if principal_id and str(row.get("principal_id") or "") != principal_id:
            continue
        if q:
            needle = q.strip().lower()
            haystack = " ".join(str(row.get(k) or "") for k in
                                ("label", "principal_name", "principal_upn", "role_name",
                                 "scope_name", "justification", "ticket_number")).lower()
            if needle not in haystack:
                continue
        scoped.append(row)

    def _count(pred) -> int:
        return sum(1 for r in scoped if pred(r))

    return _envelope(
        snapshot, cid,
        sessions=scoped[:_ACTIVATION_PAGE],
        total=len(scoped),
        capabilities=domain.get("capabilities") or {},
        ledger=activations_ledger.stats(tenant_id),
        lookback_days=domain.get("lookback_days", 0),
        facets={
            "entra": _count(lambda r: r.get("plane") == "entra"),
            "azure": _count(lambda r: r.get("plane") == "azure"),
            "tier0": _count(lambda r: r.get("tier") == "tier0"),
            "out_of_hours": _count(lambda r: r.get("in_business_hours") is False),
            "no_justification": _count(lambda r: r.get("justification_quality") == "missing"),
            "weak_justification": _count(lambda r: r.get("justification_quality") == "weak"),
            "granted_by_other": _count(
                lambda r: bool(r.get("requestor_id")) and not r.get("self_service")),
            "principals": len({r.get("principal_id") for r in scoped if r.get("principal_id")}),
        },
    )


@router.get("/privileged/activations/{session_id:path}/actions")
async def privileged_activation_actions(
    session_id: str,
    connection_id: str | None = None,
    refresh: bool = False,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """What the principal actually did during one activation.

    The only endpoint in this feature that talks to Microsoft, and only for a single window
    on demand — the Activity Log is per-subscription and slow enough that doing this during
    a refresh would add tens of minutes across a large estate.
    """
    from app.entra import activation_actions

    connection, tenant_id, cid = _target(principal, connection_id)
    snapshot = snapshot_mod.analyse(tenant_id)
    rows, _domain = _activation_sessions(snapshot, tenant_id)
    match = next((r for r in rows if str(r.get("id") or "") == session_id), None)
    if match is None:
        raise HTTPException(status_code=404, detail="Unknown activation session.")
    result = await activation_actions.collect_actions(
        tenant_id, connection, match, snapshot.get("data") or {}, refresh=refresh)
    decorated = _decorate([match], snapshot, 0.0, (8, 18))[0]
    return _envelope(snapshot, cid, session=decorated, **result)


@router.get("/privileged/activations-export")
async def privileged_activations_export(
    connection_id: str | None = None,
    days: int = 90,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Evidence pack: every session in the window with its provenance.

    An auditor needs to know not just what happened but where the claim came from and when
    it was read, so each row keeps its source endpoint and the ledger timestamps.
    """
    from app.entra import activations_ledger

    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    rows, domain = _activation_sessions(snapshot, tenant_id)
    rows = _decorate(rows, snapshot, 0.0, (8, 18))
    cutoff = cache.age_seconds
    scoped = [r for r in rows
              if not days or (cutoff(r.get("start") or "") or 0) <= days * 86400]
    return _envelope(
        snapshot, cid,
        generated_at=cache.now_iso(),
        tenant_id=tenant_id,
        window_days=days,
        sessions=scoped,
        total=len(scoped),
        ledger=activations_ledger.stats(tenant_id),
        capabilities=domain.get("capabilities") or {},
        provenance={
            "entra_request": "GET /roleManagement/directory/roleAssignmentScheduleRequests",
            "entra_instance": "GET /roleManagement/directory/roleAssignmentScheduleInstances",
            "azure_request": ("GET /subscriptions/{id}/providers/Microsoft.Authorization"
                              "/roleAssignmentScheduleRequests?api-version=2020-10-01"),
        },
    )


@router.get("/privileged/cross-plane")
async def privileged_cross_plane(
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Entra power alongside Azure power, one row per principal.

    This correlation does not exist in any Microsoft surface, which is exactly why it is
    worth showing — and why its freshness caveat must travel with it."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    data = snapshot.get("data") or {}
    link = data.get("_azure_link") or {}

    from app.entra.signal_defs.priv_pim import entra_power

    users = {str(u["id"]): u for u in (data.get("people") or {}).get("users") or []}
    rows = []
    for pid, entra in entra_power(data).items():
        azure = (link.get("principals") or {}).get(pid) or {}
        u = users.get(pid) or {}
        rows.append({
            "principal_id": pid,
            "name": entra.get("name") or u.get("upn") or u.get("display_name") or azure.get("name") or pid,
            "kind": entra.get("kind", "user"),
            "entra_roles": entra.get("roles") or [],
            "entra_permissions": entra.get("permissions") or [],
            "azure_roles": azure.get("powerful_roles") or [],
            "azure_all_roles": azure.get("role_count", 0),
            "azure_broad_scopes": azure.get("broad_scopes") or [],
            "azure_subscriptions": azure.get("subscriptions") or [],
            "both_planes": bool(azure.get("powerful_roles")),
        })
    rows.sort(key=lambda r: (not r["both_planes"], -len(r["azure_roles"]), r["name"]))
    return _envelope(
        snapshot, cid, rows=rows[:1000], total=len(rows),
        azure_link={"available": link.get("available", False), "reason": link.get("reason", ""),
                    "generated_at": link.get("generated_at", ""), "stale": link.get("stale", False),
                    "age_seconds": link.get("age_seconds"), "counts": link.get("counts") or {}},
    )


@router.get("/privileged/principal/{principal_id}")
async def privileged_principal(
    principal_id: str,
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Full privilege dossier for one principal."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    data = snapshot.get("data") or {}
    roles = data.get("roles") or {}
    pim = data.get("pim") or {}
    link = data.get("_azure_link") or {}
    analysis = _analysis(snapshot)

    from app.entra.collectors.roles import effective_role_names

    users = {str(u["id"]): u for u in (data.get("people") or {}).get("users") or []}
    sps = {str(s["object_id"]): s for s in (data.get("apps") or {}).get("service_principals") or []}
    subject = users.get(principal_id) or sps.get(principal_id)
    if subject is None:
        # NOT a 404. A principal the directory no longer holds is exactly what a reader
        # clicks through to ask about — a deleted object whose assignments survive, or a
        # group / managed identity this endpoint never indexed. Raising here threw away the
        # answer and dead-ended every link into it. The dossier is empty and says so; the
        # Investigate resolver is where the "why" lives.
        return _envelope(
            snapshot, cid,
            principal={
                "id": principal_id, "name": principal_id, "kind": "unknown",
                "enabled": None, "user_type": "", "mfa_registered": None, "last_signin": "",
                "resolution": investigate.NOT_FOUND,
            },
            roles=[], assignments=[], activations=[], azure=None,
            findings=[f for f in analysis.get("findings") or [] if f.get("object_id") == principal_id],
        )

    assignments = [a for a in (roles.get("assignments") or []) + (roles.get("group_derived") or [])
                   + (roles.get("eligible") or []) if a.get("principal_id") == principal_id]
    activations = [a for a in pim.get("activations") or [] if a.get("principal_id") == principal_id]
    return _envelope(
        snapshot, cid,
        principal={
            "id": principal_id,
            "name": subject.get("upn") or subject.get("display_name") or principal_id,
            "kind": "sp" if principal_id in sps else "user",
            "enabled": subject.get("enabled"),
            "user_type": subject.get("user_type", ""),
            "mfa_registered": subject.get("mfa_registered"),
            "last_signin": subject.get("last_signin", ""),
            "resolution": investigate.RESOLVED,
        },
        roles=sorted(effective_role_names(roles, principal_id)),
        assignments=assignments,
        activations=sorted(activations, key=lambda a: a.get("created_at", ""), reverse=True)[:100],
        azure=(link.get("principals") or {}).get(principal_id),
        findings=[f for f in analysis.get("findings") or [] if f.get("object_id") == principal_id],
    )


# ==================================================================== Investigate (identity)
# One principal, everything we already know about it. Owns no collectors: every section
# reads a source another module fills. See docs/improvement-plans/identity-investigate/.
require_investigate = require_permission("investigate.read")
require_investigate_activity = require_permission("investigate.activity")


async def _access_rows(tenant_id: str) -> list[dict[str, Any]]:
    return await investigate.access_rows(tenant_id)


async def _resolve_principal(
    data: dict[str, Any], tenant_id: str, needle: str,
) -> dict[str, Any]:
    return await investigate.resolve(data, tenant_id, needle)


@router.get("/investigate/resolve")
async def investigate_resolve(
    q: str = Query(..., min_length=1, max_length=256),
    connection_id: str | None = None,
    principal: Principal = Depends(require_investigate),
) -> dict[str, Any]:
    """Resolve an object id, UPN, mail address or appId to a principal of any kind.

    Never 404s: an unresolvable principal is the answer, with ``resolution`` saying which
    kind of unresolvable it is."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    data = snapshot.get("data") or {}
    resolved = await _resolve_principal(data, tenant_id, q.strip())
    return _envelope(snapshot, cid, **investigate.envelope(resolved))


@router.get("/investigate/search")
async def investigate_search(
    q: str = Query(..., min_length=2, max_length=256),
    limit: int = Query(default=25, ge=1, le=100),
    connection_id: str | None = None,
    principal: Principal = Depends(require_investigate),
) -> dict[str, Any]:
    """Type-ahead across users, groups and service principals."""
    snapshot, _tenant_id, cid = _snapshot(principal, connection_id)
    results = investigate.search(snapshot.get("data") or {}, q.strip(), limit=limit)
    return _envelope(snapshot, cid, results=results, query=q)


@router.get("/investigate/recent")
async def investigate_recent(
    since: str | None = None,
    limit: int = Query(default=8, ge=1, le=investigate.RECENT_LIMIT),
    connection_id: str | None = None,
    principal: Principal = Depends(require_investigate),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The principals THIS user investigated most recently, newest first.

    Read back from the audit log rather than a second store: every dossier view already
    records who looked at whom, so a separate history would be a duplicate record — and an
    unaudited one.

    Two constraints this endpoint exists to honour:

    * it is gated on ``investigate.read``, NOT ``audit.read``, and hard-filters to the
      caller's own ``actor_id``. Returning anyone else's history would turn a convenience
      into "show me who the compliance team has been looking at";
    * ``since`` is a client-side "cleared at" watermark. Clearing the strip HIDES entries,
      it never deletes audit rows — a history you can erase is not an audit trail.
    """
    from sqlalchemy import select

    snapshot, _tenant_id, cid = _snapshot(principal, connection_id)

    stmt = (
        select(AuditLog)
        .where(
            AuditLog.tenant_id == principal.tenant_id,
            AuditLog.actor_id == principal.subject,
            AuditLog.action == "investigate.view",
        )
        .order_by(AuditLog.created_at.desc())
        # Over-fetch: the rows are per VIEW and collapse to one entry per principal, so a
        # user who reloaded the same dossier twenty times would otherwise get one chip.
        .limit(400)
    )
    if since:
        try:
            cutoff = _dt.datetime.fromisoformat(since.replace("Z", "+00:00"))
            stmt = stmt.where(AuditLog.created_at > cutoff)
        except ValueError:
            pass  # an unparsable watermark must not hide the whole history

    rows = list((await db.execute(stmt)).scalars().all())
    entries = investigate.recent_entries(
        [{"target": r.target, "metadata": r.metadata_json or {}, "at": r.created_at.isoformat()}
         for r in rows],
        connection_id=cid,
        limit=limit,
    )
    entries = investigate.refresh_recent_names(snapshot.get("data") or {}, entries)
    return _envelope(snapshot, cid, recent=entries)


@router.get("/investigate/{principal_id}")
async def investigate_dossier(
    principal_id: str,
    connection_id: str | None = None,
    principal: Principal = Depends(require_investigate),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Everything already collected about one principal, converged.

    Reads caches only — this endpoint makes no Graph or ARM call. Behavioural history
    lives behind ``POST /investigate/{id}/activity`` and its own permission."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    env, sections = await investigate.build_dossier(snapshot, tenant_id, principal_id.strip())
    subject = env["principal"]

    db.add(AuditLog(
        tenant_id=principal.tenant_id, actor_id=principal.subject,
        action="investigate.view", target=str(subject.get("id") or principal_id),
        metadata_json={"kind": subject.get("kind"), "resolution": subject.get("resolution"),
                       # Kept so the recency strip can still name a principal the directory
                       # no longer holds — a deleted object is one you may well return to.
                       "name": subject.get("display_name") or "",
                       "connection_id": cid},
    ))
    await db.commit()

    return _envelope(snapshot, cid, **env, sections=sections)


@router.get("/investigate/{principal_id}/export")
async def investigate_export(
    principal_id: str,
    connection_id: str | None = None,
    principal: Principal = Depends(require_investigate),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """The dossier as a workbook — one sheet per section.

    Goes through the shared builder rather than hand-rolled CSV, which is what
    neutralises formula injection: a display name beginning ``=`` is attacker-influenced
    in a guest-heavy tenant and would otherwise execute on open."""
    from app.core.xlsx import WorkbookBuilder, coerce
    from app.iam import cpu as iam_cpu

    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    env, sections = await investigate.build_dossier(snapshot, tenant_id, principal_id.strip())
    subject = env["principal"]

    def _build() -> bytes:
        wb = WorkbookBuilder()
        sub = subject
        wb.sheet(
            "Identity",
            ["Field", "Value"],
            [[k, coerce(v)] for k, v in [
                ("Name", sub.get("display_name")), ("Kind", sub.get("kind")),
                ("Object id", sub.get("id")), ("UPN", sub.get("upn")),
                ("App id", sub.get("app_id")), ("Enabled", sub.get("enabled")),
                ("Resolution", sub.get("resolution")),
                ("Managing tenant", (sub.get("managing_tenant") or {}).get("name") if sub.get("managing_tenant") else ""),
                ("Tenant", snapshot.get("tenant_id", "")),
                ("Directory collected at", snapshot.get("generated_at", "")),
            ]],
            note="Exported from Investigate. Sections the tenant could not read are named "
                 "on the Provenance sheet rather than omitted.",
        )

        access = sections["access"]["data"]
        wb.sheet("Directory roles", ["Role"], [[r] for r in access["directory_roles"]])
        wb.sheet(
            "Azure access",
            ["Role", "Scope", "Scope type", "Path", "Eligible", "Effect", "Subscription"],
            [[coerce(r.get("roleName")), coerce(r.get("scope")), coerce(r.get("scopeType")),
              coerce(r.get("accessPath")), coerce(r.get("eligible")), coerce(r.get("effect")),
              coerce(r.get("subscriptionName"))]
             for r in access["azure_assignments"]],
        )
        wb.sheet(
            "Findings", ["Severity", "Title", "Signal"],
            [[coerce(f.get("severity")), coerce(f.get("title")), coerce(f.get("signal_id"))]
             for f in sections["findings"]["data"]],
        )
        wb.sheet(
            "Timeline", ["When", "Change", "Detail"],
            [[coerce(e.get("at") or e.get("run_at")), coerce(e.get("kind") or e.get("change")),
              coerce(e.get("detail") or e.get("summary"))]
             for e in sections["timeline"]["data"]["events"]],
        )
        wb.sheet(
            "Activations", ["Start", "End", "Role", "Scope", "Justification", "Ticket"],
            [[coerce(a.get("start")), coerce(a.get("end")), coerce(a.get("role_name")),
              coerce(a.get("scope_name")), coerce(a.get("justification")),
              coerce(a.get("ticket_number"))]
             for a in sections["activations"]["data"]],
        )
        if "members" in sections:
            m = sections["members"]["data"]
            wb.sheet(
                "Members", ["Name", "Kind", "UPN", "Object id"],
                [[coerce(r.get("display_name")), coerce(r.get("kind")), coerce(r.get("upn")),
                  coerce(r.get("id"))]
                 for r in m["members"]],
                note=("Transitive membership: nested groups were expanded away when this was "
                      "collected, so a member here may belong through a subgroup."
                      + (" Membership is rule-derived (dynamic group)." if m.get("dynamic") else "")
                      + (" Membership is authored in on-premises AD." if m.get("on_prem_synced") else "")),
            )
        # Provenance is a SHEET, not a footnote: an auditor reading "no findings" needs to
        # know whether that means none were raised or the domain could not be read.
        wb.sheet(
            "Provenance", ["Section", "Source", "Collected at", "Truncated", "Unreadable", "Reason"],
            [[name, coerce(s["provenance"]["source"]), coerce(s["provenance"]["collected_at"]),
              coerce(s["provenance"]["truncated"]), coerce(s["provenance"]["unreadable"]),
              coerce(s["provenance"]["reason"])]
             for name, s in sections.items()],
        )
        return wb.to_bytes()

    content = await iam_cpu.run(_build, label="investigate export")
    db.add(AuditLog(
        tenant_id=principal.tenant_id, actor_id=principal.subject,
        action="investigate.export", target=str(subject.get("id") or principal_id),
        metadata_json={"kind": subject.get("kind"), "connection_id": cid},
    ))
    await db.commit()
    name = (str(subject.get("display_name") or subject.get("id") or "identity")
            .replace(" ", "_")[:60])
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="investigate_{name}.xlsx"'},
    )


class InvestigateActivityBody(BaseModel):
    types: list[str] = Field(default_factory=lambda: list(inv_activity.EAGER_TYPES))
    days: int = Field(default=3, ge=1, le=365)
    justification: str = Field(default="", max_length=512)


class InvestigateMembersBody(BaseModel):
    """Which branches of the membership tree to open, and in which direction."""

    expand: list[str] = Field(default_factory=list, max_length=100)
    direction: str = Field(default="down", pattern="^(down|up)$")


@router.post("/investigate/{principal_id}/members")
async def investigate_members(
    principal_id: str,
    body: InvestigateMembersBody,
    connection_id: str | None = None,
    principal: Principal = Depends(require_investigate),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """One level of the group's membership tree, per branch asked for.

    POST because it is a live directory read and a recorded act, and because the set of
    opened branches is a body, not a URL.

    Deliberately NOT part of the dossier: the dossier reads caches only and must stay fast
    enough to be linked from dozens of places. Nested structure exists nowhere in those
    caches — both collectors resolve membership transitively, which discards the
    intermediate groups — so a tree can only come from a live call, and a live call must be
    asked for rather than made on everyone's behalf.

    Gated on ``investigate.read`` rather than ``investigate.activity``: group membership is
    a structural fact about access, not behavioural data about a person.
    """
    from app.entra import investigate_members as inv_members

    connection, tenant_id, cid = _target(principal, connection_id)
    snapshot = snapshot_mod.analyse(tenant_id)
    data = snapshot.get("data") or {}

    subject = await _resolve_principal(data, tenant_id, principal_id.strip())
    subject_id = str(subject.get("id") or principal_id)

    if subject.get("kind") != investigate.KIND_GROUP:
        # Answered, not 400'd: asking a user for its members is a reasonable mistake to make
        # from a deep link, and the answer is a sentence.
        return _envelope(
            snapshot, cid, root=subject_id, direction=body.direction, nodes={}, truncated=False,
            notes=[f"{subject.get('display_name') or subject_id} is a "
                   f"{subject.get('kind')}, not a group — only groups have members."],
        )

    result = await inv_members.expand(
        connection, subject_id, expand_ids=list(body.expand), direction=body.direction,
    )

    db.add(AuditLog(
        tenant_id=principal.tenant_id, actor_id=principal.subject,
        action="investigate.members", target=subject_id,
        metadata_json={"direction": body.direction, "branches": len(body.expand) + 1,
                       "connection_id": cid},
    ))
    await db.commit()

    return _envelope(snapshot, cid, root=subject_id, direction=body.direction, **result)


@router.post("/investigate/{principal_id}/activity")
async def investigate_activity(
    principal_id: str,
    body: InvestigateActivityBody,
    connection_id: str | None = None,
    principal: Principal = Depends(require_investigate_activity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """What this principal actually DID, over a window.

    POST rather than GET for three reasons: it is expensive, it carries a justification,
    and it is a recorded act. Reading a named person's sign-in and audit history is
    behavioural data, which is why it sits behind its own permission and why every call
    lands in the audit log with who asked, about whom, and why.

    The Azure Activity Log is never included unless explicitly asked for: it is
    per-subscription and slow, and this screen is linked from dozens of places."""
    from app.entra import activation_actions

    connection, tenant_id, cid = _target(principal, connection_id)
    snapshot = snapshot_mod.analyse(tenant_id)
    data = snapshot.get("data") or {}

    subject = await _resolve_principal(data, tenant_id, principal_id.strip())
    caps = investigate.envelope(subject)["capabilities"]
    subject_id = str(subject.get("id") or principal_id)

    wanted = [t for t in body.types if t in inv_activity.ALL_TYPES]
    notes: list[str] = []
    # Asking for a section this kind cannot have is answered, not silently dropped.
    refused = [t for t in wanted if t not in caps]
    for t in refused:
        notes.append(f"'{t}' does not apply to a {subject.get('kind')} and was not read.")
    wanted = [t for t in wanted if t in caps]

    days, clamp_note = inv_activity.clamp_days(body.days)
    if clamp_note:
        notes.append(clamp_note)
    start_iso, end_iso = inv_activity.window(days)

    sections: dict[str, Any] = {}

    if connection is None:
        notes.append("No Azure connection is attached, so no activity could be read.")
        wanted = []

    if inv_activity.TYPE_SIGNINS in wanted:
        rows, err = await inv_activity.signins(connection, subject, start_iso, end_iso)
        sections[inv_activity.TYPE_SIGNINS] = investigate.section(rows, investigate.provenance(
            "Microsoft Graph /auditLogs/signIns", collected_at=end_iso,
            unreadable=bool(err), reason=(f"Sign-in log {err}" if err else ""),
            truncated=len(rows) >= inv_activity.MAX_SIGNIN_ROWS))

    if inv_activity.TYPE_RISK in wanted:
        rows, err = await inv_activity.risk_detections(connection, subject, start_iso, end_iso)
        sections[inv_activity.TYPE_RISK] = investigate.section(rows, investigate.provenance(
            "Microsoft Graph /identityProtection/riskDetections", collected_at=end_iso,
            unreadable=bool(err), reason=(f"Risk detections {err}" if err else ""),
            truncated=len(rows) >= inv_activity.MAX_RISK_ROWS))

    planes: list[str] = []
    if inv_activity.TYPE_AUDIT in wanted:
        planes.append("entra")
    subs: list[str] = []
    if inv_activity.TYPE_AZURE in wanted:
        planes.append("azure")
        azure_days, azure_clamp = inv_activity.clamp_days(body.days, azure=True)
        if azure_clamp:
            notes.append(azure_clamp)
        if azure_days != days:
            # The Azure log reaches further back than Graph does; say so rather than
            # quietly showing two different windows under one heading.
            notes.append(f"The Azure Activity Log window is {azure_days} days; the Graph "
                         f"sources are limited to {days}.")
        try:
            rows = await _access_rows(tenant_id)
            subs = inv_activity.subscriptions_for(rows, subject_id)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Subscriptions in scope could not be determined: {exc}")
        if subs:
            notes.append(
                f"The Azure Activity Log was read for the {len(subs)} subscription(s) where "
                "this principal currently holds access. Access removed since an action was "
                "taken would put that subscription out of scope.")

    if planes:
        body_actions = await activation_actions.actions_in_window(
            connection, subject_id, start_iso, end_iso, data,
            subscriptions=subs, planes=tuple(planes),
        )
        entra_rows = [a for a in body_actions["actions"] if a.get("plane") == "entra"]
        azure_rows = [a for a in body_actions["actions"] if a.get("plane") == "azure"]
        if inv_activity.TYPE_AUDIT in wanted:
            sections[inv_activity.TYPE_AUDIT] = investigate.section(
                entra_rows, investigate.provenance(
                    "Microsoft Graph /auditLogs/directoryAudits", collected_at=end_iso,
                    truncated=bool(body_actions.get("truncated"))))
        if inv_activity.TYPE_AZURE in wanted:
            sections[inv_activity.TYPE_AZURE] = investigate.section(
                azure_rows, investigate.provenance(
                    "Azure Activity Log (per subscription)", collected_at=end_iso,
                    unreadable=not subs, truncated=bool(body_actions.get("truncated")),
                    reason=("" if subs else
                            "No subscription is in scope for this principal, so no resource "
                            "operations could be read.")))
        notes.extend(body_actions.get("notes") or [])
        attribution = {
            "counts": body_actions["counts"],
            "standing_entra_roles": body_actions["standing_entra_roles"],
            "standing_azure_roles": body_actions["standing_azure_roles"],
            "azure_link_available": body_actions["azure_link_available"],
        }
    else:
        attribution = {}

    db.add(AuditLog(
        tenant_id=principal.tenant_id, actor_id=principal.subject,
        action="investigate.activity", target=subject_id,
        metadata_json={"types": wanted, "days": days, "justification": body.justification,
                       "kind": subject.get("kind"), "connection_id": cid},
    ))
    await db.commit()

    return _envelope(
        snapshot, cid,
        principal=subject,
        window={"start": start_iso, "end": end_iso, "days": days},
        sections=sections,
        attribution=attribution,
        notes=notes,
    )


# ============================================================== Application 360 (P4)
@router.get("/apps")
async def apps_inventory(
    search: str | None = None,
    tier: str | None = None,
    ownerless: bool = False,
    risk_min: int = Query(default=0, ge=0, le=100),
    sort: str | None = Query(default=None, pattern="^(risk|name|permissions|credentials|owners|assigned|tier)$"),
    dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    offset: int = 0,
    limit: int = Query(default=200, ge=1, le=2000),
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Unified application + enterprise-application grid, sorted by risk."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    data = snapshot.get("data") or {}
    apps = data.get("apps") or {}
    sps = {str(s["object_id"]): s for s in apps.get("service_principals") or []}

    rows = []
    for app in apps.get("applications") or []:
        sp = sps.get(app.get("sp_object_id")) or {}
        rows.append(_app_row(app, sp))
    # Enterprise applications with no local registration (third-party consented apps).
    local = {a.get("sp_object_id") for a in apps.get("applications") or []}
    for oid, sp in sps.items():
        if oid in local or sp.get("is_first_party"):
            continue
        rows.append(_app_row({}, sp))

    if search:
        needle = search.lower()
        rows = [r for r in rows if needle in f"{r['display_name']} {r['app_id']}".lower()]
    if tier:
        rows = [r for r in rows if r["max_permission_tier"] == tier]
    if ownerless:
        rows = [r for r in rows if r["owners_known"] and not r["owner_count"]]
    if risk_min:
        rows = [r for r in rows if r["risk_score"] >= risk_min]
    rows.sort(key=lambda r: (-r["risk_score"], r["display_name"]))
    rows = _apply_sort(rows, {
        "risk": lambda r: r.get("risk_score"),
        "name": lambda r: _text_key(r.get("display_name")),
        "permissions": lambda r: r.get("granted_permissions"),
        "credentials": lambda r: r.get("credential_count"),
        # Ownerless is a fact; "we cannot read owners" is not. Only the former sorts.
        "owners": lambda r: r.get("owner_count") if r.get("owners_known") else None,
        "assigned": lambda r: r.get("assigned_principals"),
        "tier": lambda r: (_TIER_ORDER.index(r["max_permission_tier"])
                           if r.get("max_permission_tier") in _TIER_ORDER else None),
    }, sort, dir)

    return _envelope(
        snapshot, cid,
        apps=rows[offset: offset + limit], total=len(rows), offset=offset, limit=limit,
        sort=sort or "", dir=dir,
        counts=apps.get("counts") or {}, capabilities=apps.get("capabilities") or {},
        risk_components=_risk_component_meta(),
    )


def _risk_component_meta() -> list[dict[str, Any]]:
    from app.entra.collectors.apps import RISK_COMPONENTS

    return RISK_COMPONENTS


_TIER_ORDER = ["low", "medium", "high", "critical"]


def _app_row(app: dict[str, Any], sp: dict[str, Any]) -> dict[str, Any]:
    perms = sp.get("granted_app_permissions") or []
    creds = (app.get("credentials") or []) + (sp.get("credentials") or [])
    expiring = [c for c in creds if c.get("days_left") is not None and 0 <= c["days_left"] <= 90]
    risk = app.get("risk") or sp.get("risk") or {}
    return {
        "object_id": app.get("object_id") or sp.get("object_id", ""),
        "sp_object_id": sp.get("object_id", ""),
        "app_id": app.get("app_id") or sp.get("app_id", ""),
        "display_name": app.get("display_name") or sp.get("display_name", ""),
        "has_registration": bool(app),
        "sp_type": sp.get("sp_type", ""),
        "enabled": sp.get("enabled", True),
        "multi_tenant": app.get("multi_tenant", False),
        "verified_publisher": app.get("verified_publisher") or sp.get("verified_publisher", ""),
        "is_external": sp.get("is_external", False),
        "owner_count": len((app.get("owner_ids") or []) + (sp.get("owner_ids") or [])),
        "owners_known": bool(app.get("owners_known") or sp.get("owners_known")),
        "granted_permissions": len(perms),
        "max_permission_tier": max((p.get("tier", "low") for p in perms),
                                   key=lambda t: _TIER_ORDER.index(t) if t in _TIER_ORDER else 0,
                                   default="low"),
        "consent_grant_capable": any(p.get("flags", {}).get("consent_grant") for p in perms),
        "tenant_wide": any(p.get("flags", {}).get(k) for p in perms for k in ("mail", "files", "chat")),
        "credential_count": len(creds),
        "expiring_credentials": len(expiring),
        "expired_credentials": sum(1 for c in creds if c.get("expired")),
        "assigned_principals": sp.get("assigned_principals", 0),
        "orphaned": sp.get("orphaned", False),
        "risk_score": int(risk.get("score") or 0),
        "platform_managed": bool(risk.get("platform_managed")),
    }


@router.get("/apps/{object_id}")
async def app_360(
    object_id: str,
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Application 360 — everything about one application, in one payload."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    data = snapshot.get("data") or {}
    apps = data.get("apps") or {}
    analysis = _analysis(snapshot)

    app = next((a for a in apps.get("applications") or []
                if a.get("object_id") == object_id or a.get("app_id") == object_id), None)
    sp = next((s for s in apps.get("service_principals") or []
               if s.get("object_id") == object_id
               or (app and s.get("object_id") == app.get("sp_object_id"))), None)
    if app is None and sp is None:
        raise HTTPException(status_code=404, detail="Application not found in the current snapshot.")
    app = app or {}
    sp = sp or {}

    users = {str(u["id"]): u for u in (data.get("people") or {}).get("users") or []}
    owner_ids = (app.get("owner_ids") or []) + (sp.get("owner_ids") or [])
    owners = [{"id": oid, "name": (users.get(oid) or {}).get("upn")
               or (users.get(oid) or {}).get("display_name") or oid} for oid in dict.fromkeys(owner_ids)]

    # Conditional Access coverage for this application.
    ca_analysis = snapshot.get("_ca_analysis") or {}
    enforced = [p for p in ca_analysis.get("policies") or [] if p.get("is_enforced")]
    app_id = app.get("app_id") or sp.get("app_id", "")
    covering = [
        p["display_name"] for p in enforced
        if p.get("targets_all_apps") or app_id in set((p.get("conditions") or {}).get("include_apps") or [])
    ]

    # Azure reach, from the RBAC cache.
    link = data.get("_azure_link") or {}
    azure = (link.get("principals") or {}).get(sp.get("object_id", "")) or {}

    requested = app.get("requested_permissions") or []
    granted_names = {p["permission"] for p in sp.get("granted_app_permissions") or []}
    return _envelope(
        snapshot, cid,
        app={**_app_row(app, sp), "created_at": app.get("created_at", ""),
             "sign_in_audience": app.get("sign_in_audience", ""),
             "notes": app.get("notes", ""), "sso_mode": sp.get("sso_mode", ""),
             "assignment_required": sp.get("assignment_required", False),
             "app_owner_tenant_id": sp.get("app_owner_tenant_id", "")},
        owners=owners,
        credentials=(app.get("credentials") or []) + (sp.get("credentials") or []),
        federated_credentials=app.get("federated_credentials") or [],
        granted_application_permissions=sp.get("granted_app_permissions") or [],
        granted_delegated=sp.get("granted_delegated") or [],
        requested_not_granted=[p for p in requested if p["permission"] not in granted_names],
        redirect_uris=app.get("redirect_uris") or [],
        provisioning=sp.get("provisioning_jobs") or [],
        conditional_access={"covered_by": covering, "enforced_policies": len(enforced)},
        azure_reach={"roles": azure.get("powerful_roles") or [],
                     "role_count": azure.get("role_count", 0),
                     "subscriptions": azure.get("subscriptions") or [],
                     "stale": link.get("stale", False),
                     "generated_at": link.get("generated_at", "")},
        risk=app.get("risk") or sp.get("risk") or {},
        findings=[f for f in analysis.get("findings") or []
                  if f.get("object_id") in {app.get("object_id"), sp.get("object_id")}],
    )


@router.get("/apps-consent")
async def apps_consent(
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Tenant consent posture plus every tenant-wide delegated grant."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    data = snapshot.get("data") or {}
    tenant = data.get("tenant") or {}
    apps = data.get("apps") or {}

    grants = []
    for sp in apps.get("service_principals") or []:
        for g in sp.get("granted_delegated") or []:
            if g.get("consent_type") != "AllPrincipals":
                continue
            grants.append({
                "client": sp.get("display_name", ""), "client_id": sp.get("object_id", ""),
                "resource": g.get("resource", ""), "scopes": g.get("scopes") or [],
                "max_tier": g.get("max_tier", "low"),
            })
    grants.sort(key=lambda g: (-_TIER_ORDER.index(g["max_tier"]) if g["max_tier"] in _TIER_ORDER else 0,
                               g["client"]))
    return _envelope(
        snapshot, cid,
        authorization_policy=tenant.get("authorization_policy") or {},
        admin_consent_policy=tenant.get("admin_consent_policy") or {},
        permission_grant_policies=tenant.get("permission_grant_policies") or [],
        all_principals_grants=grants,
        counts=apps.get("counts") or {},
    )


# ================================================================ CA simulator (P5)
class SimulateBody(BaseModel):
    changes: list[dict[str, Any]] = Field(default_factory=list)
    contexts: list[str] = Field(default_factory=list)
    cohorts: list[str] = Field(default_factory=list)
    sample_size: int = 400
    save: bool = False
    label: str = ""


@router.post("/ca/simulate")
async def ca_simulate(
    body: SimulateBody,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Simulate a Conditional Access change.

    Pure computation over the cached snapshot — no policy is ever written to the tenant.
    Gated behind ``entra.admin`` because it enumerates cohort membership broadly, and every
    run is written to the audit trail."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    analysis = snapshot.get("_ca_analysis") or {}
    if not analysis:
        raise HTTPException(
            status_code=400,
            detail="Conditional Access policies have not been collected for this tenant yet.",
        )
    if not body.changes:
        raise HTTPException(status_code=400, detail="At least one policy change is required.")

    try:
        result = ca_simulator.simulate(
            snapshot.get("data") or {}, analysis, body.changes,
            contexts=body.contexts or None, cohorts=body.cohorts or None,
            sample_size=max(50, min(5000, body.sample_size)),
        )
    except ca_simulator.InvalidChange as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    saved_id = ""
    if body.save:
        saved_id = _save_simulation(tenant_id, body, result, principal.subject)

    db.add(AuditLog(
        tenant_id=principal.tenant_id, actor_id=principal.subject,
        action="entra.ca_simulate", target=tenant_id,
        metadata_json={"changes": body.changes[:10], "counts": result["counts"],
                       "break_glass_affected": result["break_glass_affected"]},
    ))
    await db.commit()
    return _envelope(snapshot, cid, result=result, saved_id=saved_id)


def _save_simulation(tenant_id: str, body: SimulateBody, result: dict[str, Any], actor: str) -> str:
    import uuid as _uuid

    saved = cache.read_state(tenant_id, "simulations", [])
    if not isinstance(saved, list):
        saved = []
    sim_id = _uuid.uuid4().hex[:12]
    saved.append({
        "id": sim_id,
        "label": body.label or ", ".join(result.get("changes") or []) or "Simulation",
        "at": model.now_iso(),
        "actor": actor,
        "input": {"changes": body.changes, "contexts": body.contexts, "cohorts": body.cohorts,
                  "sample_size": body.sample_size},
        "result": result,
    })
    cache.write_state(tenant_id, "simulations", saved[-50:])
    return sim_id


@router.get("/ca/simulations")
async def ca_simulations(
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Saved simulations, newest first, with a staleness flag against the current snapshot."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    saved = cache.read_state(tenant_id, "simulations", [])
    rows = []
    for s in reversed(saved if isinstance(saved, list) else []):
        result = s.get("result") or {}
        rows.append({
            "id": s.get("id"), "label": s.get("label"), "at": s.get("at"), "actor": s.get("actor"),
            "counts": result.get("counts") or {},
            "break_glass_affected": result.get("break_glass_affected", 0),
            "stale": bool(snapshot.get("generated_at") and s.get("at", "") < snapshot["generated_at"]),
        })
    return _envelope(snapshot, cid, simulations=rows)


@router.get("/ca/simulations/{simulation_id}")
async def ca_simulation(
    simulation_id: str,
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    saved = cache.read_state(tenant_id, "simulations", [])
    found = next((s for s in (saved if isinstance(saved, list) else []) if s.get("id") == simulation_id), None)
    if found is None:
        raise HTTPException(status_code=404, detail="Simulation not found.")
    stale = bool(snapshot.get("generated_at") and found.get("at", "") < snapshot["generated_at"])
    return _envelope(snapshot, cid, simulation=found, stale=stale)


@router.post("/ca/simulations/{simulation_id}/rerun")
async def ca_simulation_rerun(
    simulation_id: str,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Re-run a saved simulation against the CURRENT snapshot."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    saved = cache.read_state(tenant_id, "simulations", [])
    rows = saved if isinstance(saved, list) else []
    found = next((s for s in rows if s.get("id") == simulation_id), None)
    if found is None:
        raise HTTPException(status_code=404, detail="Simulation not found.")
    analysis = snapshot.get("_ca_analysis") or {}
    if not analysis:
        raise HTTPException(status_code=400, detail="No Conditional Access snapshot to simulate against.")
    spec = found.get("input") or {}
    try:
        result = ca_simulator.simulate(
            snapshot.get("data") or {}, analysis, spec.get("changes") or [],
            contexts=spec.get("contexts") or None, cohorts=spec.get("cohorts") or None,
            sample_size=max(50, min(5000, int(spec.get("sample_size") or 400))),
        )
    except ca_simulator.InvalidChange as exc:
        # A saved simulation can reference a policy that has since been deleted.
        raise HTTPException(
            status_code=409,
            detail=f"This simulation no longer applies to the current snapshot: {exc}",
        ) from exc
    found["result"] = result
    found["at"] = model.now_iso()
    cache.write_state(tenant_id, "simulations", rows)
    return _envelope(snapshot, cid, result=result, simulation_id=simulation_id)


@router.get("/ca/simulate/contexts")
async def ca_simulate_contexts(principal: Principal = Depends(require_read)) -> dict[str, Any]:
    """The sign-in contexts and cohorts the simulator can run over."""
    return {
        "contexts": [
            {"key": c.key, "label": c.label, "client_app": c.client_app,
             "platform": c.platform, "location": c.location,
             "device_compliant": c.device_compliant, "app_class": c.app_class,
             "sign_in_risk": c.sign_in_risk}
            for c in ca_simulator.DEFAULT_CONTEXTS
        ],
        "always_full_cohorts": list(ca_simulator.ALWAYS_FULL),
        "limitations": list(ca_simulator.LIMITATIONS),
    }


# ================================================ Risk & sign-in intelligence (P6)
def _risk_domain(snapshot: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    data = (snapshot.get("data") or {}).get("risk") or {}
    caps = data.get("capabilities") or {}
    return data, caps


@router.get("/signals/overview")
async def signals_overview(
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Sign-in health: volume, outcome, MFA challenge rate, client mix and CA results."""
    snapshot, _tenant_id, cid = _snapshot(principal, connection_id)
    risk, caps = _risk_domain(snapshot)
    signins = risk.get("signins") or {}
    return _envelope(
        snapshot, cid,
        signins=signins,
        capabilities=caps,
        thresholds=risk.get("thresholds") or {},
        counts=risk.get("counts") or {},
        # Repeated at the top level so no chart can render without seeing it.
        sampled=bool(signins.get("sampled")),
        # The window is the only lever a reader has against the row cap, so the screen has
        # to be able to offer it. `days` is what the NEXT collection will use; `data_days`
        # is what the numbers on screen actually cover. They differ between changing the
        # setting and re-collecting, and conflating them would let the page claim a window
        # it has not collected.
        lookback=_signin_lookback(signins),
        domain=(snapshot.get("domains") or {}).get("risk") or {},
    )


# Matches the clamp in app.core.app_settings, which is the authority.
_LOOKBACK_MIN = 1
_LOOKBACK_MAX = 90


def _signin_lookback(signins: dict[str, Any]) -> dict[str, Any]:
    return {
        "days": snapshot_mod.settings()["signin_lookback_days"],
        "data_days": signins.get("lookback_days"),
        "min": _LOOKBACK_MIN,
        "max": _LOOKBACK_MAX,
        "setting_key": "entra_signin_lookback_days",
    }


@router.get("/signals/auth-methods")
async def signals_auth_methods(
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Registration coverage and method distribution, overall and for the admin cohort.

    Sourced from the people domain's registration report rather than the sign-in logs — one
    paged call covers the whole tenant, and it is the same field the CA simulator uses to
    tell 'challenged' from 'effectively blocked'."""
    from app.entra.collectors.roles import privileged_principal_ids

    snapshot, _tenant_id, cid = _snapshot(principal, connection_id)
    data = snapshot.get("data") or {}
    people = data.get("people") or {}
    enabled = [u for u in people.get("users") or [] if u.get("enabled")]
    privileged = privileged_principal_ids(data.get("roles") or {})
    known = bool((people.get("capabilities") or {}).get("mfa_registration_report"))
    # Only score users the registration report actually returned. Counting the rest as
    # "no method registered" would inflate the gap with accounts we simply have no reading
    # for, and the number that drives the remediation queue has to be defensible.
    users = [u for u in enabled if u.get("registration_reported")] if known else enabled
    unreported = len(enabled) - len(users)

    def _slice(rows: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(rows)
        if not total:
            return {"total": 0, "registered": 0, "capable": 0, "passwordless": 0, "sspr": 0,
                    "phishing_resistant": 0, "weak_only": 0, "none": 0}
        weak = {"mobilePhone", "alternateMobilePhone", "officePhone", "email", "voice", "sms"}
        return {
            "total": total,
            "registered": sum(1 for u in rows if u.get("mfa_registered")),
            "capable": sum(1 for u in rows if u.get("mfa_capable")),
            "passwordless": sum(1 for u in rows if u.get("passwordless_capable")),
            "sspr": sum(1 for u in rows if u.get("sspr_registered")),
            "phishing_resistant": sum(1 for u in rows if u.get("phishing_resistant")),
            "weak_only": sum(1 for u in rows
                             if u.get("methods") and set(u["methods"]) <= weak),
            "none": sum(1 for u in rows if not u.get("methods")),
        }

    distribution: dict[str, int] = {}
    for u in users:
        for method in u.get("methods") or []:
            distribution[str(method)] = distribution.get(str(method), 0) + 1

    admins = [u for u in users if str(u.get("id") or "") in privileged]
    gap = [
        {"id": u.get("id"), "upn": u.get("upn"), "display_name": u.get("display_name"),
         "privileged": str(u.get("id") or "") in privileged,
         "last_signin": u.get("last_signin", "")}
        for u in users if not u.get("methods")
    ]
    return _envelope(
        snapshot, cid,
        known=known,
        overall=_slice(users),
        privileged=_slice(admins),
        distribution=dict(sorted(distribution.items(), key=lambda kv: -kv[1])),
        gap=sorted(gap, key=lambda r: (not r["privileged"], r["upn"] or ""))[:500],
        gap_total=len(gap),
        enabled_total=len(enabled),
        unreported=unreported,
        # Federated users register their factors with the identity provider, not with Entra,
        # so every figure above describes the cloud-authenticated population plus whoever
        # separately registered a method here. Without this the screen reports a gap it
        # structurally cannot see — the same "blind is not zero" failure the score avoids.
        identity_fabric=_fabric_brief(snapshot),
    )


@router.get("/signals/legacy-auth")
async def signals_legacy_auth(
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Legacy protocol breakdown, and whether a blocking policy is actually working."""
    snapshot, _tenant_id, cid = _snapshot(principal, connection_id)
    risk, caps = _risk_domain(snapshot)
    signins = risk.get("signins") or {}
    analysis = snapshot.get("_ca_analysis") or {}
    blocking = [
        {"id": p.get("id"), "display_name": p.get("display_name"),
         "is_enforced": p.get("is_enforced"), "state": p.get("state")}
        for p in analysis.get("policies") or [] if p.get("blocks_legacy")
    ]
    rows = signins.get("legacy") or []
    succeeding = [r for r in rows if r.get("success")]
    return _envelope(
        snapshot, cid,
        capabilities=caps,
        sampled=bool(signins.get("sampled")),
        protocols=rows,
        successful_users=signins.get("legacy_success_users", 0),
        blocking_policies=blocking,
        # The gap that matters: a block policy exists AND legacy sign-ins still succeed.
        policy_gap=bool(blocking and succeeding
                        and any(p["is_enforced"] for p in blocking)),
    )


@router.get("/signals/failures")
async def signals_failures(
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Failure clustering by error code, with each code translated into plain English."""
    snapshot, _tenant_id, cid = _snapshot(principal, connection_id)
    risk, caps = _risk_domain(snapshot)
    signins = risk.get("signins") or {}
    return _envelope(
        snapshot, cid,
        capabilities=caps,
        sampled=bool(signins.get("sampled")),
        codes=signins.get("by_failure_code") or [],
        by_day=signins.get("by_day") or [],
        failure_rate=signins.get("failure_rate", 0.0),
        total=signins.get("total", 0),
        apps=[a for a in signins.get("by_app") or [] if a.get("failure")][:50],
    )


@router.get("/signals/risky-users")
async def signals_risky_users(
    level: str | None = None,
    state: str | None = None,
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Identity Protection risky users, joined to privilege and self-remediation capability."""
    from app.entra.collectors.roles import privileged_principal_ids

    snapshot, _tenant_id, cid = _snapshot(principal, connection_id)
    data = snapshot.get("data") or {}
    risk, caps = _risk_domain(snapshot)
    privileged = privileged_principal_ids(data.get("roles") or {})
    users = {str(u.get("id") or ""): u for u in (data.get("people") or {}).get("users") or []}

    rows = []
    for row in risk.get("risky_users") or []:
        if level and row.get("level") != level:
            continue
        if state and row.get("state") != state:
            continue
        uid = str(row.get("id") or "")
        user = users.get(uid) or {}
        rows.append({
            **row,
            "privileged": uid in privileged,
            "mfa_registered": user.get("mfa_registered"),
            "can_self_remediate": user.get("mfa_registered") is not False,
            "enabled": user.get("enabled"),
            "portal_link": model.portal_user(uid),
        })
    rows.sort(key=lambda r: (not r["privileged"],
                             {"high": 0, "medium": 1, "low": 2}.get(r.get("level", ""), 3),
                             r.get("upn", "")))
    return _envelope(
        snapshot, cid,
        capabilities=caps,
        users=rows,
        total=len(rows),
        detections=risk.get("risk_detections") or [],
        detection_counts=risk.get("detection_counts") or {},
        workload_identities=risk.get("risky_service_principals") or [],
    )


@router.get("/signals/patterns")
async def signals_patterns(
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Deterministic sign-in patterns. Each carries the rule that produced it."""
    snapshot, _tenant_id, cid = _snapshot(principal, connection_id)
    risk, caps = _risk_domain(snapshot)
    return _envelope(
        snapshot, cid,
        capabilities=caps,
        sampled=bool((risk.get("signins") or {}).get("sampled")),
        patterns=risk.get("patterns") or [],
        thresholds=risk.get("thresholds") or {},
    )


# ============================================================ Governance hub (P6)
@router.get("/governance/overview")
async def governance_overview(
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    snapshot, _tenant_id, cid = _snapshot(principal, connection_id)
    gov = (snapshot.get("data") or {}).get("governance") or {}
    analysis = _analysis(snapshot)
    return _envelope(
        snapshot, cid,
        counts=gov.get("counts") or {},
        capabilities=gov.get("capabilities") or {},
        findings=[f for f in analysis.get("findings") or [] if f.get("pillar") == "gov"][:200],
        domain=(snapshot.get("domains") or {}).get("governance") or {},
    )


@router.get("/governance/guests")
async def governance_guests(
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Guest (B2B) hygiene — the whole guest population, its lifecycle and its partner orgs.

    Cache-only like every other read here. The per-guest rows are returned in full rather
    than paged: a review campaign is exported and worked offline, and 1,700 flattened rows
    is ~1MB, well inside what the other inventory endpoints already return.
    """
    snapshot, _tenant_id, cid = _snapshot(principal, connection_id)
    data = snapshot.get("data") or {}
    people = data.get("people") or {}
    tenant = data.get("tenant") or {}
    s = snapshot_mod.settings()
    summary = guests_mod.summarise(people, stale_days=s["guest_stale_days"])
    summary["domains"] = guests_mod.annotate_partners(
        summary["domains"], tenant.get("cross_tenant_partners") or {})
    analysis = _analysis(snapshot)
    guest_signals = {"ppl.guest_stale", "ppl.guest_pending_invite", "ppl.guest_sprawl",
                     "ppl.guest_no_sponsor", "ppl.guest_invite_anyone",
                     "ppl.guest_full_directory_read", "ppl.guest_accepted_never_used",
                     "ppl.guest_human_dormant", "ppl.guest_consumer_domain"}
    return _envelope(
        snapshot, cid,
        **summary,
        # Guest access level is a tenant-wide fact that changes what every row above MEANS,
        # so it travels with them rather than sitting on another screen.
        guest_access=(tenant.get("authorization_policy") or {}),
        cross_tenant_known=bool((tenant.get("cross_tenant_partners") or {}).get("known")),
        findings=[f for f in analysis.get("findings") or []
                  if f.get("signal_id") in guest_signals][:500],
        domain=(snapshot.get("domains") or {}).get("people") or {},
    )


@router.get("/governance/reviews")
async def governance_reviews(
    overdue: bool = False,
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    snapshot, _tenant_id, cid = _snapshot(principal, connection_id)
    gov = (snapshot.get("data") or {}).get("governance") or {}
    ctx = snapshot_mod.context_from_settings(snapshot.get("tenant_id", ""))
    rows = []
    for review in gov.get("reviews") or []:
        days_overdue = 0
        for instance in review.get("instances") or []:
            if str(instance.get("status") or "") in ("Completed", "Applied"):
                continue
            days = ctx.days_since(str(instance.get("end") or ""))
            if days and days > days_overdue:
                days_overdue = days
        row = {**review, "days_overdue": days_overdue,
               "quality_flags": _review_flags(review)}
        if overdue and not days_overdue:
            continue
        rows.append(row)
    rows.sort(key=lambda r: (-r["days_overdue"], r.get("display_name", "")))
    return _envelope(snapshot, cid, reviews=rows, total=len(rows),
                     capabilities=gov.get("capabilities") or {})


def _review_flags(review: dict[str, Any]) -> list[str]:
    """The quality problems a campaign can have. Named, so the grid can explain itself."""
    flags = []
    if not review.get("auto_apply"):
        flags.append("decisions_not_applied")
    if review.get("default_decision_enabled") and str(review.get("default_decision")) == "Approve":
        flags.append("default_approve")
    if str(review.get("recurrence")) == "one-off":
        flags.append("not_recurring")
    if not review.get("justification_required"):
        flags.append("no_justification")
    if review.get("self_review"):
        flags.append("self_review")
    return flags


@router.get("/governance/entitlement")
async def governance_entitlement(
    expiring_days: int = Query(default=30, ge=1, le=365),
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    snapshot, _tenant_id, cid = _snapshot(principal, connection_id)
    gov = (snapshot.get("data") or {}).get("governance") or {}
    ctx = snapshot_mod.context_from_settings(snapshot.get("tenant_id", ""))
    expiring = []
    for row in gov.get("assignments") or []:
        days = ctx.days_until(str(row.get("expires_at") or ""))
        if days is None or days < 0 or days > expiring_days:
            continue
        expiring.append({**row, "days_left": days})
    expiring.sort(key=lambda r: r["days_left"])
    packages = sorted(
        ({**p,
          "no_review": not any(pol.get("review_required") for pol in p.get("policies") or []),
          "no_expiry": any(not pol.get("expires") for pol in p.get("policies") or [])}
         for p in gov.get("packages") or []),
        key=lambda p: (not p["no_review"], p.get("display_name", "")),
    )
    return _envelope(
        snapshot, cid, packages=packages, expiring=expiring,
        assignments_total=len(gov.get("assignments") or []),
        capabilities=gov.get("capabilities") or {},
    )


@router.get("/governance/lifecycle")
async def governance_lifecycle(
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    snapshot, _tenant_id, cid = _snapshot(principal, connection_id)
    gov = (snapshot.get("data") or {}).get("governance") or {}
    workflows = sorted(
        ({**w, "failure_rate": round((w.get("runs") or {}).get("failed", 0)
                                     / max(1, (w.get("runs") or {}).get("total", 0)), 3)}
         for w in gov.get("workflows") or []),
        key=lambda w: -w["failure_rate"],
    )
    categories = {str(w.get("category")) for w in workflows if w.get("enabled")}
    return _envelope(
        snapshot, cid, workflows=workflows,
        missing_categories=sorted({"joiner", "mover", "leaver"} - categories),
        capabilities=gov.get("capabilities") or {},
    )


@router.get("/governance/coverage")
async def governance_coverage(
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """The synthesis: for each object class that should be governed, is it?

    Computed from the INVENTORY domains, so it renders on a tenant with no governance
    licence at all — where every row honestly reads "never reviewed"."""
    from app.entra.collectors.governance import COVERAGE_CLASSES, coverage

    snapshot, _tenant_id, cid = _snapshot(principal, connection_id)
    gov = (snapshot.get("data") or {}).get("governance") or {}
    rows = coverage(snapshot.get("data") or {})
    return _envelope(
        snapshot, cid,
        rows=rows,
        classes=[dict(c) for c in COVERAGE_CLASSES],
        capabilities=gov.get("capabilities") or {},
        governance_readable=bool((gov.get("capabilities") or {}).get("access_reviews")),
    )


# =========================================================== Blast-radius graph (P7)
@router.get("/graph/scopes")
async def graph_scopes(principal: Principal = Depends(require_read)) -> dict[str, Any]:
    """The entry points the graph offers, and the escalation primitives it can derive."""
    return {
        "scopes": [dict(s) for s in blastradius.SCOPES],
        "primitives": [dict(p) for p in blastradius.ESCALATION_PRIMITIVES],
        "node_kinds": list(blastradius.NODE_KINDS),
        "edge_kinds": list(blastradius.EDGE_KINDS),
        "max_nodes": blastradius.MAX_NODES,
    }


@router.get("/graph")
async def graph_build(
    scope_kind: str = Query(default="privileged"),
    scope_id: str = "",
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Build one scoped identity graph.

    Never the whole tenant: a 100,000-user identity graph cannot render and would not be
    legible if it could. Unknown scopes fall back to the privileged overview."""
    snapshot, _tenant_id, cid = _snapshot(principal, connection_id)
    result = blastradius.build(
        snapshot.get("data") or {}, snapshot.get("_ca_analysis") or {},
        scope_kind=scope_kind, scope_id=scope_id,
    )
    return _envelope(snapshot, cid, scope_kind=scope_kind, scope_id=scope_id, **result)


@router.get("/graph/escalations")
async def graph_escalations(
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Every derived escalation path as a readable list, with the rule behind each one."""
    snapshot, _tenant_id, cid = _snapshot(principal, connection_id)
    edges = blastradius.escalation_edges(snapshot.get("data") or {})
    rows = [{
        "source": e["source"], "target": e["target"], "primitive": e["data"]["primitive"],
        "name": e["label"], "reason": e["data"]["reason"],
        "confidence": e["data"]["confidence"], "rule": e["data"]["rule"],
    } for e in edges]
    by_primitive: dict[str, int] = {}
    for row in rows:
        by_primitive[row["primitive"]] = by_primitive.get(row["primitive"], 0) + 1
    return _envelope(
        snapshot, cid, escalations=rows, total=len(rows), by_primitive=by_primitive,
        primitives=[dict(p) for p in blastradius.ESCALATION_PRIMITIVES],
    )


@router.get("/export/workbook")
async def export_workbook(
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> Response:
    """Every tab and sub-tab under /entra as one multi-sheet Excel workbook.

    Built from the snapshot rather than by replaying twenty-five endpoints, so it is a single
    read and it cannot disagree with the screens. That also side-steps the page caps those
    endpoints apply for the browser's benefit — the findings list is capped at 2,000 and the
    MFA gap at 500, and an export that silently stopped there would be a quieter version of the
    bug it exists to fix.

    Carries the raw directory too (users, groups, service principals, registrations), which the
    tabs only ever show counts of. **The Users sheet contains personal data.**"""
    connection, tenant_id, _cid = _target(principal, connection_id)
    snapshot = snapshot_mod.analyse(tenant_id)

    # The few things the screens compute rather than store. Passed in so the formatter stays
    # pure and can be unit-tested without a tenant.
    escalations = [{
        "source": e["source"], "target": e["target"], "primitive": e["data"]["primitive"],
        "name": e["label"], "reason": e["data"]["reason"],
        "confidence": e["data"]["confidence"], "rule": e["data"]["rule"],
    } for e in blastradius.escalation_edges(snapshot.get("data") or {})]

    granted = set((snapshot.get("permissions") or {}).get("granted") or [])
    setup_tiers = [{
        **tier,
        "granted": [s for s in tier["scopes"] if s in granted],
        "missing": [s for s in tier["scopes"] if s not in granted],
        "complete": bool(granted) and all(s in granted for s in tier["scopes"]),
    } for tier in permissions_probe.TIERS]

    scanner_cards = [s.public() for s in scanners_mod.registry()]

    # The screen merges the snapshot's sessions with the durable ledger so history reaches past
    # Graph's 30-day retention. Reading the snapshot alone loses those older sessions.
    merged_sessions, _domain = _activation_sessions(snapshot, tenant_id, history=True)
    merged_sessions = _decorate(merged_sessions, snapshot, 0.0, (8, 18))

    content = await asyncio.to_thread(
        entra_export.to_workbook,
        snapshot=snapshot,
        escalations=escalations,
        history=score_mod.history(tenant_id) if hasattr(score_mod, "history") else None,
        setup_tiers=setup_tiers,
        scanners=scanner_cards,
        activations=merged_sessions,
    )
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=entra-identity-review.xlsx"},
    )


@router.get("/graph/targets")
async def graph_targets(
    connection_id: str | None = None,
    q: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """The pickable focus targets, so the UI never asks the operator to paste an object id.

    ``q`` narrows the list server-side. Without it the picker is a plain list capped at
    ``_PICK_LIMIT``, which on a 20,000-seat tenant put 95% of the directory out of reach:
    the blast-radius screen could not be pointed at most of the people it exists to analyse.
    """
    snapshot, _tenant_id, cid = _snapshot(principal, connection_id)
    data = snapshot.get("data") or {}
    roles = data.get("roles") or {}
    apps = data.get("apps") or {}
    analysis = snapshot.get("_ca_analysis") or {}
    needle = (q or "").strip().lower()

    def _match(row: dict[str, Any]) -> bool:
        if not needle:
            return True
        return needle in row["label"].lower() or needle in row["id"].lower()

    holders: set[str] = set()
    for bucket in ("assignments", "group_derived", "eligible"):
        for row in roles.get(bucket) or []:
            if row.get("role_privileged") and row.get("principal_id"):
                holders.add(str(row["principal_id"]))
    users = [
        {"id": str(u.get("id")), "label": str(u.get("upn") or u.get("display_name") or ""),
         "privileged": str(u.get("id")) in holders}
        for u in (data.get("people") or {}).get("users") or []
    ]
    users = [u for u in users if _match(u)]
    users.sort(key=lambda u: (not u["privileged"], u["label"]))
    applications = sorted(
        (r for r in (
            {"id": str(s.get("object_id")), "label": str(s.get("display_name") or ""),
             "risk_score": int((s.get("risk") or {}).get("score") or 0)}
            for s in apps.get("service_principals") or []
        ) if _match(r)),
        key=lambda a: -a["risk_score"])
    role_rows = sorted(
        (r for r in (
            {"id": str(d.get("id")), "label": str(d.get("display_name") or ""),
             "tier": str(d.get("tier") or "")}
            for d in roles.get("definitions") or [] if d.get("privileged")
        ) if _match(r)),
        key=lambda r: r["label"])
    return _envelope(
        snapshot, cid,
        query=needle,
        principals=users[:_PICK_LIMIT],
        principal_total=len(users),
        applications=applications[:_PICK_LIMIT],
        application_total=len(applications),
        roles=role_rows[:_PICK_LIMIT],
        role_total=len(role_rows),
        policies=[{"id": str(p.get("id")), "label": str(p.get("display_name") or ""),
                   "enforced": bool(p.get("is_enforced"))}
                  for p in analysis.get("policies") or []],
    )


# ============================================================== Proactive hub (P8)
@router.get("/scanners")
async def scanners_list(
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """Every scanner, its selection, its last run and whether it can run at all."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    runs = scanners_mod.read_runs(tenant_id)
    domain_meta = snapshot.get("domains") or {}
    rows = []
    for scanner in scanners_mod.registry():
        last = runs.get(scanner.id) or {}
        rows.append({
            **scanner.public(),
            "last_run": last.get("at", ""),
            "last_counts": last.get("counts") or {},
            "blocked": scanners_mod.unavailable_reason(scanner, domain_meta),
        })
    return _envelope(
        snapshot, cid, scanners=rows,
        always_immediate=list(scanners_mod.ALWAYS_IMMEDIATE),
        severity_order=list(scanners_mod.SEVERITY_ORDER),
    )


class ScannerRunBody(BaseModel):
    scanner_ids: list[str] = Field(default_factory=list)
    force: bool = True
    notify: bool = False


# How many findings a scanner response carries. A scanner over a large tenant can select
# several hundred; the screen pages through them rather than shipping the whole set.
_SCANNER_FINDING_LIMIT = 200


@router.post("/scanners/run")
async def scanners_run(
    body: ScannerRunBody,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Run scanners against the CURRENT snapshot. Never triggers a collection.

    Keeping scan and collect independent is deliberate: a scanner reports on the snapshot
    that exists, so an operator can re-run a scanner a dozen times while investigating
    without hammering Microsoft Graph."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    unknown = [s for s in body.scanner_ids if s not in scanners_mod.SCANNER_BY_ID]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown scanner(s): {', '.join(unknown)}")

    analysis = _analysis(snapshot)
    ctx = snapshot_mod.context_from_settings(tenant_id)
    result = scanners_mod.sweep(
        tenant_id, analysis, snapshot.get("domains") or {}, ctx,
        force=body.force, only=body.scanner_ids or None,
    )
    scanners_mod.update_ledger(tenant_id, analysis.get("findings") or [])

    delivered = 0
    if body.notify:
        delivered = await _notify_scanner_results(principal.tenant_id, result.ran)

    db.add(AuditLog(
        tenant_id=principal.tenant_id, actor_id=principal.subject,
        action="entra.scanners_run", target=tenant_id,
        metadata_json={"scanners": [r["scanner_id"] for r in result.ran],
                       "new": result.new_total, "notified": delivered},
    ))
    await db.commit()
    return _envelope(
        snapshot, cid,
        ran=[{k: v for k, v in r.items() if k != "new"} | {"new": r["new"][:50]}
             for r in result.ran],
        skipped=result.skipped,
        new_total=result.new_total,
        immediate=result.immediate,
        notified=delivered,
    )


@router.get("/scanners/{scanner_id}/findings")
async def scanner_findings(
    scanner_id: str,
    connection_id: str | None = None,
    limit: int = _SCANNER_FINDING_LIMIT,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """What this scanner reports on the CURRENT snapshot, worst finding first.

    Read-only on purpose — it does NOT record a run. Viewing results must not move the
    new/resolved baseline: if merely opening the screen marked every finding as seen, the
    next real run would report "nothing changed" and the digest would go quiet precisely
    because someone looked."""
    scanner = scanners_mod.SCANNER_BY_ID.get(scanner_id)
    if scanner is None:
        raise HTTPException(status_code=404, detail=f"Unknown scanner: {scanner_id}")
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    blocked = scanners_mod.unavailable_reason(scanner, snapshot.get("domains") or {})
    selected = ([] if blocked
                else scanners_mod.select(scanner, _analysis(snapshot).get("findings") or []))
    findings = model.sort_findings(selected)
    last = scanners_mod.read_runs(tenant_id).get(scanner_id) or {}
    # Fingerprints recorded by the last run. Anything absent from that set appeared since,
    # which is the only honest way to flag "new" without pretending we just ran.
    seen = set(last.get("fingerprints") or [])
    limit = max(1, min(int(limit), 1000))
    return _envelope(
        snapshot, cid,
        scanner_id=scanner_id,
        name=scanner.name,
        blocked=blocked,
        total=len(findings),
        by_severity=model.count_by_severity(findings),
        last_run=str(last.get("at") or ""),
        findings=[f | {"is_new": bool(last) and f["fingerprint"] not in seen}
                  for f in findings[:limit]],
        truncated=len(findings) > limit,
    )


async def _notify_scanner_results(tenant_id: str, results: list[dict[str, Any]]) -> int:
    """Push only what changed. A digest that repeats known findings trains people to
    filter the sender, and after that the product detects nothing."""
    from app.notifications.engine import publish

    sent = 0
    for result in results:
        if not scanners_mod.should_notify(result):
            continue
        try:
            await publish(
                tenant_id=tenant_id,
                type="entra.scanner",
                source="entra",
                severity=scanners_mod.notification_severity(result),
                title=f"{result['name']}: {result['counts']['new']} new finding(s)",
                body=scanners_mod.summarise(result),
                facts={"scanner_id": result["scanner_id"], **result["counts"],
                       "immediate": len(result["immediate"])},
                links={"Open the findings inbox": "/entra/findings"},
                # Fingerprinted per scanner per day so a re-run while investigating does not
                # spam the channel with the same delta.
                fingerprint=f"entra.scanner:{result['scanner_id']}:{result['at'][:10]}",
            )
            sent += 1
        except Exception:  # noqa: BLE001 - a delivery failure must not fail the scan
            log.exception("entra scanner notification failed for %s", result["scanner_id"])
    return sent


@router.get("/inbox")
async def findings_inbox(
    severity: str | None = None,
    pillar: str | None = None,
    state: str | None = None,
    ageing_days: int | None = None,
    unassigned: bool = False,
    search: str | None = None,
    sort: str | None = Query(default=None, pattern="^(severity|title|object|state|age|assignee)$"),
    dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    offset: int = 0,
    limit: int = Query(default=200, ge=1, le=2000),
    connection_id: str | None = None,
    principal: Principal = Depends(require_read),
) -> dict[str, Any]:
    """The operational inbox: findings with workflow state, first-seen and age.

    ``age`` is what turns a list into a conversation — a 200-day-old critical is a
    different problem from one that appeared this morning, and the raw findings list
    cannot tell them apart."""
    snapshot, tenant_id, cid = _snapshot(principal, connection_id)
    analysis = _analysis(snapshot)
    ctx = snapshot_mod.context_from_settings(tenant_id)
    ledger = scanners_mod.read_ledger(tenant_id)
    user_state = snapshot_mod.read_state(tenant_id)
    per_finding = user_state.get("findings") or {}

    rows = []
    for f in analysis.get("findings") or []:
        entry = ledger.get(f["fingerprint"]) or {}
        st = per_finding.get(f["fingerprint"]) or {}
        snoozed_until = str(st.get("snoozed_until") or "")
        current_state = str(st.get("state") or "open")
        # A snooze that has expired returns to open on its own; an operator should never
        # have to remember to un-snooze something.
        if current_state == "snoozed" and snoozed_until:
            left = ctx.days_until(snoozed_until)
            if left is not None and left < 0:
                current_state = "open"
        rows.append({
            **f,
            "state": current_state,
            "assignee": st.get("assignee", ""),
            "note": st.get("note", ""),
            "ticket": st.get("ticket", ""),
            "snoozed_until": snoozed_until,
            "first_seen": entry.get("first_seen", "") or st.get("first_seen", ""),
            "age_days": ctx.days_since(str(entry.get("first_seen") or st.get("first_seen") or "")),
        })

    if severity:
        wanted = {s.strip() for s in severity.split(",") if s.strip()}
        rows = [r for r in rows if r["severity"] in wanted]
    if pillar:
        rows = [r for r in rows if r["pillar"] == pillar]
    if state:
        rows = [r for r in rows if r["state"] == state]
    if unassigned:
        rows = [r for r in rows if not r["assignee"]]
    if ageing_days is not None:
        rows = [r for r in rows if (r["age_days"] or 0) >= ageing_days]
    if search:
        needle = search.lower()
        rows = [r for r in rows
                if needle in f"{r.get('object_name','')} {r.get('title','')}".lower()]

    rows.sort(key=lambda r: (-model.SEVERITY_RANK.get(r["severity"], 0),
                             -(r["age_days"] or 0), r.get("object_name", "")))
    rows = _apply_sort(rows, {
        "severity": lambda r: model.SEVERITY_RANK.get(r.get("severity"), None),
        "title": lambda r: _text_key(r.get("title")),
        "object": lambda r: _text_key(r.get("object_name")),
        "state": lambda r: _FINDING_STATE_RANK.get(r.get("state"), None),
        "age": lambda r: r.get("age_days"),
        "assignee": lambda r: _text_key(r.get("assignee")),
    }, sort, dir)
    total = len(rows)
    resolved = [
        {"fingerprint": fp, **entry} for fp, entry in ledger.items() if entry.get("resolved_at")
    ]
    resolved.sort(key=lambda r: str(r.get("resolved_at") or ""), reverse=True)
    return _envelope(
        snapshot, cid,
        findings=rows[offset: offset + limit], total=total, offset=offset, limit=limit,
        sort=sort or "", dir=dir,
        by_severity=model.count_by_severity(rows),
        by_state=_count_by(rows, "state"),
        recently_resolved=resolved[:50],
        suppressed_count=len(user_state.get("suppressed") or []),
        ledger_size=len(ledger),
    )


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        out[str(row.get(key) or "")] = out.get(str(row.get(key) or ""), 0) + 1
    return out


class BulkStateBody(BaseModel):
    fingerprints: list[str] = Field(default_factory=list)
    state: str = "acknowledged"
    reason: str = ""
    assignee: str = ""
    note: str = ""
    snooze_days: int = 0


@router.post("/inbox/bulk")
async def inbox_bulk(
    body: BulkStateBody,
    connection_id: str | None = None,
    principal: Principal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Apply a workflow state to a selection. The inbox is unusable one row at a time."""
    _conn, tenant_id, cid = _target(principal, connection_id)
    valid = {"open", "acknowledged", "snoozed", "suppressed"}
    if body.state not in valid:
        raise HTTPException(status_code=400, detail=f"state must be one of {sorted(valid)}")
    if body.state == "suppressed" and not body.reason.strip():
        raise HTTPException(status_code=400, detail="A suppression requires a reason.")
    if body.state == "snoozed" and body.snooze_days <= 0:
        raise HTTPException(status_code=400,
                            detail="A snooze requires snooze_days so it can expire on its own.")
    if not body.fingerprints:
        raise HTTPException(status_code=400, detail="No findings selected.")

    from datetime import datetime, timedelta, timezone

    state = snapshot_mod.read_state(tenant_id)
    per = state.setdefault("findings", {})
    suppressed = set(state.get("suppressed") or [])
    until = ""
    if body.state == "snoozed":
        until = (datetime.now(timezone.utc) + timedelta(days=body.snooze_days)).isoformat()

    for fingerprint in body.fingerprints[:2000]:
        entry = per.setdefault(fingerprint, {"first_seen": model.now_iso()})
        entry.update({"state": body.state, "reason": body.reason, "assignee": body.assignee,
                      "note": body.note, "snoozed_until": until,
                      "updated_at": model.now_iso()})
        suppressed.discard(fingerprint)
        if body.state == "suppressed":
            suppressed.add(fingerprint)
    state["suppressed"] = sorted(suppressed)
    snapshot_mod.write_state(tenant_id, state)
    snapshot_mod.invalidate(tenant_id)

    db.add(AuditLog(
        tenant_id=principal.tenant_id, actor_id=principal.subject,
        action="entra.inbox_bulk", target=tenant_id,
        metadata_json={"state": body.state, "count": len(body.fingerprints),
                       "reason": body.reason[:400]},
    ))
    await db.commit()
    return {"ok": True, "updated": len(body.fingerprints[:2000]), "state": body.state,
            "snoozed_until": until}

