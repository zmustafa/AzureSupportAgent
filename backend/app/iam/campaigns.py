"""Access review campaigns — the workflow layer a snapshot tool cannot have.

An auditor asks: *"prove that a human reviewed who has privileged access to production, that they
made a decision on each one, and that the revocations happened."* Everything here exists to make
that answerable.

Four rules are load-bearing and each one is a deliberate rejection of what access-review products
usually do:

**Nothing is ever auto-approved.** A campaign that reaches its due date with undecided items
completes as *incomplete* and says so. Auto-approving on expiry is the single worst default in
this category — it manufactures the exact evidence the auditor came for, out of nothing. Auto-
revoking is also wrong here, because the product is read-only and would be asserting an outcome
it cannot cause.

**Nobody reviews their own access.** Self-review is not certification. It is available only as an
explicitly-labeled attestation campaign, and the label travels into the export so it cannot be
mistaken for the real thing later.

**An item whose access changed since the baseline is re-presented, not updated.** The reviewer
certified what they were shown. Silently swapping the underlying row keeps the decision and
changes its meaning.

**The reviewer sees why, not just what.** "Alice — Contributor — /subscriptions/x" is a
rubber-stamp prompt. Each item carries how the access is held, whether it is standing, what it
can reach, and what breaks if it is revoked.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, func, select

from app.core.db import SessionLocal
from app.iam import compose, diff as diff_mod, leavers, schema
from app.models import IamReviewCampaign, IamReviewItem

log = logging.getLogger("app.iam.campaigns")

# Lifecycle.
DRAFT = "draft"
ACTIVE = "active"
COMPLETED = "completed"
CANCELLED = "cancelled"
EXPIRED = "expired"

# Decisions. `None` means undecided, which is never the same as approved.
APPROVE = "approve"
REVOKE = "revoke"
REDUCE = "reduce"
DELEGATE = "delegate"
NEEDS_INFO = "needs_info"
DECISIONS = (APPROVE, REVOKE, REDUCE, DELEGATE, NEEDS_INFO)

# Reviewer strategies.
BY_OWNER = "owner"
BY_MANAGER = "manager"
FIXED = "fixed"
SELF = "self"
STRATEGIES = (BY_OWNER, BY_MANAGER, FIXED, SELF)

#: A campaign is capped so one careless selector cannot create a review nobody will ever finish.
MAX_ITEMS = 2000


class CampaignError(ValueError):
    """A campaign operation that must be refused with a reason the caller can show."""


# --------------------------------------------------------------------------- selectors
def select_rows(
    rows: list[dict[str, Any]],
    selector: dict[str, Any],
    findings: list[dict[str, Any]] | None = None,
    *,
    tenant_id: str = "",
) -> list[dict[str, Any]]:
    """Apply a campaign selector to the composed access rows.

    Deny rows are excluded from every selector: a deny assignment grants nothing, so asking a
    human to certify it wastes the only resource this feature is really spending, which is their
    attention."""
    kind = str(selector.get("kind", "")).strip()
    live = [r for r in rows if r.get("effect") != schema.EFFECT_DENY]

    if kind == "privileged":
        out = [r for r in live if r.get("roleIsPrivileged")]
    elif kind == "scope":
        scope = str(selector.get("scope_id", "")).lower()
        out = [r for r in live if str(r.get("scope", "")).lower().startswith(scope)] if scope else []
    elif kind == "external":
        include = set(selector.get("include") or ["guest"])
        out = [r for r in live if _is_external(r, include)]
    elif kind == "principal_type":
        types = {str(t).lower() for t in (selector.get("types") or [])}
        out = [r for r in live if str(r.get("effectivePrincipalType", "") or r.get("principalType", "")).lower() in types]
    elif kind == "disabled":
        # Access held by accounts that are DISABLED in Entra ID.
        #
        # Only a known ``false`` qualifies. ``unknown`` must never be swept into a certification
        # campaign: a cache that predates the account-state collector would otherwise put the
        # entire estate in front of a reviewer under the heading "these people have left", and
        # the reviewer would rightly stop trusting the tool after the first wrong name.
        out = [r for r in live if schema.is_disabled(r)]
        # …then narrowed by the SAME filter set the screen applies. This selector previously
        # understood two of the sixteen filters, so a campaign started from a screen showing 3
        # identities covered all 78 — the artifact not matching the screen it was launched from,
        # which is the exact defect the export path was built to avoid.
        #
        # Two of them are derivable from the rows alone and are applied here. The rest need the
        # per-identity rollup, which needs a tenant. When one of those is present WITHOUT a
        # tenant, this RAISES rather than quietly ignoring it: a selector that silently drops
        # half its own filters recreates the bug in a form nobody can see.
        if selector.get("privileged_only"):
            out = [r for r in out if r.get("roleIsPrivileged")]
        if str(selector.get("tier", "")).strip() == "live_now":
            # Owns a service principal — the only sub-population whose access is exercisable
            # today rather than one re-enable away.
            owners = {
                str(r.get("effectivePrincipalId", "")).lower()
                for r in out
                if r.get("accessPath") == schema.PATH_OWNER
            }
            out = [r for r in out if str(r.get("effectivePrincipalId", "")).lower() in owners]

        rollup_keys = {
            k for k in selector
            if k in leavers.FILTER_KEYS and k not in ("privileged_only", "tier", "signin_kind")
            and selector[k] not in (None, "", False, [])
        }
        if rollup_keys:
            if not tenant_id:
                raise CampaignError(
                    "these filters need the identity rollup and cannot be applied without a "
                    f"tenant: {', '.join(sorted(rollup_keys))}"
                )
            leaver_filter = {k: v for k, v in selector.items() if k in leavers.FILTER_KEYS}
            keep = leavers.selected_principal_ids(tenant_id, leaver_filter)
            out = [
                r for r in out
                if str(r.get("effectivePrincipalId") or r.get("principalId") or "").lower() in keep
            ]
    elif kind == "signal":
        wanted = set(selector.get("signal_ids") or [])
        subjects = {
            str(f.get("subject", "")).lower()
            for f in (findings or [])
            if not wanted or f.get("signal_id") in wanted
        }
        out = [
            r for r in live
            if str(r.get("assignmentId", "")).lower() in subjects
            or str(r.get("effectivePrincipalId", "")).lower() in subjects
            or str(r.get("scope", "")).lower() in subjects
        ]
    else:
        raise CampaignError(f"unknown selector kind {kind!r}")

    # Stable order so re-running a selector produces the same campaign, and so the reviewer sees
    # the riskiest items first rather than alphabetically.
    out.sort(key=lambda r: (not r.get("roleIsPrivileged"), str(r.get("effectivePrincipalName", "")), str(r.get("scope", ""))))
    return out


def _is_external(row: dict[str, Any], include: set[str]) -> bool:
    upn = str(row.get("effectivePrincipalUserPrincipalName", "") or row.get("principalUserPrincipalName", ""))
    ptype = str(row.get("effectivePrincipalType", "") or row.get("principalType", "")).lower()
    if "guest" in include and "#ext#" in upn.lower():
        return True
    if "lighthouse" in include and str(row.get("accessModel", "")).lower().find("lighthouse") >= 0:
        return True
    if "multi_tenant_sp" in include and ptype == "serviceprincipal" and row.get("principalAppId"):
        return True
    return False


# --------------------------------------------------------------------------- reviewers
def resolve_reviewer(
    row: dict[str, Any],
    *,
    strategy: str,
    tenant_id: str,
    fallback: str = "",
) -> tuple[str, str]:
    """Return ``(reviewer_id, source)`` for one access row.

    Priority: resource/subscription owner → manager of the principal → owner of the service
    principal → fixed fallback. Each step is tried in the order that produces the person most
    likely to know whether the access is still needed.

    **Never returns the principal themselves** outside an explicit `self` attestation campaign.
    A reviewer asked to certify their own access will approve it, and an audit trail that records
    that as certification is actively misleading."""
    subject = str(row.get("effectivePrincipalId", "") or row.get("principalId", "")).lower()

    if strategy == SELF:
        return subject, SELF

    candidates: list[tuple[str, str]] = []
    if strategy in (BY_OWNER, BY_MANAGER):
        owner = _owner_for(tenant_id, str(row.get("scope", "")))
        if owner:
            candidates.append((owner, "owner"))
    if strategy == BY_MANAGER:
        manager = _manager_for(tenant_id, subject)
        if manager:
            candidates.insert(0, (manager, "manager"))
    if fallback:
        candidates.append((fallback, "fallback"))

    for reviewer, source in candidates:
        if reviewer.lower() != subject:
            return reviewer, source
    # Everything resolved to the subject, or nothing resolved at all. Say so rather than quietly
    # assigning them to themselves.
    return "", "unassigned"


def _owner_for(tenant_id: str, scope: str) -> str:
    """Owner of the scope, from the ownership feature. Best-effort — a missing owner is a normal
    condition and must not fail campaign creation."""
    if not scope:
        return ""
    try:
        from app.ownership import resolve as ownership

        parsed = schema.parse_scope(scope)
        kind = {
            schema.SCOPE_SUBSCRIPTION: "subscription",
            schema.SCOPE_RESOURCE_GROUP: "resource_group",
            schema.SCOPE_RESOURCE: "resource",
        }.get(parsed.get("scopeType", ""), "")
        if not kind:
            return ""
        resolved = ownership.resolve_owner(
            tenant_id, kind, scope,
            subscription_id=parsed.get("subscriptionId", ""),
            resource_group=parsed.get("resourceGroup", ""),
        )
        owners = resolved.get("owners") or []
        primary = next((o for o in owners if o.get("primary")), owners[0] if owners else None)
        return str((primary or {}).get("owner_id") or (primary or {}).get("email") or "")
    except Exception:  # noqa: BLE001 — ownership is an optional enrichment
        log.debug("ownership lookup failed for %s", scope, exc_info=True)
        return ""


def _manager_for(tenant_id: str, principal_id: str) -> str:
    """Manager of a user principal. Returns empty for service principals by design — a service
    principal has no manager, and inventing one puts a decision in front of someone who cannot
    make it."""
    return ""


# --------------------------------------------------------------------------- item context
def build_context(row: dict[str, Any], *, escalation: dict[str, Any] | None = None, findings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """What a reviewer needs to make a real decision rather than a rubber stamp."""
    principal = str(row.get("effectivePrincipalId", "")).lower()
    reach = []
    for path in (escalation or {}).get("paths", []) or []:
        if str(path.get("from", "")).lower() == principal:
            reach.append({"length": path.get("length"), "confidence": path.get("min_confidence")})
    open_findings = [
        {"id": f.get("signal_id"), "title": f.get("title"), "severity": f.get("severity")}
        for f in (findings or [])
        if str(f.get("subject", "")).lower() in {principal, str(row.get("assignmentId", "")).lower()}
    ]
    return {
        "why": str(row.get("accessPath", "")) or schema.PATH_DIRECT,
        "groupChain": row.get("groupChain", ""),
        "standing": schema.is_standing_privilege(row),
        "assignmentCreatedOn": row.get("assignmentCreatedOn", ""),
        "escalationPaths": reach[:5],
        "openFindings": open_findings[:5],
        # Deliberately absent rather than faked: usage needs the CIEM collection from P8, and a
        # "last used: never" that actually means "never measured" would get access revoked on the
        # strength of a blank.
        "usage": None,
        "usageNote": "Usage data is not collected. This item shows no last-used date because it "
                     "was never measured, not because the access is unused.",
    }


# --------------------------------------------------------------------------- lifecycle
def _dedupe_by_review_key(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Collapse rows that are the SAME review decision, keeping every path that produced them.

    ``diff.row_key`` is (principal, role, scope, surface, state) and deliberately excludes the
    access path — "same access, different governance". ``iam_review_item`` is UNIQUE on
    (campaign_id, row_key), so any selector that returns one principal holding one role at one
    scope through **two different groups** produced two items with the same key and the whole
    campaign died with an IntegrityError, as a 500, after the rows had been chosen.

    Nothing existing hit it because no previous selector was principal-centric; the disabled
    -account selector is, and on a real tenant 53 of 78 leavers hold their access through
    groups, where overlapping membership is completely ordinary.

    De-duplicating is also the right REVIEW semantics: "should this person have this access" is
    one decision, not one per group that grants it. But the folded paths must not be lost —
    revoking one group membership while another still grants the same role leaves the access in
    place — so they are carried on the surviving row for the remediation step. A DIRECT row wins
    over a group-derived one, because its remediation (delete the assignment) differs from the
    group's (remove the member)."""
    best: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    folded: dict[str, list[str]] = {}
    for row in rows:
        key = diff_mod.row_key(row)
        via = str(row.get("sourceGroupName") or row.get("sourceGroupId") or "") or str(
            row.get("accessPath") or ""
        )
        prev = best.get(key)
        if prev is None:
            best[key] = row
            order.append(key)
            folded[key] = [via]
            continue
        if via not in folded[key]:
            folded[key].append(via)
        if prev.get("accessPath") == schema.PATH_GROUP and row.get("accessPath") != schema.PATH_GROUP:
            best[key] = row
    out = [best[k] for k in order]
    return out, {k: v for k, v in folded.items() if len(v) > 1}


