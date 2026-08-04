"""Guest (B2B) hygiene — derivation over the collected ``people`` snapshot.

Pure functions, no I/O, no Graph calls: everything here is computed from data the people
collector already gathers. That is deliberate — the collector is the only place allowed to
talk to Graph, and guest hygiene is a *reading* of the directory, not a new source of it.

Four things in here are load-bearing and were each got wrong at least once in the field:

1. **The invitation date is destroyed on acceptance.** ``externalUserStateChangeDateTime``
   means "invited at" while the guest is pending, and silently becomes "accepted at" the
   moment they accept. The original invite date is then gone from that field forever. So
   ``invited_at`` is ALWAYS ``createdDateTime`` (the user object is created when the
   invitation is sent) and ``accepted_at`` is only read from the state-change stamp when the
   state actually says ``Accepted``.

2. **The guest's organisation is NOT the UPN suffix.** A guest UPN looks like
   ``ada_contoso.com#EXT#@yourtenant.onmicrosoft.com`` — the suffix is always the HOST
   tenant, so keying on it reports every guest as belonging to your own company. The domain
   comes from ``mail``, falling back to the segment after the last underscore of the
   ``#EXT#`` prefix.

3. **Non-interactive sign-in is not evidence of a human.** ``lastNonInteractiveSignInDateTime``
   fires on token refresh, so a guest who left the partner months ago keeps looking "active"
   while a refresh token cycles. ``last_human_signin`` (interactive only) is the number that
   answers "is this person still working with us"; the combined one answers "is this identity
   still live". Both are reported, and they are not interchangeable.

4. **"Not measured" is never "not used".** When the sign-in pass did not run (no P1, or the
   permission is missing) the lifecycle is ``unknown`` — never ``dormant``. Telling somebody
   an account is unused when nobody looked is how real access gets revoked for the wrong
   reason.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable

# --------------------------------------------------------------------------- lifecycle
STATE_PENDING = "pending"
STATE_NEVER_USED = "accepted_never_used"
STATE_ACTIVE = "active"
STATE_DORMANT = "dormant"
STATE_UNKNOWN = "unknown"

#: Ordered worst-to-best for display. ``unknown`` sits apart: it is an absence of
#: measurement, not a position on the lifecycle.
LIFECYCLE_ORDER = (STATE_PENDING, STATE_NEVER_USED, STATE_DORMANT, STATE_ACTIVE, STATE_UNKNOWN)

LIFECYCLE_LABEL = {
    STATE_PENDING: "Invitation pending",
    STATE_NEVER_USED: "Accepted, never used",
    STATE_ACTIVE: "Active",
    STATE_DORMANT: "Dormant",
    STATE_UNKNOWN: "Not measured",
}

# --------------------------------------------------------------------------- domain classes
CLASS_CORPORATE = "corporate"
CLASS_CONSUMER = "consumer"
CLASS_GOVERNMENT = "government"
CLASS_EDUCATION = "education"
CLASS_UNRESOLVED = "unresolved"

DOMAIN_CLASS_LABEL = {
    CLASS_CORPORATE: "Corporate",
    CLASS_CONSUMER: "Consumer email",
    CLASS_GOVERNMENT: "Government",
    CLASS_EDUCATION: "Education",
    CLASS_UNRESOLVED: "Unresolved",
}

#: Free/consumer mailbox providers. A guest on one of these cannot be de-provisioned by any
#: partner organisation when an engagement ends — there is no counterparty to ask. That makes
#: them a different governance class from a corporate partner, not merely a different domain.
CONSUMER_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "outlook.co.uk", "hotmail.com",
    "hotmail.co.uk", "live.com", "live.co.uk", "msn.com", "yahoo.com", "yahoo.co.uk",
    "yahoo.co.in", "ymail.com", "aol.com", "icloud.com", "me.com", "mac.com",
    "protonmail.com", "proton.me", "pm.me", "gmx.com", "gmx.de", "mail.com",
    "mail.ru", "yandex.com", "yandex.ru", "zoho.com", "qq.com", "163.com", "126.com",
    "naver.com", "hanmail.net", "daum.net", "rediffmail.com", "fastmail.com",
    "tutanota.com", "hushmail.com", "inbox.com", "web.de", "libero.it", "orange.fr",
    "free.fr", "comcast.net", "verizon.net", "att.net", "sbcglobal.net", "cox.net",
    "btinternet.com", "sky.com", "virginmedia.com", "shaw.ca", "rogers.com",
    "bell.net", "telus.net", "bigpond.com", "optusnet.com.au",
})

#: Suffixes that identify a public-sector counterparty. Kept separate from corporate because
#: the review conversation is different: there is usually a contract or FOIA angle, and the
#: domain cannot be assumed to belong to a single manageable tenant.
_GOV_SUFFIXES = (".gov", ".mil", ".gov.uk", ".gc.ca", ".gov.au", ".govt.nz", ".gouv.fr",
                 ".gov.in", ".gov.za", ".gov.sg", ".europa.eu")
_EDU_SUFFIXES = (".edu", ".ac.uk", ".edu.au", ".ac.nz", ".edu.sg", ".ac.jp", ".edu.in")


# --------------------------------------------------------------------------- helpers
def _parse(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def days_since(ts: str, now: datetime) -> int | None:
    dt = _parse(ts)
    return None if dt is None else int((now - dt).total_seconds() // 86400)


def is_guest(user: dict[str, Any]) -> bool:
    return str(user.get("user_type") or "").strip().lower() == "guest"


def guests_of(people_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Every guest in the people domain, enabled or not.

    Disabled guests are INCLUDED. A disabled guest still carries group memberships and app
    assignments, and "disabled but still assigned" is one of the findings this feature
    exists to surface — filtering them out here would make it unreachable.
    """
    users = people_data.get("users") or []
    if isinstance(users, dict):
        users = list(users.values())
    return [u for u in users if isinstance(u, dict) and is_guest(u)]


