"""Signals over privileged activation sessions (Entra ID + Azure subscriptions).

The PIM signals in ``priv_pim.py`` judge how activation is *configured*. These judge how it
is actually *used*, which is a different question and one the configuration cannot answer:
a role can be perfectly governed on paper and still be activated at 3am, without a reason,
by someone who then did nothing with it.

Every check here degrades honestly. The activation domain reads from up to four sources and
usually not all of them, so a signal that cannot see what it needs raises
``SignalUnavailable`` rather than reporting a clean result from missing data.
"""
from __future__ import annotations

from typing import Any

from app.entra import model
from app.entra.collectors.activations import parse_time
from app.entra.signals import (
    IMPACT_SATURATING,
    SignalContext,
    SignalSpec,
    SignalUnavailable,
    domain,
    principal_label,
)

PIM_DOC = ("https://learn.microsoft.com/entra/id-governance/privileged-identity-management/"
           "pim-how-to-activate-role")
AUDIT_DOC = "https://learn.microsoft.com/entra/identity/monitoring-health/concept-audit-logs"

# A justification shorter than this is not a reason, it is a keystroke. "x", "test" and "-"
# are what people type when the field is mandatory but nobody reads it.
MIN_JUSTIFICATION = 15


def _sessions(data: dict[str, Any]) -> list[dict[str, Any]]:
    dom = domain(data, "activations")
    caps = dom.get("capabilities") or {}
    if not (caps.get("entra_instances") or caps.get("entra_requests")
            or caps.get("azure_requests")):
        raise SignalUnavailable(
            "No activation source is readable — needs RoleManagement.Read.Directory for Entra "
            "activations, or Azure RBAC on a subscription for Azure ones.")
    rows = dom.get("sessions") or []
    if not rows:
        raise SignalUnavailable("No privileged role was activated in the collected window.")
    # A failed or pending request granted nothing. Reporting it as an elevation would accuse
    # someone of holding privilege they were refused.
    granted = [r for r in rows if r.get("granted", True)]
    if not granted:
        raise SignalUnavailable(
            "Every activation in the window failed or is still pending, so none granted access.")
    return granted


