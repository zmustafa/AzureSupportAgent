"""CIEM signals — granted versus used (least-privilege pillar).

Every signal here gates on usage having been *measured*. That gate is the whole reason this file
is separate from the rest of the `lp` pillar: the other least-privilege checks read a snapshot
that always exists, and these read one that usually does not, because usage collection is a
separate schedulable job with its own freshness.

Without the gate, a tenant that has never run a usage scan would see "0 over-privileged
principals" — the most reassuring possible rendering of "we have not looked".
"""
from __future__ import annotations

from typing import Any

from app.iam import diff as diff_mod, effective, usage
from app.iam.signals import Finding, SignalContext, SignalSpec

#: A principal holding a privileged role that only ever read is the clearest CIEM finding there
#: is, and it needs no threshold to justify it.
_READ_MARKERS = ("/read", "/list", "read/action")


def _measured(ctx: SignalContext) -> dict[str, Any]:
    ctx.require(
        usage.is_measured(ctx.usage),
        "Usage has not been collected for this tenant. Nothing here is a claim about what is "
        "unused — run a usage scan first.",
    )
    return ctx.usage


def _overprivileged(ctx: SignalContext) -> list[Finding]:
    """Principals holding far more than they exercised.

    Reads the right-sizing analysis rather than recomputing it, so the finding and the
    recommendation screen can never disagree about the same principal."""
    _measured(ctx)
    out = []
    for rec in (ctx.rightsizing.get("recommendations") or []):
        if rec.get("confidence") == usage.LOW:
            # A low-confidence "unused" is not a finding, it is a prompt to collect more data.
            continue
        proposal = rec.get("recommendation")
        out.append(Finding(
            signal_id="lp.overprivileged",
            title="Far more access granted than exercised",
            severity="warning", pillar="lp", object_kind="principal",
            subject=f"{rec['principalId']}|{rec['scope']}",
            subject_label=rec.get("principalName") or rec["principalId"],
            detail=(
                # Both numbers, always. "99.8% over-privileged" alone is a figure designed to be
                # quoted out of context.
                f"{rec.get('principalName')} used {rec['usedActionCount']} of the "
                f"{rec['grantedActionCount']} action patterns {rec['currentRoles'][0]} grants at "
                f"{rec['scope']}, over {rec['window']['days']} days. {rec['confidenceWhy']}"
            ),
            count=rec["grantedActionCount"] - rec["usedActionCount"],
            evidence={
                "usedActionCount": rec["usedActionCount"],
                "grantedActionCount": rec["grantedActionCount"],
                "unusedRatio": rec["unusedRatio"],
                "window_days": rec["window"]["days"],
                "confidence": rec["confidence"],
                "recommendation": proposal,
                "note": rec.get("note", ""),
            },
            remediation=(
                f"Consider {', '.join(proposal['roles'])} at {proposal['scope']} instead. "
                f"{proposal['residualRisk']}"
                if proposal else
                "No built-in combination covers everything this principal did, so review manually."
            ),
            frameworks=("NIST:AC-6", "MCSB:PA-7"),
        ))
    return out


