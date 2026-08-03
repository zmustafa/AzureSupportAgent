"""Conditional Access analysis engine — pure, deterministic, golden-file testable.

The portal shows Conditional Access one policy at a time. Every question that actually
matters is a *join across policies*: who is protected by nothing, which exclusion defeats
which control, which policy is shadowed, and whether an emergency account is about to be
locked out. That join is this module.

Everything here is a pure function over an already-collected snapshot — no Graph, no disk,
no clock except the ``now`` passed in. That is what makes a coverage matrix golden-file
testable, and a coverage matrix that cannot be regression-tested cannot be trusted.

Resolution rules (each one is a place other tools get it wrong):

* **Exclusions always win.** ``effective = resolved_include - resolved_exclude`` at every level.
* **``All`` includes guests**, unless a guest-type filter narrows it — most gaps are guest gaps.
* **Roles expand through eligibility.** A policy scoped to "Global Administrator" covers
  *eligible* holders too, which is not visible in the portal.
* **``includeRoles`` carries roleTemplateId**, not roleDefinitionId. Mapping the wrong one
  silently produces an empty role set and a policy that looks like it protects nobody.
* **Nested groups expand transitively** (the collector already resolved transitive members).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Iterable

from app.entra.collectors.ca import (
    APP_ADMIN_PORTALS,
    APP_ALL,
    APP_AZURE_MANAGEMENT,
    APP_OFFICE365,
    LEGACY_CLIENT_APPS,
    STATE_DISABLED,
    STATE_ENABLED,
    STATE_REPORT_ONLY,
)
from app.entra import ca_coverage, ca_taxonomy

# ------------------------------------------------------------------ control vocabulary
CTRL_MFA = "mfa"
CTRL_PHISH = "phishing_resistant"
CTRL_COMPLIANT = "compliant_device"
CTRL_HYBRID = "hybrid_joined"
CTRL_BLOCK = "block"
CTRL_SESSION = "session_limits"
CTRL_APPROVED_APP = "approved_app"

# Sub-controls split out of the former single "Session limits" column, plus the legacy-auth
# column. Every one of these was ALREADY collected and then discarded at this line — a policy
# that required app-enforced restrictions or CAE was rendered identically to one that set a
# sign-in frequency, which made the DLP-relevant session gap on collaboration content
# invisible. Splitting them is what lets `no_session_control_on_content` exist.
CTRL_APP_PROTECTION = "app_protection_policy"
CTRL_COMPLIANT_OR_HYBRID = "compliant_or_hybrid_device"
CTRL_TERMS = "terms_of_use"
CTRL_AUTH_STRENGTH = "auth_strength"
CTRL_SIGNIN_FREQUENCY = "sign_in_frequency"
CTRL_PERSISTENT_BROWSER = "persistent_browser"
CTRL_APP_ENFORCED = "app_enforced_restrictions"
CTRL_CASB = "cloud_app_security_proxy"
CTRL_CAE = "continuous_access_evaluation"
CTRL_LEGACY_BLOCKED = "legacy_auth_blocked"

# Every control that governs the SESSION rather than the sign-in. A grant control decides
# whether you get in; these decide what the session may do once you are. Nothing here can
# ever be a reason someone is refused access, which is why the simulator must subtract the
# whole set before computing what a principal has to satisfy — see `required_controls`.
SESSION_CONTROLS: frozenset[str] = frozenset({
    CTRL_SESSION, CTRL_SIGNIN_FREQUENCY, CTRL_PERSISTENT_BROWSER,
    CTRL_APP_ENFORCED, CTRL_CASB, CTRL_CAE,
})

# The subset that bounds what leaves the session, as opposed to how long it lasts. "Can they
# download the file" is answered by these two and by nothing else: a sign-in frequency of one
# hour does not stop a single download in minute one.
EGRESS_CONTROLS: frozenset[str] = frozenset({CTRL_APP_ENFORCED, CTRL_CASB})

CONTROLS: list[dict[str, str]] = [
    {"key": CTRL_MFA, "label": "MFA"},
    {"key": CTRL_AUTH_STRENGTH, "label": "Auth strength"},
    {"key": CTRL_PHISH, "label": "Phishing-resistant"},
    {"key": CTRL_COMPLIANT_OR_HYBRID, "label": "Compliant/hybrid device"},
    {"key": CTRL_APPROVED_APP, "label": "Approved client app"},
    {"key": CTRL_APP_PROTECTION, "label": "App protection policy"},
    {"key": CTRL_BLOCK, "label": "Block"},
    {"key": CTRL_TERMS, "label": "Terms of use"},
    {"key": CTRL_SIGNIN_FREQUENCY, "label": "Sign-in frequency"},
    {"key": CTRL_PERSISTENT_BROWSER, "label": "Persistent browser"},
    {"key": CTRL_APP_ENFORCED, "label": "App-enforced restrictions"},
    {"key": CTRL_CASB, "label": "Cloud App Security proxy"},
    {"key": CTRL_CAE, "label": "Continuous access evaluation"},
    {"key": CTRL_LEGACY_BLOCKED, "label": "Legacy auth blocked"},
]

CONTROL_KEYS = [c["key"] for c in CONTROLS]

# Session controls that constitute a real data-handling control on content. Named here rather
# than inline so the detector and the matrix cannot drift apart.
SESSION_CONTENT_CONTROLS = (CTRL_APP_ENFORCED, CTRL_CASB, CTRL_SIGNIN_FREQUENCY, CTRL_CAE)

# Retained ONLY so older callers and the compatibility alias below keep working. The taxonomy in
# `ca_taxonomy` is now the source of truth for classes.
APP_CLASSES: list[dict[str, str]] = [
    {"key": "all", "label": "All cloud apps"},
    {"key": "admin_portals", "label": "Microsoft Admin Portals"},
    {"key": "office365", "label": "Office 365"},
    {"key": "azure_management", "label": "Azure Management"},
]

# Auth-strength combinations that are genuinely phishing-resistant.
_PHISH_RESISTANT_COMBOS = {
    "fido2", "windowsHelloForBusiness", "x509CertificateMultiFactor",
    "deviceBasedPush",
}

CELL_ENFORCED = "enforced"
CELL_PARTIAL = "partial"
CELL_REPORT_ONLY = "report_only"
CELL_NONE = "none"

_MAX_SAMPLE = 50


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _days_since(ts: str, now: datetime) -> int | None:
    dt = _parse(ts)
    return None if dt is None else int((now - dt).total_seconds() // 86400)


# =============================================================== policy normalisation
def _role_template_index(roles_data: dict[str, Any]) -> dict[str, set[str]]:
    """roleTemplateId -> principal ids holding it (active, group-derived AND eligible)."""
    defs = {d.get("id"): d for d in roles_data.get("definitions") or []}
    by_template: dict[str, set[str]] = {}
    for bucket in ("assignments", "group_derived", "eligible"):
        for row in roles_data.get(bucket) or []:
            definition = defs.get(row.get("role_id")) or {}
            template = str(definition.get("template_id") or row.get("role_id") or "")
            pid = str(row.get("principal_id") or "")
            if template and pid:
                by_template.setdefault(template, set()).add(pid)
    return by_template


def _resolve_principals(
    spec_users: list[str],
    spec_groups: list[str],
    spec_roles: list[str],
    spec_guests: list[str],
    *,
    all_user_ids: set[str],
    guest_ids: set[str],
    group_members: dict[str, list[str]],
    role_index: dict[str, set[str]],
) -> tuple[set[str], bool]:
    """Resolve one side (include or exclude) of a policy's user condition.

    Returns ``(user_ids, is_all)``. ``is_all`` matters: "All users" must stay symbolic so a
    policy that targets everyone is still recognised as such on a partial user snapshot.
    """
    out: set[str] = set()
    is_all = False
    for token in spec_users:
        if token == "All":
            is_all = True
            out |= all_user_ids
        elif token == "None":
            continue
        elif token == "GuestsOrExternalUsers":
            out |= guest_ids
        elif token:
            out.add(token)
    if spec_guests:
        # A guest-type filter narrows an otherwise-broad guest inclusion.
        out |= guest_ids
    for gid in spec_groups:
        out.update(group_members.get(gid) or [])
    for template in spec_roles:
        out |= role_index.get(template, set())
    return out, is_all


def policy_fingerprint(policy: dict[str, Any]) -> str:
    """Stable hash over the *semantic* content, ignoring modifiedDateTime.

    Used for duplicate-intent detection and for change history — hashing the timestamp
    would make every save look like a real change."""
    c = policy.get("conditions") or {}
    g = policy.get("grant") or {}
    parts = [
        ",".join(sorted(c.get("include_users") or [])),
        ",".join(sorted(c.get("exclude_users") or [])),
        ",".join(sorted(c.get("include_groups") or [])),
        ",".join(sorted(c.get("exclude_groups") or [])),
        ",".join(sorted(c.get("include_roles") or [])),
        ",".join(sorted(c.get("include_apps") or [])),
        ",".join(sorted(c.get("exclude_apps") or [])),
        ",".join(sorted(c.get("client_app_types") or [])),
        ",".join(sorted(c.get("platforms_include") or [])),
        ",".join(sorted(c.get("locations_include") or [])),
        ",".join(sorted(c.get("sign_in_risk") or [])),
        ",".join(sorted(c.get("user_risk") or [])),
        str(g.get("operator") or ""),
        ",".join(sorted(g.get("controls") or [])),
        str(g.get("auth_strength_id") or ""),
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


def _controls_of(policy: dict[str, Any], strengths: dict[str, dict[str, Any]]) -> set[str]:
    grant = policy.get("grant") or {}
    session = policy.get("session") or {}
    builtin = set(grant.get("controls") or [])
    out: set[str] = set()
    if "block" in builtin:
        out.add(CTRL_BLOCK)
    if "mfa" in builtin:
        out.add(CTRL_MFA)
    if "compliantDevice" in builtin:
        out.add(CTRL_COMPLIANT)
        out.add(CTRL_COMPLIANT_OR_HYBRID)
    if "domainJoinedDevice" in builtin:
        out.add(CTRL_HYBRID)
        out.add(CTRL_COMPLIANT_OR_HYBRID)
    if "approvedApplication" in builtin:
        out.add(CTRL_APPROVED_APP)
    if "compliantApplication" in builtin:
        # An app-protection policy is a DIFFERENT control from an approved client app: one
        # governs which app may connect, the other governs what that app may do with the data
        # once it has it. Collapsing them (as this line used to) hides the MAM gap entirely.
        out.add(CTRL_APP_PROTECTION)
    if grant.get("terms_of_use"):
        out.add(CTRL_TERMS)
    strength_id = str(grant.get("auth_strength_id") or "")
    if strength_id:
        out.add(CTRL_MFA)
        out.add(CTRL_AUTH_STRENGTH)
        strength = strengths.get(strength_id) or {}
        combos = set(strength.get("combinations") or [])
        name = str(strength.get("display_name") or grant.get("auth_strength_name") or "").lower()
        if "phishing" in name or (combos and combos <= _phish_combo_universe(combos)):
            out.add(CTRL_PHISH)
    if session.get("sign_in_frequency"):
        out.add(CTRL_SIGNIN_FREQUENCY)
    if session.get("persistent_browser"):
        out.add(CTRL_PERSISTENT_BROWSER)
    if session.get("app_enforced_restrictions"):
        out.add(CTRL_APP_ENFORCED)
    if session.get("cloud_app_security"):
        out.add(CTRL_CASB)
    # CAE is a MODE string, not a bool. `disabled` is a deliberate opt-OUT and must not read as
    # the control being present.
    cae = str(session.get("continuous_access_evaluation") or "").lower()
    if cae and cae != "disabled":
        out.add(CTRL_CAE)
    if session.get("sign_in_frequency") or session.get("persistent_browser"):
        # Backwards-compatible aggregate so the pre-existing export sheet and any stored view
        # that asks for `session_limits` still resolves.
        out.add(CTRL_SESSION)
    return out


def _phish_combo_universe(combos: set[str]) -> set[str]:
    """Combination names Microsoft uses for phishing-resistant methods."""
    resistant = set()
    for c in combos:
        base = c.split(",")[0]
        if base in _PHISH_RESISTANT_COMBOS or base.startswith("fido2") or base.startswith("x509"):
            resistant.add(c)
    return resistant if resistant == combos else set()


def _app_classes_of(policy: dict[str, Any]) -> set[str]:
    """Which app classes a policy's application condition covers."""
    c = policy.get("conditions") or {}
    include = set(c.get("include_apps") or [])
    exclude = set(c.get("exclude_apps") or [])
    out: set[str] = set()
    if APP_ALL in include:
        out |= {"all", "admin_portals", "office365", "azure_management"}
    if APP_ADMIN_PORTALS in include:
        out.add("admin_portals")
    if APP_OFFICE365 in include:
        out.add("office365")
    if APP_AZURE_MANAGEMENT in include:
        out.add("azure_management")
    if APP_ADMIN_PORTALS in exclude:
        out.discard("admin_portals")
    if APP_OFFICE365 in exclude:
        out.discard("office365")
    if APP_AZURE_MANAGEMENT in exclude:
        out.discard("azure_management")
    return out


