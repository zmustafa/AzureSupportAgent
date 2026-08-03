"""Identity Investigate — resolve one principal, then converge what we already know about it.

Every other screen answers *"which principals match this condition?"*. This answers
*"everything about **this one** principal"*. It owns no collectors: each section reads a
source another module already fills, and the only thing this module adds is the join.

Three rules the rest of the feature depends on:

1. **An unresolvable principal is a result, not an error.** A deleted object whose role
   assignment survives in ARM, or a Lighthouse principal living in someone else's
   directory, is often exactly what the reader clicked to find out about. Raising 404
   would throw away the answer.
2. **Capabilities are decided once, here.** A group has no sign-ins, a managed identity
   has no MFA, a guest has no licences. The UI renders whatever ``capabilities`` says and
   never switches on ``kind`` — otherwise the component grows a bug per new kind.
3. **Unreadable is not empty.** Every section carries provenance saying where it came
   from, when, and whether it was truncated or unreadable. A section we could not read
   says so; it is never rendered as "nothing found", which is the opposite fact.
"""
from __future__ import annotations

from typing import Any

from app.iam import schema as iam_schema

# --------------------------------------------------------------------------- kinds
KIND_USER = "user"
KIND_GUEST = "guest"
KIND_GROUP = "group"
KIND_SP = "servicePrincipal"
KIND_MI = "managedIdentity"
KIND_PLATFORM = "platform"
KIND_UNKNOWN = "unknown"

ALL_KINDS = (KIND_USER, KIND_GUEST, KIND_GROUP, KIND_SP, KIND_MI, KIND_PLATFORM, KIND_UNKNOWN)

# Graph's own ``servicePrincipalType`` for a managed identity. Managed identities ARE
# servicePrincipal objects — there is no ``managedIdentity`` directory type — so this
# field, not a heuristic, is the reliable discriminator when the SP is in our snapshot.
SP_TYPE_MANAGED_IDENTITY = "ManagedIdentity"

# --------------------------------------------------------------------- resolution states
RESOLVED = "resolved"
DELETED = "deleted"
CROSS_TENANT = "cross_tenant"
UNREADABLE = "unreadable"
NOT_FOUND = "not_found"

# --------------------------------------------------------------------------- capabilities
CAP_ACCESS = "access"
CAP_FINDINGS = "findings"
CAP_TIMELINE = "timeline"
CAP_SIGNINS = "signins"
CAP_AUDIT = "audit"
CAP_AZURE_ACTIVITY = "azure_activity"
CAP_RISK = "risk"
CAP_MEMBERS = "members"
CAP_CREDENTIALS = "credentials"
CAP_OWNING_RESOURCE = "owning_resource"
CAP_REGISTRATION = "registration"

# What each kind can possibly have. Absence is explained in ``notes`` rather than being
# silently dropped, so "no sign-in section" reads as "groups do not sign in" and not as
# "this group has never signed in".
_CAPABILITIES_BY_KIND: dict[str, tuple[str, ...]] = {
    KIND_USER: (CAP_ACCESS, CAP_FINDINGS, CAP_TIMELINE, CAP_SIGNINS, CAP_AUDIT,
                CAP_AZURE_ACTIVITY, CAP_RISK, CAP_REGISTRATION),
    KIND_GUEST: (CAP_ACCESS, CAP_FINDINGS, CAP_TIMELINE, CAP_SIGNINS, CAP_AUDIT,
                 CAP_AZURE_ACTIVITY, CAP_RISK, CAP_REGISTRATION),
    KIND_GROUP: (CAP_ACCESS, CAP_FINDINGS, CAP_TIMELINE, CAP_MEMBERS),
    KIND_SP: (CAP_ACCESS, CAP_FINDINGS, CAP_TIMELINE, CAP_SIGNINS, CAP_AUDIT,
              CAP_AZURE_ACTIVITY, CAP_CREDENTIALS),
    KIND_MI: (CAP_ACCESS, CAP_FINDINGS, CAP_TIMELINE, CAP_SIGNINS, CAP_AUDIT,
              CAP_AZURE_ACTIVITY, CAP_CREDENTIALS, CAP_OWNING_RESOURCE),
    KIND_PLATFORM: (),
    KIND_UNKNOWN: (),
}

