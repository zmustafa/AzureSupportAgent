"""Azure Policy advisors — deterministic analyzers + AI assistants.

Deterministic (pure functions over collected inventory + compliance):
  * promote_to_deny_candidates — audit policies that are 100% compliant → safe to enforce.
  * exemption_hygiene — expired / never-expiring / unjustified / over-broad exemptions.
  * remediation_gaps — DINE/modify assignments missing a managed identity (RBAC) → silent.
  * conflicts — duplicate / overlapping assignments at different scopes.
  * tag_gaps — resources missing required governance tags (needs a live query).

AI (grounded LLM JSON/text completions, no tools):
  * authoring — natural language → policy JSON (with the right aliases).
  * explain — policy JSON → plain-English explanation.
  * triage — a deny error → the offending assignment/condition + fix options.
  * whatif_predicate — a candidate policy → a Resource Graph predicate to count impact.
  * blast_radius — narrative + risk score for a what-if result.
  * coverage_proposals — concrete built-in policies to close baseline gaps.
  * safe_rollout — a staged DoNotEnforce→audit→deny plan with selectors/exemptions.
  * drift_reconcile — diff live vs IaC source and propose reconciliation.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.agent.factory import build_provider
from app.core.utils import safe_json_parse

logger = logging.getLogger("app.policy.advisors")

_DINE_EFFECTS = {"deployifnotexists", "modify"}


# =========================================================================== AI helpers
async def _complete_text(system: str, user: str) -> str:
    provider = build_provider()
    text = ""
    async for ev in provider.stream(
        [{"role": "system", "content": system}, {"role": "user", "content": user}], None
    ):
        if ev.type == "token":
            text += ev.text
    return text.strip()


async def _complete_json(system: str, user: str) -> Any:
    raw = await _complete_text(system, user)
    t = raw.strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
        if m:
            t = m.group(1)
    if not t.startswith("{") and not t.startswith("["):
        m = re.search(r"(\{.*\}|\[.*\])", t, re.DOTALL)
        if m:
            t = m.group(1)
    return safe_json_parse(t, default=None)


# =========================================================================== deterministic
def promote_to_deny_candidates(
    assignments: list[dict[str, Any]], compliance: dict[str, Any]
) -> list[dict[str, Any]]:
    """Audit/auditIfNotExists assignments with zero non-compliant resources → flipping to
    deny would block nothing today. The headline 'safe to enforce' advisor.

    When compliance data is unavailable we still surface audit assignments but flag them
    as ``compliance_unknown`` so the UI can prompt a compliance scan first."""
    by_asg = compliance.get("by_assignment", {}) if compliance else {}
    have_compliance = bool(compliance and compliance.get("available"))
    out: list[dict[str, Any]] = []
    for a in assignments:
        eff = (a.get("effect") or "").lower()
        if eff not in ("audit", "auditifnotexists", "parameterized", ""):
            continue
        if (a.get("enforcement_mode") or "Default") != "Default":
            continue
        comp = by_asg.get((a.get("id") or "").lower())
        nc = int(comp.get("non_compliant_resources", 0)) if comp else 0
        safe = have_compliance and nc == 0
        out.append({
            "assignment_id": a.get("id"),
            "display_name": a.get("display_name"),
            "scope_label": a.get("scope_label"),
            "current_effect": a.get("effect") or "audit",
            "non_compliant_resources": nc,
            "compliance_unknown": not have_compliance,
            "safe_to_promote": safe,
            "reason": (
                "100% compliant across scope — promoting to deny blocks nothing today."
                if safe else
                "Run a compliance scan to confirm zero non-compliant resources before promoting."
                if not have_compliance else
                f"{nc} non-compliant resource(s) would be blocked — remediate first."
            ),
        })
    # Safe candidates first, then by fewest blockers.
    out.sort(key=lambda x: (not x["safe_to_promote"], x["compliance_unknown"], x["non_compliant_resources"]))
    return out


def exemption_hygiene(exemptions: list[dict[str, Any]], now_iso: str) -> dict[str, Any]:
    """Classify every exemption: expired, expiring-soon (<30d), never-expiring,
    unjustified (no description), or healthy. Returns grouped lists + per-item flags."""
    from datetime import datetime, timedelta, timezone

    def _parse(ts: str):
        if not ts:
            return None
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
        # Azure timestamps without an offset are UTC; normalize so naive values never get
        # compared against the tz-aware ``now`` below (which would raise TypeError).
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    now = datetime.now(timezone.utc)
    soon = now + timedelta(days=30)
    items: list[dict[str, Any]] = []
    buckets = {"expired": 0, "expiring_soon": 0, "never_expires": 0, "unjustified": 0, "healthy": 0}
    for e in exemptions:
        exp = _parse(e.get("expires_on", ""))
        flags: list[str] = []
        if exp is None:
            flags.append("never_expires")
        elif exp < now:
            flags.append("expired")
        elif exp < soon:
            flags.append("expiring_soon")
        if not (e.get("description") or "").strip():
            flags.append("unjustified")
        if e.get("scope_kind") in ("managementGroup", "subscription") and e.get("category") == "Waiver":
            flags.append("broad_scope")
        status = "healthy"
        if "expired" in flags:
            status, buckets["expired"] = "expired", buckets["expired"] + 1
        elif "expiring_soon" in flags:
            status, buckets["expiring_soon"] = "expiring_soon", buckets["expiring_soon"] + 1
        elif "never_expires" in flags:
            status, buckets["never_expires"] = "never_expires", buckets["never_expires"] + 1
        else:
            buckets["healthy"] += 1
        if "unjustified" in flags:
            buckets["unjustified"] += 1
        items.append({
            "id": e.get("id"), "display_name": e.get("display_name"),
            "scope_label": e.get("scope_label"), "category": e.get("category"),
            "expires_on": e.get("expires_on"), "description": e.get("description"),
            "flags": flags, "status": status,
        })
    items.sort(key=lambda x: {"expired": 0, "expiring_soon": 1, "never_expires": 2, "healthy": 3}.get(x["status"], 4))
    return {"items": items, "buckets": buckets, "total": len(items)}


def remediation_gaps(assignments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """deployIfNotExists / modify assignments that lack a managed identity — their
    remediation can never run (and likely lack the RBAC to fix resources). Silent failure."""
    out: list[dict[str, Any]] = []
    for a in assignments:
        eff = (a.get("effect") or "").lower()
        is_dine = eff in _DINE_EFFECTS or (eff == "parameterized" and a.get("is_initiative"))
        no_identity = (a.get("identity_type") or "None") == "None" or not a.get("identity_principal_id")
        if is_dine and no_identity:
            out.append({
                "assignment_id": a.get("id"),
                "display_name": a.get("display_name"),
                "scope_label": a.get("scope_label"),
                "effect": a.get("effect"),
                "is_initiative": a.get("is_initiative"),
                "issue": "No managed identity — remediation tasks can't run and resources stay unfixed.",
                "fix": "Assign a system-assigned identity and grant it the policy's roleDefinitionIds at the scope.",
            })
    return out


def detect_conflicts(assignments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Find assignments of the same policy/initiative bound at more than one scope
    (redundant inheritance), and same-scope duplicates by definition. A consolidation hint."""
    by_def: dict[str, list[dict[str, Any]]] = {}
    for a in assignments:
        key = (a.get("policy_definition_id") or "").lower()
        if key:
            by_def.setdefault(key, []).append(a)
    out: list[dict[str, Any]] = []
    for def_id, group in by_def.items():
        if len(group) < 2:
            continue
        scopes = {g.get("scope") for g in group}
        same_scope = len(scopes) < len(group)
        out.append({
            "policy_definition_id": def_id,
            "definition_name": group[0].get("definition_name") or def_id.split("/")[-1],
            "assignment_count": len(group),
            "scopes": [{"id": g.get("id"), "label": g.get("scope_label"), "effect": g.get("effect")} for g in group],
            "kind": "duplicate_same_scope" if same_scope else "redundant_inheritance",
            "hint": (
                "Same policy assigned twice at the same scope — remove the duplicate."
                if same_scope else
                "Assigned at multiple scopes in one hierarchy — keep the highest scope and drop the rest."
            ),
        })
    out.sort(key=lambda x: -x["assignment_count"])
    return out