def normalize_policies(snapshot_data: dict[str, Any], tenant_id: str = "") -> list[dict[str, Any]]:
    """Resolve every policy's user condition to concrete user ids + derived flags."""
    ca = snapshot_data.get("ca") or {}
    people = snapshot_data.get("people") or {}
    roles = snapshot_data.get("roles") or {}

    users = people.get("users") or []
    all_user_ids = {str(u["id"]) for u in users if u.get("id") and u.get("enabled")}
    guest_ids = {str(u["id"]) for u in users if u.get("id") and u.get("user_type") == "Guest" and u.get("enabled")}
    group_members = ca.get("group_members") or {}
    role_index = _role_template_index(roles)
    strengths = {s["id"]: s for s in ca.get("auth_strengths") or [] if s.get("id")}
    app_index = ca_taxonomy.build_app_index(snapshot_data, tenant_id)

    out: list[dict[str, Any]] = []
    for p in ca.get("policies") or []:
        c = p.get("conditions") or {}
        inc, inc_all = _resolve_principals(
            c.get("include_users") or [], c.get("include_groups") or [],
            c.get("include_roles") or [], c.get("include_guests") or [],
            all_user_ids=all_user_ids, guest_ids=guest_ids,
            group_members=group_members, role_index=role_index,
        )
        exc, _ = _resolve_principals(
            c.get("exclude_users") or [], c.get("exclude_groups") or [],
            c.get("exclude_roles") or [], c.get("exclude_guests") or [],
            all_user_ids=all_user_ids, guest_ids=guest_ids,
            group_members=group_members, role_index=role_index,
        )
        effective = inc - exc
        controls = _controls_of(p, strengths)
        blocks_legacy = bool(
            CTRL_BLOCK in controls
            and set(c.get("client_app_types") or []) & LEGACY_CLIENT_APPS
            and not (set(c.get("client_app_types") or []) & {"browser", "mobileAppsAndDesktopClients", "all"})
        )
        if blocks_legacy:
            controls.add(CTRL_LEGACY_BLOCKED)
        class_coverage = ca_taxonomy.resolve_policy(p, app_index)
        out.append({
            **p,
            "fingerprint": policy_fingerprint(p),
            "include_all_users": inc_all,
            "included_ids": sorted(inc),
            "excluded_ids": sorted(exc),
            "effective_ids": sorted(effective),
            "effective_user_count": len(effective),
            "excluded_user_count": len(exc),
            "controls": sorted(controls),
            "class_coverage": class_coverage,
            "app_classes": sorted(class_coverage),
            "is_block": CTRL_BLOCK in controls,
            "is_enforced": p.get("state") == STATE_ENABLED,
            "is_report_only": p.get("state") == STATE_REPORT_ONLY,
            "is_disabled": p.get("state") == STATE_DISABLED,
            "targets_all_apps": APP_ALL in set(c.get("include_apps") or []),
            "blocks_legacy": blocks_legacy,
            "has_risk_condition": bool(c.get("sign_in_risk") or c.get("user_risk")),
            # A block that only fires under a condition (from these countries, on this
            # platform, at this risk level) does NOT always beat a grant. Treating every
            # block as unconditional reported "the grant can never be satisfied" for
            # policies that block a handful of sessions a year.
            "narrowing_conditions": _narrowing_conditions(c),
        })
    return out


