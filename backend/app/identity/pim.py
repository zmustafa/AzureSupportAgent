"""PIM / JIT lifecycle review — eligible-vs-active role drift, stale privileged access,
and Just-In-Time activation review over time.

Four signal groups, each normalised to a common *finding* shape (a superset of the Identity
dashboard finding so the UI + ticket/investigate handoffs can treat them uniformly):

    standing_access     — permanent/active assignments to privileged roles that should be
                          eligible (JIT) instead — the eligible-vs-active drift.
    stale_eligible      — eligible assignments never (or not recently) activated — unused
                          standing privilege to prune.
    stale_active        — active assignments idle for a long time (no recent activation /
                          sign-in) — privilege nobody is exercising.
    activation_review   — recent / currently-active JIT activations to review (long-lived or
                          high-privilege activations get flagged).

Live Microsoft Graph PIM *schedule* APIs (roleEligibilitySchedules / roleAssignmentSchedules)
are not exposed by the bundled EntraID MCP toolset, so the live path derives what it can from
``get_privileged_users`` (the active/standing role holders) and records a clear, per-group note
for the schedule-only signals — exactly like the other identity collectors degrade. The full
experience is driven by demo synthesis (:func:`build_demo_snapshot`)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("app.identity.pim")

# Finding kinds (stable identifiers used by the frontend + ticket text).
KIND_STANDING = "pim_standing"
KIND_STALE_ELIGIBLE = "pim_stale_eligible"
KIND_STALE_ACTIVE = "pim_stale_active"
KIND_ACTIVATION = "pim_activation"

GROUP_KEYS = ("standing_access", "stale_eligible", "stale_active", "activation_review")

_SEV_RANK = {"critical": 0, "error": 1, "warning": 2, "info": 3, "ok": 4}

# Privilege tiers drive base severity. Tier-0 = tenant-takeover roles; tier-1 = broad admin.
_TIER0 = {
    "global administrator", "privileged role administrator", "privileged authentication administrator",
}
_TIER1 = {
    "security administrator", "user administrator", "application administrator",
    "cloud application administrator", "conditional access administrator",
    "exchange administrator", "sharepoint administrator", "intune administrator",
    "authentication administrator", "helpdesk administrator", "billing administrator",
}

# How long an eligible/active assignment can sit idle before we flag it.
_STALE_ELIGIBLE_DAYS = 90
_STALE_ACTIVE_DAYS = 90
# A single JIT activation longer than this (hours) is unusual and worth a look.
_LONG_ACTIVATION_HOURS = 8


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _days_since(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int((_now() - dt).total_seconds() // 86400)


def _days_until(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int((dt - _now()).total_seconds() // 86400)


def _role_tier(role: str | None) -> str:
    r = (role or "").strip().lower()
    if r in _TIER0:
        return "tier0"
    if r in _TIER1:
        return "tier1"
    return "tier2"


def _pim_finding(**kw: Any) -> dict[str, Any]:
    """Normalise a PIM finding to the common shape consumed by the UI + handoffs."""
    return {
        "id": kw.get("id", ""),
        "kind": kw.get("kind", ""),
        "title": kw.get("title", ""),
        "detail": kw.get("detail", ""),
        "severity": kw.get("severity", "info"),
        "subject": kw.get("subject", ""),
        "subject_id": kw.get("subject_id", ""),
        "role": kw.get("role", ""),
        "role_tier": kw.get("role_tier", "tier2"),
        "scope": kw.get("scope", "Directory"),
        "assignment_type": kw.get("assignment_type", ""),  # eligible | active | activated
        "last_activated_at": kw.get("last_activated_at"),
        "days_idle": kw.get("days_idle"),
        "activation_count_90d": kw.get("activation_count_90d"),
        "expires_at": kw.get("expires_at"),
        "days_left": kw.get("days_left"),
        "workload_id": kw.get("workload_id"),
        "workload_name": kw.get("workload_name"),
        "remediation": kw.get("remediation", ""),
    }


def _sort(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Most urgent first: severity tier, then most-idle, then subject name."""
    def key(f: dict[str, Any]):
        idle = f.get("days_idle")
        return (
            _SEV_RANK.get(f.get("severity", "info"), 3),
            -(idle if isinstance(idle, int) else 0),
            (f.get("subject") or "").lower(),
        )

    return sorted(findings, key=key)


