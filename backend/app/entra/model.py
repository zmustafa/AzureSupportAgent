"""Shared shapes for Entra domain payloads, findings and domain metadata.

Payloads are plain dicts (JSON-native, so gzipped sidecars round-trip with no
serialisation layer), but every producer goes through the constructors here so the
envelope is identical across collectors and the UI can rely on it.

Domain status vocabulary — the whole "blind is not zero" contract depends on these:

===============  ==========================================================
``ok``           collected cleanly
``partial``      collected, but something was capped or a sub-call failed
``blind``        the identity lacks the permission (name it, don't hide it)
``unlicensed``   needs an Entra tier the tenant does not have
``error``        the collector failed; previous payload retained if any
``stale``        payload older than the TTL
``not_collected``the domain is not implemented / not requested yet
===============  ==========================================================
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Iterable

STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_BLIND = "blind"
STATUS_UNLICENSED = "unlicensed"
STATUS_ERROR = "error"
STATUS_STALE = "stale"
STATUS_NOT_COLLECTED = "not_collected"

# Statuses whose data may be used by signal evaluation.
USABLE_STATUSES = (STATUS_OK, STATUS_PARTIAL, STATUS_STALE)

SEVERITIES = ("critical", "high", "medium", "low", "info")
SEVERITY_RANK = {s: i for i, s in enumerate(reversed(SEVERITIES))}  # info=0 ... critical=4

OBJECT_KINDS = (
    "user", "group", "app", "sp", "role", "policy", "tenant",
    "package", "workflow", "review", "device", "credential",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ blockers
# What is stopping a domain from being fully measured.
#
# Free-text notes cannot be grouped, deduplicated or acted on. They produced a wall of prose
# in which one missing scope was reported three times across two domains, and in which "grant
# a permission", "assign an Azure role", "buy a licence" and "we stopped early to bound cost"
# all looked identical. A blocker names the KIND of obstacle, so the reader can tell what they
# can fix today from what costs money from what is simply inherent.
BLOCKER_CONSENT = "consent"        # a Microsoft Graph permission an admin can grant
BLOCKER_AZURE_ROLE = "azure_role"  # an Azure RBAC role — the other control plane
BLOCKER_LICENCE = "licence"        # a higher Entra tier; consent will never fix it
BLOCKER_CAP = "cap"                # we stopped early on purpose; a limit, not a gap
BLOCKER_KINDS = (BLOCKER_CONSENT, BLOCKER_AZURE_ROLE, BLOCKER_LICENCE, BLOCKER_CAP)


def blocker(
    kind: str,
    text: str,
    *,
    scope: str = "",
    subject: str = "",
    impact: str = "",
) -> dict[str, Any]:
    """One obstacle, in a shape the UI can group and dedupe.

    ``scope``   the thing to grant / assign / buy — also the dedupe key, so the same missing
                permission reported by two domains collapses into one row.
    ``subject`` what it applies to (a subscription, a report), when naming it is what makes
                the blocker actionable.
    ``impact``  what the reader loses while it stands.
    """
    if kind not in BLOCKER_KINDS:
        raise ValueError(f"unknown blocker kind: {kind}")
    return {"kind": kind, "text": text, "scope": scope, "subject": subject, "impact": impact}


# ------------------------------------------------------------------ domain payloads
def domain_payload(
    domain: str,
    data: dict[str, Any] | None = None,
    *,
    status: str = STATUS_OK,
    item_count: int = 0,
    duration_ms: int = 0,
    error: str = "",
    missing_permissions: Iterable[str] = (),
    truncated: bool = False,
    notes: Iterable[str] = (),
    blockers: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Build the standard envelope every collector returns."""
    return {
        "domain": domain,
        "status": status,
        "generated_at": now_iso(),
        "item_count": int(item_count),
        "duration_ms": int(duration_ms),
        "error": error,
        "missing_permissions": list(missing_permissions),
        "truncated": bool(truncated),
        "notes": list(notes),
        "blockers": list(blockers),
        "data": data or {},
    }


def blind_payload(domain: str, reason: str, missing: Iterable[str] = ()) -> dict[str, Any]:
    return domain_payload(domain, {}, status=STATUS_BLIND, error=reason, missing_permissions=missing)


def unlicensed_payload(domain: str, reason: str) -> dict[str, Any]:
    return domain_payload(domain, {}, status=STATUS_UNLICENSED, error=reason)


def error_payload(domain: str, reason: str) -> dict[str, Any]:
    return domain_payload(domain, {}, status=STATUS_ERROR, error=reason)