_NARROWING = (
    ("locations_include", "named location"),
    ("platforms_include", "device platform"),
    ("sign_in_risk", "sign-in risk"),
    ("user_risk", "user risk"),
    ("service_principal_risk", "workload identity risk"),
    ("client_app_types", "client app type"),
    ("device_filter_rule", "device filter"),
    # A policy scoped to device-code flow or authentication transfer applies to almost no
    # ordinary sign-in. Omitting it here let "Block authentication flows" — all users, all
    # apps, block — count as an UNCONDITIONAL block, which is how a hardening policy every
    # tenant is told to create turned into "everyone is blocked from everything".
    ("auth_flows", "authentication flow"),
)


def _narrowing_conditions(conditions: dict[str, Any]) -> list[str]:
    """Which conditions stop a policy applying to every session in scope."""
    out = []
    for key, label in _NARROWING:
        value = conditions.get(key)
        if key == "client_app_types" and set(value or []) >= {"all"}:
            continue  # "all" is the default and narrows nothing
        if key == "locations_include" and set(value or []) == {"All"}:
            continue
        if value:
            out.append(label)
    return out


# ============================================================================ cohorts
_SERVICE_ACCOUNT_HINTS = ("svc", "service", "sa-", "-sa", "srv", "automation", "daemon", "noreply", "no-reply")