def _owner_used_as_reader(ctx: SignalContext) -> list[Finding]:
    """Holds a tier-0 role, exercised only reads.

    Stronger than the ratio check because it needs no threshold and no judgment: somebody with
    the ability to delete the subscription spent the window looking at it."""
    _measured(ctx)
    used = usage.used_actions(ctx.usage)
    out = []
    seen: set[str] = set()
    for row in ctx.grants:
        if diff_mod.privilege_tier(row) < diff_mod.TIER_OWNER:
            continue
        pid = str(row.get("effectivePrincipalId") or "").lower()
        if not pid or pid in seen:
            continue
        actions = used.get(pid)
        if not actions:
            # No recorded activity at all is the "blind" case, not the "read-only" case. It is
            # covered by the ratio check with its own confidence, and asserting read-only here
            # would be inventing a behavior profile from an empty log.
            continue
        if any(not _looks_read(a) for a in actions):
            continue
        if usage.is_break_glass(row):
            continue
        seen.add(pid)
        out.append(Finding(
            signal_id="lp.owner_used_as_reader",
            title="Owner-level access used only for reading",
            severity="warning", pillar="lp", object_kind="principal",
            subject=pid,
            subject_label=str(row.get("effectivePrincipalName") or pid),
            detail=(
                f"{row.get('effectivePrincipalName') or pid} holds {row.get('roleName')} at "
                f"{row.get('scope')} but every one of the {len(actions)} operation(s) recorded in "
                f"the window was a read. They can delete the subscription; they have been looking "
                f"at it."
            ),
            count=len(actions),
            evidence={"role": row.get("roleName"), "scope": row.get("scope"),
                      "actions": sorted(actions)[:10], "window_days": ctx.usage.get("window_days")},
            remediation="Move to Reader, and use PIM eligibility for the occasions that need more.",
            frameworks=("NIST:AC-6", "MCSB:PA-1", "MCSB:PA-7"),
        ))
    return out


def _looks_read(action: str) -> bool:
    a = (action or "").lower()
    return any(a.endswith(m) or m in a for m in _READ_MARKERS)


def _scope_too_broad(ctx: SignalContext) -> list[Finding]:
    """Everything exercised sits inside one resource group, but the grant is at subscription or
    higher. The difference between those two is what an attacker gets on a bad day."""
    _measured(ctx)
    out = []
    for rec in (ctx.rightsizing.get("recommendations") or []):
        proposal = rec.get("recommendation") or {}
        narrower = str(proposal.get("scope", ""))
        current = str(rec.get("scope", ""))
        if not narrower or narrower == current:
            continue
        if diff_mod.scope_depth(narrower) <= diff_mod.scope_depth(current):
            continue
        out.append(Finding(
            signal_id="lp.scope_too_broad",
            title="Access granted far above where it is used",
            severity="warning", pillar="lp", object_kind="assignment",
            subject=f"{rec['principalId']}|{current}",
            subject_label=rec.get("principalName") or rec["principalId"],
            detail=(
                f"{rec.get('principalName')} holds {rec['currentRoles'][0]} at {current}, but every "
                f"operation recorded in {rec['window']['days']} days was inside {narrower}."
            ),
            evidence={"currentScope": current, "narrowerScope": narrower,
                      "window_days": rec["window"]["days"], "confidence": rec["confidence"]},
            remediation=f"Re-scope the assignment to {narrower}. {proposal.get('residualRisk', '')}",
            frameworks=("NIST:AC-6", "MCSB:PA-7"),
        ))
    return out


SIGNALS: list[SignalSpec] = [
    SignalSpec(
        id="lp.overprivileged",
        title="Far more access granted than exercised",
        pillar="lp", severity="warning", weight=8, object_kind="principal",
        why="Granted-but-never-used access is the blast radius of a compromise for no benefit.",
        remediation="Right-size to the narrowest roles covering what was actually used.",
        frameworks=("NIST:AC-6", "MCSB:PA-7"),
        evaluate=_overprivileged,
    ),
    SignalSpec(
        id="lp.owner_used_as_reader",
        title="Owner-level access used only for reading",
        pillar="lp", severity="warning", weight=7, object_kind="principal",
        why="Somebody who can delete the subscription spent the window looking at it.",
        remediation="Move to Reader with PIM eligibility for the rest.",
        frameworks=("NIST:AC-6", "MCSB:PA-1", "MCSB:PA-7"),
        evaluate=_owner_used_as_reader,
    ),
    SignalSpec(
        id="lp.scope_too_broad",
        title="Access granted far above where it is used",
        pillar="lp", severity="warning", weight=6, object_kind="assignment",
        why="The gap between granted scope and used scope is what an attacker gets on a bad day.",
        remediation="Re-scope to the narrowest scope containing the activity.",
        frameworks=("NIST:AC-6", "MCSB:PA-7"),
        evaluate=_scope_too_broad,
    ),
]
