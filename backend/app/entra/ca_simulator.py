"""Conditional Access Change Simulator — "if I enable this, who gets blocked?"

The highest-value and highest-risk feature in the product. A wrong *"nobody gets blocked"*
is worse than having no simulator at all, so every decision here is subordinate to that:

* **Diff first.** The simulator never answers "here is what happens". It answers "here is
  what *changes*" — baseline result versus proposed result, per principal per context.
* **Challenged vs blocked-effective.** "Requires MFA" is benign for a user with a registered
  method and a *hard block* for a service account that has none. That distinction is the
  entire product value and is computed from each principal's real capability profile.
* **Break-glass first, always.** The emergency account caught by the policy that broke the
  tenant is the most expensive Conditional Access mistake in the field.
* **Protection lost.** Disabling or deleting a policy is simulated too — the silent risk of
  a "cleanup" is the category nobody tests for.
* **Never a bare verdict.** Results carry a confidence label and a published limitations
  list. Where Microsoft's beta `evaluate` API is available, both engines run and any
  divergence is surfaced rather than hidden.

Pure computation over a cached snapshot: no writes, no policy is ever persisted to the
tenant, and Microsoft's `evaluate` endpoint is itself read-only.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from app.entra import ca_engine
from app.entra.ca_engine import (
    CTRL_APPROVED_APP,
    CTRL_BLOCK,
    CTRL_COMPLIANT,
    CTRL_HYBRID,
    CTRL_MFA,
    CTRL_PHISH,
    CTRL_SESSION,
)
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

# ------------------------------------------------------------------------- verdicts
GRANTED = "granted"
CHALLENGED = "challenged"          # can satisfy the control — will see friction
BLOCKED = "blocked"                # an explicit block policy
BLOCKED_EFFECTIVE = "blocked_effective"   # cannot satisfy the control — a hard block in practice
NOT_APPLICABLE = "not_applicable"

BLOCKING_VERDICTS = (BLOCKED, BLOCKED_EFFECTIVE)

# Stated in the UI with every result. An honest limitation list is what makes the tool
# trustworthy; a simulator that implies completeness it does not have is worse than none.
LIMITATIONS: tuple[str, ...] = (
    "Does not model Continuous Access Evaluation revocation timing.",
    "Does not model app-enforced session restrictions inside the application.",
    "Does not model per-application authentication context assignment inside workloads.",
    "Device compliance is taken from the simulated context, not a live Intune evaluation.",
    "Guest MFA satisfaction from a home tenant is modelled from the cross-tenant access "
    "policy and may lag reality.",
    "Risk levels are hypothetical inputs, not predictions.",
    "Only enabled (enforced) policies are evaluated; report-only policies never block.",
)

_SAMPLE_SEED = 20260730          # seeded so re-runs are comparable
_MAX_CASES = 20000
_MAX_LISTED = 100

# The complete change vocabulary. Anything else is rejected rather than ignored.
CHANGE_KINDS = frozenset({"enable", "disable", "report_only", "delete", "add", "modify"})
_POLICY_SCOPED_KINDS = frozenset({"enable", "disable", "report_only", "delete"})


class InvalidChange(ValueError):
    """A requested change cannot be applied to this snapshot."""


# --------------------------------------------------------------------------- context
@dataclass(frozen=True)
class SignInContext:
    key: str
    label: str
    client_app: str = "browser"          # browser | mobileAppsAndDesktopClients | exchangeActiveSync | other
    platform: str = "windows"
    location: str = "untrusted"          # trusted | untrusted | unknown
    device_compliant: bool = False
    device_hybrid_joined: bool = False
    sign_in_risk: str = "none"
    user_risk: str = "none"
    app_class: str = "all_cloud_apps"    # a class id from app.entra.ca_taxonomy


DEFAULT_CONTEXTS: tuple[SignInContext, ...] = (
    SignInContext("browser_unmanaged", "Browser, unmanaged device", client_app="browser"),
    SignInContext("desktop_compliant", "Desktop client, compliant device",
                  client_app="mobileAppsAndDesktopClients", device_compliant=True,
                  device_hybrid_joined=True),
    SignInContext("legacy_eas", "Exchange ActiveSync (legacy)", client_app="exchangeActiveSync"),
    SignInContext("legacy_other", "Other legacy clients (IMAP/POP/SMTP)", client_app="other"),
    SignInContext("trusted_location", "Browser from a trusted location", location="trusted"),
    SignInContext("admin_portal", "Microsoft Admin Portals", app_class="admin_planes"),
    SignInContext("azure_mgmt", "Azure management", app_class="management_apis"),
    # The content surface. Without this, a change scoped to SharePoint/Exchange/Teams simulated
    # as affecting nobody — the only classes modelled were the two admin ones, so the simulator
    # was blind to changes on the applications where the organisation's data actually lives.
    SignInContext("collab_content", "SharePoint, Exchange and Teams content",
                  app_class="collaboration_content"),
    SignInContext("office365", "Office 365 suite", app_class="office365_bundle"),
    SignInContext("third_party", "Third-party SaaS application", app_class="third_party_saas"),
    # The two user actions. A policy targeting a user action targets no application at all, so
    # every application-shaped context missed it entirely.
    SignInContext("register_security_info", "Registering security information",
                  app_class="identity_lifecycle"),
    SignInContext("register_device", "Registering or joining a device",
                  app_class="identity_lifecycle"),
    SignInContext("high_risk", "High sign-in risk", sign_in_risk="high", user_risk="high"),
)

CONTEXTS_BY_KEY = {c.key: c for c in DEFAULT_CONTEXTS}

#: Classes that are NOT cloud applications. A policy scoped to "All cloud apps" does not reach
#: these — Entra targets applications, user actions and authentication contexts separately.
_NON_APP_CLASSES = frozenset({"identity_lifecycle", "scoped_constructs", "legacy_protocols"})


# ------------------------------------------------------------------------- principal
@dataclass
class SimPrincipal:
    id: str
    label: str
    kind: str = "user"                   # user | servicePrincipal
    enabled: bool = True
    user_type: str = "Member"
    mfa_registered: bool | None = None
    phishing_resistant: bool | None = None
    on_prem_synced: bool = False
    cohorts: list[str] = field(default_factory=list)

    def capabilities(self, ctx: SignInContext) -> set[str]:
        """What this principal can actually satisfy, in this context.

        ``mfa_registered is None`` means the registration report was unavailable. We treat
        that as *satisfiable* so the simulator does not invent hard blocks it cannot prove —
        and the result flags the assumption."""
        caps: set[str] = set()
        if self.mfa_registered is not False and self.kind == "user":
            caps.add(CTRL_MFA)
        if self.phishing_resistant:
            caps.add(CTRL_PHISH)
        if ctx.device_compliant:
            caps.add(CTRL_COMPLIANT)
        if ctx.device_hybrid_joined:
            caps.add(CTRL_HYBRID)
        if ctx.client_app in ("browser", "mobileAppsAndDesktopClients"):
            caps.add(CTRL_APPROVED_APP)
        caps.add(CTRL_SESSION)           # session controls are never a hard block
        return caps

    @property
    def mfa_unknown(self) -> bool:
        return self.mfa_registered is None and self.kind == "user"


# ----------------------------------------------------------------------- evaluation
def matches(policy: dict[str, Any], principal: SimPrincipal, ctx: SignInContext) -> bool:
    """Does this policy apply to this principal in this context?

    Order mirrors Entra's own evaluation: users -> applications -> platform -> location ->
    client app type -> risk. Exclusions have already been applied when the policy's
    ``effective_ids`` were resolved."""
    if principal.id not in policy["_effective"]:
        return False
    # A policy targeting every cloud app also governs the narrower application classes.
    # Comparing class ids for equality (as this once did) made an "All cloud apps" policy
    # invisible to the admin portal and Azure management scenarios, which is exactly where a
    # simulation is trusted most.
    #
    # The fallback stops at application classes. In Entra a policy targets cloud apps OR user
    # actions OR an authentication context — they are mutually exclusive on the target blade —
    # so "All cloud apps" does NOT protect device registration or security-info registration.
    # Letting the wildcard cover them would simulate those actions as already protected on
    # almost every tenant, which is the opposite of the truth and the exact gap the
    # `ca.device_join_unprotected` detector exists to report.
    classes = set(policy.get("app_classes") or ())
    if ctx.app_class not in classes:
        if ctx.app_class in _NON_APP_CLASSES or "all_cloud_apps" not in classes:
            return False
        # Reached by the wildcard. Fall through to the remaining conditions - returning True
        # here would skip the platform, location, client-app and risk checks entirely, which
        # made a legacy-only block policy apply to a browser sign-in.

    conditions = policy.get("conditions") or {}

    include_platforms = set(conditions.get("platforms_include") or [])
    exclude_platforms = set(conditions.get("platforms_exclude") or [])
    if include_platforms and "all" not in include_platforms and ctx.platform not in include_platforms:
        return False
    if ctx.platform in exclude_platforms:
        return False

    include_locations = set(conditions.get("locations_include") or [])
    exclude_locations = set(conditions.get("locations_exclude") or [])
    if include_locations:
        if "All" in include_locations:
            pass
        elif "AllTrusted" in include_locations and ctx.location != "trusted":
            return False
        elif "AllTrusted" not in include_locations and ctx.location != "trusted":
            # Named locations are IP sets we cannot evaluate for a synthetic context; a
            # policy scoped to specific locations is treated as not matching an untrusted
            # or unknown context rather than guessed either way.
            return False
    if "AllTrusted" in exclude_locations and ctx.location == "trusted":
        return False

    client_apps = set(conditions.get("client_app_types") or [])
    if client_apps and "all" not in client_apps and ctx.client_app not in client_apps:
        return False

    sign_in_risk = set(conditions.get("sign_in_risk") or [])
    if sign_in_risk and ctx.sign_in_risk not in sign_in_risk:
        return False
    user_risk = set(conditions.get("user_risk") or [])
    if user_risk and ctx.user_risk not in user_risk:
        return False
    return True


def required_controls(applicable: list[dict[str, Any]]) -> set[str]:
    """Union of grant controls across applicable policies.

    Within one policy an ``OR`` operator means any one control satisfies it, so only the
    cheapest is required; ``AND`` requires all. Across policies the union is conjunctive —
    every applicable policy must be satisfied."""
    required: set[str] = set()
    for p in applicable:
        controls = set(p["controls"]) - {CTRL_BLOCK, CTRL_SESSION}
        if not controls:
            continue
        operator = str((p.get("grant") or {}).get("operator") or "OR").upper()
        if operator == "AND":
            required |= controls
        else:
            # Cheapest satisfiable control, in ascending order of friction.
            for candidate in (CTRL_MFA, CTRL_COMPLIANT, CTRL_HYBRID, CTRL_APPROVED_APP, CTRL_PHISH):
                if candidate in controls:
                    required.add(candidate)
                    break
            else:
                required |= controls
    return required


def evaluate(
    policies: list[dict[str, Any]], principal: SimPrincipal, ctx: SignInContext
) -> dict[str, Any]:
    """Evaluate one (principal, context) against a policy set. Deterministic and pure."""
    applicable = [p for p in policies if p["is_enforced"] and matches(p, principal, ctx)]
    if not applicable:
        return {"verdict": GRANTED, "policies": [], "required": [], "missing": [],
                "protected": False}

    blocks = [p for p in applicable if p["is_block"]]
    if blocks:
        return {"verdict": BLOCKED, "policies": [p["display_name"] for p in applicable],
                "required": ["block"], "missing": ["block"], "protected": True,
                "blocked_by": [p["display_name"] for p in blocks]}

    required = required_controls(applicable)
    satisfied = principal.capabilities(ctx)
    missing = sorted(required - satisfied)
    if not missing:
        return {"verdict": GRANTED if not required else CHALLENGED,
                "policies": [p["display_name"] for p in applicable],
                "required": sorted(required), "missing": [], "protected": bool(required)}
    return {"verdict": BLOCKED_EFFECTIVE,
            "policies": [p["display_name"] for p in applicable],
            "required": sorted(required), "missing": missing, "protected": True}


# ------------------------------------------------------------------- change application
def apply_changes(
    policies: list[dict[str, Any]], changes: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Produce the proposed policy set. Never mutates the baseline, never writes to Graph.

    A change that cannot be applied is an error, never a silent no-op: an unrecognised
    kind or an unknown policy id would otherwise produce a reassuring "nothing changes"
    diff, which is the worst possible answer for a security decision."""
    by_id = {p["id"]: dict(p) for p in policies}
    notes: list[str] = []
    problems: list[str] = []
    for change in changes or []:
        kind = str(change.get("kind") or "")
        pid = str(change.get("policy_id") or "")
        if kind not in CHANGE_KINDS:
            problems.append(
                f"unknown change kind {kind or '(missing)'!r} — expected one of "
                f"{', '.join(sorted(CHANGE_KINDS))}"
            )
            continue
        if kind in _POLICY_SCOPED_KINDS and pid not in by_id:
            problems.append(
                f"no Conditional Access policy with id {pid or '(missing)'!r} in this snapshot"
            )
            continue
        if kind == "enable":
            by_id[pid]["state"] = STATE_ENABLED
            by_id[pid]["is_enforced"] = True
            by_id[pid]["is_report_only"] = False
            by_id[pid]["is_disabled"] = False
            notes.append(f"enable '{by_id[pid].get('display_name')}'")
        elif kind == "disable":
            by_id[pid]["state"] = STATE_DISABLED
            by_id[pid]["is_enforced"] = False
            by_id[pid]["is_disabled"] = True
            notes.append(f"disable '{by_id[pid].get('display_name')}'")
        elif kind == "report_only":
            by_id[pid]["state"] = STATE_REPORT_ONLY
            by_id[pid]["is_enforced"] = False
            by_id[pid]["is_report_only"] = True
            notes.append(f"set '{by_id[pid].get('display_name')}' to report-only")
        elif kind == "delete":
            notes.append(f"delete '{by_id[pid].get('display_name')}'")
            by_id.pop(pid, None)
        elif kind in ("add", "modify"):
            proposed = change.get("policy") or {}
            new_id = str(proposed.get("id") or pid or f"proposed-{len(by_id)}")
            merged = dict(by_id.get(new_id) or {})
            merged.update(proposed)
            merged["id"] = new_id
            merged.setdefault("display_name", proposed.get("display_name") or "Proposed policy")
            by_id[new_id] = merged
            notes.append(f"{kind} '{merged['display_name']}'")
    if problems:
        raise InvalidChange("; ".join(problems))
    return list(by_id.values()), notes