def build_cohorts(snapshot_data: dict[str, Any], breakglass_ids: set[str]) -> list[dict[str, Any]]:
    """The user cohorts the coverage matrix is computed over.

    Cohorts, not individuals: real incidents come from the population nobody thought to
    test (service accounts with no MFA method, guests, break-glass accounts)."""
    people = snapshot_data.get("people") or {}
    roles = snapshot_data.get("roles") or {}
    users = {str(u["id"]): u for u in people.get("users") or [] if u.get("id")}

    from app.entra.collectors.roles import global_admin_ids, privileged_principal_ids

    privileged = privileged_principal_ids(roles) & set(users)
    gas = global_admin_ids(roles) & set(users)

    enabled = {uid for uid, u in users.items() if u.get("enabled")}
    guests = {uid for uid in enabled if users[uid].get("user_type") == "Guest"}
    members = enabled - guests
    no_mfa = {uid for uid in enabled if users[uid].get("mfa_registered") is False}
    service_accounts = {
        uid for uid in members
        if _looks_like_service_account(users[uid])
    }

    cohorts = [
        {"key": "global_admins", "label": "Global Administrators", "ids": sorted(gas)},
        {"key": "privileged", "label": "All privileged roles", "ids": sorted(privileged)},
        {"key": "break_glass", "label": "Break-glass accounts", "ids": sorted(breakglass_ids & set(users))},
        {"key": "members", "label": "Members (non-privileged)", "ids": sorted(members - privileged)},
        {"key": "guests", "label": "Guests", "ids": sorted(guests)},
        {"key": "service_accounts", "label": "Likely service accounts", "ids": sorted(service_accounts)},
        {"key": "no_mfa", "label": "Users with no MFA method", "ids": sorted(no_mfa)},
    ]
    for c in cohorts:
        c["size"] = len(c["ids"])
    return cohorts


