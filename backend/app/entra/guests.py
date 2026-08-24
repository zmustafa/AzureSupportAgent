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

2. **The guest's organization is NOT the UPN suffix.** A guest UPN looks like
   ``ada_contoso.com#EXT#@yourtenant.onmicrosoft.com`` — the suffix is always the HOST
   tenant, so keying on it reports every guest as belonging to your own company. The domain
   comes from ``mail``, falling back to the segment after the last underscore of the
   ``#EXT#`` prefix.

3. **Non-interactive sign-in is not evidence of a human.** ``lastNonInteractiveSignInDateTime``
   fires on token refresh, so a guest who left the partner months ago keeps looking "active"
   while a refresh token cycles. ``last_human_signin`` (interactive, and corroborated as
   successful) is the number that answers "is this person still working with us"; the combined
   one answers "is this identity still live". Both are reported, and they are not interchangeable.

4. **"Not measured" is never "not used".** When the sign-in pass did not run (no P1, or the
   permission is missing) the lifecycle is ``unknown`` — never ``dormant``. Telling somebody
   an account is unused when nobody looked is how real access gets revoked for the wrong
   reason.

5. **A refused sign-in is not a sign-in.** Both attempt stamps move whether or not the attempt
   worked, and Graph says so of each: ``lastSignInDateTime`` records an attempt "whether the
   attempt was successful or not", ``lastNonInteractiveSignInDateTime`` one made "either
   successfully or unsuccessfully". Every attempt against a DISABLED guest fails, so reading
   those stamps as usage makes a locked-out account look like this week's most active partner.
   Only ``lastSuccessfulSignInDateTime`` evidences access, and a refusal is reported as a
   refusal rather than quietly dropped.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable

from app.core.signin_activity import failed_signin, latest, refusal_provable

# --------------------------------------------------------------------------- lifecycle
STATE_DISABLED = "disabled"
STATE_PENDING = "pending"
STATE_NEVER_USED = "accepted_never_used"
STATE_ACTIVE = "active"
STATE_DORMANT = "dormant"
STATE_UNKNOWN = "unknown"

#: Ordered worst-to-best for display. ``unknown`` sits apart: it is an absence of
#: measurement, not a position on the lifecycle.
LIFECYCLE_ORDER = (STATE_DISABLED, STATE_PENDING, STATE_NEVER_USED, STATE_DORMANT,
                   STATE_ACTIVE, STATE_UNKNOWN)

LIFECYCLE_LABEL = {
    STATE_DISABLED: "Disabled",
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
#: partner organization when an engagement ends — there is no counterparty to ask. That makes
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
    """The guest's own organization, never the host tenant.

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


def refused_signin(user: dict[str, Any]) -> str:
    """The most recent sign-in attempt that demonstrably did NOT succeed, or "".

    Graph never reports a failure directly. It stamps the attempt, and separately the last
    success: an attempt stamped later than the last success cannot BE that success, so it was
    refused. This is the whole reason a disabled guest stops reading as active — every attempt
    against a disabled account fails, and the attempt stamp moves regardless.
    """
    attempt = latest(user.get("last_signin"), user.get("last_noninteractive_signin"))
    if not refusal_provable(attempt):
        return ""
    return failed_signin(attempt, str(user.get("last_successful_signin") or ""))


def last_human_signin(user: dict[str, Any]) -> str:
    """Last INTERACTIVE sign-in that we can show actually worked.

    Suppressed when that attempt is provably a refusal: crediting a rejected sign-in as
    "last seen" is what made a locked-out guest look like an active one."""
    attempt = str(user.get("last_signin") or "")
    if not attempt:
        return ""
    if refusal_provable(attempt) and failed_signin(
            attempt, str(user.get("last_successful_signin") or "")):
        return ""
    return attempt


def last_any_signin(user: dict[str, Any]) -> str:
    """Most recent access of any kind that actually happened, including token refresh.

    ``lastSuccessfulSignInDateTime`` covers interactive AND non-interactive success, so it is
    this answer whenever it exists. It only began in December 2023 and was not backfilled,
    and before that the attempt stamps are the best evidence there is — absence of a success
    that far back proves nothing, so it is not read as a failure.
    """
    success = str(user.get("last_successful_signin") or "")
    if success:
        return success
    attempt = latest(user.get("last_signin"), user.get("last_noninteractive_signin"))
    return "" if refusal_provable(attempt) else attempt


def never_used(user: dict[str, Any]) -> bool:
    """Accepted and measured, with no successful access ever recorded.

    Separate from ``lifecycle`` on purpose. The lifecycle answers "what do we show in the
    state column", where DISABLED outranks everything; this answers "was this access ever
    used", which stays true whether or not the account was later blocked — a disabled guest
    that never used its access still holds whatever it was granted.
    """
    if str(user.get("external_user_state") or "") == "PendingAcceptance":
        return False
    if not user.get("signin_known"):
        return False
    return not last_any_signin(user)


def lifecycle(user: dict[str, Any], *, now: datetime, stale_days: int) -> str:
    """Which of the six states this guest is in. Mutually exclusive by construction."""
    # First, because it outranks every other reading: a disabled account cannot sign in, so
    # grading it on sign-in would say "Active" about an identity nobody can use.
    if not user.get("enabled", True):
        return STATE_DISABLED
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
    refused = refused_signin(user)
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
        # Reported so an empty sign-in column reads as "refused" rather than "never tried".
        "last_refused_signin": refused,
        "last_refused_days_ago": days_since(refused, now) if refused else None,
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
    disabled = sum(1 for r in rows if r["lifecycle"] == STATE_DISABLED)
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
        # A disabled guest is out of the engagement funnel entirely — it cannot sign in, so
        # grading it active/dormant/never-used would be grading it on something it cannot do.
        "disabled": disabled,
        # Reported separately so it can never be mistaken for a lifecycle outcome.
        "not_measured": unknown,
    }


def by_domain(rows: Iterable[dict[str, Any]],
              tenants: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Per partner organization. This is the unit an enterprise actually decides on.

    Nobody revokes a supplier's guests one at a time; they end an engagement and want
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
    guest population is keyed by EMAIL DOMAIN, so nobody sees "N guests from this company
    and no policy naming them".

    Resolution needs no extra consent: ``findTenantInformationByDomainName`` was verified
    against v1.0 with the scopes this product already holds. So a domain that does NOT
    resolve is a fact about the domain — a consumer mailbox, or an organization with no
    Entra tenant behind it — and not a permission problem. Saying otherwise would send an
    operator to grant a scope that changes nothing.

    ``unknown`` is reserved for the case where the partner list itself could not be read.
    Reporting every partner as ungoverned because we could not look would be the loudest
    false claim this screen could make.
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
                "No Entra tenant is published for this domain, so no cross-tenant policy "
                "can name it. Consumer mailboxes and organizations without Entra land here.")
        elif tid in configured:
            d["governance"] = "governed"
            d["governance_reason"] = "A cross-tenant access policy names this partner tenant."
        else:
            d["governance"] = "default_only"
            d["governance_reason"] = (
                "No cross-tenant policy names this partner — it inherits the tenant default.")
    return domains


def summarize(people_data: dict[str, Any], *, now: datetime | None = None,
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