# A principal we cannot resolve still has structural facts: its assignments survive in ARM
# even when the object is gone, and that is precisely the audit finding.
_CAPABILITIES_UNRESOLVED = (CAP_ACCESS, CAP_FINDINGS, CAP_TIMELINE)

_ABSENCE_REASON = {
    CAP_SIGNINS: "Groups do not sign in — there is no sign-in history to read.",
    CAP_AUDIT: "Groups do not act; they are acted upon. Audit events name the actor, not the group.",
    CAP_AZURE_ACTIVITY: "Groups do not act, so they never appear as the actor on an Azure change.",
    CAP_RISK: "Identity Protection scores users, not workload identities or groups.",
    CAP_REGISTRATION: "Authentication-method registration applies to users only.",
    CAP_MEMBERS: "Only groups have members.",
    CAP_CREDENTIALS: "Only workload identities carry credentials.",
    CAP_OWNING_RESOURCE: "Only a managed identity is owned by an Azure resource.",
}


def capabilities_for(kind: str, resolution: str) -> tuple[list[str], list[str]]:
    """Return ``(capabilities, notes)`` for a principal.

    Notes explain, in the reader's language, why a section they might expect is absent."""
    if resolution in (DELETED, CROSS_TENANT, UNREADABLE):
        caps = list(_CAPABILITIES_UNRESOLVED)
        note = {
            DELETED: "This object no longer exists in the directory, so only the access it "
                     "left behind can be read. Assignments outlive the principal.",
            CROSS_TENANT: "This principal lives in another organisation's directory "
                          "(Azure Lighthouse). We can name it and show what it reaches here, "
                          "but nothing about the principal itself is readable from this tenant.",
            UNREADABLE: "The directory could not be read, so we cannot say what this principal "
                        "is. This is not the same as it not existing.",
        }[resolution]
        return caps, [note]

    caps = list(_CAPABILITIES_BY_KIND.get(kind, ()))
    notes: list[str] = []
    if kind in (KIND_GROUP, KIND_SP, KIND_MI):
        for cap, reason in _ABSENCE_REASON.items():
            if cap not in caps and cap in (CAP_SIGNINS, CAP_AUDIT, CAP_RISK, CAP_REGISTRATION):
                notes.append(reason)
    if kind == KIND_PLATFORM:
        notes.append("This is Azure acting on its own behalf, not a principal in your "
                     "directory. There is nothing to investigate.")
    return caps, notes


# --------------------------------------------------------------------------- provenance
def provenance(source: str, *, collected_at: str = "", truncated: bool = False,
               unreadable: bool = False, reason: str = "") -> dict[str, Any]:
    """Where a section came from, and whether to trust its emptiness.

    ``unreadable`` is the load-bearing field: a section we could not read and a section
    with genuinely nothing in it look identical once rendered, and they are opposite
    facts."""
    return {
        "source": source,
        "collected_at": collected_at,
        "truncated": bool(truncated),
        "unreadable": bool(unreadable),
        "reason": reason,
    }


def section(data: Any, prov: dict[str, Any]) -> dict[str, Any]:
    return {"data": data, "provenance": prov}


# --------------------------------------------------------------------------- resolution
def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _user_kind(rec: dict[str, Any]) -> str:
    return KIND_GUEST if _norm(rec.get("user_type")) == "guest" else KIND_USER


def _sp_kind(rec: dict[str, Any]) -> str:
    sp_type = str(rec.get("sp_type") or rec.get("service_principal_type") or "")
    return KIND_MI if sp_type == SP_TYPE_MANAGED_IDENTITY else KIND_SP