def _looks_like_service_account(user: dict[str, Any]) -> bool:
    """Heuristic, and labelled as such everywhere it surfaces.

    A service account is dangerous in a Conditional Access rollout precisely because it
    cannot satisfy an MFA grant — so the heuristic deliberately leans on 'no MFA method
    registered and no interactive sign-in' rather than on naming alone."""
    name = f"{user.get('upn', '')} {user.get('display_name', '')}".lower()
    named = any(h in name for h in _SERVICE_ACCOUNT_HINTS)
    no_mfa = user.get("mfa_registered") is False
    never_interactive = user.get("signin_known") and not user.get("last_signin")
    return bool(named or (no_mfa and never_interactive))


# =================================================================== coverage matrix
def build_coverage(
    policies: list[dict[str, Any]],
    cohorts: list[dict[str, Any]],
    snapshot_data: dict[str, Any],
    tenant_id: str = "",
) -> dict[str, Any]:
    """Cohort x application-class x control coverage.

    Delegates the matrix to :mod:`ca_coverage`, which computes coverage on BOTH the user and the
    application axis. The headline stays here because it is about the tenant, not the matrix."""
    index = ca_taxonomy.build_app_index(snapshot_data, tenant_id)
    out = ca_coverage.build(
        policies,
        cohorts,
        index,
        controls=CONTROLS,
        signin_activity=(snapshot_data.get("apps") or {}).get("signin_activity"),
    )
    out["headline"] = build_headline(policies, snapshot_data)
    out["app_index"] = {
        "app_count": index["app_count"],
        "members": {k: len(v) for k, v in index["members"].items()},
    }
    return out


def _cell(
    cohort_ids: set[str],
    enforced: list[dict[str, Any]],
    report_only: list[dict[str, Any]],
    app_class: str,
    control: str,
) -> dict[str, Any]:
    if not cohort_ids:
        return {"state": CELL_NONE, "covered": 0, "size": 0, "policies": [], "uncovered_sample": []}

    covered: set[str] = set()
    hits: list[str] = []
    for p in enforced:
        if app_class not in p["app_classes"] or control not in p["controls"]:
            continue
        reached = cohort_ids & set(p["effective_ids"])
        if reached:
            covered |= reached
            hits.append(p["display_name"] or p["id"])

    if covered >= cohort_ids:
        state = CELL_ENFORCED
    elif covered:
        state = CELL_PARTIAL
    else:
        ro_hit = any(
            app_class in p["app_classes"] and control in p["controls"] and (cohort_ids & set(p["effective_ids"]))
            for p in report_only
        )
        state = CELL_REPORT_ONLY if ro_hit else CELL_NONE

    uncovered = sorted(cohort_ids - covered)
    return {
        "state": state,
        "covered": len(covered),
        "size": len(cohort_ids),
        "policies": hits[:10],
        "uncovered_sample": uncovered[:_MAX_SAMPLE],
        "uncovered_total": len(uncovered),
    }