def guest_domain(user: dict[str, Any]) -> str:
    """The guest's own organisation, never the host tenant.

    ``mail`` first because it is the address the invitation was actually sent to. The
    ``#EXT#`` prefix is the fallback: Entra rewrites ``ada@contoso.com`` as
    ``ada_contoso.com#EXT#@host.onmicrosoft.com``, so the domain is the tail after the last
    underscore — NOT the UPN suffix, which is always the host tenant.
    """
    mail = str(user.get("mail") or "").strip().lower()
    if "@" in mail:
        dom = mail.rsplit("@", 1)[-1].strip()
        if dom:
            return dom
    upn = str(user.get("upn") or "").strip().lower()
    if "#ext#" in upn:
        local = upn.split("#ext#", 1)[0]
        if "_" in local:
            dom = local.rsplit("_", 1)[-1].strip()
            if "." in dom:
                return dom
    # A plain @-address in the UPN (rare for guests, but possible for external members).
    if "@" in upn:
        dom = upn.rsplit("@", 1)[-1].strip()
        # Guard: never report the host tenant's own onmicrosoft domain as a partner org.
        if dom and not dom.endswith(".onmicrosoft.com"):
            return dom
    return ""


def classify_domain(domain: str) -> str:
    d = (domain or "").strip().lower()
    if not d:
        return CLASS_UNRESOLVED
    if d in CONSUMER_DOMAINS:
        return CLASS_CONSUMER
    if any(d == s.lstrip(".") or d.endswith(s) for s in _GOV_SUFFIXES):
        return CLASS_GOVERNMENT
    if any(d == s.lstrip(".") or d.endswith(s) for s in _EDU_SUFFIXES):
        return CLASS_EDUCATION
    return CLASS_CORPORATE


def invited_at(user: dict[str, Any]) -> str:
    """When the invitation was sent.

    ALWAYS ``createdDateTime``. See the module docstring: the state-change stamp is
    overwritten with the acceptance time and cannot answer this question for anyone who
    accepted.
    """
    return str(user.get("created_at") or "")


def accepted_at(user: dict[str, Any]) -> str:
    """When the guest accepted, or "" if they have not.

    Only meaningful once the state says ``Accepted``; while pending, the same field holds
    the invitation time and returning it here would report an acceptance that never happened.
    """
    if str(user.get("external_user_state") or "") != "Accepted":
        return ""
    return str(user.get("external_state_changed_at") or "")


def last_human_signin(user: dict[str, Any]) -> str:
    """Last INTERACTIVE sign-in — the only one that evidences a person."""
    return str(user.get("last_signin") or "")


