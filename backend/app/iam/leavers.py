"""Disabled principals that still hold access — the person-centric view.

Every other IAM screen is *grant*-centric: one row per assignment. That is the right shape for
"what exactly is granted", and the wrong shape for the question this module answers, which is
"**who** should not still be here". A leaver with Contributor on four subscriptions is one
offboarding task, not four findings, and the remediation is performed once against the person.

Three things this module refuses to do, each because the opposite has already shipped as a bug
somewhere in this codebase:

* It never reports a count when account state was not collected. ``measured=False`` is a wall,
  not an empty list — "0 disabled principals hold access" is the most reassuring sentence this
  feature can produce and it must never come from not having looked.
* It never claims a *percentage* of a population it could not fully measure, and it publishes
  the denominator (how many principals were checkable, and how many were not) alongside every
  number that depends on it.
* It does not invent a "currently exploitable" bucket it cannot populate. See :data:`TIERS`.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.iam import cache, compose, schema, usage

# Two tiers, not three.
#
# The obvious third tier — "tokens issued before the account was disabled are still valid" — is
# real (an access token lives up to about an hour, and refresh-token revocation is only
# immediate for resources that support Continuous Access Evaluation). It is deliberately NOT a
# bucket here, because populating it requires knowing WHEN each account was disabled, and Graph
# exposes no such property. An empty tier labelled "residual sessions" would read as "we checked
# and there are none", so it is published as a stated limitation instead of a fake zero.
TIER_LIVE = "live_now"
TIER_RESTORABLE = "restorable"

TIERS: dict[str, dict[str, str]] = {
    TIER_LIVE: {
        "key": TIER_LIVE,
        "label": "Live now",
        "detail": (
            "This account owns a service principal or application. That identity signs in with "
            "its own secret or certificate, which disabling the owner's account does nothing "
            "to — so this access is exercisable today, not merely restorable."
        ),
    },
    TIER_RESTORABLE: {
        "key": TIER_RESTORABLE,
        "label": "One re-enable away",
        "detail": (
            "The account cannot sign in, so these grants are dormant rather than live. Azure "
            "did not revoke any of them: re-enabling the account is a single helpdesk action, "
            "needs no approval and triggers no access review, and restores all of it at once."
        ),
    },
}

# Roles ranked for the "highest role held" column. Anything not listed ranks below these and is
# ordered by whether the product classified it as privileged, so a custom role that grants
# roleAssignments/write still sorts above Reader.
_ROLE_RANK: dict[str, int] = {
    "global administrator": 0,
    "company administrator": 0,
    "privileged role administrator": 1,
    "owner": 2,
    "user access administrator": 3,
    "role based access control administrator": 3,
    "co-administrator": 4,
    "service administrator": 4,
    "account administrator": 4,
    "application administrator": 5,
    "cloud application administrator": 5,
    "user administrator": 6,
    "contributor": 7,
    "service principal owner": 8,
}


def _rank(row: dict[str, Any]) -> tuple[int, int, str]:
    name = str(row.get("roleName") or "").strip().lower()
    return (_ROLE_RANK.get(name, 50), 0 if row.get("roleIsPrivileged") else 1, name)


def _identity_key(row: dict[str, Any]) -> str:
    return str(row.get("effectivePrincipalId") or row.get("principalId") or "").strip()


def _display(row: dict[str, Any]) -> str:
    return str(
        row.get("effectivePrincipalName")
        or row.get("principalDisplayName")
        or _identity_key(row)
        or "unknown principal"
    )


def _upn(row: dict[str, Any]) -> str:
    return str(
        row.get("effectivePrincipalUserPrincipalName")
        or row.get("principalUserPrincipalName")
        or ""
    )


def signin_enrichment(tenant_id: str) -> dict[str, Any]:
    """Sign-in history for this tenant's principals, from the Entra snapshot, if one exists.

    Strictly ENRICHMENT. The Entra identity scan is a separate feature with its own refresh
    cadence, so the snapshot may be absent, or months older than the access cache. It is never
    allowed to decide whether an account is disabled — that comes from the IAM directory layer —
    and every value it supplies travels with ``source`` and ``generated_at`` so a reader can see
    how old the fact is before acting on it.

    All FOUR kinds are carried, because they answer different questions and Microsoft populates
    them differently:

    * ``interactive`` — a human typed a password or approved an MFA prompt;
    * ``nonInteractive`` — a token was refreshed by a client on their behalf. An account can be
      dead interactively for months while a mail client quietly renews tokens daily;
    * ``successful`` — the last sign-in that actually SUCCEEDED. Present only on v1.0, and the
      only one of the three that cannot be satisfied by a stream of failures;
    * ``servicePrincipal`` — from ``/reports/servicePrincipalSignInActivities``, which only
      covers apps seen inside a bounded window (see ``window_days`` on the payload). Its absence
      therefore means "not seen in that window", **never** "never", and it is labelled that way
      everywhere it is shown. It is also keyed by **appId**, not by object id.
    """
    empty = {"available": False, "generated_at": "", "by_id": {}, "sp_by_id": {}, "sp_window_days": 0}
    try:
        from app.entra import cache as entra_cache

        payload = entra_cache.read_domain(tenant_id, "people")
    except Exception:  # pragma: no cover - the Entra feature is optional
        return empty
    if not isinstance(payload, dict):
        return empty
    data = payload.get("data")
    users = (data or payload).get("users") if isinstance(data or payload, dict) else None
    if not isinstance(users, list):
        return empty
    by_id: dict[str, dict[str, Any]] = {}
    known = False
    for u in users:
        if not isinstance(u, dict):
            continue
        uid = str(u.get("id") or "").lower()
        if not uid:
            continue
        if u.get("signin_known"):
            known = True
        by_id[uid] = {
            "interactive": str(u.get("last_signin") or ""),
            "nonInteractive": str(u.get("last_noninteractive_signin") or ""),
            "successful": str(u.get("last_successful_signin") or ""),
            "known": bool(u.get("signin_known")),
        }

    # Service principals live in a different domain payload and a different Graph report.
    sp_by_id: dict[str, str] = {}
    sp_window = 0
    try:
        apps = entra_cache.read_domain(tenant_id, "apps")
        adata = (apps or {}).get("data") if isinstance(apps, dict) else None
        activity = (adata or {}).get("signin_activity") if isinstance(adata, dict) else None
        if isinstance(activity, dict):
            sp_window = int(activity.get("window_days") or 0)
            for app_id, seen in (activity.get("last_seen") or {}).items():
                sp_by_id[str(app_id).lower()] = str(seen or "")
    except Exception:  # pragma: no cover - optional
        pass

    return {
        "available": bool(by_id) and known,
        "generated_at": str(payload.get("generated_at") or ""),
        "by_id": by_id,
        "sp_by_id": sp_by_id,
        "sp_window_days": sp_window,
    }


def usage_enrichment(tenant_id: str) -> dict[str, Any]:
    """When each principal was last seen DOING something, from the Activity Log sweep.

    A stronger signal than sign-in for this screen: it separates a leaver who was actively
    exercising Owner from one who was granted it years ago and never touched it.

    Read-only, and gated hard. The usage sweep is a separate job with its own cadence, so when
    it has not run every value here is absent and the UI must say "not measured" rather than
    "never used" — the two are opposite conclusions and only one of them justifies a removal."""
    payload = cache.read_usage(tenant_id) or {}
    measured = bool(payload.get("principals") or payload.get("event_count"))
    # A payload written BEFORE the `truncated` flag existed still records the truncation in its
    # notes. Defaulting a missing flag to False would silently upgrade every one of those older
    # sweeps to "complete", which is the reassuring reading — the same trap as defaulting an
    # unstamped account state to "enabled".
    notes = payload.get("notes") or []
    truncated = bool(
        payload.get("truncated")
        if "truncated" in payload
        else any(usage.TRUNCATION_MARKER in str(n).lower() for n in notes)
    )
    by_id: dict[str, dict[str, Any]] = {}
    for p in payload.get("principals") or []:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("principalId") or "").lower()
        if pid:
            by_id[pid] = {"lastSeen": str(p.get("lastSeen") or ""), "events": int(p.get("events") or 0)}
    return {
        "available": bool(measured),
        "window_days": int(payload.get("window_days") or 0),
        "start": str(payload.get("start") or ""),
        "end": str(payload.get("end") or ""),
        # A truncated sweep holds a PREFIX of the activity. Absence of an operation in it is not
        # evidence of disuse, and "never used" is an argument for deleting access.
        "truncated": truncated,
        "by_id": by_id,
    }


# Dormancy buckets. Deliberately coarse: the decision this screen supports is "remove it or
# not", and nobody makes that differently at 361 days than at 359.
DORMANCY_BUCKETS: tuple[tuple[str, str, int], ...] = (
    # key, label, minimum age in days (checked newest-first)
    ("recent", "Seen in the last 90 days", 0),
    ("over_90d", "90 days – 1 year", 90),
    ("over_1y", "1 – 2 years", 365),
    ("over_2y", "Over 2 years", 730),
)
DORMANCY_NEVER = "never"
DORMANCY_UNKNOWN = "unknown"

DORMANCY_LABELS: dict[str, str] = {
    **{k: label for k, label, _ in DORMANCY_BUCKETS},
    DORMANCY_NEVER: "No sign-in ever recorded",
    # NEVER folded into `never`. "We did not look" and "this account has never been used" are
    # opposite findings, and only one of them is an argument for deleting the access.
    DORMANCY_UNKNOWN: "Not measured",
}


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def dormancy_of(last_seen: str, *, known: bool, now: datetime) -> tuple[str, int | None]:
    """Bucket + age in days for a last-seen timestamp. ``(unknown, None)`` when unmeasured."""
    if not known:
        return DORMANCY_UNKNOWN, None
    if not last_seen:
        return DORMANCY_NEVER, None
    when = _parse_iso(last_seen)
    if when is None:
        return DORMANCY_UNKNOWN, None
    days = max(0, int((now - when).total_seconds() // 86400))
    bucket = "recent"
    for key, _label, minimum in DORMANCY_BUCKETS:
        if days >= minimum:
            bucket = key
    return bucket, days


def _resource_tree(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per distinct scope this principal reaches, keeping the structure.

    The rollup used to flatten every scope to ``scopeDisplayName or scope`` and hand the UI a
    list of strings, which it then truncated to six with a "+4 more" that was not a control.
    Everything needed to organise them — scope type, subscription, resource group, resource
    type — was on the row and thrown away, so the reader could neither group by resource type
    nor recover the full ARM id of the thing they were being asked to act on."""
    by_scope: dict[str, dict[str, Any]] = {}
    for r in rows:
        scope = str(r.get("scope") or "")
        entry = by_scope.get(scope)
        if entry is None:
            entry = {
                "scope": scope,
                "scopeType": str(r.get("scopeType") or ""),
                "scopeDisplayName": str(r.get("scopeDisplayName") or ""),
                "subscriptionId": str(r.get("subscriptionId") or ""),
                "subscriptionName": str(r.get("subscriptionName") or ""),
                "resourceGroup": str(r.get("resourceGroup") or ""),
                "resourceType": str(r.get("resourceType") or ""),
                "resourceName": str(r.get("resourceName") or ""),
                "managementGroupName": str(r.get("managementGroupName") or ""),
                "roles": [],
                "grants": 0,
                "privileged": 0,
                "viaGroups": [],
                "direct": False,
            }
            by_scope[scope] = entry
        entry["grants"] += 1
        if r.get("roleIsPrivileged"):
            entry["privileged"] += 1
        role = str(r.get("roleName") or "")
        if role and role not in entry["roles"]:
            entry["roles"].append(role)
        if r.get("accessPath") == schema.PATH_GROUP:
            grp = str(r.get("sourceGroupName") or r.get("sourceGroupId") or "")
            if grp and grp not in entry["viaGroups"]:
                entry["viaGroups"].append(grp)
        else:
            entry["direct"] = True
    out = list(by_scope.values())
    # Privileged first, then widest scope first, so the reader meets the worst thing first.
    order = {
        schema.SCOPE_TENANT: 0, schema.SCOPE_MANAGEMENT_GROUP: 1, schema.SCOPE_SUBSCRIPTION: 2,
        schema.SCOPE_RESOURCE_GROUP: 3, schema.SCOPE_RESOURCE: 4, schema.SCOPE_DIRECTORY: 5,
    }
    out.sort(key=lambda e: (not e["privileged"], order.get(e["scopeType"], 9), e["scope"]))
    return out