def build_headline(policies: list[dict[str, Any]], snapshot_data: dict[str, Any]) -> dict[str, Any]:
    """The sentence the whole page exists for.

    Counts *enabled* policies only, applies exclusions, and states its own assumptions —
    an inaccurate headline here destroys trust in everything else on the screen."""
    people = snapshot_data.get("people") or {}
    apps = snapshot_data.get("apps") or {}
    roles = snapshot_data.get("roles") or {}
    from app.entra.collectors.roles import privileged_principal_ids

    enabled_users = {str(u["id"]) for u in people.get("users") or [] if u.get("id") and u.get("enabled")}
    enforced = [p for p in policies if p["is_enforced"]]
    protected = set()
    for p in enforced:
        protected |= set(p["effective_ids"])
    uncovered_users = enabled_users - protected

    # Enterprise applications a user could actually sign in to.
    sps = [
        s for s in apps.get("service_principals") or []
        if s.get("enabled") and s.get("sp_type") == "Application"
    ]
    all_apps_policy = any(p["targets_all_apps"] for p in enforced)
    covered_app_ids: set[str] = set()
    for p in enforced:
        covered_app_ids |= set((p.get("conditions") or {}).get("include_apps") or [])
        covered_app_ids -= set((p.get("conditions") or {}).get("exclude_apps") or [])
    if all_apps_policy:
        uncovered_apps: list[dict[str, Any]] = []
    else:
        uncovered_apps = [s for s in sps if s.get("app_id") not in covered_app_ids]

    privileged_uncovered = sorted(uncovered_users & privileged_principal_ids(roles))
    return {
        "uncovered_users": len(uncovered_users),
        "uncovered_apps": len(uncovered_apps),
        "total_users": len(enabled_users),
        "total_apps": len(sps),
        "privileged_uncovered": len(privileged_uncovered),
        "privileged_uncovered_sample": privileged_uncovered[:_MAX_SAMPLE],
        "uncovered_user_sample": sorted(uncovered_users)[:_MAX_SAMPLE],
        "uncovered_app_sample": [
            {"app_id": s.get("app_id"), "name": s.get("display_name")} for s in uncovered_apps[:_MAX_SAMPLE]
        ],
        "assumptions": [
            "Counts enabled (enforced) policies only — report-only policies do not protect anyone.",
            "Exclusions are applied; a user excluded from every policy counts as uncovered.",
            "Role-scoped policies include eligible role holders, not only active ones.",
            "Disabled user accounts are not counted.",
        ],
    }