def _detailed(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Only sessions whose source can carry a justification.

    The schedule-instances fallback has no justification field at all. Judging those rows
    would report every activation on such a tenant as unjustified, which is a statement
    about our permissions, not about the tenant's hygiene.
    """
    rows = [r for r in _sessions(data) if r.get("detail_known")]
    if not rows:
        raise SignalUnavailable(
            "Activation justifications are not readable on this connection — grant "
            "RoleAssignmentSchedule.Read.Directory (Entra) or read access to Azure PIM.")
    return rows


def _who(data: dict[str, Any], row: dict[str, Any]) -> str:
    return (row.get("principal_upn") or row.get("principal_name")
            or principal_label(data, str(row.get("principal_id") or "")))


def _where(row: dict[str, Any]) -> str:
    return "Azure" if row.get("plane") == "azure" else "Entra ID"


def _finding(row: dict[str, Any], data: dict[str, Any], *, signal_id: str, severity: str,
             title: str, detail: str, **evidence: Any) -> dict[str, Any]:
    return model.finding(
        signal_id=signal_id, severity=severity, pillar="priv",
        object_kind="user" if row.get("plane") == "entra" else "sp",
        object_id=str(row.get("principal_id") or ""),
        object_name=_who(data, row),
        title=title, detail=detail,
        # Findings are grouped per principal AND role, so the role has to be part of the
        # fingerprint or one person's two roles collapse into a single inbox row.
        discriminator=str(row.get("role_name") or row.get("role_id") or ""),
        evidence={
            "session_id": row.get("id"), "plane": row.get("plane"),
            "role": row.get("role_name") or row.get("role_id"),
            "tier": row.get("tier"), "scope": row.get("scope_name") or row.get("scope_id"),
            "start": row.get("start"), "end": row.get("end"),
            "granted_hours": row.get("granted_hours"),
            "source": row.get("source"), **evidence,
        },
        portal_link=model.portal_user(str(row.get("principal_id") or "")),
    )


def _group(rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Group sessions by principal and role.

    One finding per session buries the queue: a team that elevates daily produces hundreds
    of identical rows saying the same thing about the same person. The reader needs "this
    person keeps doing this, N times" once, with the occurrences as evidence.
    """
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (str(row.get("principal_id") or ""), str(row.get("role_name") or row.get("role_id") or ""))
        out.setdefault(key, []).append(row)
    return out


def _occurrences(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda r: r.get("start") or "", reverse=True)
    return {
        "occurrences": len(ordered),
        "most_recent": ordered[0].get("start"),
        "earliest": ordered[-1].get("start"),
        "recent_sessions": [
            {"id": r.get("id"), "start": r.get("start"), "justification": r.get("justification"),
             "scope": r.get("scope_name") or r.get("scope_id")}
            for r in ordered[:5]
        ],
    }


def _no_justification(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    out = []
    blank = [r for r in _detailed(data) if not str(r.get("justification") or "").strip()]
    for (_pid, role), rows in _group(blank).items():
        row = rows[0]
        times = "" if len(rows) == 1 else f" {len(rows)} times"
        out.append(_finding(
            row, data, signal_id="priv.activation_no_justification",
            severity="high" if row.get("tier") == "tier0" else "medium",
            title=f"{_who(data, row)} activated {role or 'a privileged role'}{times} "
                  f"with no reason recorded",
            detail="An activation with no justification cannot be reviewed after the fact. "
                   "The reason is the only thing that distinguishes routine work from misuse "
                   "when the access is examined weeks later.",
            **_occurrences(rows)))
    return out


def _weak_justification(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    out = []
    weak = [r for r in _detailed(data)
            if 0 < len(str(r.get("justification") or "").strip()) < MIN_JUSTIFICATION]
    for (_pid, role), rows in _group(weak).items():
        row = rows[0]
        texts = sorted({str(r.get("justification") or "").strip() for r in rows})
        times = "" if len(rows) == 1 else f" {len(rows)} times"
        out.append(_finding(
            row, data, signal_id="priv.activation_weak_justification", severity="low",
            title=f"{_who(data, row)} activated {role or 'a privileged role'}{times} "
                  f"with a token justification",
            detail="A justification this short satisfies the control without informing anyone. "
                   "Either the field is being treated as a formality or the policy is not "
                   f"asking for anything useful. Recorded: {', '.join(repr(t) for t in texts[:5])}.",
            justifications=texts[:10], **_occurrences(rows)))
    return out


def _out_of_hours(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    """Tier-0 elevation outside the working day, judged in the tenant's local time."""
    from datetime import timedelta

    offset_hours = float(getattr(ctx, "utc_offset_hours", 0.0) or 0.0)
    offset = timedelta(hours=offset_hours)
    day_start = int(getattr(ctx, "business_hours_start", 7))
    day_end = int(getattr(ctx, "business_hours_end", 19))
    zone = "UTC" if not offset_hours else f"UTC{offset_hours:+g}"

    odd: list[dict[str, Any]] = []
    locals_by_id: dict[str, str] = {}
    for row in _sessions(data):
        if row.get("tier") != "tier0":
            continue
        started = parse_time(row.get("start") or "")
        if started is None:
            continue
        local = started + offset
        if local.weekday() < 5 and day_start <= local.hour < day_end:
            continue
        locals_by_id[str(row.get("id") or "")] = local.strftime("%Y-%m-%dT%H:%M")
        odd.append(row)

    out = []
    for (_pid, role), rows in _group(odd).items():
        row = rows[0]
        times = "" if len(rows) == 1 else f" {len(rows)} times"
        out.append(_finding(
            row, data, signal_id="priv.activation_outside_hours", severity="medium",
            title=f"{_who(data, row)} activated {role or 'a tier-0 role'}{times} "
                  f"outside working hours",
            detail="Out-of-hours elevation of the most powerful roles is when unattended "
                   "misuse is least likely to be noticed. It is often legitimate — the point "
                   f"is that it should be explainable, not invisible. Judged in {zone} against "
                   f"a {day_start:02d}:00-{day_end:02d}:00 working day.",
            local_time=locals_by_id.get(str(row.get("id") or "")), timezone=zone,
            business_hours=f"{day_start:02d}:00-{day_end:02d}:00", **_occurrences(rows)))
    return out


def _granted_by_other(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    """Someone elevated a principal that is not themselves."""
    third_party = [r for r in _detailed(data)
                   if str(r.get("requestor_id") or "") and not r.get("self_service")]
    out = []
    for (_pid, role), rows in _group(third_party).items():
        row = rows[0]
        requestor = str(row.get("requestor_id") or "")
        times = "" if len(rows) == 1 else f" {len(rows)} times"
        out.append(_finding(
            row, data, signal_id="priv.activation_granted_by_other", severity="medium",
            title=f"{_who(data, row)} was elevated into {role or 'a privileged role'}{times} "
                  f"by another principal",
            detail="Self-activation is the normal PIM flow. A different requestor means one "
                   "identity handed privilege to another, which is the pattern an attacker "
                   "uses to move access onto an account they already control.",
            requestor_id=requestor, requestor=principal_label(data, requestor),
            **_occurrences(rows)))
    return out


def _long_window(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    limit = float(getattr(ctx, "max_activation_hours", 8.0) or 8.0)
    out = []
    over = [r for r in _sessions(data)
            if r.get("granted_hours") is not None and r["granted_hours"] > limit]
    for (_pid, role), rows in _group(over).items():
        row = max(rows, key=lambda r: r.get("granted_hours") or 0)
        hours = row["granted_hours"]
        times = "" if len(rows) == 1 else f" (on {len(rows)} occasions)"
        out.append(_finding(
            row, data, signal_id="priv.activation_long_window",
            severity="high" if row.get("tier") == "tier0" else "medium",
            title=f"{_who(data, row)} held {role or 'a privileged role'} "
                  f"for {hours:g} hours{times}",
            detail=f"The elevation stayed open well beyond the {limit:g}-hour working maximum. "
                   "A long window is indistinguishable from a standing assignment for most of "
                   "its life, which defeats the point of making the role eligible.",
            hours=hours, limit=limit, **_occurrences(rows)))
    return out


def _broad_azure_scope(data: dict[str, Any], ctx: SignalContext) -> list[dict[str, Any]]:
    """Azure elevation at a scope that covers more than one subscription."""
    broad = [r for r in _sessions(data)
             if r.get("plane") == "azure" and r.get("scope_type") == "managementGroup"]
    out = []
    for (_pid, role), rows in _group(broad).items():
        row = rows[0]
        times = "" if len(rows) == 1 else f" {len(rows)} times"
        out.append(_finding(
            row, data, signal_id="priv.activation_broad_azure_scope", severity="high",
            title=f"{_who(data, row)} activated {role or 'an Azure role'}{times} over the "
                  f"management group {row.get('scope_name') or ''}".rstrip(),
            detail="A management-group activation applies to every subscription beneath it. "
                   "The same role taken at a single subscription would have been enough for "
                   "most work, and would have bounded the blast radius if the session were "
                   "hijacked.",
            scope_type=row.get("scope_type"), **_occurrences(rows)))
    return out


SPECS: list[SignalSpec] = [
    SignalSpec(
        id="priv.activation_no_justification",
        title="Privileged roles activated with no reason recorded",
        question="When someone elevated, did they say why?",
        why="The justification is the only durable record of intent. Without it an activation "
            "can be audited for who and when, but never for whether it should have happened.",
        pillar="priv", severity="high", weight=6, object_kind="user",
        domains=("activations",), requires=("RoleManagement.Read.Directory",),
        benchmarks=("MCSB PA-2",), impact=IMPACT_SATURATING, saturation=5,
        remediation="Require justification on activation for every privileged role.",
        remediation_steps=(
            "Entra admin center > Identity Governance > PIM > Microsoft Entra roles > Settings.",
            "Select the role > Edit > Activation > tick 'Require justification on activation'.",
            "For Azure roles, do the same under PIM > Azure resources > the subscription.",
        ),
        doc_link=PIM_DOC, evaluate=_no_justification, tags=("audit", "zero-trust"),
    ),
    SignalSpec(
        id="priv.activation_weak_justification",
        title="Activation justifications that say nothing",
        question="Are the recorded reasons actually reasons?",
        why="A one-word justification satisfies the control and defeats it at the same time, "
            "which is worse than no control because it looks compliant.",
        pillar="priv", severity="low", weight=2, object_kind="user",
        domains=("activations",), requires=("RoleManagement.Read.Directory",),
        impact=IMPACT_SATURATING, saturation=10,
        remediation="Ask for a ticket reference rather than free text, and review the field.",
        remediation_steps=(
            "Enable 'Require ticket information on activation' so the reason is verifiable.",
            "Review recent activations with your role owners and agree what good looks like.",
        ),
        doc_link=PIM_DOC, evaluate=_weak_justification, tags=("audit",),
    ),
    SignalSpec(
        id="priv.activation_outside_hours",
        title="Tier-0 roles activated outside working hours",
        question="Is the most powerful access being taken when nobody is watching?",
        why="Out-of-hours elevation is normal for on-call and abnormal for everything else. "
            "It is the cheapest signal that separates the two.",
        pillar="priv", severity="medium", weight=4, object_kind="user",
        domains=("activations",), requires=("RoleManagement.Read.Directory",),
        impact=IMPACT_SATURATING, saturation=5,
        remediation="Review out-of-hours tier-0 activations against your on-call rota.",
        remediation_steps=(
            "Confirm each activation belongs to a scheduled on-call or incident.",
            "Require approval on activation for tier-0 roles so out-of-hours needs a second person.",
        ),
        doc_link=AUDIT_DOC, evaluate=_out_of_hours, tags=("detection",),
    ),
    SignalSpec(
        id="priv.activation_granted_by_other",
        title="Principals elevated by somebody else",
        question="Did anyone hand privileged access to another identity?",
        why="PIM is designed around self-activation by an eligible principal. A third-party "
            "requestor is how privilege gets moved onto an account an attacker already holds.",
        pillar="priv", severity="medium", weight=5, object_kind="user",
        domains=("activations",), requires=("RoleAssignmentSchedule.Read.Directory",),
        impact=IMPACT_SATURATING, saturation=3,
        remediation="Confirm each third-party elevation was requested through your change process.",
        remediation_steps=(
            "Identify the requestor and confirm the elevation was expected.",
            "Restrict who can administer PIM assignments to a small, reviewed group.",
        ),
        doc_link=PIM_DOC, evaluate=_granted_by_other, tags=("zero-trust",),
    ),
    SignalSpec(
        id="priv.activation_long_window",
        title="Elevations that stayed open too long",
        question="How long was privilege actually held?",
        why="An eight-hour role held for twenty-four is a standing assignment wearing a "
            "just-in-time label.",
        pillar="priv", severity="high", weight=5, object_kind="user",
        domains=("activations",), requires=("RoleManagement.Read.Directory",),
        impact=IMPACT_SATURATING, saturation=5,
        remediation="Lower the maximum activation duration for privileged roles.",
        remediation_steps=(
            "PIM > Settings > the role > Edit > Activation > reduce 'Activation maximum duration'.",
            "Prefer a short window plus the ability to re-activate over one long window.",
        ),
        doc_link=PIM_DOC, evaluate=_long_window, tags=("zero-trust",),
    ),
    SignalSpec(
        id="priv.activation_broad_azure_scope",
        title="Azure roles activated over a whole management group",
        question="How much of Azure did each elevation actually cover?",
        why="Management-group scope multiplies an elevation across every subscription beneath "
            "it, so one compromised session reaches the entire estate rather than one workload.",
        pillar="priv", severity="high", weight=6, object_kind="sp",
        domains=("activations",), requires=(),
        impact=IMPACT_SATURATING, saturation=3,
        remediation="Make eligibility subscription-scoped unless the work genuinely spans the group.",
        remediation_steps=(
            "PIM > Azure resources > the management group > Roles > review eligible assignments.",
            "Re-create the eligibility at the narrowest scope the role holder actually needs.",
        ),
        doc_link=PIM_DOC, evaluate=_broad_azure_scope, tags=("blast-radius",),
    ),
]