def last_any_signin(user: dict[str, Any]) -> str:
    """Most recent activity of any kind, including token refresh.

    Useful for "is this identity still live", useless for "is this person still engaged".
    """
    stamps = [s for s in (user.get("last_signin"), user.get("last_noninteractive_signin"),
                          user.get("last_successful_signin")) if s]
    return max((str(s) for s in stamps), default="")


def lifecycle(user: dict[str, Any], *, now: datetime, stale_days: int) -> str:
    """Which of the five states this guest is in. Mutually exclusive by construction."""
    state = str(user.get("external_user_state") or "")
    if state == "PendingAcceptance":
        return STATE_PENDING
    # Anything not measured stops here. A guest whose sign-in was never collected must not be
    # graded on sign-in.
    if not user.get("signin_known"):
        return STATE_UNKNOWN
    newest = last_any_signin(user)
    if not newest:
        # Accepted (or state blank but measured) and never used the access at all.
        return STATE_NEVER_USED
    age = days_since(newest, now)
    if age is None:
        return STATE_UNKNOWN
    return STATE_DORMANT if age >= stale_days else STATE_ACTIVE


# --------------------------------------------------------------------------- projection
def project(user: dict[str, Any], *, now: datetime, stale_days: int) -> dict[str, Any]:
    """One guest, flattened into exactly what the grid, the rollup and the export all read.

    Computed once here so the screen, the Excel sheet and the signals cannot drift into
    three different answers to "is this guest dormant".
    """
    dom = guest_domain(user)
    inv = invited_at(user)
    acc = accepted_at(user)
    human = last_human_signin(user)
    any_signin = last_any_signin(user)
    return {
        "id": str(user.get("id") or ""),
        "display_name": str(user.get("display_name") or ""),
        "upn": str(user.get("upn") or ""),
        "mail": str(user.get("mail") or ""),
        "domain": dom,
        "domain_class": classify_domain(dom),
        "enabled": bool(user.get("enabled")),
        "external_user_state": str(user.get("external_user_state") or ""),
        "creation_type": str(user.get("creation_type") or ""),
        "lifecycle": lifecycle(user, now=now, stale_days=stale_days),
        "invited_at": inv,
        "invited_days_ago": days_since(inv, now),
        "accepted_at": acc,
        "accepted_days_ago": days_since(acc, now) if acc else None,
        "last_human_signin": human,
        "last_human_days_ago": days_since(human, now) if human else None,
        "last_any_signin": any_signin,
        "last_any_days_ago": days_since(any_signin, now) if any_signin else None,
        "signin_known": bool(user.get("signin_known")),
        "company_name": str(user.get("company_name") or ""),
        "department": str(user.get("department") or ""),
        "job_title": str(user.get("job_title") or ""),
        "sponsors": list(user.get("sponsors") or []),
        "licence_count": int(user.get("licence_count") or 0),
    }


def project_all(people_data: dict[str, Any], *, now: datetime | None = None,
                stale_days: int = 90) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    return [project(u, now=now, stale_days=stale_days) for u in guests_of(people_data)]