def not_collected_payload(domain: str, reason: str = "Not collected yet.") -> dict[str, Any]:
    return domain_payload(domain, {}, status=STATUS_NOT_COLLECTED, error=reason)


def domain_usable(meta: dict[str, Any] | None) -> bool:
    """True when a domain's data may be read by signal evaluation."""
    return bool(meta) and str(meta.get("status")) in USABLE_STATUSES


def domain_reason(meta: dict[str, Any] | None, domain: str) -> str:
    """Human explanation for why a domain could not be used."""
    if not meta:
        return f"{domain}: not loaded"
    status = meta.get("status")
    if status == STATUS_BLIND:
        missing = ", ".join(meta.get("missing_permissions") or []) or "a Graph permission"
        return f"{domain}: not permitted (missing {missing})"
    if status == STATUS_UNLICENSED:
        return f"{domain}: {meta.get('error') or 'requires a higher Entra licence tier'}"
    if status == STATUS_ERROR:
        return f"{domain}: collection failed ({(meta.get('error') or '')[:160]})"
    if status == STATUS_NOT_COLLECTED:
        return f"{domain}: not collected yet"
    return f"{domain}: unavailable"


# ------------------------------------------------------------------------ findings
def fingerprint(signal_id: str, object_id: str, discriminator: str = "") -> str:
    """Stable identity for a finding.

    MUST NOT include timestamps or counts — delta notifications, snoozing, ticket links and
    "new since last scan" all key off this value being identical run to run for the same
    underlying condition.
    """
    raw = f"{signal_id}|{object_id}|{discriminator}"
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:20]


def finding(
    *,
    signal_id: str,
    severity: str,
    pillar: str,
    object_kind: str,
    object_id: str,
    object_name: str,
    title: str,
    detail: str = "",
    evidence: dict[str, Any] | None = None,
    discriminator: str = "",
    portal_link: str = "",
) -> dict[str, Any]:
    """One finding. ``evidence`` is mandatory in spirit — it is what makes the score
    verifiable rather than a black box, so every spec should populate it."""
    return {
        "signal_id": signal_id,
        "severity": severity,
        "pillar": pillar,
        "object_kind": object_kind,
        "object_id": object_id,
        "object_name": object_name,
        "title": title,
        "detail": detail,
        "evidence": evidence or {},
        "portal_link": portal_link,
        "fingerprint": fingerprint(signal_id, object_id, discriminator),
    }


def sort_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Critical first, then by signal id and object name — deterministic ordering."""
    return sorted(
        findings,
        key=lambda f: (-SEVERITY_RANK.get(f.get("severity", "info"), 0), f.get("signal_id", ""), f.get("object_name", "")),
    )


def count_by_severity(findings: list[dict[str, Any]]) -> dict[str, int]:
    out = {s: 0 for s in SEVERITIES}
    for f in findings:
        sev = f.get("severity", "info")
        if sev in out:
            out[sev] += 1
    return out


# --------------------------------------------------------------------- portal links
_PORTAL = "https://entra.microsoft.com/#view/Microsoft_AAD_IAM"


def portal_user(object_id: str) -> str:
    return f"{_PORTAL}/UserDetailsMenuBlade/~/Profile/userId/{object_id}" if object_id else ""


def portal_group(object_id: str) -> str:
    return f"{_PORTAL}/GroupDetailsMenuBlade/~/Overview/groupId/{object_id}" if object_id else ""


def portal_app(app_id: str) -> str:
    return f"{_PORTAL}/RegisteredAppsMenuBlade/~/Overview/appId/{app_id}" if app_id else ""


def portal_sp(object_id: str) -> str:
    return (
        f"https://entra.microsoft.com/#view/Microsoft_AAD_IAM/ManagedAppMenuBlade/~/Overview/objectId/{object_id}"
        if object_id else ""
    )


def portal_ca_policy(policy_id: str) -> str:
    return (
        "https://entra.microsoft.com/#view/Microsoft_AAD_ConditionalAccess/PolicyBlade/policyId/"
        f"{policy_id}" if policy_id else
        "https://entra.microsoft.com/#view/Microsoft_AAD_ConditionalAccess/ConditionalAccessBlade"
    )


def portal_roles() -> str:
    return f"{_PORTAL}/RolesManagementMenuBlade/~/AllRoles"


def portal_consent() -> str:
    return f"{_PORTAL}/ConsentPoliciesMenuBlade/~/UserSettings"