def _prepare(policies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach the fast lookup sets the evaluator needs, without touching the originals."""
    out = []
    for p in policies:
        row = dict(p)
        row["_effective"] = set(p.get("effective_ids") or [])
        row.setdefault("controls", [])
        row.setdefault("app_classes", [])
        row["is_enforced"] = bool(p.get("is_enforced", p.get("state") == STATE_ENABLED))
        row["is_block"] = bool(p.get("is_block", CTRL_BLOCK in (p.get("controls") or [])))
        out.append(row)
    return out


# ------------------------------------------------------------------------- principals
def build_principals(
    snapshot_data: dict[str, Any], analysis: dict[str, Any]
) -> list[SimPrincipal]:
    """Every principal worth simulating, tagged with the cohorts it belongs to."""
    people = snapshot_data.get("people") or {}
    roles = snapshot_data.get("roles") or {}
    apps = snapshot_data.get("apps") or {}
    breakglass = set((analysis.get("breakglass") or {}).get("confirmed_ids") or [])
    candidates = {c["user_id"] for c in (analysis.get("breakglass") or {}).get("candidates") or []}

    from app.entra.collectors.roles import global_admin_ids, privileged_principal_ids

    privileged = privileged_principal_ids(roles)
    gas = global_admin_ids(roles)

    cohort_ids = {c["key"]: set(c["ids"]) for c in
                  ca_engine.build_cohorts(snapshot_data, breakglass)}

    out: list[SimPrincipal] = []
    for u in people.get("users") or []:
        uid = str(u.get("id") or "")
        if not uid or not u.get("enabled"):
            continue
        cohorts = [key for key, ids in cohort_ids.items() if uid in ids]
        if uid in breakglass:
            cohorts.append("break_glass")
        elif uid in candidates:
            cohorts.append("break_glass_candidate")
        if uid in gas:
            cohorts.append("global_admins")
        if uid in privileged:
            cohorts.append("privileged")
        if u.get("on_prem_synced"):
            cohorts.append("on_prem_synced")
        out.append(SimPrincipal(
            id=uid,
            label=u.get("upn") or u.get("display_name") or uid,
            kind="user",
            user_type=str(u.get("user_type") or "Member"),
            mfa_registered=u.get("mfa_registered"),
            phishing_resistant=u.get("phishing_resistant"),
            on_prem_synced=bool(u.get("on_prem_synced")),
            cohorts=sorted(set(cohorts)),
        ))

    for sp in apps.get("service_principals") or []:
        if sp.get("is_first_party") or not sp.get("enabled"):
            continue
        out.append(SimPrincipal(
            id=str(sp.get("object_id") or ""),
            label=sp.get("display_name") or str(sp.get("app_id") or ""),
            kind="servicePrincipal",
            mfa_registered=False,          # workload identities cannot perform MFA
            phishing_resistant=False,
            cohorts=["workload_identities"],
        ))
    return out


# Cohorts that are ALWAYS evaluated in full — never sampled, however large the tenant.
ALWAYS_FULL = ("break_glass", "break_glass_candidate", "global_admins", "privileged")


def select_principals(
    principals: list[SimPrincipal], *, sample_size: int, cohorts: Iterable[str] | None = None
) -> tuple[list[SimPrincipal], dict[str, Any]]:
    """Full evaluation for the cohorts that matter, seeded sampling for the rest."""
    wanted = set(cohorts or [])
    pool = [p for p in principals if not wanted or (set(p.cohorts) & wanted)]
    always = [p for p in pool if set(p.cohorts) & set(ALWAYS_FULL)]
    rest = [p for p in pool if p not in always]
    sampled = False
    if len(rest) > sample_size:
        rng = random.Random(_SAMPLE_SEED)
        rest = rng.sample(rest, sample_size)
        sampled = True
    selected = always + rest
    return selected, {
        "total_principals": len(pool),
        "evaluated": len(selected),
        "sampled": sampled,
        "always_full_cohorts": list(ALWAYS_FULL),
        "sample_size": sample_size,
    }


# ----------------------------------------------------------------------------- run
def simulate(
    snapshot_data: dict[str, Any],
    analysis: dict[str, Any],
    changes: list[dict[str, Any]],
    *,
    contexts: list[str] | None = None,
    cohorts: list[str] | None = None,
    sample_size: int = 400,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the diff. Pure: no I/O, deterministic for a given snapshot + change set."""
    now = now or datetime.now(timezone.utc)
    baseline_policies = _prepare(analysis.get("policies") or [])
    proposed_raw, change_notes = apply_changes(analysis.get("policies") or [], changes)
    proposed_policies = _prepare(proposed_raw)

    ctx_list = [CONTEXTS_BY_KEY[c] for c in (contexts or []) if c in CONTEXTS_BY_KEY] or list(DEFAULT_CONTEXTS)
    principals = build_principals(snapshot_data, analysis)
    selected, sampling = select_principals(principals, sample_size=sample_size, cohorts=cohorts)

    cases: list[dict[str, Any]] = []
    counts = {"newly_blocked": 0, "newly_challenged": 0, "newly_granted": 0,
              "protection_lost": 0, "unchanged": 0}
    by_cohort: dict[str, dict[str, int]] = {}
    by_context: dict[str, dict[str, int]] = {}
    break_glass_impact: list[dict[str, Any]] = []
    mfa_unknown = 0

    budget = _MAX_CASES
    for principal in selected:
        if principal.mfa_unknown:
            mfa_unknown += 1
        for ctx in ctx_list:
            if budget <= 0:
                break
            budget -= 1
            before = evaluate(baseline_policies, principal, ctx)
            after = evaluate(proposed_policies, principal, ctx)
            category = _categorise(before, after)
            counts[category] += 1
            for cohort in principal.cohorts:
                by_cohort.setdefault(cohort, dict.fromkeys(counts, 0))[category] += 1
            by_context.setdefault(ctx.key, dict.fromkeys(counts, 0))[category] += 1

            if category == "unchanged":
                continue
            case = {
                "principal_id": principal.id,
                "principal": principal.label,
                "kind": principal.kind,
                "cohorts": principal.cohorts,
                "context": ctx.key,
                "context_label": ctx.label,
                "from": before["verdict"],
                "to": after["verdict"],
                "category": category,
                "missing": after.get("missing") or [],
                "policies_before": before.get("policies") or [],
                "policies_after": after.get("policies") or [],
                "mfa_unknown": principal.mfa_unknown,
            }
            cases.append(case)
            if "break_glass" in principal.cohorts and category in ("newly_blocked",):
                break_glass_impact.append(case)

    ordered = _order_cases(cases)
    return {
        "generated_at": now.isoformat(),
        "changes": change_notes,
        "counts": counts,
        "break_glass_impact": break_glass_impact[:_MAX_LISTED],
        "break_glass_affected": len({c["principal_id"] for c in break_glass_impact}),
        "by_cohort": by_cohort,
        "by_context": by_context,
        "cases": ordered[:_MAX_LISTED],
        "case_total": len(cases),
        "sampling": {**sampling, "contexts": [c.key for c in ctx_list],
                     "case_budget_exhausted": budget <= 0},
        "assumptions": {
            "mfa_unknown_principals": mfa_unknown,
            "mfa_unknown_note": (
                "MFA registration could not be read for these principals (needs Entra ID P1), "
                "so they are assumed able to satisfy an MFA control. Real blocks may be higher."
            ) if mfa_unknown else "",
        },
        "limitations": list(LIMITATIONS),
        "baseline_enforced": sum(1 for p in baseline_policies if p["is_enforced"]),
        "proposed_enforced": sum(1 for p in proposed_policies if p["is_enforced"]),
        "confidence": "modelled",
        "confidence_label": "Modelled locally",
        "fingerprint": _fingerprint(changes, ctx_list),
    }


def _categorise(before: dict[str, Any], after: dict[str, Any]) -> str:
    b, a = before["verdict"], after["verdict"]
    if b == a:
        # Same verdict, but protection may still have been lost (e.g. a control disappeared).
        if before.get("protected") and not after.get("protected"):
            return "protection_lost"
        return "unchanged"
    if a in BLOCKING_VERDICTS and b not in BLOCKING_VERDICTS:
        return "newly_blocked"
    if a == CHALLENGED and b == GRANTED:
        return "newly_challenged"
    if a == GRANTED and b in (CHALLENGED, *BLOCKING_VERDICTS):
        # A protection was removed. This is the silent risk of a "cleanup" and the category
        # nobody thinks to test for.
        return "protection_lost" if before.get("protected") else "newly_granted"
    if a == CHALLENGED and b in BLOCKING_VERDICTS:
        return "newly_granted"
    return "unchanged"


_CATEGORY_ORDER = {"newly_blocked": 0, "protection_lost": 1, "newly_challenged": 2,
                   "newly_granted": 3, "unchanged": 4}


def _order_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Break-glass first, then by severity of change — never alphabetical."""
    def key(c: dict[str, Any]) -> tuple:
        return (
            0 if "break_glass" in c["cohorts"] else 1,
            _CATEGORY_ORDER.get(c["category"], 9),
            0 if "privileged" in c["cohorts"] else 1,
            c["principal"],
        )
    return sorted(cases, key=key)


def _fingerprint(changes: list[dict[str, Any]], contexts: list[SignInContext]) -> str:
    parts = [f"{c.get('kind')}:{c.get('policy_id')}" for c in changes or []]
    parts += [c.key for c in contexts]
    return hashlib.sha1("|".join(parts).encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


# ------------------------------------------------------------------ Microsoft engine
def microsoft_cases(result: dict[str, Any], limit: int = 40) -> list[dict[str, Any]]:
    """The sample of cases worth cross-checking against Microsoft's evaluate API."""
    return result.get("cases", [])[:limit]


def compare_engines(local: list[dict[str, Any]], microsoft: dict[str, str]) -> dict[str, Any]:
    """Compare per-case verdicts. A divergence is a finding, not something to hide."""
    disagreements = []
    for case in local:
        key = f"{case['principal_id']}|{case['context']}"
        ms = microsoft.get(key)
        if ms is None:
            continue
        if _normalise(ms) != _normalise(case["to"]):
            disagreements.append({**case, "microsoft_verdict": ms})
    sampled = sum(1 for c in local if f"{c['principal_id']}|{c['context']}" in microsoft)
    rate = (len(disagreements) / sampled) if sampled else 0.0
    return {
        "sampled": sampled,
        "disagreements": len(disagreements),
        "rate": round(rate, 3),
        "cases": disagreements[:25],
        "confidence": "verified" if sampled and not disagreements
        else ("modelled_unverified" if rate > 0.1 else "modelled"),
    }


def _normalise(verdict: str) -> str:
    v = (verdict or "").lower()
    if v in ("blocked", "blocked_effective", "failure", "notapplied_blocked"):
        return "blocked"
    if v in ("challenged", "success_with_mfa", "mfarequired"):
        return "challenged"
    return "granted"