def _owning_resource(rec: dict[str, Any]) -> str:
    """A system-assigned managed identity's SP carries its resource id in alternativeNames.

    Only present on snapshots collected after that field was added to the select, so an
    empty answer here means "not captured", never "not owned"."""
    for name in rec.get("alternative_names") or []:
        text = str(name)
        if text.startswith("/subscriptions/"):
            return text
    return ""


def _principal_from_user(rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(rec.get("id") or ""),
        "kind": _user_kind(rec),
        "display_name": rec.get("display_name") or rec.get("upn") or "",
        "upn": rec.get("upn") or "",
        "app_id": "",
        "enabled": rec.get("enabled"),
        "resolution": RESOLVED,
        "managing_tenant": None,
        "sub_kind": {
            "user_type": rec.get("user_type") or "Member",
            "on_prem_synced": bool(rec.get("on_prem_synced")),
            "external_user_state": rec.get("external_user_state") or "",
            "department": rec.get("department") or "",
            "job_title": rec.get("job_title") or "",
            "manager_id": rec.get("manager_id") or "",
            "licence_count": rec.get("licence_count"),
            "mfa_registered": rec.get("mfa_registered"),
            "last_signin": rec.get("last_signin") or "",
            "signin_known": bool(rec.get("signin_known")),
        },
    }


def _principal_from_group(rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(rec.get("id") or ""),
        "kind": KIND_GROUP,
        "display_name": rec.get("display_name") or "",
        "upn": "",
        "app_id": "",
        "enabled": None,
        "resolution": RESOLVED,
        "managing_tenant": None,
        "sub_kind": {
            "group_type": "m365" if rec.get("unified") else "security",
            "dynamic": bool(rec.get("dynamic")),
            "role_assignable": bool(rec.get("is_assignable_to_role")),
            "on_prem_synced": bool(rec.get("on_prem_synced")),
            "membership_rule": rec.get("membership_rule") or "",
            "security_enabled": bool(rec.get("security_enabled")),
            "mail_enabled": bool(rec.get("mail_enabled")),
            "owner_ids": rec.get("owner_ids") or [],
            "owners_known": bool(rec.get("owners_known")),
        },
    }


def _principal_from_sp(rec: dict[str, Any]) -> dict[str, Any]:
    kind = _sp_kind(rec)
    creds = rec.get("credentials") or []
    return {
        "id": str(rec.get("object_id") or ""),
        "kind": kind,
        "display_name": rec.get("display_name") or "",
        "upn": "",
        "app_id": rec.get("app_id") or "",
        "enabled": rec.get("enabled"),
        "resolution": RESOLVED,
        "managing_tenant": None,
        "sub_kind": {
            "sp_type": rec.get("sp_type") or "",
            "first_party": bool(rec.get("is_first_party")),
            "external": bool(rec.get("is_external")),
            "app_owner_tenant_id": rec.get("app_owner_tenant_id") or "",
            "credential_count": len(creds),
            "owner_ids": rec.get("owner_ids") or [],
            "owners_known": bool(rec.get("owners_known")),
            "assigned_to_resource": _owning_resource(rec) if kind == KIND_MI else "",
            "disabled_by_microsoft": rec.get("disabled_by_microsoft") or "",
        },
    }


def resolve_in_snapshot(data: dict[str, Any], needle: str) -> dict[str, Any] | None:
    """Find a principal in the Entra snapshot by object id, UPN, mail or appId.

    Deliberately identifier-only: display names are ambiguous and belong to search."""
    q = _norm(needle)
    if not q:
        return None

    people = data.get("people") or {}
    for rec in people.get("users") or []:
        if q in {_norm(rec.get("id")), _norm(rec.get("upn")), _norm(rec.get("mail"))} - {""}:
            return _principal_from_user(rec)
    for rec in people.get("groups") or []:
        if q == _norm(rec.get("id")):
            return _principal_from_group(rec)
    for rec in (data.get("apps") or {}).get("service_principals") or []:
        if q in {_norm(rec.get("object_id")), _norm(rec.get("app_id"))} - {""}:
            return _principal_from_sp(rec)
    return None