def summarize_for_snapshot(inventory: dict[str, Any], compliance: dict[str, Any]) -> dict[str, Any]:
    """Compact snapshot summary for trend/drift storage."""
    by_effect: dict[str, int] = {}
    by_enforce: dict[str, int] = {}
    for a in inventory.get("assignments", []):
        eff = a.get("effect") or "unknown"
        by_effect[eff] = by_effect.get(eff, 0) + 1
        en = a.get("enforcement_mode") or "Default"
        by_enforce[en] = by_enforce.get(en, 0) + 1
    return {
        "counts": inventory.get("counts", {}),
        "by_effect": by_effect,
        "by_enforcement": by_enforce,
        "compliance": {
            "available": bool(compliance.get("available")),
            "total_non_compliant_resources": compliance.get("total_non_compliant_resources", 0),
            "subscriptions_scanned": compliance.get("subscriptions_scanned", 0),
        },
    }


def diff_snapshots(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """Drift between two snapshot summaries (counts + non-compliant delta)."""
    def _c(s, k):
        return int((s.get("counts", {}) or {}).get(k, 0))
    cur, prev = current, previous
    return {
        "assignments_delta": _c(cur, "assignments") - _c(prev, "assignments"),
        "exemptions_delta": _c(cur, "exemptions") - _c(prev, "exemptions"),
        "definitions_delta": _c(cur, "definitions") - _c(prev, "definitions"),
        "non_compliant_delta": (
            int(cur.get("compliance", {}).get("total_non_compliant_resources", 0))
            - int(prev.get("compliance", {}).get("total_non_compliant_resources", 0))
        ),
    }


# =========================================================================== AI advisors
_AUTHOR_SYSTEM = """\
You are an Azure Policy authoring expert. Convert the user's plain-English intent into a \
single valid Azure Policy DEFINITION. You know the correct resource provider types and \
the exact ARG/policy ALIASES (e.g. Microsoft.Network/networkInterfaces/ipConfigurations[*].publicIPAddress.id, \
Microsoft.Storage/storageAccounts/supportsHttpsTrafficOnly, \
Microsoft.Compute/virtualMachines/storageProfile.osDisk.managedDisk.id). Prefer a \
parameterized `effect` with an allowedValues list and a sensible default. Use `field`/\
`anyOf`/`allOf`/`not` correctly. If the intent implies an exception scope (e.g. "except \
the DMZ RG"), express it in the rule logic where possible and note that a notScopes/\
exemption is the cleaner mechanism in `notes`.

Respond with ONLY a JSON object (no prose, no code fence):
{
  "display_name": "Short policy title",
  "description": "What it does and why",
  "mode": "All | Indexed",
  "recommended_effect": "audit | deny | deployIfNotExists | modify | append",
  "policy_definition": { "properties": { "displayName": "...", "mode": "...", "parameters": {...}, "policyRule": { "if": {...}, "then": { "effect": "[parameters('effect')]" } } } },
  "aliases_used": ["..."],
  "notes": "Caveats, exception handling, and rollout advice (audit first)."
}
"""

_WHATIF_SYSTEM = """\
You translate an Azure Policy rule into a SINGLE Azure Resource Graph (KQL) boolean \
predicate over the `resources` table that matches the resources the policy's DENY would \
affect (i.e. the non-compliant set). Use real ARG columns: `type`, `location`, \
`resourceGroup`, `subscriptionId`, `tags`, `kind`, `sku`, and `properties.*` paths. \
Return ONLY the boolean expression text that goes after `| where` — no table name, no \
projection, no pipes, no comments. If you cannot express it safely, return the single \
word: UNSUPPORTED.
Example output: type =~ 'microsoft.storage/storageaccounts' and tobool(properties.supportsHttpsTrafficOnly) == false
"""

_EXPLAIN_SYSTEM = """\
You are an Azure Policy expert. Explain the provided policy (or initiative) JSON in clear, \
non-jargon English for a cloud/security architect. Cover: (1) what it targets, (2) the \
effect and what happens on a match, (3) each parameter and sane values, (4) notable edge \
cases or gaps, and (5) a one-line rollout recommendation. Use short markdown with \
headings/bullets. No code fences.
"""

_TRIAGE_SYSTEM = """\
You are an Azure Policy incident responder. Given a deployment/Activity-Log error caused \
by a policy DENY (and, when provided, the list of candidate assignments in scope), \
pinpoint the most likely offending policy and condition, explain WHY the deployment was \
blocked, and give three resolution options ranked by safety: (a) fix the resource to \
comply, (b) create a narrowly-scoped, time-boxed exemption with justification, (c) adjust \
the policy/parameters. Be specific about the property at fault.

Respond with ONLY JSON:
{
  "likely_policy": "best-guess display name or definition id",
  "blocked_property": "the resource property/value that violated the rule",
  "explanation": "1-3 sentences",
  "options": [
    {"action": "fix_resource", "summary": "...", "risk": "low|medium|high", "steps": "..."},
    {"action": "exempt", "summary": "...", "risk": "...", "steps": "..."},
    {"action": "adjust_policy", "summary": "...", "risk": "...", "steps": "..."}
  ]
}
"""

_ROLLOUT_SYSTEM = """\
You are an Azure Policy rollout strategist. Given a policy intent (and optional candidate \
definition), produce a SAFE staged rollout plan that minimizes breakage: \
Stage 1 DoNotEnforce (dry-run) to gather compliance, Stage 2 audit, Stage 3 deny — using \
resourceSelectors (region/ring rings) and overrides for staged scope, and pre-seeded \
notScopes/exemptions for known exceptions. Include exit criteria per stage.

Respond with ONLY JSON:
{
  "summary": "one-line strategy",
  "stages": [
    {"name": "Stage 1 — Dry run", "enforcement_mode": "DoNotEnforce", "effect": "deny", "selectors": "e.g. region in (eastus)", "exit_criteria": "..."},
    ...
  ],
  "recommended_exemptions": [{"scope": "...", "reason": "...", "expires_in_days": 90}],
  "risks": ["..."]
}
"""

_COVERAGE_SYSTEM = """\
You are an Azure governance advisor. For each MISSING baseline control provided, name the \
specific Azure BUILT-IN policy (or initiative) that implements it, the recommended effect, \
the scope to assign it at, and why it matters. Prefer well-known built-ins.

Respond with ONLY JSON: {"proposals": [{"control_id": "...", "control_title": "...", "builtin_policy": "exact built-in display name", "effect": "audit|deny|...", "assign_at": "Management group root | Subscription", "why": "1 sentence"}]}
"""

_DRIFT_SYSTEM = """\
You are a policy-as-code reviewer. Compare the LIVE policy assignments (JSON list) with \
the declared SOURCE OF TRUTH (IaC text). Identify drift: assignments that exist live but \
not in code (portal-created), assignments in code but missing live, and parameter/effect \
mismatches. Then give reconciliation guidance.

Respond with ONLY JSON:
{
  "in_sync": true|false,
  "live_only": [{"name": "...", "scope": "...", "note": "likely portal-created — add to code or remove"}],
  "code_only": [{"name": "...", "note": "declared but not deployed"}],
  "mismatched": [{"name": "...", "difference": "..."}],
  "recommendation": "1-3 sentences on how to reconcile"
}
"""

_TAGGOV_SYSTEM = """\
You are an Azure tag-governance advisor. Given a list of required tag keys and a sample of \
resources missing them, propose: (1) a 'modify' policy to append/inherit each tag from the \
resource group, (2) an 'audit' policy to flag non-compliance, and rollout advice.

Respond with ONLY JSON: {"summary": "...", "modify_policies": [{"tag": "...", "approach": "inherit from RG | append default", "policy_hint": "built-in name"}], "audit_policy": "built-in name", "notes": "..."}
"""


async def author_policy(intent: str) -> dict[str, Any] | None:
    return await _complete_json(_AUTHOR_SYSTEM, f"Intent: {intent.strip()[:1500]}")


async def explain_policy(policy_json: str) -> str:
    return await _complete_text(_EXPLAIN_SYSTEM, f"POLICY JSON:\n{policy_json.strip()[:12000]}")


async def whatif_predicate(policy_json: str) -> str:
    """Return a Resource Graph predicate (or '' when UNSUPPORTED)."""
    pred = await _complete_text(_WHATIF_SYSTEM, f"POLICY:\n{policy_json.strip()[:8000]}")
    pred = pred.strip().strip("`").strip()
    if not pred or pred.upper().startswith("UNSUPPORTED"):
        return ""
    # Strip a leading "| where" if the model added it.
    pred = re.sub(r"^\|?\s*where\s+", "", pred, flags=re.IGNORECASE).strip()
    return pred[:1200]


async def blast_radius(display_name: str, count: int, sample: list[dict[str, Any]]) -> dict[str, Any] | None:
    rgs = sorted({s.get("resourceGroup", "") for s in sample if s.get("resourceGroup")})
    user = (
        f"Candidate deny policy: {display_name}\n"
        f"Resources it would currently block: {count}\n"
        f"Affected resource groups (sample): {', '.join(rgs[:20]) or '(none)'}\n"
        f"Sample resources: {json.dumps(sample[:15], separators=(',', ':'))}"
    )
    system = (
        "You assess the blast radius of enforcing an Azure Policy deny. Given the count and "
        "sample of currently-non-compliant resources, return ONLY JSON: "
        '{"risk_score": 0-100, "risk_level": "low|medium|high|critical", "summary": "1-2 sentences", '
        '"teams_or_rgs_impacted": ["..."], "recommendation": "promote now | audit first | remediate first"}'
    )
    return await _complete_json(system, user)


async def triage_deny(error_text: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    cand = [
        {"display_name": c.get("display_name"), "effect": c.get("effect"), "scope": c.get("scope_label"),
         "definition": c.get("definition_name")}
        for c in candidates[:40]
    ]
    user = (
        f"DENY ERROR:\n{error_text.strip()[:4000]}\n\n"
        f"CANDIDATE ASSIGNMENTS IN SCOPE:\n{json.dumps(cand, separators=(',', ':'))}"
    )
    return await _complete_json(_TRIAGE_SYSTEM, user)


async def rollout_plan(intent: str, policy_json: str = "") -> dict[str, Any] | None:
    user = f"Intent: {intent.strip()[:1200]}"
    if policy_json.strip():
        user += f"\n\nCandidate policy JSON:\n{policy_json.strip()[:6000]}"
    return await _complete_json(_ROLLOUT_SYSTEM, user)


# --------------------------------------------------------------------------- simulator
_SIM_ROLLOUT_SYSTEM = """\
You are an Azure Policy rollout strategist running a CHANGE SIMULATION for a cloud admin. \
You are given the change being made (deploy a new policy, or promote an existing one), the \
TARGET scope, the TARGET effect and enforcement mode, and the measured impact (how many \
live resources would be affected, with a sample and affected resource groups). Produce a \
SAFE staged rollout plan tailored to THIS effect and scope that minimizes breakage.

Guidance by target effect:
- deny / denyAction: stage DoNotEnforce (dry-run) -> audit -> deny, ringed by region/RG via \
  resourceSelectors; pre-seed notScopes/exemptions for the sampled exceptions.
- deployIfNotExists / modify: ensure a managed identity + roleDefinitionIds exist first; \
  plan a remediation task; start in DoNotEnforce to preview, then enforce.
- audit / auditIfNotExists: low risk; can assign directly but still suggest reviewing the \
  first compliance scan before relying on it.
- append / disabled: note the practical effect and any caveats.

Respond with ONLY JSON:
{
  "summary": "one-line strategy specific to this effect + scope",
  "impact_interpretation": "what the measured impact MEANS for this effect (e.g. 'N resources would be blocked on next write' / 'N resources would trigger remediation')",
  "stages": [
    {"name": "Stage 1 — Dry run", "enforcement_mode": "DoNotEnforce", "effect": "deny", "selectors": "e.g. region in (eastus)", "exit_criteria": "...", "duration": "e.g. 1-2 weeks"}
  ],
  "prerequisites": ["e.g. assign a managed identity with <role> at <scope>"],
  "recommended_exemptions": [{"scope": "...", "reason": "...", "expires_in_days": 90}],
  "risks": ["..."],
  "go_no_go": "go | caution | hold",
  "rationale": "1-2 sentences on the go/no-go call"
}
"""


async def simulate_rollout(context: dict[str, Any]) -> dict[str, Any] | None:
    """Produce a tailored, scope+effect-aware staged rollout plan for a simulated change.

    ``context`` carries the change description, target scope/effect/enforcement, and the
    measured impact (count + sample + affected RGs + compliance, when known)."""
    return await _complete_json(_SIM_ROLLOUT_SYSTEM, json.dumps(context, separators=(",", ":")))


def assignment_json_template(
    *, display_name: str, definition_id: str, scope: str, effect: str, enforcement_mode: str
) -> dict[str, Any]:
    """A copy-ready ARM policy ASSIGNMENT body for the target state. Read-only artifact —
    the UI shows it for the admin to apply themselves; nothing here touches Azure."""
    props: dict[str, Any] = {
        "displayName": display_name,
        "policyDefinitionId": definition_id,
        "enforcementMode": enforcement_mode or "Default",
    }
    # Effect is usually parameterized on built-ins; surface it so the admin can wire it.
    if effect:
        props["parameters"] = {"effect": {"value": effect}}
    return {
        "type": "Microsoft.Authorization/policyAssignments",
        "apiVersion": "2022-06-01",
        "name": (display_name or "assignment")[:64].lower().replace(" ", "-"),
        "scope": scope,
        "properties": props,
        "identity": (
            {"type": "SystemAssigned"}
            if (effect or "").lower() in _DINE_EFFECTS else None
        ),
    }


def az_cli_commands(
    *, name: str, definition_id: str, scope: str, effect: str, enforcement_mode: str, is_initiative: bool
) -> list[str]:
    """Copy-ready ``az policy assignment`` commands for the target state (read-only)."""
    flag = "--policy-set-definition" if is_initiative else "--policy"
    safe_name = (name or "assignment")[:24].lower().replace(" ", "-")
    cmds = [
        f"az policy assignment create --name {safe_name} "
        f"{flag} \"{definition_id}\" --scope \"{scope}\" "
        f"--enforcement-mode {enforcement_mode or 'Default'} "
        f"--params '{{\"effect\":{{\"value\":\"{effect}\"}}}}'"
    ]
    if (effect or "").lower() in _DINE_EFFECTS:
        cmds[0] += " --mi-system-assigned --location eastus"
        cmds.append(
            "# Then grant the assignment's identity the policy's roleDefinitionIds at the scope, "
            "and create a remediation task:\n"
            f"az policy remediation create --name remediate-{safe_name} "
            f"--policy-assignment {safe_name} --scope \"{scope}\""
        )
    return cmds



async def coverage_proposals(baseline_label: str, missing: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not missing:
        return {"proposals": []}
    user = (
        f"Baseline: {baseline_label}\n"
        f"MISSING controls:\n{json.dumps(missing, separators=(',', ':'))}"
    )
    return await _complete_json(_COVERAGE_SYSTEM, user)


async def drift_reconcile(live_assignments: list[dict[str, Any]], iac_content: str, fmt: str) -> dict[str, Any] | None:
    live = [
        {"name": a.get("display_name"), "scope": a.get("scope_label"), "definition": a.get("definition_name"),
         "effect": a.get("effect"), "enforcement": a.get("enforcement_mode")}
        for a in live_assignments[:120]
    ]
    user = (
        f"SOURCE-OF-TRUTH FORMAT: {fmt}\n\n"
        f"LIVE ASSIGNMENTS:\n{json.dumps(live, separators=(',', ':'))}\n\n"
        f"SOURCE OF TRUTH:\n{iac_content.strip()[:20000]}"
    )
    return await _complete_json(_DRIFT_SYSTEM, user)


async def tag_governance(required_tags: list[str], missing_sample: list[dict[str, Any]]) -> dict[str, Any] | None:
    user = (
        f"Required tag keys: {', '.join(required_tags)}\n"
        f"Sample resources missing tags:\n{json.dumps(missing_sample[:20], separators=(',', ':'))}"
    )
    return await _complete_json(_TAGGOV_SYSTEM, user)


# --------------------------------------------------------------------------- finding → policy
_STOP_WORDS = {
    "the", "and", "are", "for", "with", "should", "that", "have", "has", "use", "using",
    "azure", "accounts", "account", "access", "enable", "enabled", "disable", "allow",
    "allows", "permit", "permits", "not", "without", "their", "this", "from", "into",
}


def finding_keywords(title: str, resource_types: list[str]) -> list[str]:
    """Significant keywords for searching built-in policies (title words + resource nouns)."""
    words = re.findall(r"[a-zA-Z][a-zA-Z\-]{2,}", (title or "").lower())
    kws = [w for w in words if w not in _STOP_WORDS]
    for t in resource_types[:3]:
        leaf = t.split("/")[-1]
        if leaf and leaf not in kws:
            kws.append(leaf)
    # De-dup preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for k in kws:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out[:8]


_MATCH_BUILTIN_SYSTEM = """\
You map an Azure governance FINDING to the Azure BUILT-IN policy that enforces it. You are \
given the finding (title, description, the resource types, the detection predicate, and the \
remediation) and a CANDIDATE list of real built-in policy definitions (id + display name + \
category) discovered in the tenant. Pick the SINGLE best candidate that would prevent/flag \
this exact misconfiguration, or return none if no candidate fits. Also recommend the target \
effect for ENFORCEMENT (deny for config gates that should block non-compliant writes; \
deployIfNotExists/modify when the fix is to deploy/alter a setting; audit if deny is too \
risky).

Respond with ONLY JSON:
{
  "matched": true|false,
  "definition_id": "exact id of the chosen candidate, or ''",
  "builtin_display_name": "its display name, or ''",
  "recommended_effect": "deny|denyAction|audit|deployIfNotExists|modify|append",
  "confidence": 0.0,
  "reasoning": "one sentence",
  "custom_needed": true|false
}
"""


async def match_builtin_policy(
    *, title: str, description: str, resource_types: list[str], detection: str,
    remediation: str, candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """AI-pick the real built-in policy definition that enforces a finding (from the
    candidate list resolved off live Azure), with a recommended enforcement effect."""
    cand = [
        {"id": c.get("id"), "display_name": c.get("display_name"), "category": c.get("category"), "effect": c.get("effect")}
        for c in candidates[:20]
    ]
    user = (
        f"FINDING:\n- title: {title}\n- description: {description[:400]}\n"
        f"- resource types: {', '.join(resource_types)}\n"
        f"- detection predicate: {detection[:400]}\n- remediation: {remediation[:300]}\n\n"
        f"CANDIDATE BUILT-IN POLICIES:\n{json.dumps(cand, separators=(',', ':'))}"
    )
    return await _complete_json(_MATCH_BUILTIN_SYSTEM, user)


async def author_policy_from_finding(
    *, title: str, detection: str, remediation: str, resource_types: list[str]
) -> dict[str, Any] | None:
    """Fallback: author a CUSTOM policy that enforces a finding, from its detection
    predicate + remediation (used when no built-in matches)."""
    intent = (
        f"Create a policy that enforces this control: '{title}'. "
        f"It should flag/deny resources of type(s) {', '.join(resource_types)} matching this "
        f"detection logic: {detection}. Remediation guidance: {remediation}. "
        "Default the effect to audit for safe rollout."
    )
    return await author_policy(intent)