def _worst(items: list[dict[str, Any]]) -> str:
    if not items:
        return "ok"
    return min((f.get("severity", "info") for f in items), key=lambda s: _SEV_RANK.get(s, 3))


def _assemble(
    groups: dict[str, list[dict[str, Any]]],
    *,
    tenant_id: str,
    connection_configured: bool,
    errors: dict[str, str],
    meta: dict[str, Any],
) -> dict[str, Any]:
    """Sort groups and compute KPIs / per-group severity into the final snapshot shape."""
    for k in GROUP_KEYS:
        groups[k] = _sort(groups.get(k, []))
    kpis = {
        "standing_access": len(groups["standing_access"]),
        "high_priv_standing": sum(1 for f in groups["standing_access"] if f.get("role_tier") == "tier0"),
        "stale_eligible": len(groups["stale_eligible"]),
        "stale_active": len(groups["stale_active"]),
        "activations": len(groups["activation_review"]),
    }
    return {
        "generated_at": _now_iso(),
        "tenant_id": tenant_id,
        "connection_configured": connection_configured,
        "kpis": kpis,
        "group_severity": {g: _worst(groups[g]) for g in GROUP_KEYS},
        "groups": groups,
        "errors": errors,
        "meta": meta,
    }


# --------------------------------------------------------------------------- live collection
_SCHEDULE_NOTE = (
    "PIM schedule data (eligible / activation history) isn't available through this "
    "connection's Graph tools. Load demo data or connect PIM Graph access to populate this."
)


def _standing_from_privileged_user(u: dict[str, Any]) -> list[dict[str, Any]]:
    """One standing-access finding per privileged role a user actively holds."""
    uid = u.get("id") or ""
    upn = u.get("userPrincipalName") or uid
    name = u.get("displayName") or upn or uid
    roles = u.get("roles") or []
    if not isinstance(roles, list):
        roles = [str(roles)]
    out: list[dict[str, Any]] = []
    for role in roles:
        role = str(role)
        tier = _role_tier(role)
        sev = {"tier0": "critical", "tier1": "error"}.get(tier, "warning")
        out.append(
            _pim_finding(
                id=f"standing:{uid}:{role}".lower(),
                kind=KIND_STANDING,
                title=f"“{name}” holds {role} as standing access",
                detail=(
                    f"{role} is assigned actively (not via Just-In-Time activation), so the "
                    "privilege is always on. Convert to an eligible PIM assignment so it must "
                    "be activated, time-bound and justified."
                ),
                severity=sev,
                subject=name,
                subject_id=upn,
                role=role,
                role_tier=tier,
                assignment_type="active",
                remediation=(
                    "Make the assignment eligible (JIT) in Privileged Identity Management, "
                    "require activation with justification + approval, and cap the activation "
                    "duration. Keep at most two permanent assignments for break-glass only."
                ),
            )
        )
    return out


async def collect_pim(
    connection: dict[str, Any] | None,
    *,
    tenant_id: str,
) -> dict[str, Any]:
    """Build the PIM snapshot from live Graph where possible. Never raises."""
    from app.core.config import get_settings
    from app.mcp.client import build_entra_mcp_client, entra_graph_config_error, unwrap_exc_message
    from app.identity.collector import _tool_result_json

    settings = get_settings()
    groups: dict[str, list[dict[str, Any]]] = {k: [] for k in GROUP_KEYS}
    errors: dict[str, str] = {}
    meta: dict[str, Any] = {"source": "live"}

    cfg_err = entra_graph_config_error(connection)
    if cfg_err:
        for g in GROUP_KEYS:
            errors[g] = cfg_err
        return _assemble(groups, tenant_id=tenant_id, connection_configured=connection is not None, errors=errors, meta=meta)

    client = build_entra_mcp_client(settings, connection=connection)
    try:
        raw = _tool_result_json(await client.call_tool("get_privileged_users", {}))
        for u in raw if isinstance(raw, list) else []:
            groups["standing_access"].extend(_standing_from_privileged_user(u))
    except Exception as exc:  # noqa: BLE001 - isolate; degrade gracefully
        errors["standing_access"] = unwrap_exc_message(exc)[:300]
        log.info("pim standing_access failed: %s", exc)
    finally:
        client.close()

    # The schedule-only signals have no live Graph source through the bundled toolset.
    for g in ("stale_eligible", "stale_active", "activation_review"):
        errors[g] = _SCHEDULE_NOTE

    return _assemble(groups, tenant_id=tenant_id, connection_configured=connection is not None, errors=errors, meta=meta)