# --------------------------------------------------------------------------- rollups
def funnel(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Invited -> accepted -> used -> still active, with the leak at each step.

    Percentages are deliberately absent: the denominators differ per step and a single
    "conversion %" invites the reader to quote one number that means four different things.
    """
    rows = list(rows)
    invited = len(rows)
    pending = sum(1 for r in rows if r["lifecycle"] == STATE_PENDING)
    unknown = sum(1 for r in rows if r["lifecycle"] == STATE_UNKNOWN)
    accepted = invited - pending
    never_used = sum(1 for r in rows if r["lifecycle"] == STATE_NEVER_USED)
    dormant = sum(1 for r in rows if r["lifecycle"] == STATE_DORMANT)
    active = sum(1 for r in rows if r["lifecycle"] == STATE_ACTIVE)
    return {
        "invited": invited,
        "pending": pending,
        "accepted": accepted,
        "never_used": never_used,
        "dormant": dormant,
        "active": active,
        # Reported separately so it can never be mistaken for a lifecycle outcome.
        "not_measured": unknown,
    }


def by_domain(rows: Iterable[dict[str, Any]],
              tenants: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Per partner organisation. This is the unit an enterprise actually decides on.

    Nobody revokes 87 guests one at a time; they end an engagement with a supplier and want
    every identity that came with it. Sorted by guest count so the biggest exposure leads.
    """
    tenants = tenants or {}
    buckets: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        buckets.setdefault(r["domain"], []).append(r)
    out: list[dict[str, Any]] = []
    for dom, items in buckets.items():
        states = Counter(i["lifecycle"] for i in items)
        invited_ages = [i["invited_days_ago"] for i in items if i["invited_days_ago"] is not None]
        info = tenants.get(dom) or {}
        out.append({
            "domain": dom,
            "domain_class": classify_domain(dom),
            # The partner's real name where Graph could resolve it — "Fabrikam" reads better
            # than "fabrikam.com" in a review conversation.
            "partner_tenant_id": str(info.get("tenant_id") or ""),
            "partner_name": str(info.get("display_name") or ""),
            "guests": len(items),
            "enabled": sum(1 for i in items if i["enabled"]),
            "disabled": sum(1 for i in items if not i["enabled"]),
            "pending": states.get(STATE_PENDING, 0),
            "never_used": states.get(STATE_NEVER_USED, 0),
            "dormant": states.get(STATE_DORMANT, 0),
            "active": states.get(STATE_ACTIVE, 0),
            "not_measured": states.get(STATE_UNKNOWN, 0),
            "oldest_invite_days": max(invited_ages) if invited_ages else None,
            "newest_invite_days": min(invited_ages) if invited_ages else None,
        })
    out.sort(key=lambda d: (-d["guests"], d["domain"]))
    return out


def annotate_partners(domains: list[dict[str, Any]],
                      cross_tenant: dict[str, Any]) -> list[dict[str, Any]]:
    """Mark each guest domain with whether a cross-tenant access policy governs it.

    This is the join no Entra blade offers: the partner list is keyed by TENANT ID, and the
    guest population is keyed by EMAIL DOMAIN, so nobody sees "87 guests from this company
    and no policy naming them".

    Resolving domain -> tenant id needs ``CrossTenantInformation.ReadBasic.All``, which is
    tier-3 and may not be consented. Until it is, the honest answer is ``unknown`` for every
    row, NOT ``ungoverned`` — telling an operator that 410 partners are ungoverned when we
    simply could not look would be the loudest false claim this screen could make.
    """
    known = bool(cross_tenant.get("known"))
    configured = {
        str(p.get("tenant_id") or "")
        for p in (cross_tenant.get("partners") or [])
        if p.get("b2b_inbound_configured")
    }
    for d in domains:
        tid = str(d.get("partner_tenant_id") or "")
        if not known:
            d["governance"] = "unknown"
            d["governance_reason"] = "The cross-tenant partner list could not be read."
        elif not tid:
            d["governance"] = "unknown"
            d["governance_reason"] = (
                "This domain has not been resolved to a partner tenant "
                "(needs CrossTenantInformation.ReadBasic.All).")
        elif tid in configured:
            d["governance"] = "governed"
            d["governance_reason"] = "A cross-tenant access policy names this partner tenant."
        else:
            d["governance"] = "default_only"
            d["governance_reason"] = (
                "No cross-tenant policy names this partner — it inherits the tenant default.")
    return domains


def summarise(people_data: dict[str, Any], *, now: datetime | None = None,
              stale_days: int = 90) -> dict[str, Any]:
    """Everything the Guests screen renders, computed once."""
    now = now or datetime.now(timezone.utc)
    rows = project_all(people_data, now=now, stale_days=stale_days)
    domains = by_domain(rows, people_data.get("guest_domain_tenants") or {})
    classes = Counter(r["domain_class"] for r in rows)
    return {
        "generated_at": now.isoformat(),
        "stale_days": stale_days,
        "counts": funnel(rows),
        "by_class": {k: classes.get(k, 0) for k in
                     (CLASS_CORPORATE, CLASS_CONSUMER, CLASS_GOVERNMENT,
                      CLASS_EDUCATION, CLASS_UNRESOLVED)},
        "domain_count": len(domains),
        "domains": domains,
        "guests": rows,
        # The share of the enabled directory that is external. Reported with BOTH numbers,
        # never as a bare percentage.
        "signin_measured": sum(1 for r in rows if r["signin_known"]),
    }