# One identity's grant list is capped so a principal in a very wide group cannot make a single
# API response enormous. The count is always published alongside, so a truncated list can never
# read as a complete one.
MAX_GRANT_DETAIL = 250


def _grant_detail(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The actual grants behind the aggregate numbers.

    Aggregates alone force the reader back to the Access grid to answer "which assignment,
    exactly?" — the question every one of these rows ends in."""
    out = [
        {
            "roleName": str(r.get("roleName") or ""),
            "roleIsPrivileged": bool(r.get("roleIsPrivileged")),
            "roleHasDataActions": bool(r.get("roleHasDataActions")),
            "surface": str(r.get("surface") or ""),
            "accessPath": str(r.get("accessPath") or ""),
            "sourceGroupName": str(r.get("sourceGroupName") or r.get("sourceGroupId") or ""),
            "scope": str(r.get("scope") or ""),
            "scopeType": str(r.get("scopeType") or ""),
            "scopeDisplayName": str(r.get("scopeDisplayName") or ""),
            "subscriptionName": str(r.get("subscriptionName") or r.get("subscriptionId") or ""),
            "resourceGroup": str(r.get("resourceGroup") or ""),
            "resourceType": str(r.get("resourceType") or ""),
            "resourceName": str(r.get("resourceName") or ""),
            "assignmentId": str(r.get("assignmentId") or ""),
            "roleDefinitionId": str(r.get("roleDefinitionId") or ""),
            "assignmentCreatedOn": str(r.get("assignmentCreatedOn") or ""),
            "assignmentState": str(r.get("assignmentState") or ""),
            "pimManaged": bool(r.get("pimManaged")),
            "isPermanentEligible": bool(r.get("isPermanentEligible")),
        }
        for r in rows
    ]
    out.sort(key=lambda g: (not g["roleIsPrivileged"], g["roleName"], g["scope"]))
    return out[:MAX_GRANT_DETAIL]



def escalation_enrichment(tenant_id: str) -> dict[str, Any]:
    """Which principals appear on a path to full control, read STRICTLY from cache.

    Never calls :func:`escalation.graph_for_tenant`. Building that graph takes about half a
    minute on a real tenant (measured: 1,216 nodes, 7,052 edges, 32 s), and putting it behind a
    row expansion would be the same inline-CPU defect this codebase has already fixed three
    times. The refresh job builds and persists it; this reads what is there or reports nothing.

    ``available`` is False when no graph has been persisted — in which case the absence of a
    flag means "not computed", not "no escalation path", and the UI must not draw the badge at
    all rather than drawing its absence."""
    stored = cache.read_escalation(tenant_id) or {}
    graph = stored.get("graph") or {}
    if not graph:
        return {"available": False, "by_id": {}}
    # A path is keyed by the principal it STARTS from — `from` — not by a node list. Counting
    # every node on the way would flag the intermediate service principals and key vaults as
    # though they were the ones who could escalate.
    on_path: dict[str, int] = {}
    for path in graph.get("paths") or []:
        key = str(path.get("from") or "").lower()
        if key:
            on_path[key] = on_path.get(key, 0) + 1
    return {"available": True, "by_id": on_path}


def build_leavers(tenant_id: str) -> dict[str, Any]:
    """The full disabled-access report for a tenant. Pure CPU over the composed cache."""
    rows = compose.build_master_rows(tenant_id)
    directory = cache.read_directory(tenant_id)
    measured = bool(directory.get("principal_state"))
    signin = signin_enrichment(tenant_id)
    usage = usage_enrichment(tenant_id)
    esc = escalation_enrichment(tenant_id)

    grants = [r for r in rows if r.get("effect") != schema.EFFECT_DENY]

    # ---- denominator, computed over every principal that holds access -------------------
    # Published whether or not anything was found, because "0 disabled" means nothing without
    # "…out of how many, and how many could we not check".
    state_by_principal: dict[str, str] = {}
    for r in grants:
        pid = _identity_key(r)
        if not pid:
            continue
        value = str(r.get("principalAccountEnabled") or schema.ENABLED_UNKNOWN)
        prev = state_by_principal.get(pid)
        # A principal appears on many rows. Prefer any decisive answer over `unknown`, and
        # prefer `false` over `true` — one row proving the account is disabled outweighs a stale
        # row that never got stamped.
        if prev is None or prev == schema.ENABLED_UNKNOWN or value == schema.ENABLED_FALSE:
            state_by_principal[pid] = value

    denominator = {
        "principals_with_access": len(state_by_principal),
        "state_resolved": sum(
            1 for v in state_by_principal.values()
            if v in (schema.ENABLED_TRUE, schema.ENABLED_FALSE)
        ),
        "state_unknown": sum(
            1 for v in state_by_principal.values() if v == schema.ENABLED_UNKNOWN
        ),
        "not_applicable": sum(
            1 for v in state_by_principal.values() if v == schema.ENABLED_NA
        ),
    }

    limitations: list[str] = [
        "Tokens issued before an account was disabled remain valid until they expire — up to "
        "about an hour for an access token, and longer for a refresh token unless the resource "
        "supports Continuous Access Evaluation. Microsoft Graph does not publish when an "
        "account was disabled, so this report cannot tell you which accounts are still inside "
        "that window.",
    ]
    if denominator["state_unknown"]:
        limitations.append(
            f"{denominator['state_unknown']} principal(s) holding access could not be checked "
            f"against the directory. They are absent from this report — not cleared by it."
        )
    if not signin["available"]:
        limitations.append(
            "Last sign-in dates come from the Entra identity scan, which has not run for this "
            "tenant. Sign-in columns are blank rather than zero, and the dormancy filter reports "
            "'not measured' rather than 'never signed in'."
        )
    if signin.get("sp_window_days"):
        limitations.append(
            f"Service-principal sign-in activity only covers apps seen in the last "
            f"{signin['sp_window_days']} day(s). An owned application with no sign-in shown was "
            f"not seen in that window — which is not the same as never being used."
        )
    if not usage["available"]:
        limitations.append(
            "Nobody's access has been checked against the Azure Activity Log for this tenant, so "
            "'last used' is blank. That is not evidence the access is unused."
        )
    else:
        limitations.append(
            f"'Last used' covers the last {usage['window_days']} day(s) of the Azure Activity "
            f"Log, which records management-plane writes well and reads poorly. Data-plane "
            f"activity (reading a blob, fetching a secret) is not in it at all."
        )
        limitations.append(
            "A disabled account cannot obtain a token, so it cannot appear in the Activity Log "
            "at all. 'No operations recorded' is expected for anyone disabled before this "
            "window opened, and means nothing about whether they ever used the access."
        )
        if usage["truncated"]:
            limitations.append(
                "The Activity Log query hit its 6 MB per-subscription cap and returned a partial "
                "result, so 'last used' is a lower bound and an absent operation is not evidence "
                "the access was unused. Re-run the usage sweep over a shorter window for a "
                "complete answer."
            )

    if not measured:
        return {
            "measured": False,
            "reason": (
                "Entra account state has not been collected for this tenant, so no principal "
                "can be called disabled. Refresh the directory layer with a connection that can "
                "read Microsoft Graph."
            ),
            "identities": [],
            "denominator": denominator,
            "tiers": TIERS,
            "tier_counts": {},
            "totals": {},
            "limitations": limitations,
            "dormancy_labels": DORMANCY_LABELS,
            "signin": {"available": False, "generated_at": ""},
            "usage": {"available": False, "window_days": 0, "truncated": False},
            "escalation": {"available": False},
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    # ---- roll the disabled rows up per person -------------------------------------------
    by_principal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in grants:
        if schema.is_disabled(r):
            pid = _identity_key(r)
            if pid:
                by_principal[pid].append(r)

    identities: list[dict[str, Any]] = []
    state_map = directory.get("principal_state") or {}
    now = datetime.now(timezone.utc)
    for pid, prows in by_principal.items():
        top = min(prows, key=_rank)
        owner_rows = [r for r in prows if r.get("accessPath") == schema.PATH_OWNER]
        group_rows = [r for r in prows if r.get("accessPath") == schema.PATH_GROUP]
        direct_rows = [r for r in prows if r.get("accessPath") == schema.PATH_DIRECT]
        eligible_rows = [r for r in prows if r.get("assignmentState") == schema.STATE_ELIGIBLE]
        ptype = str(prows[0].get("effectivePrincipalType") or prows[0].get("principalType") or "")
        # The recycle bin is a materially different state from "disabled": the account is gone
        # from the directory but restorable for 30 days, and restoring it restores every one of
        # these grants at once. Advising "delete the assignment, there is nobody left to lose
        # access" is simply untrue inside that window.
        deleted_at = str((state_map.get(pid.lower()) or {}).get("deletedDateTime") or "")

        # --- sign-in, all four kinds ---------------------------------------------------
        sign = dict(signin["by_id"].get(pid.lower()) or {})
        sp_seen = ""
        owned: list[dict[str, Any]] = []
        for r in owner_rows:
            app_id = str(r.get("principalAppId") or "").lower()
            seen = signin["sp_by_id"].get(app_id, "") if app_id else ""
            if seen > sp_seen:
                sp_seen = seen
            owned.append({
                "name": str(r.get("principalDisplayName") or r.get("principalId") or ""),
                "principalId": str(r.get("principalId") or ""),
                "appId": str(r.get("principalAppId") or ""),
                # An SP with no entry was simply not seen inside the report's window. Rendering
                # that as "never signed in" would argue for deleting a credential that may be
                # in daily use just outside the window.
                "lastSignIn": seen,
                "lastSignInKnown": bool(signin["sp_by_id"]),
            })
        signin_known = bool(sign.get("known")) if sign else False
        # The best evidence of life across the three user kinds. Non-interactive counts: an
        # account can be dead interactively for months while a client quietly refreshes tokens.
        best = max(
            [sign.get("interactive", ""), sign.get("nonInteractive", ""), sign.get("successful", "")],
            default="",
        )
        if ptype == "ServicePrincipal":
            best = sp_seen
            signin_known = bool(signin["sp_by_id"])
        bucket, age_days = dormancy_of(best, known=signin_known, now=now)

        usage_entry = usage["by_id"].get(pid.lower()) or {}
        # Does the usage window actually cover a period this account was ALIVE?
        #
        # A disabled account cannot obtain a token, so it cannot appear in the Activity Log at
        # all. "No operations in the last 90 days" is therefore exactly what you would expect
        # of somebody disabled two years ago, and concluding "they never used this access" from
        # it is unsound — it is a fact about the window, not about the person. The conclusion is
        # only available when the account was last seen INSIDE the window.
        window_start = usage["start"]
        covered = bool(usage["available"] and window_start and best and best >= window_start)
        created = sorted({str(r.get("assignmentCreatedOn") or "") for r in prows} - {""})

        identities.append(
            {
                "principalId": pid,
                "displayName": _display(prows[0]),
                "userPrincipalName": _upn(prows[0]),
                "principalType": ptype,
                "userType": str(prows[0].get("principalUserType") or ""),
                "accountEnabled": schema.ENABLED_FALSE,
                "softDeleted": bool(deleted_at),
                "deletedDateTime": deleted_at,
                "onPremSynced": str(prows[0].get("principalOnPremSynced") or schema.ENABLED_UNKNOWN),
                "tier": TIER_LIVE if owner_rows else TIER_RESTORABLE,
                "grants": len(prows),
                "privilegedGrants": sum(1 for r in prows if r.get("roleIsPrivileged")),
                "highestRole": str(top.get("roleName") or ""),
                "highestRoleIsPrivileged": bool(top.get("roleIsPrivileged")),
                "planes": sorted({str(r.get("surface") or "") for r in prows} - {""}),
                "directGrants": len(direct_rows),
                "groupGrants": len(group_rows),
                "groupsGrantingAccess": sorted(
                    {str(r.get("sourceGroupName") or r.get("sourceGroupId") or "") for r in group_rows} - {""}
                ),
                "ownedServicePrincipals": sorted({o["name"] for o in owned} - {""}),
                "ownedDetail": owned,
                "pimEligible": len(eligible_rows),
                "permanentlyEligible": sum(1 for r in eligible_rows if r.get("isPermanentEligible")),
                # Kept as flat strings for the CSV, which is a spreadsheet and not a tree.
                "scopes": sorted({str(r.get("scopeDisplayName") or r.get("scope") or "") for r in prows} - {""}),
                "subscriptions": sorted(
                    {str(r.get("subscriptionName") or r.get("subscriptionId") or "") for r in prows} - {""}
                ),
                # …and structured for the screen, which is not.
                "resources": _resource_tree(prows),
                "grantDetail": _grant_detail(prows),
                "grantDetailTruncated": len(prows) > MAX_GRANT_DETAIL,
                # --- sign-in / dormancy -----------------------------------------------
                "signIn": {
                    "interactive": sign.get("interactive", ""),
                    "nonInteractive": sign.get("nonInteractive", ""),
                    "successful": sign.get("successful", ""),
                    "servicePrincipal": sp_seen,
                    "known": signin_known,
                },
                "lastSignIn": best,
                "lastSignInSource": "Entra identity scan" if signin["available"] else "",
                "dormancyBucket": bucket,
                "dormancyDays": age_days,
                # --- did they ever USE it --------------------------------------------
                "lastActivity": str(usage_entry.get("lastSeen") or ""),
                "activityEvents": int(usage_entry.get("events") or 0),
                "activityMeasured": bool(usage["available"]),
                # The gate on any "never used" conclusion. False means the window cannot answer
                # the question for this account, which is NOT the same as answering "no".
                "activityConclusive": bool(covered and not usage["truncated"]),
                "activityWindowCovers": covered,
                # --- how old is this access ------------------------------------------
                "oldestGrantAt": created[0] if created else "",
                "newestGrantAt": created[-1] if created else "",
                # --- can this account reach further than its own grants? --------------
                # Read from the persisted escalation graph. `escalationMeasured` is the gate:
                # without it, a missing flag means "no graph has been built", which is not the
                # same as "this principal cannot escalate".
                "escalationPaths": esc["by_id"].get(pid.lower(), 0),
                "escalationMeasured": esc["available"],
            }
        )

    # Worst first: live-now before restorable, then most privilege, then most grants.
    identities.sort(
        key=lambda i: (
            0 if i["tier"] == TIER_LIVE else 1,
            -i["privilegedGrants"],
            -i["grants"],
            i["displayName"].lower(),
        )
    )

    disabled_rows = [r for group in by_principal.values() for r in group]
    tier_counts = {
        TIER_LIVE: sum(1 for i in identities if i["tier"] == TIER_LIVE),
        TIER_RESTORABLE: sum(1 for i in identities if i["tier"] == TIER_RESTORABLE),
    }
    return {
        "measured": True,
        "reason": "",
        "identities": identities,
        "denominator": denominator,
        "tiers": TIERS,
        "tier_counts": tier_counts,
        "totals": {
            "identities": len(identities),
            "grants": len(disabled_rows),
            "privileged_grants": sum(1 for r in disabled_rows if r.get("roleIsPrivileged")),
            "users": sum(1 for i in identities if i["principalType"] == "User"),
            "service_principals": sum(1 for i in identities if i["principalType"] == "ServicePrincipal"),
            "on_prem_synced": sum(1 for i in identities if i["onPremSynced"] == schema.ENABLED_TRUE),
            "via_group_only": sum(1 for i in identities if i["groupGrants"] and not i["directGrants"]),
            "pim_eligible": sum(1 for i in identities if i["pimEligible"]),
            "soft_deleted": sum(1 for i in identities if i["softDeleted"]),
            "cloud_only": sum(1 for i in identities if i["onPremSynced"] == schema.ENABLED_FALSE),
            "sync_unknown": sum(1 for i in identities if i["onPremSynced"] == schema.ENABLED_UNKNOWN),
            "never_used": sum(
                1 for i in identities
                if i["activityMeasured"] and i["activityConclusive"] and not i["lastActivity"]
            ),
            "subscriptions_touched": len(
                {s for i in identities for s in i["subscriptions"]}
            ),
        },
        "limitations": limitations,
        "dormancy_labels": DORMANCY_LABELS,
        "signin": {
            "available": signin["available"],
            "generated_at": signin["generated_at"],
            "sp_window_days": signin.get("sp_window_days", 0),
        },
        "usage": {
            "available": usage["available"],
            "window_days": usage["window_days"],
            "start": usage["start"],
            "end": usage["end"],
            "truncated": usage["truncated"],
        },
        "escalation": {"available": esc["available"]},
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def disabled_grant_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The grant-level projection: every access row held by a known-disabled principal.

    Kept as full schema rows so the grant export round-trips through the same CSV/scanner
    writers as the main access export, and so a remediation script can be generated from it
    without a second collection."""
    return [
        r for r in rows
        if r.get("effect") != schema.EFFECT_DENY and schema.is_disabled(r)
    ]


# --------------------------------------------------------------------------- filtering
# On-prem sync is a THREE-state fact, not a boolean. `unknown` matters because the remediation
# differs by directory: an account mastered in on-premises AD must be changed there or the next
# sync cycle reverts it, and quietly filing "we could not tell" under "cloud" sends an operator
# to the wrong console.
ON_PREM_ANY = ""
ON_PREM_CLOUD = "cloud"
ON_PREM_SYNCED = "onprem"
ON_PREM_UNKNOWN = "unknown"

# Which timestamp the dormancy filter is measured from. They answer different questions and
# Microsoft populates them differently — an account can be dead interactively for months while a
# mail client quietly refreshes tokens every day, and only `successful` cannot be satisfied by a
# stream of failures.
SIGNIN_KINDS = ("any", "interactive", "nonInteractive", "successful", "servicePrincipal")

#: Every filter this screen can apply. ONE list, because the report endpoint, the export
#: endpoint and the review-campaign selector must all understand exactly the same set — the
#: moment one of them understands fewer, the artifact it produces stops matching the screen.
#: That defect has already shipped twice here: once on the access export, and once on this
#: feature's own "Start a review" button, which covered 78 identities while the screen showed 3.
FILTER_KEYS: tuple[str, ...] = (
    "tier", "principal_type", "privileged_only", "on_prem_synced", "on_prem", "via_group_only",
    "soft_deleted", "has_owned_sp", "pim_eligible", "never_used", "dormancy", "signin_kind",
    "subscription", "role", "plane", "group", "search", "principal_ids",
)


def signin_at(identity: dict[str, Any], kind: str) -> tuple[str, bool]:
    """(timestamp, was-it-measured) for one sign-in kind."""
    sign = identity.get("signIn") or {}
    known = bool(sign.get("known"))
    if kind in ("", "any"):
        return str(identity.get("lastSignIn") or ""), known
    return str(sign.get(kind) or ""), known


def filter_identities(
    identities: list[dict[str, Any]], query: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Apply the disabled-access filter set to a rolled-up identity list.

    Lives HERE rather than in the API layer because three callers need it and only one of them
    is an HTTP endpoint: the report, the export, and the review-campaign selector. When it lived
    beside the endpoints, the campaign selector understood two of the sixteen filters and
    silently created a review 26 times larger than the screen it was launched from."""
    q = dict(query or {})
    out = list(identities)

    # An explicit id list is an ADDITIONAL constraint, not a replacement: it narrows whatever
    # the filters already selected, so a stale id cannot resurrect an identity the filters
    # (or a later scan) have excluded.
    ids = q.get("principal_ids")
    if ids:
        wanted = {str(x).lower() for x in ids}
        out = [i for i in out if str(i.get("principalId", "")).lower() in wanted]

    if q.get("tier"):
        out = [i for i in out if i.get("tier") == q["tier"]]
    if q.get("principal_type"):
        out = [i for i in out if i.get("principalType") == q["principal_type"]]
    if q.get("privileged_only"):
        out = [i for i in out if i.get("privilegedGrants")]

    on_prem = q.get("on_prem")
    if on_prem == ON_PREM_CLOUD:
        out = [i for i in out if i.get("onPremSynced") == schema.ENABLED_FALSE]
    elif on_prem == ON_PREM_SYNCED:
        out = [i for i in out if i.get("onPremSynced") == schema.ENABLED_TRUE]
    elif on_prem == ON_PREM_UNKNOWN:
        out = [i for i in out if i.get("onPremSynced") == schema.ENABLED_UNKNOWN]
    elif q.get("on_prem_synced"):  # legacy boolean; `on_prem` supersedes it
        out = [i for i in out if i.get("onPremSynced") == schema.ENABLED_TRUE]

    if q.get("via_group_only"):
        out = [i for i in out if i.get("groupGrants") and not i.get("directGrants")]
    if q.get("soft_deleted"):
        out = [i for i in out if i.get("softDeleted")]
    if q.get("has_owned_sp"):
        out = [i for i in out if i.get("ownedServicePrincipals")]
    if q.get("pim_eligible"):
        out = [i for i in out if i.get("pimEligible")]
    if q.get("never_used"):
        # Three gates, not one. "We did not look" is not evidence of disuse; a TRUNCATED sweep
        # holds only a prefix of the activity; and a window that ends before the account was
        # last alive cannot see anything it did. This filter is the one most likely to end in a
        # deletion, so it only matches identities where the answer is actually available.
        out = [
            i for i in out
            if i.get("activityMeasured")
            and i.get("activityConclusive")
            and not i.get("lastActivity")
        ]

    if q.get("dormancy"):
        now = datetime.now(timezone.utc)
        kind = q.get("signin_kind") if q.get("signin_kind") in SIGNIN_KINDS else "any"
        picked: list[dict[str, Any]] = []
        for i in out:
            stamp, known = signin_at(i, kind)
            if dormancy_of(stamp, known=known, now=now)[0] == q["dormancy"]:
                picked.append(i)
        out = picked

    if q.get("subscription"):
        out = [i for i in out if q["subscription"] in (i.get("subscriptions") or [])]
    if q.get("plane"):
        out = [i for i in out if q["plane"] in (i.get("planes") or [])]
    if q.get("group"):
        out = [i for i in out if q["group"] in (i.get("groupsGrantingAccess") or [])]
    if q.get("role"):
        rl = str(q["role"]).lower()
        out = [
            i for i in out
            if rl == str(i.get("highestRole", "")).lower()
            or any(rl == str(g.get("roleName", "")).lower() for g in i.get("grantDetail") or [])
        ]
    if q.get("search"):
        s = str(q["search"]).lower()
        out = [
            i for i in out
            if s in str(i.get("displayName", "")).lower()
            or s in str(i.get("userPrincipalName", "")).lower()
            or s in str(i.get("highestRole", "")).lower()
            or s in str(i.get("principalId", "")).lower()
            or any(s in str(g).lower() for g in i.get("groupsGrantingAccess") or [])
            or any(s in str(x).lower() for x in i.get("subscriptions") or [])
            or any(s in str(x).lower() for x in i.get("ownedServicePrincipals") or [])
        ]
    return out


def count_identities(identities: list[dict[str, Any]], signin_kind: str = "any") -> dict[str, Any]:
    """Count maps over the WHOLE filtered set, for the group headers.

    Published by the server on purpose. A header count derived from the loaded page shrinks as
    the reader scrolls, which is exactly the defect the Findings tab had to fix: the number
    beside a section has to describe the section, not the part of it that happens to be in the
    DOM."""
    from collections import Counter

    now = datetime.now(timezone.utc)
    kind = signin_kind if signin_kind in SIGNIN_KINDS else "any"
    dormancy: Counter[str] = Counter()
    for i in identities:
        stamp, known = signin_at(i, kind)
        dormancy[dormancy_of(stamp, known=known, now=now)[0]] += 1
    return {
        "tier": dict(Counter(str(i.get("tier") or "") for i in identities)),
        "principal_type": dict(Counter(str(i.get("principalType") or "") for i in identities)),
        "on_prem": dict(Counter(str(i.get("onPremSynced") or "") for i in identities)),
        "dormancy": dict(dormancy),
        "highest_role": dict(Counter(str(i.get("highestRole") or "") for i in identities)),
        "subscription": dict(
            Counter(s for i in identities for s in (i.get("subscriptions") or []))
        ),
        "plane": dict(Counter(p for i in identities for p in (i.get("planes") or []))),
        "group": dict(
            Counter(g for i in identities for g in (i.get("groupsGrantingAccess") or []))
        ),
    }


def selected_principal_ids(tenant_id: str, query: dict[str, Any] | None = None) -> set[str]:
    """Lower-cased ids of the disabled identities a filter set selects.

    The join between "what the screen shows" and "what a campaign or a remediation script covers".
    """
    report = build_leavers(tenant_id)
    if not report.get("measured"):
        return set()
    return {
        str(i.get("principalId", "")).lower()
        for i in filter_identities(report.get("identities") or [], query)
    }