def resolve_in_access_rows(rows: list[dict[str, Any]], needle: str) -> dict[str, Any] | None:
    """Fall back to the Azure access rows for a principal the directory does not hold.

    This is where deleted principals and Azure Lighthouse principals are found: the role
    assignment survives in ARM long after the object stops resolving, and a principal from
    a managing tenant never resolved here in the first place.

    ``principalExists`` is a STRING for a reason — ``false`` is an orphan, ``unknown``
    means we could not look, and collapsing them into a boolean would force one of them to
    be a lie."""
    q = _norm(needle)
    if not q:
        return None
    for row in rows:
        ids = {_norm(row.get("principalId")), _norm(row.get("effectivePrincipalId"))} - {""}
        if q not in ids:
            continue
        exists = str(row.get("principalExists") or iam_schema.EXISTS_UNKNOWN)
        managing_id = str(row.get("managingTenantId") or "")
        if managing_id:
            resolution = CROSS_TENANT
        elif exists == iam_schema.EXISTS_FALSE:
            resolution = DELETED
        else:
            resolution = UNREADABLE
        raw_kind = _norm(row.get("principalType"))
        kind = {
            "user": KIND_USER, "guest": KIND_GUEST, "group": KIND_GROUP,
            "serviceprincipal": KIND_SP, "managedidentity": KIND_MI,
        }.get(raw_kind, KIND_UNKNOWN)
        return {
            "id": str(row.get("principalId") or needle),
            "kind": kind,
            "display_name": row.get("principalDisplayName") or str(needle),
            "upn": row.get("principalUserPrincipalName") or "",
            "app_id": "",
            "enabled": None,
            "resolution": resolution,
            "managing_tenant": (
                {"id": managing_id, "name": row.get("managingTenantName") or ""}
                if managing_id else None
            ),
            "sub_kind": {"principal_exists": exists},
        }
    return None


def unresolved(needle: str) -> dict[str, Any]:
    """Nothing in the directory or the access rows knows this identifier."""
    return {
        "id": str(needle),
        "kind": KIND_UNKNOWN,
        "display_name": str(needle),
        "upn": "",
        "app_id": "",
        "enabled": None,
        "resolution": NOT_FOUND,
        "managing_tenant": None,
        "sub_kind": {},
    }


def envelope(principal: dict[str, Any]) -> dict[str, Any]:
    """The resolver's response: the principal plus what can be asked about it."""
    caps, notes = capabilities_for(principal.get("kind", KIND_UNKNOWN),
                                   principal.get("resolution", NOT_FOUND))
    if principal.get("resolution") == NOT_FOUND:
        caps, notes = [], [
            "No principal with that identifier exists in this tenant's directory or in any "
            "access assignment we hold. Check the tenant selector — an object id means "
            "nothing without the directory it belongs to."
        ]
    return {"principal": principal, "capabilities": caps, "notes": notes}


# --------------------------------------------------------------------------- search
def search(data: dict[str, Any], needle: str, limit: int = 25) -> list[dict[str, Any]]:
    """Type-ahead across users, groups and service principals by name or identifier."""
    q = _norm(needle)
    if len(q) < 2:
        return []
    hits: list[tuple[int, dict[str, Any]]] = []

    def consider(principal: dict[str, Any], *fields: Any) -> None:
        for field in fields:
            text = _norm(field)
            if not text or q not in text:
                continue
            # Exact beats prefix beats contains, so typing a full UPN puts it first.
            rank = 0 if text == q else (1 if text.startswith(q) else 2)
            hits.append((rank, principal))
            return

    people = data.get("people") or {}
    for rec in people.get("users") or []:
        consider(_principal_from_user(rec), rec.get("upn"), rec.get("display_name"),
                 rec.get("mail"), rec.get("id"))
    for rec in people.get("groups") or []:
        consider(_principal_from_group(rec), rec.get("display_name"), rec.get("id"))
    for rec in (data.get("apps") or {}).get("service_principals") or []:
        consider(_principal_from_sp(rec), rec.get("display_name"), rec.get("app_id"),
                 rec.get("object_id"))

    hits.sort(key=lambda pair: (pair[0], _norm(pair[1].get("display_name"))))
    return [
        {
            "id": p["id"], "kind": p["kind"], "display_name": p["display_name"],
            "upn": p.get("upn", ""), "app_id": p.get("app_id", ""), "enabled": p.get("enabled"),
        }
        for _rank, p in hits[:limit]
    ]