# ================================================================== conflict detection
def detect_conflicts(policies: list[dict[str, Any]], privileged_ids: set[str]) -> list[dict[str, Any]]:
    """Set-logic detectors over the resolved policy sets. Each returns an explainable row."""
    out: list[dict[str, Any]] = []
    enforced = [p for p in policies if p["is_enforced"]]

    # 1. No effect — resolves to zero users, or to no application class.
    for p in policies:
        if p["is_disabled"]:
            continue
        if p["effective_user_count"] == 0:
            out.append(_conflict("policy_no_effect", p, None,
                                 "Resolves to zero users, so it can never apply."))
        elif not p["app_classes"] and not (p.get("conditions") or {}).get("include_apps"):
            out.append(_conflict("policy_no_effect", p, None,
                                 "No application is in scope, so it can never apply."))

    # 2. Unreachable condition — the same value both included and excluded.
    for p in policies:
        c = p.get("conditions") or {}
        for field, label in (("platforms", "device platform"), ("locations", "location")):
            inc = set(c.get(f"{field}_include") or [])
            exc = set(c.get(f"{field}_exclude") or [])
            clash = inc & exc
            if clash and inc and inc <= exc:
                out.append(_conflict("unreachable_condition", p, None,
                                     f"Every included {label} ({', '.join(sorted(clash))}) is also excluded."))

    # 3. Block/grant contradiction — an UNCONDITIONAL block always wins in Entra, whatever
    #    the grant says. A conditional block (named location, platform, risk) only wins for
    #    the sessions that match it, so reporting it as an absolute contradiction is wrong.
    blocks = [p for p in enforced if p["is_block"] and not p.get("narrowing_conditions")]
    grants = [p for p in enforced if not p["is_block"] and p["controls"]]
    for b in blocks:
        b_ids = set(b["effective_ids"])
        b_apps = set(b["app_classes"])
        for g in grants:
            overlap = b_ids & set(g["effective_ids"])
            if overlap and (b_apps & set(g["app_classes"])):
                out.append(_conflict(
                    "conflicting_block_grant", g, b,
                    f"{len(overlap):,} user(s) are blocked by '{b['display_name']}', so this policy's "
                    "grant can never be satisfied for them. Entra evaluates all policies and block wins.",
                    affected=len(overlap),
                ))

    # 4. Shadowing / redundancy — B adds nothing over A.
    for i, a in enumerate(enforced):
        a_ids, a_ctl, a_apps = set(a["effective_ids"]), set(a["controls"]), set(a["app_classes"])
        for b in enforced[i + 1:]:
            b_ids, b_ctl, b_apps = set(b["effective_ids"]), set(b["controls"]), set(b["app_classes"])
            if not b_ids or not b_ctl:
                continue
            if b_ids <= a_ids and b_ctl <= a_ctl and b_apps <= a_apps and a["fingerprint"] != b["fingerprint"]:
                out.append(_conflict("redundant_policy", b, a,
                                     f"Fully subsumed by '{a['display_name']}' — same or narrower users, "
                                     "applications and controls."))
            elif a["fingerprint"] == b["fingerprint"]:
                out.append(_conflict("duplicate_intent", b, a,
                                     f"Identical conditions and grants to '{a['display_name']}'."))

    # 5. Exclusion defeats the policy's own purpose.
    for p in policies:
        if p["is_disabled"] or not p["controls"]:
            continue
        excluded_priv = sorted(set(p["excluded_ids"]) & privileged_ids)
        if excluded_priv and (set(p["controls"]) & {CTRL_MFA, CTRL_BLOCK, CTRL_PHISH, CTRL_COMPLIANT}):
            out.append(_conflict(
                "exclusion_privileged", p, None,
                f"{len(excluded_priv)} privileged principal(s) are excluded from a security control.",
                affected=len(excluded_priv), sample=excluded_priv[:_MAX_SAMPLE],
            ))
        included = len(p["included_ids"])
        excluded = p["excluded_user_count"]
        if included and excluded >= 20 and excluded / included > 0.2:
            out.append(_conflict(
                "exclusion_sprawl", p, None,
                f"{excluded} of {included} targeted users are excluded ({excluded / included:.0%}).",
                affected=excluded,
            ))
    return out


def _conflict(
    kind: str,
    policy: dict[str, Any],
    other: dict[str, Any] | None,
    detail: str,
    *,
    affected: int = 0,
    sample: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "policy_id": policy["id"],
        "policy_name": policy.get("display_name") or policy["id"],
        "policy_state": policy.get("state"),
        "other_id": (other or {}).get("id", ""),
        "other_name": (other or {}).get("display_name", ""),
        "detail": detail,
        "affected": affected,
        "sample": sample or [],
    }


# ==================================================================== break-glass
_BG_NAME_HINTS = ("break", "glass", "emergency", "bg-", "bg_", "eba", "firecall", "breakglass")