async def create(
    tenant_id: str,
    *,
    name: str,
    selector: dict[str, Any],
    baseline_run_id: str = "",
    reviewer_strategy: str = BY_OWNER,
    reviewer_fallback_id: str = "",
    description: str = "",
    due_at: datetime | None = None,
    reminder_days: list[int] | None = None,
    connection_id: str | None = None,
    created_by: str = "",
    escalation: dict[str, Any] | None = None,
    findings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a campaign in `draft` from a selector over the current access snapshot."""
    if reviewer_strategy not in STRATEGIES:
        raise CampaignError(f"unknown reviewer strategy {reviewer_strategy!r}")
    if not name.strip():
        raise CampaignError("a campaign needs a name")

    rows = compose.build_master_rows(tenant_id)
    chosen = select_rows(rows, selector, findings, tenant_id=tenant_id)
    if not chosen:
        raise CampaignError("that selector matched no access rows — nothing to certify")
    scoped_principals = {
        str(r.get("effectivePrincipalId") or r.get("principalId") or "").lower() for r in chosen
    } - {""}
    chosen, folded = _dedupe_by_review_key(chosen)
    truncated = len(chosen) > MAX_ITEMS
    chosen = chosen[:MAX_ITEMS]

    async with SessionLocal() as db:
        campaign = IamReviewCampaign(
            tenant_id=tenant_id,
            connection_id=connection_id,
            name=name.strip(),
            description=description,
            selector_json=selector,
            baseline_run_id=baseline_run_id,
            reviewer_strategy=reviewer_strategy,
            reviewer_fallback_id=reviewer_fallback_id,
            status=DRAFT,
            due_at=due_at,
            reminder_days=reminder_days or [7, 3, 1],
            created_by=created_by,
        )
        db.add(campaign)
        await db.flush()

        for row in chosen:
            reviewer, source = resolve_reviewer(
                row, strategy=reviewer_strategy, tenant_id=tenant_id, fallback=reviewer_fallback_id
            )
            key = diff_mod.row_key(row)
            context = build_context(row, escalation=escalation, findings=findings)
            if key in folded:
                # Every path that grants this same access. Revoking one while another still
                # grants it leaves the access exactly where it was.
                context["alsoGrantedVia"] = folded[key]
            db.add(
                IamReviewItem(
                    campaign_id=campaign.id,
                    tenant_id=tenant_id,
                    row_key=key,
                    row_snapshot_json=row,
                    context_json=context,
                    reviewer_id=reviewer,
                    reviewer_source=source,
                )
            )
        campaign.stats_json = _stats(
            chosen, truncated=truncated, principals=len(scoped_principals), selector=selector
        )
        await db.commit()
        await db.refresh(campaign)
        return _public(campaign)


def _stats(
    rows: list[dict[str, Any]],
    *,
    truncated: bool = False,
    principals: int = 0,
    selector: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "total": len(rows),
        "privileged": sum(1 for r in rows if r.get("roleIsPrivileged")),
        "decided": 0,
        "by_decision": {d: 0 for d in DECISIONS},
        "truncated": truncated,
        "cap": MAX_ITEMS,
        # What this campaign was scoped to WHEN IT WAS CREATED.
        #
        # The selector is re-evaluated on refresh, which is the property that lets a standing
        # review notice a new leaver — but it also means the list a reviewer sees can differ
        # from the one the operator was looking at when they created it. Recording the original
        # population makes that visible instead of surprising.
        "scoped_principals": principals,
        "scoped_at_creation": len(rows),
        "scope_filter": {k: v for k, v in (selector or {}).items() if v not in (None, "", False)},
    }


async def activate(tenant_id: str, campaign_id: str) -> dict[str, Any]:
    async with SessionLocal() as db:
        campaign = await _get(db, tenant_id, campaign_id)
        if campaign.status != DRAFT:
            raise CampaignError(f"a {campaign.status} campaign cannot be activated")
        unassigned = (
            await db.execute(
                select(func.count()).select_from(IamReviewItem).where(
                    IamReviewItem.campaign_id == campaign_id, IamReviewItem.reviewer_id == ""
                )
            )
        ).scalar_one()
        campaign.status = ACTIVE
        stats = dict(campaign.stats_json or {})
        stats["unassigned"] = int(unassigned)
        campaign.stats_json = stats
        await db.commit()
        await db.refresh(campaign)
        return _public(campaign)


async def decide(
    tenant_id: str,
    campaign_id: str,
    item_id: str,
    *,
    decision: str,
    reason: str = "",
    decided_by: str = "",
    delegated_to: str = "",
) -> dict[str, Any]:
    """Record one reviewer decision.

    A decision on an item flagged `changed_since_baseline` is refused: the reviewer is looking at
    a row that no longer describes reality, and accepting the decision would file a certification
    of something that is not true any more."""
    if decision not in DECISIONS:
        raise CampaignError(f"unknown decision {decision!r}")
    async with SessionLocal() as db:
        campaign = await _get(db, tenant_id, campaign_id)
        if campaign.status != ACTIVE:
            raise CampaignError(f"a {campaign.status} campaign cannot take decisions")
        item = (
            await db.execute(
                select(IamReviewItem).where(IamReviewItem.id == item_id, IamReviewItem.campaign_id == campaign_id)
            )
        ).scalar_one_or_none()
        if not item:
            raise CampaignError("no such review item")
        if item.changed_since_baseline and not reason:
            raise CampaignError(
                "this access changed since the campaign baseline and has been re-presented — "
                "review the new state and record a reason before deciding"
            )
        if decision == DELEGATE and not delegated_to:
            raise CampaignError("a delegation needs someone to delegate to")

        item.decision = decision
        item.decision_reason = reason
        item.decided_by = decided_by
        item.decided_at = datetime.now(timezone.utc)
        item.delegated_to = delegated_to
        if decision == DELEGATE:
            # A delegation is not a decision; it moves the item and clears it for the new owner.
            item.reviewer_id = delegated_to
            item.reviewer_source = "delegated"
            item.decision = None
        await db.flush()
        campaign.stats_json = await _recount(db, campaign)
        await db.commit()
        return {"ok": True, "stats": campaign.stats_json}


async def _recount(db: Any, campaign: IamReviewCampaign) -> dict[str, Any]:
    items = (
        await db.execute(select(IamReviewItem).where(IamReviewItem.campaign_id == campaign.id))
    ).scalars().all()
    stats = dict(campaign.stats_json or {})
    stats["total"] = len(items)
    stats["decided"] = sum(1 for i in items if i.decision)
    stats["by_decision"] = {d: sum(1 for i in items if i.decision == d) for d in DECISIONS}
    stats["changed_since_baseline"] = sum(1 for i in items if i.changed_since_baseline)
    stats["unassigned"] = sum(1 for i in items if not i.reviewer_id)
    return stats


async def refresh_against(tenant_id: str, campaign_id: str) -> dict[str, Any]:
    """Re-check every item against the CURRENT access snapshot.

    An item whose row is gone or whose row changed is flagged, and a flagged item that already
    carried a decision has that decision cleared. Keeping a decision that was made about a
    different grant is exactly the failure this flag exists to prevent."""
    current = {diff_mod.row_key(r): r for r in compose.build_master_rows(tenant_id)}
    async with SessionLocal() as db:
        campaign = await _get(db, tenant_id, campaign_id)
        items = (
            await db.execute(select(IamReviewItem).where(IamReviewItem.campaign_id == campaign_id))
        ).scalars().all()
        changed = 0
        for item in items:
            still_there = item.row_key in current
            was = item.changed_since_baseline
            item.changed_since_baseline = not still_there
            if item.changed_since_baseline and not was:
                changed += 1
                if item.decision:
                    item.decision = None
                    item.decision_reason = ""
                    item.decided_at = None
        campaign.stats_json = await _recount(db, campaign)
        await db.commit()
        return {"ok": True, "re_presented": changed, "stats": campaign.stats_json}


async def complete(tenant_id: str, campaign_id: str, *, expired: bool = False) -> dict[str, Any]:
    """Close a campaign. Undecided items stay undecided.

    `completeness` is published on the campaign because "completed" alone would imply everything
    was reviewed. A campaign that closed with 40% of its items untouched is a real and reportable
    outcome, and it is not the same artifact as one that finished."""
    async with SessionLocal() as db:
        campaign = await _get(db, tenant_id, campaign_id)
        if campaign.status not in (ACTIVE, DRAFT):
            raise CampaignError(f"a {campaign.status} campaign is already closed")
        stats = await _recount(db, campaign)
        total = stats.get("total", 0)
        decided = stats.get("decided", 0)
        stats["undecided"] = total - decided
        stats["complete"] = total > 0 and decided == total
        stats["completeness_pct"] = round(100 * decided / total) if total else None
        campaign.stats_json = stats
        campaign.status = EXPIRED if expired else COMPLETED
        campaign.completed_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(campaign)
        return _public(campaign)


async def expire_due(tenant_id: str, *, now: datetime | None = None) -> list[str]:
    """Close every active campaign past its due date, as incomplete. Nothing is auto-approved."""
    now = now or datetime.now(timezone.utc)
    closed: list[str] = []
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(IamReviewCampaign).where(
                    IamReviewCampaign.tenant_id == tenant_id, IamReviewCampaign.status == ACTIVE
                )
            )
        ).scalars().all()
        due = [c.id for c in rows if c.due_at and c.due_at <= now]
    for cid in due:
        await complete(tenant_id, cid, expired=True)
        closed.append(cid)
    return closed


# --------------------------------------------------------------------------- reads
async def _get(db: Any, tenant_id: str, campaign_id: str) -> IamReviewCampaign:
    campaign = (
        await db.execute(
            select(IamReviewCampaign).where(
                IamReviewCampaign.tenant_id == tenant_id, IamReviewCampaign.id == campaign_id
            )
        )
    ).scalar_one_or_none()
    if not campaign:
        raise CampaignError("no such campaign")
    return campaign


def _public(c: IamReviewCampaign) -> dict[str, Any]:
    return {
        "id": c.id,
        "name": c.name,
        "description": c.description,
        "selector": c.selector_json or {},
        "baseline_run_id": c.baseline_run_id,
        "reviewer_strategy": c.reviewer_strategy,
        "status": c.status,
        "due_at": c.due_at.isoformat() if c.due_at else "",
        "reminder_days": c.reminder_days or [],
        "stats": c.stats_json or {},
        "evidence_id": c.evidence_id,
        "created_by": c.created_by,
        "created_at": c.created_at.isoformat() if c.created_at else "",
        "completed_at": c.completed_at.isoformat() if c.completed_at else "",
        # Self-review is not certification. The label travels with the record so it cannot be
        # mistaken for one six months later.
        "attestation_only": c.reviewer_strategy == SELF,
    }


def _item_public(i: IamReviewItem) -> dict[str, Any]:
    row = i.row_snapshot_json or {}
    return {
        "id": i.id,
        "row_key": i.row_key,
        "principalId": row.get("effectivePrincipalId", ""),
        "principalName": row.get("effectivePrincipalName", "") or row.get("principalDisplayName", ""),
        "principalType": row.get("effectivePrincipalType", "") or row.get("principalType", ""),
        "roleName": row.get("roleName", ""),
        "scope": row.get("scope", ""),
        "scopeName": row.get("scopeDisplayName", ""),
        "surface": row.get("surface", ""),
        "privileged": bool(row.get("roleIsPrivileged")),
        "context": i.context_json or {},
        "reviewer_id": i.reviewer_id,
        "reviewer_source": i.reviewer_source,
        "decision": i.decision,
        "decision_reason": i.decision_reason,
        "decided_by": i.decided_by,
        "decided_at": i.decided_at.isoformat() if i.decided_at else "",
        "delegated_to": i.delegated_to,
        "changed_since_baseline": i.changed_since_baseline,
        "remediation_state": i.remediation_state,
    }


async def list_campaigns(tenant_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(IamReviewCampaign)
                .where(IamReviewCampaign.tenant_id == tenant_id)
                .order_by(desc(IamReviewCampaign.created_at))
                .limit(limit)
            )
        ).scalars().all()
        return [_public(c) for c in rows]


async def get_campaign(tenant_id: str, campaign_id: str) -> dict[str, Any] | None:
    async with SessionLocal() as db:
        c = (
            await db.execute(
                select(IamReviewCampaign).where(
                    IamReviewCampaign.tenant_id == tenant_id, IamReviewCampaign.id == campaign_id
                )
            )
        ).scalar_one_or_none()
        return _public(c) if c else None


async def list_items(
    tenant_id: str,
    campaign_id: str,
    *,
    reviewer_id: str = "",
    undecided_only: bool = False,
    limit: int = 500,
) -> list[dict[str, Any]]:
    async with SessionLocal() as db:
        q = select(IamReviewItem).where(
            IamReviewItem.tenant_id == tenant_id, IamReviewItem.campaign_id == campaign_id
        )
        if reviewer_id:
            q = q.where(IamReviewItem.reviewer_id == reviewer_id)
        if undecided_only:
            q = q.where(IamReviewItem.decision.is_(None))
        rows = (await db.execute(q.limit(limit))).scalars().all()
        return [_item_public(i) for i in rows]


async def decided_rows(tenant_id: str, campaign_id: str, decisions: tuple[str, ...] = (REVOKE, REDUCE)) -> list[dict[str, Any]]:
    """The access snapshots behind every actionable decision — the input to remediation."""
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(IamReviewItem).where(
                    IamReviewItem.tenant_id == tenant_id,
                    IamReviewItem.campaign_id == campaign_id,
                    IamReviewItem.decision.in_(decisions),
                )
            )
        ).scalars().all()
        return [{"item_id": i.id, "decision": i.decision, "row": i.row_snapshot_json or {}} for i in rows]


async def mark_remediation(tenant_id: str, campaign_id: str, item_ids: list[str], state: str) -> int:
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(IamReviewItem).where(
                    IamReviewItem.tenant_id == tenant_id,
                    IamReviewItem.campaign_id == campaign_id,
                    IamReviewItem.id.in_(item_ids),
                )
            )
        ).scalars().all()
        for i in rows:
            i.remediation_state = state
        await db.commit()
        return len(rows)


async def auto_confirm_applied(tenant_id: str, campaign_id: str) -> dict[str, Any]:
    """Verify asserted revocations against the current snapshot.

    This is what closes the loop without a write path: the reviewer says they applied it, and the
    NEXT scan proves it. An evidence pack that says "revoked (verified absent from the following
    scan)" is a different quality of artifact from one that says "revoked (someone ticked a box)"."""
    present = {diff_mod.row_key(r) for r in compose.build_master_rows(tenant_id)}
    confirmed = still_present = 0
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(IamReviewItem).where(
                    IamReviewItem.tenant_id == tenant_id,
                    IamReviewItem.campaign_id == campaign_id,
                    IamReviewItem.decision == REVOKE,
                )
            )
        ).scalars().all()
        for i in rows:
            if i.row_key not in present:
                if i.remediation_state != "confirmed_applied":
                    i.remediation_state = "confirmed_applied"
                    confirmed += 1
            elif i.remediation_state == "confirmed_applied":
                # Somebody marked it applied and the access is still there. Do NOT leave the
                # confirmation standing — that is the one state an auditor will rely on.
                i.remediation_state = "exported"
                still_present += 1
        await db.commit()
    return {"confirmed": confirmed, "reverted_claims": still_present}