# --------------------------------------------------------------------------- composition
# The dossier lives here rather than in the route so the Evidence Locker can freeze exactly
# what the screen showed. Two implementations would drift, and an evidence pack that does
# not match the screen it was taken from is worse than no evidence pack.
log = __import__("logging").getLogger("app.entra.investigate")


async def access_rows(tenant_id: str) -> list[dict[str, Any]]:
    """The Azure access rows, off the event loop — it walks the whole master row set."""
    from app.iam import compose as iam_compose
    from app.iam import cpu as iam_cpu

    return await iam_cpu.run(iam_compose.build_master_rows, tenant_id, label="investigate rows")


async def resolve(data: dict[str, Any], tenant_id: str, needle: str) -> dict[str, Any]:
    """Directory first, then the access rows, then honestly nothing.

    The access-row fallback is what makes deleted and cross-tenant principals reachable:
    the assignment outlives the object, and a Lighthouse principal never lived here."""
    found = resolve_in_snapshot(data, needle)
    if found is not None:
        return found
    try:
        rows = await access_rows(tenant_id)
    except Exception as exc:  # noqa: BLE001 - a missing IAM cache must not fail the resolve
        log.info("investigate: access-row fallback unavailable: %s", exc)
        return unresolved(needle)
    return resolve_in_access_rows(rows, needle) or unresolved(needle)