def detect_breakglass(
    policies: list[dict[str, Any]],
    snapshot_data: dict[str, Any],
    confirmed: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Heuristic emergency-account detection, always user-confirmable.

    Auto-classifying an account as break-glass and then quietly excluding it from findings
    would be dangerous, so detection produces *candidates* and the operator confirms or
    rejects; the decision persists in ``findings_state``.
    """
    now = now or _now()
    confirmed = confirmed or {}
    people = snapshot_data.get("people") or {}
    roles = snapshot_data.get("roles") or {}
    users = {str(u["id"]): u for u in people.get("users") or [] if u.get("id")}

    from app.entra.collectors.roles import global_admin_ids

    gas = global_admin_ids(roles)
    enforced = [p for p in policies if p["is_enforced"]]
    security_policies = [p for p in enforced if set(p["controls"]) & {CTRL_MFA, CTRL_BLOCK, CTRL_PHISH, CTRL_COMPLIANT}]
    # Built once per policy, not once per user per policy. These were previously constructed
    # inside the loop below, which on a 5,000-user tenant with 60 policies meant 600,000
    # rebuilds of a 5,000-element set and turned this function into a 29-second stall on the
    # Conditional Access page. Hoisting them makes it ~0.1s; the logic is unchanged.
    _sec = [(p, frozenset(p["effective_ids"]), frozenset(p["excluded_ids"])) for p in security_policies]

    candidates: list[dict[str, Any]] = []
    for uid, u in users.items():
        if not u.get("enabled"):
            continue
        # A break-glass account is an internal, cloud-only emergency identity. A guest is
        # by definition someone else's account and can never be one. Guests score highly
        # on every generic signal here — cloud-only, no department, no recent interactive
        # sign-in, excluded from a policy — so on a tenant with a real B2B population they
        # flooded the candidate list and buried the one genuine emergency account.
        if str(u.get("user_type") or "") == "Guest":
            continue
        covered_by = [p for p, eff, _exc in _sec if uid in eff]
        excluded_from = [p for p, _eff, exc in _sec if uid in exc]
        reasons: list[str] = []
        score = 0
        if security_policies and not covered_by:
            score += 3
            reasons.append("Not covered by any enforced security policy")
        if excluded_from:
            score += 2
            reasons.append(f"Explicitly excluded from {len(excluded_from)} security policy/policies")
        if uid in gas:
            score += 2
            reasons.append("Holds Global Administrator")
        if not u.get("on_prem_synced"):
            score += 1
            reasons.append("Cloud-only account")
        name = f"{u.get('upn', '')} {u.get('display_name', '')}".lower()
        if any(h in name for h in _BG_NAME_HINTS):
            score += 2
            reasons.append("Naming pattern suggests emergency access")
        if not u.get("department") and not u.get("job_title"):
            score += 1
            reasons.append("No department or job title")
        last = _days_since(str(u.get("last_signin") or ""), now)
        if u.get("signin_known") and (last is None or last > 90):
            score += 1
            reasons.append("No recent interactive sign-in")

        state = confirmed.get(uid)
        if score >= 5 or state is not None:
            candidates.append({
                "user_id": uid,
                "upn": u.get("upn", ""),
                "display_name": u.get("display_name", ""),
                "score": score,
                "reasons": reasons,
                "confirmed": bool((state or {}).get("confirmed")) if isinstance(state, dict) else None,
                "note": (state or {}).get("note", "") if isinstance(state, dict) else "",
                "is_global_admin": uid in gas,
                "excluded_from": [p["display_name"] for p in excluded_from][:10],
                "covered_by": [p["display_name"] for p in covered_by][:10],
                "mfa_registered": u.get("mfa_registered"),
                "lockout_risk": bool(covered_by) and u.get("mfa_registered") is False,
            })

    confirmed_ids = {c["user_id"] for c in candidates if c["confirmed"]}
    return {
        "candidates": sorted(candidates, key=lambda c: -c["score"]),
        "confirmed_ids": sorted(confirmed_ids),
        "confirmed_count": len(confirmed_ids),
        "candidate_count": len(candidates),
        "over_covered": [c for c in candidates if c["confirmed"] and c["lockout_risk"]],
        "heuristic_note": (
            "Break-glass accounts are detected heuristically and must be confirmed. "
            "An unconfirmed candidate is never excluded from any other finding."
        ),
    }


# ========================================================================= entry point
def analyse(
    snapshot_data: dict[str, Any],
    *,
    confirmed_breakglass: dict[str, Any] | None = None,
    now: datetime | None = None,
    tenant_id: str = "",
) -> dict[str, Any]:
    """Full Conditional Access analysis over a collected snapshot. Pure."""
    now = now or _now()
    roles = snapshot_data.get("roles") or {}
    from app.entra.collectors.roles import privileged_principal_ids

    policies = normalize_policies(snapshot_data, tenant_id)
    privileged = privileged_principal_ids(roles)
    breakglass = detect_breakglass(policies, snapshot_data, confirmed_breakglass, now=now)
    cohorts = build_cohorts(snapshot_data, set(breakglass["confirmed_ids"]))
    coverage = build_coverage(policies, cohorts, snapshot_data, tenant_id)
    conflicts = detect_conflicts(policies, privileged)

    enforced = [p for p in policies if p["is_enforced"]]
    return {
        "policies": policies,
        "coverage": coverage,
        "conflicts": conflicts,
        "breakglass": breakglass,
        "counts": {
            "policies": len(policies),
            "enforced": len(enforced),
            "report_only": sum(1 for p in policies if p["is_report_only"]),
            "disabled": sum(1 for p in policies if p["is_disabled"]),
            "block_policies": sum(1 for p in enforced if p["is_block"]),
            "conflicts": len(conflicts),
        },
        "generated_at": now.isoformat(),
    }
