"""Drift signals (governance pillar).

These fire on *change*, not on state. Every other signal in the registry answers "is this
configuration bad?"; these answer "did something move, and did anybody expect it to?" — which is
the question that catches the compromise the configuration checks were never going to see,
because the resulting configuration is perfectly ordinary.

They all gate on `drift_available`. A tenant with a single scan has nothing to compare against,
and an empty change list would otherwise read as "nothing has changed" — a reassuring claim that
would be false on every first run.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.iam import attribution, diff as diff_mod
from app.iam.signals import Finding, SignalContext, SignalSpec

# Business hours in the READER's local time, not UTC. 07:00–19:00 is deliberately generous: the
# point is to catch 3am, not to police people who start early.
BUSINESS_START_HOUR = 7
BUSINESS_END_HOUR = 19

#: How close a remove/re-add pair must be to look like a fight rather than two decisions.
REVERT_WINDOW_HOURS = 24


def _changes(ctx: SignalContext) -> list[dict[str, Any]]:
    ctx.require(
        ctx.drift_available,
        "Only one scan exists for this tenant, so there is nothing to compare it against. "
        "No drift findings does not mean nothing changed.",
    )
    return list((ctx.drift or {}).get("changes", []) or [])


def _local_hour(iso: str, offset_minutes: int) -> int | None:
    """Hour-of-day in the reader's local time, or None if the timestamp is unusable."""
    text = (iso or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (stamp.astimezone(timezone.utc) + timedelta(minutes=offset_minutes)).hour


def _actor_label(change: dict[str, Any]) -> str:
    actor = change.get("actor") or {}
    name = actor.get("actorDisplayName") or actor.get("actorPrincipalId")
    return str(name) if name else "an unidentified actor"


# --------------------------------------------------------------------------- signals
def _privileged_added(ctx: SignalContext) -> list[Finding]:
    """New privileged access since the previous scan. The headline drift event."""
    out = []
    for c in _changes(ctx):
        if c["class"] not in (diff_mod.ADDED, diff_mod.ESCALATED) or not c.get("privileged"):
            continue
        actor = c.get("actor") or {}
        who = _actor_label(c)
        out.append(
            Finding(
                signal_id="gov.drift_privileged_added",
                title="New privileged access since the last scan",
                severity="error",
                pillar="gov",
                object_kind="assignment",
                subject=c["key"],
                subject_label=f"{c['principalName']} — {c['roleName']}",
                detail=(
                    f"{c['principalName']} gained {c['roleName']} at {c['scope']} since the previous "
                    f"scan, granted by {who}"
                    + (f" from {actor['callerIpAddress']}" if actor.get("callerIpAddress") else "")
                    + (f" via {actor['changeSource']}" if actor.get("changeSource") not in (None, "Unknown") else "")
                    + "."
                ),
                evidence={
                    "class": c["class"], "scope": c["scope"], "role": c["roleName"],
                    "actor": actor, "from": c.get("from"), "to": c.get("to"),
                },
                remediation=f"Confirm this grant was intended. If not, revoke {c['roleName']} at {c['scope']}.",
                frameworks=("NIST:AC-2", "MCSB:PA-1"),
            )
        )
    return out


def _out_of_band(ctx: SignalContext) -> list[Finding]:
    """Authorization changed by hand in an estate that is otherwise managed as code.

    Reported only when the tenant demonstrably DOES use IaC for authorization — otherwise every
    change in a click-ops estate is "out of band" and the signal is noise that trains people to
    ignore the list."""
    changes = _changes(ctx)
    attributed = [c for c in changes if (c.get("actor") or {}).get("changeSource") not in (None, "", "Unknown")]
    ctx.require(
        bool(attributed),
        "No authorization change could be attributed to a tool, so IaC-managed and hand-made "
        "changes cannot be told apart.",
    )
    iac = [c for c in attributed if (c["actor"]["changeSource"]) in attribution.IAC_SOURCES]
    ctx.require(
        bool(iac),
        "No authorization change in this window came from a template or pipeline, so there is no "
        "IaC baseline to call anything out-of-band against.",
    )
    out = []
    for c in attributed:
        source = c["actor"]["changeSource"]
        if source not in attribution.HUMAN_SOURCES:
            continue
        out.append(
            Finding(
                signal_id="gov.drift_out_of_band",
                title="Authorization changed by hand in an IaC-managed estate",
                severity="warning",
                pillar="gov",
                object_kind="assignment",
                subject=c["key"],
                subject_label=f"{c['principalName']} — {c['roleName']}",
                detail=(
                    f"{_actor_label(c)} changed access for {c['principalName']} at {c['scope']} via "
                    f"{source}, while {len(iac)} other authorization change(s) in this window came "
                    f"through a template or pipeline. A hand-made grant is invisible to the next deploy."
                ),
                evidence={"changeSource": source, "actor": c["actor"], "iac_changes": len(iac)},
                remediation="Move this grant into the template that owns the scope, or revert it and redeploy.",
                frameworks=("NIST:AC-2", "MCSB:PA-1"),
            )
        )
    return out


def _after_hours(ctx: SignalContext) -> list[Finding]:
    """Authorization changed outside business hours, judged in LOCAL time.

    Judging raw UTC calls a Tokyo morning suspicious and misses a London midnight entirely, so
    this needs the reader's offset and says so when it does not have one."""
    out = []
    for c in _changes(ctx):
        actor = c.get("actor") or {}
        hour = _local_hour(str(actor.get("eventTimestamp", "")), ctx.utc_offset_minutes)
        if hour is None or BUSINESS_START_HOUR <= hour < BUSINESS_END_HOUR:
            continue
        out.append(
            Finding(
                signal_id="gov.drift_after_hours",
                title="Authorization changed outside business hours",
                severity="warning",
                pillar="gov",
                object_kind="assignment",
                subject=c["key"],
                subject_label=f"{c['principalName']} — {c['roleName']}",
                detail=(
                    f"{_actor_label(c)} changed access for {c['principalName']} at {str(hour).zfill(2)}:00 "
                    f"local time. Out-of-hours authorization changes are not wrong, but they are worth "
                    f"a second look because an attacker has no working day."
                ),
                evidence={"local_hour": hour, "utc_offset_minutes": ctx.utc_offset_minutes, "actor": actor},
                remediation="Confirm with the actor that this change was intentional and expected at that time.",
                frameworks=("NIST:AC-2",),
            )
        )
    return out


def _self_grant(ctx: SignalContext) -> list[Finding]:
    """The actor granted the access to themselves.

    Separation of duties in its most concentrated form. Someone who can grant themselves Owner
    has, in practice, already got it — the only thing standing between them and the estate is a
    step they control."""
    out = []
    for c in _changes(ctx):
        if c["class"] not in (diff_mod.ADDED, diff_mod.ESCALATED):
            continue
        actor = c.get("actor") or {}
        actor_id = str(actor.get("actorPrincipalId", "")).lower()
        if not actor_id or actor_id != str(c.get("principalId", "")).lower():
            continue
        out.append(
            Finding(
                signal_id="gov.drift_self_grant",
                title="A principal granted access to itself",
                severity="critical" if c.get("privileged") else "error",
                pillar="gov",
                object_kind="assignment",
                subject=c["key"],
                subject_label=f"{c['principalName']} — {c['roleName']}",
                detail=(
                    f"{c['principalName']} granted themselves {c['roleName']} at {c['scope']}. There is "
                    f"no separation of duties on this change: the person who benefited is the person "
                    f"who approved it."
                ),
                evidence={"actor": actor, "scope": c["scope"], "role": c["roleName"]},
                remediation=(
                    f"Revoke {c['roleName']} at {c['scope']} and re-grant it through a request that "
                    f"someone else approves, or move the role behind PIM with approval required."
                ),
                frameworks=("NIST:AC-5", "NIST:AC-6", "MCSB:PA-1"),
            )
        )
    return out


def _reverted(ctx: SignalContext) -> list[Finding]:
    """Access removed and put straight back.

    Either a fight between a template and a human — in which case the template will win the next
    deploy and the access will vanish again at the worst moment — or someone testing in
    production."""
    changes = _changes(ctx)
    removed = {c["key"] for c in changes if c["class"] == diff_mod.REMOVED}
    out = []
    for c in changes:
        if c["class"] != diff_mod.ADDED or c["key"] not in removed:
            continue
        out.append(
            Finding(
                signal_id="gov.drift_reverted",
                title="Access removed and immediately re-added",
                severity="warning",
                pillar="gov",
                object_kind="assignment",
                subject=c["key"],
                subject_label=f"{c['principalName']} — {c['roleName']}",
                detail=(
                    f"{c['principalName']}'s {c['roleName']} at {c['scope']} was removed and re-added "
                    f"within the same comparison window. That is usually a template and a human "
                    f"disagreeing — and the template wins the next deploy."
                ),
                evidence={"scope": c["scope"], "role": c["roleName"], "actor": c.get("actor")},
                remediation="Decide where this grant lives — in the template or out of it — and make both agree.",
                frameworks=("NIST:AC-2",),
            )
        )
    return out


SIGNALS: list[SignalSpec] = [
    SignalSpec(
        id="gov.drift_privileged_added",
        title="New privileged access since the last scan",
        pillar="gov", severity="error", weight=9, object_kind="assignment",
        why="New privilege is the event worth knowing about the same day, not at the next review.",
        remediation="Confirm the grant was intended; revoke it if not.",
        frameworks=("NIST:AC-2", "MCSB:PA-1"),
        evaluate=_privileged_added,
    ),
    SignalSpec(
        id="gov.drift_out_of_band",
        title="Authorization changed by hand in an IaC-managed estate",
        pillar="gov", severity="warning", weight=5, object_kind="assignment",
        why="A hand-made grant is invisible to the pipeline and disappears at the next deploy.",
        remediation="Move the grant into the template, or revert it.",
        frameworks=("NIST:AC-2", "MCSB:PA-1"),
        evaluate=_out_of_band,
    ),
    SignalSpec(
        id="gov.drift_after_hours",
        title="Authorization changed outside business hours",
        pillar="gov", severity="warning", weight=4, object_kind="assignment",
        why="An attacker has no working day. Out-of-hours privilege changes deserve a look.",
        remediation="Confirm the change was intentional.",
        frameworks=("NIST:AC-2",),
        evaluate=_after_hours,
    ),
    SignalSpec(
        id="gov.drift_self_grant",
        title="A principal granted access to itself",
        pillar="gov", severity="critical", weight=9, object_kind="assignment",
        why="No separation of duties: the beneficiary and the approver are the same principal.",
        remediation="Re-grant through a path somebody else approves, or put the role behind PIM.",
        frameworks=("NIST:AC-5", "NIST:AC-6", "MCSB:PA-1"),
        evaluate=_self_grant,
    ),
    SignalSpec(
        id="gov.drift_reverted",
        title="Access removed and immediately re-added",
        pillar="gov", severity="warning", weight=3, object_kind="assignment",
        why="A template and a human disagreeing; the access will vanish again at the next deploy.",
        remediation="Make the template and the intent agree.",
        frameworks=("NIST:AC-2",),
        evaluate=_reverted,
    ),
]