async def build_dossier(
    snapshot: dict[str, Any], tenant_id: str, needle: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Everything already collected about one principal. Reads caches only.

    Returns ``(envelope, sections)``. Makes no Graph or ARM call — behavioural history is
    a separate, permissioned, explicitly-requested endpoint."""
    from app.entra import activations_ledger, model
    from app.entra.collectors.roles import effective_role_names
    from app.iam import diff as iam_diff
    from app.iam import store as iam_store

    data = snapshot.get("data") or {}
    analysis = snapshot.get("_analysis") or {}
    domains = snapshot.get("domains") or {}
    generated_at = str(snapshot.get("generated_at") or "")

    subject = await resolve(data, tenant_id, needle)
    env = envelope(subject)
    subject_id = str(subject.get("id") or needle)
    sections: dict[str, Any] = {}

    # --- access -----------------------------------------------------------------
    roles_data = data.get("roles") or {}
    link = data.get("_azure_link") or {}
    # Active and eligible are DIFFERENT facts and must never be flattened together.
    #
    # A PIM eligibility row carries `permanent: True` when the *eligibility* itself never
    # lapses. That says nothing about standing access — the holder still has to activate,
    # with whatever MFA, justification, approval and expiry the policy demands. Merged into
    # one list under a key named `permanent`, an eligible Global Administrator read as a
    # "permanent Global Administrator": the exact opposite of the truth, and the single
    # worst thing this dossier could say about a correctly-governed account. It also
    # invites the reader to "remove" an assignment that does not exist.
    #
    # So: every row is tagged with what it is, and the ambiguous key is renamed on eligible
    # rows rather than left to be misread.
    def _mine(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [r for r in rows if str(r.get("principal_id") or "") == subject_id]

    def _as_eligible(row: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in row.items() if k != "permanent"}
        out.update({
            "assignment_kind": "eligible",
            "activated": False,
            # The point of the whole record: eligibility is not held access.
            "standing_access": False,
            "eligibility_permanent": bool(row.get("permanent")),
        })
        return out

    active_rows = _mine((roles_data.get("assignments") or [])
                        + (roles_data.get("group_derived") or []))
    eligible_rows = [_as_eligible(e) for e in _mine(roles_data.get("eligible") or [])]
    directory_assignments = active_rows + eligible_rows
    active_role_names = {
        (a.get("role_name") or "").strip().lower() for a in active_rows
    } - {""}
    eligible_role_names = {
        (e.get("role_name") or "").strip().lower() for e in eligible_rows
    } - {""}
    azure_matches: list[dict[str, Any]] = []
    azure_unreadable = False
    azure_reason = ""
    try:
        rows = await access_rows(tenant_id)
        azure_matches = [
            r for r in rows
            if subject_id in {str(r.get("principalId") or ""), str(r.get("effectivePrincipalId") or "")}
        ]
    except Exception as exc:  # noqa: BLE001
        azure_unreadable = True
        azure_reason = f"The Azure access cache could not be read: {exc}"
    sections["access"] = section(
        {
            "directory_roles": sorted(effective_role_names(roles_data, subject_id)),
            # `directory_roles` is "privileged by ANY path", which is the right definition
            # for scoring but the wrong one for a reader deciding whether someone holds a
            # role right now. These two split it without changing that contract.
            "directory_roles_active": sorted(active_role_names),
            "directory_roles_eligible_only": sorted(eligible_role_names - active_role_names),
            "directory_assignments": directory_assignments,
            "azure": (link.get("principals") or {}).get(subject_id),
            "azure_assignments": azure_matches[:500],
            "azure_assignment_count": len(azure_matches),
        },
        provenance("Entra role collection + the Azure access cache", collected_at=generated_at,
                   unreadable=azure_unreadable, reason=azure_reason,
                   truncated=len(azure_matches) > 500),
    )

    # --- findings ---------------------------------------------------------------
    people_meta = domains.get("people") or {}
    blind = str(people_meta.get("status") or "") in (
        model.STATUS_BLIND, model.STATUS_NOT_COLLECTED)
    sections["findings"] = section(
        [f for f in analysis.get("findings") or [] if f.get("object_id") == subject_id],
        provenance("Entra signal registry", collected_at=generated_at, unreadable=blind,
                   reason=("The directory has not been collected for this tenant."
                           if blind else "")),
    )

    # --- timeline ---------------------------------------------------------------
    try:
        runs = await iam_store.list_runs(tenant_id, limit=30)
        events = iam_diff.timeline_for(subject_id, runs)
        timeline_prov = provenance(
            "IAM run diffs", collected_at=generated_at,
            reason=("Runs recorded before classified diffing was added contribute nothing here. "
                    "A gap means the history was not captured, not that nothing happened."))
    except Exception as exc:  # noqa: BLE001
        events, runs = [], []
        timeline_prov = provenance("IAM run diffs", unreadable=True,
                                   reason=f"Run history could not be read: {exc}")
    sections["timeline"] = section({"events": events, "runs_considered": len(runs)}, timeline_prov)

    # --- activations: the live window plus our own durable ledger ----------------
    live = [a for a in (data.get("pim") or {}).get("activations") or []
            if str(a.get("principal_id") or "") == subject_id]
    try:
        ledger = [a for a in activations_ledger.read(tenant_id)
                  if str(a.get("principal_id") or "") == subject_id]
    except Exception:  # noqa: BLE001
        ledger = []
    merged: dict[str, dict[str, Any]] = {}
    for row in [*ledger, *live]:
        merged[str(row.get("session_id") or row.get("id") or len(merged))] = row
    sections["activations"] = section(
        sorted(merged.values(), key=lambda a: str(a.get("start") or a.get("created_at") or ""),
               reverse=True)[:200],
        provenance("PIM activations + the durable activation ledger", collected_at=generated_at,
                   reason=("Microsoft Graph retains roughly 30 days. Older sessions come from "
                           "our own ledger, which is why the two can differ in length.")),
    )

    # --- members: groups only ----------------------------------------------------
    # Built from the IAM directory's expansion, which is TRANSITIVE and had nested groups
    # filtered out when it was collected. So this is the honest answer to "who ends up with
    # access through this group" and CANNOT answer "through which nested group" — the tree
    # for that is fetched live, on demand, by the members endpoint.
    if subject.get("kind") == KIND_GROUP:
        import asyncio

        from app.entra import investigate_members

        # OFF the event loop. It is a synchronous disk read plus a JSON parse of the whole
        # directory blob — ~10ms on a real tenant, which is nothing once and several seconds
        # of a blocked loop when a caller walks a few hundred dossiers. Every other expensive
        # read in this function is already offloaded; this one has to be too.
        members, known, reason = await asyncio.to_thread(
            investigate_members.cached_members, tenant_id, subject_id,
        )
        sub = subject.get("sub_kind") or {}
        sections["members"] = section(
            {
                "members": members,
                "count": len(members),
                "known": known,
                # Both change how the list should be read, and neither is visible from the
                # list itself: a dynamic group's membership is a rule's output, and a synced
                # group's membership is authored in on-prem AD where we cannot see it change.
                "dynamic": bool(sub.get("dynamic")),
                "membership_rule": str(sub.get("membership_rule") or ""),
                "on_prem_synced": bool(sub.get("on_prem_synced")),
                "role_assignable": bool(sub.get("role_assignable")),
            },
            provenance(
                "Azure access cache — transitive group expansion",
                collected_at=generated_at,
                unreadable=not known,
                reason=reason or (
                    "Transitive membership: nested groups were expanded away when this was "
                    "collected, so a member listed here may belong through a subgroup. Open "
                    "the tree to see the path."
                ),
            ),
        )

    return env, sections


# --------------------------------------------------------------------------- recents
# How many DISTINCT principals the "recently investigated" strip remembers. Deliberately a
# count and not a time window: an investigator returning after a fortnight wants the same
# short list they left, not an empty one.
RECENT_LIMIT = 25


def recent_entries(
    rows: list[dict[str, Any]], *, connection_id: str = "", limit: int = RECENT_LIMIT,
) -> list[dict[str, Any]]:
    """Fold raw ``investigate.view`` audit rows into a deduplicated recency list.

    Each row is ``{"target", "metadata", "at"}``, NEWEST FIRST. Viewing one principal five
    times is one entry carrying the latest timestamp — a strip that repeats the same person
    is a strip with room for nothing else.

    Scoped to one connection because a principal id from another tenant resolves to nothing:
    a chip that cannot be opened is worse than no chip.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        pid = str(row.get("target") or "")
        if not pid or pid in seen:
            continue
        meta = row.get("metadata") or {}
        if connection_id and str(meta.get("connection_id") or "") != connection_id:
            continue
        resolution = str(meta.get("resolution") or RESOLVED)
        # An identifier nothing ever recognised is junk to return to — a mistyped id, or a
        # path segment that reached the dossier route. DELETED and CROSS_TENANT are kept on
        # purpose: those resolved to a real answer, and are often the answer worth revisiting.
        if resolution == NOT_FOUND:
            continue
        seen.add(pid)
        out.append({
            "id": pid,
            "kind": str(meta.get("kind") or KIND_UNKNOWN),
            # The name is a FALLBACK for principals the directory no longer holds; the caller
            # prefers a live resolution so a rename is not frozen into the strip forever.
            "display_name": str(meta.get("name") or pid),
            "resolution": resolution,
            "at": str(row.get("at") or ""),
        })
        if len(out) >= limit:
            break
    return out


def refresh_recent_names(data: dict[str, Any], entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer the directory's current name over the one recorded at view time."""
    for entry in entries:
        live = resolve_in_snapshot(data, entry["id"])
        if live is not None:
            entry["display_name"] = live.get("display_name") or entry["display_name"]
            entry["kind"] = live.get("kind") or entry["kind"]
            entry["resolution"] = live.get("resolution") or entry["resolution"]
    return entries