# --------------------------------------------------------------------------- evidence
def evidence_content(
    campaign: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    run: dict[str, Any] | None = None,
    framework_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The immutable artifact an auditor is handed.

    Hashing is done by `app.evidence.registry`, which canonicalises before digesting; this only
    has to make sure the content is complete and self-describing."""
    stats = campaign.get("stats", {})
    return {
        "kind": "iam_access_review",
        "campaign": campaign,
        "baseline_run": run or {},
        "items": items,
        "framework_mapping": framework_map or {},
        "attestation_only": campaign.get("attestation_only", False),
        "completeness": {
            "total": stats.get("total", 0),
            "decided": stats.get("decided", 0),
            "undecided": stats.get("undecided", max(0, stats.get("total", 0) - stats.get("decided", 0))),
            "complete": stats.get("complete", False),
        },
        "statements": [
            "Undecided items were NOT approved. A campaign that closed with undecided items is "
            "recorded as incomplete and the count is above.",
            "This product never writes to Azure. Remediation artifacts referenced here were "
            "generated for a human to run; a 'confirmed_applied' state means the access was "
            "verified absent from a later scan, not that this product removed it.",
        ]
        + (
            ["This was a SELF-ATTESTATION campaign: principals reviewed their own access. "
             "Self-review is not independent certification."]
            if campaign.get("attestation_only")
            else []
        ),
    }


def content_digest(content: dict[str, Any]) -> str:
    """Local digest for callers that want to compare two exports without the evidence locker."""
    import json

    return hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