# --------------------------------------------------------------------------- demo synthesis
def build_demo_snapshot(*, tenant_id: str) -> dict[str, Any]:
    """A realistic PIM lifecycle snapshot for demos — drift, stale access and JIT activations.

    Deterministic in structure; timestamps are relative to *now* so the "idle for N days" and
    "expires in N hours" narratives always read correctly."""
    now = _now()
    groups: dict[str, list[dict[str, Any]]] = {k: [] for k in GROUP_KEYS}

    def standing(name: str, upn: str, role: str, *, scope: str = "Directory") -> None:
        tier = _role_tier(role)
        sev = {"tier0": "critical", "tier1": "error"}.get(tier, "warning")
        groups["standing_access"].append(
            _pim_finding(
                id=f"standing:{upn}:{role}".lower().replace(" ", "-"),
                kind=KIND_STANDING,
                title=f"“{name}” holds {role} as standing access",
                detail=(
                    f"{role} is assigned actively at {scope} scope — always-on privilege that "
                    "bypasses Just-In-Time controls."
                ),
                severity=sev, subject=name, subject_id=upn, role=role, role_tier=tier,
                scope=scope, assignment_type="active",
                remediation=(
                    "Convert to an eligible PIM assignment requiring activation with "
                    "justification + approval, time-bound to the task."
                ),
            )
        )

    def stale_eligible(name: str, upn: str, role: str, *, last_activated: datetime | None, assigned_days: int) -> None:
        tier = _role_tier(role)
        idle = _days_since(_iso(last_activated)) if last_activated else assigned_days
        never = last_activated is None
        sev = "error" if (tier == "tier0") else ("warning" if (idle or 0) >= _STALE_ELIGIBLE_DAYS else "info")
        groups["stale_eligible"].append(
            _pim_finding(
                id=f"stale-elig:{upn}:{role}".lower().replace(" ", "-"),
                kind=KIND_STALE_ELIGIBLE,
                title=f"“{name}” is eligible for {role} but {'has never activated it' if never else f'last activated it {idle}d ago'}",
                detail=(
                    f"Eligible for {role} for {assigned_days} days. "
                    + ("It has never been activated — likely unnecessary standing eligibility."
                       if never else f"Not activated in {idle} days.")
                ),
                severity=sev, subject=name, subject_id=upn, role=role, role_tier=tier,
                assignment_type="eligible", last_activated_at=_iso(last_activated) if last_activated else None,
                days_idle=idle,
                remediation="Remove the eligible assignment if the role is no longer needed, or confirm the owner still requires it during access review.",
            )
        )

    def stale_active(name: str, upn: str, role: str, *, last_activity: datetime, scope: str = "Directory") -> None:
        tier = _role_tier(role)
        idle = _days_since(_iso(last_activity))
        sev = "error" if (idle or 0) >= 180 else ("warning" if (idle or 0) >= _STALE_ACTIVE_DAYS else "info")
        if tier == "tier0" and sev == "warning":
            sev = "error"
        groups["stale_active"].append(
            _pim_finding(
                id=f"stale-active:{upn}:{role}".lower().replace(" ", "-"),
                kind=KIND_STALE_ACTIVE,
                title=f"“{name}” holds active {role} but has been idle {idle}d",
                detail=(
                    f"Active {role} at {scope} scope with no sign-in or activation in {idle} days. "
                    "Dormant privileged access is pure attack surface."
                ),
                severity=sev, subject=name, subject_id=upn, role=role, role_tier=tier,
                scope=scope, assignment_type="active", last_activated_at=_iso(last_activity),
                days_idle=idle,
                remediation="Remove the assignment (or convert to eligible) — nobody is exercising this access.",
            )
        )

    def activation(name: str, upn: str, role: str, *, activated_at: datetime, duration_h: float,
                   count_90d: int) -> None:
        tier = _role_tier(role)
        expires = activated_at + timedelta(hours=duration_h)
        long_lived = duration_h > _LONG_ACTIVATION_HOURS
        sev = "warning" if (long_lived or tier == "tier0") else "info"
        active_now = expires > now
        verb = f"has {role} activated now" if active_now else f"activated {role}"
        groups["activation_review"].append(
            _pim_finding(
                id=f"activation:{upn}:{role}:{int(activated_at.timestamp())}".lower().replace(" ", "-"),
                kind=KIND_ACTIVATION,
                title=f"“{name}” {verb}" + (f" for {duration_h:g}h" if long_lived else ""),
                detail=(
                    f"{role} activated {_days_since(_iso(activated_at)) or 0}d ago for {duration_h:g}h "
                    f"({'still active' if active_now else 'expired'}). {count_90d} activation(s) in the last 90 days."
                ),
                severity=sev, subject=name, subject_id=upn, role=role, role_tier=tier,
                assignment_type="activated", last_activated_at=_iso(activated_at),
                activation_count_90d=count_90d,
                expires_at=_iso(expires), days_left=_days_until(_iso(expires)),
                remediation=("Confirm the activation matches a ticket / change. Shorten the maximum activation duration and require approval for tier-0 roles." if (long_lived or tier == "tier0") else "Routine activation — no action needed; shown for the access-review trail."),
            )
        )

    # ---- eligible-vs-active drift (standing access that should be JIT) ----
    standing("Olivia Park", "olivia.park@contoso.com", "Global Administrator")
    standing("Daniel Kim", "daniel.kim@contoso.com", "Privileged Role Administrator")
    standing("svc-deploy", "svc-deploy@contoso.com", "Contributor", scope="Subscription: Production")
    standing("Tom Becker", "tom.becker@contoso.com", "Application Administrator")

    # ---- stale eligible (unused standing eligibility) ----
    stale_eligible("Marcus Lee", "marcus.lee@contoso.com", "User Administrator",
                   last_activated=now - timedelta(days=128), assigned_days=210)
    stale_eligible("Priya Nair", "priya.nair@contoso.com", "Security Administrator",
                   last_activated=None, assigned_days=64)
    stale_eligible("Helen Cho", "helen.cho@contoso.com", "Helpdesk Administrator",
                   last_activated=now - timedelta(days=96), assigned_days=300)

    # ---- stale active (dormant privileged access) ----
    stale_active("Tom Becker", "tom.becker@contoso.com", "Application Administrator",
                 last_activity=now - timedelta(days=147))
    stale_active("Raj Patel", "raj.patel@contoso.com", "Exchange Administrator",
                 last_activity=now - timedelta(days=205))

    # ---- JIT activation review (over time) ----
    activation("Sara Ahmed", "sara.ahmed@contoso.com", "Global Administrator",
               activated_at=now - timedelta(minutes=35), duration_h=2, count_90d=4)
    activation("Marcus Lee", "marcus.lee@contoso.com", "User Administrator",
               activated_at=now - timedelta(hours=3), duration_h=12, count_90d=2)
    activation("Emma Wright", "emma.wright@contoso.com", "Helpdesk Administrator",
               activated_at=now - timedelta(days=2), duration_h=4, count_90d=9)

    meta = {"source": "demo", "thresholds": {
        "stale_eligible_days": _STALE_ELIGIBLE_DAYS,
        "stale_active_days": _STALE_ACTIVE_DAYS,
        "long_activation_hours": _LONG_ACTIVATION_HOURS,
    }}
    return _assemble(groups, tenant_id=tenant_id, connection_configured=True, errors={}, meta=meta)


def seed_demo(tenant_id: str) -> dict[str, Any]:
    """Build + persist the demo PIM snapshot for a tenant (used by the demo loader)."""
    from app.identity import pim_cache

    snap = build_demo_snapshot(tenant_id=tenant_id)
    return pim_cache.write_snapshot(tenant_id, snap)
