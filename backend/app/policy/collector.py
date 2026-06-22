"""Live Azure Policy collector (read-only).

Pulls policy definitions, initiatives (policy set definitions), assignments and
exemptions from Azure Resource Graph (the ``policyresources`` table), derives the scope
hierarchy, resolves the *effective* policy set at any scope (inheritance − exclusions −
exemptions), and best-effort fetches compliance summaries via ``az policy state
summarize``. Everything degrades gracefully when a query is unavailable.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.exec.command_runner import (
    close_sp_session,
    open_sp_session,
    run_kql_capture,
)

logger = logging.getLogger("app.policy.collector")

# Scope id is everything before the policy provider segment.
_SCOPE_RE = re.compile(
    r"^(?P<scope>.*?)/providers/microsoft\.authorization/policy", re.IGNORECASE
)
_MG_RE = re.compile(r"/managementgroups/([^/]+)", re.IGNORECASE)
_SUB_RE = re.compile(r"/subscriptions/([0-9a-fA-F-]{36})", re.IGNORECASE)
_RG_RE = re.compile(r"/resourcegroups/([^/]+)", re.IGNORECASE)


# --------------------------------------------------------------------------- helpers
def _esc(val: str) -> str:
    """Escape single quotes for safe embedding in a KQL string literal."""
    return (val or "").replace("'", "''")


def scope_of(resource_id: str) -> str:
    """The assignment/exemption scope (the id prefix before the policy provider)."""
    if not resource_id:
        return ""
    m = _SCOPE_RE.match(resource_id)
    return (m.group("scope") if m else resource_id).rstrip("/")


def scope_kind(scope_id: str) -> str:
    s = (scope_id or "").lower()
    if "/managementgroups/" in s:
        return "managementGroup"
    if "/resourcegroups/" in s:
        return "resourceGroup"
    if "/subscriptions/" in s:
        return "subscription"
    return "tenant"


def scope_label(scope_id: str, sub_names: dict[str, str] | None = None) -> str:
    """A short human label for a scope id. When ``sub_names`` (subscription-id → display
    name) is provided, subscription scopes resolve to the full subscription name."""
    if not scope_id:
        return "Tenant root"
    mg = _MG_RE.search(scope_id)
    if mg and "/resourcegroups/" not in scope_id.lower() and "/subscriptions/" not in scope_id.lower():
        return f"MG: {mg.group(1)}"
    rg = _RG_RE.search(scope_id)
    sub = _SUB_RE.search(scope_id)
    if rg and sub:
        return f"RG: {rg.group(1)}"
    if sub:
        sid = sub.group(1).lower()
        name = (sub_names or {}).get(sid)
        return f"Sub: {name}" if name else f"Sub: {sub.group(1)[:8]}…"
    return scope_id


def scope_depth(scope_id: str) -> int:
    """Hierarchy depth (tenant=0, MG=1, sub=2, rg=3) — used for inheritance ordering."""
    k = scope_kind(scope_id)
    return {"tenant": 0, "managementGroup": 1, "subscription": 2, "resourceGroup": 3}.get(k, 2)


def _short_def_id(def_id: str) -> str:
    """Last segment (the definition/initiative name/guid) of a policy(set)definition id."""
    return (def_id or "").rstrip("/").split("/")[-1]


def effect_of(definition: dict[str, Any]) -> str:
    """Best-effort static effect of a definition (may be a parameter reference)."""
    eff = definition.get("effect") or ""
    if isinstance(eff, str) and eff.startswith("["):
        # e.g. "[parameters('effect')]" — surface as the parameter's default if known.
        params = definition.get("parameters") or {}
        m = re.search(r"parameters\('([^']+)'\)", eff)
        if m and isinstance(params, dict):
            p = params.get(m.group(1)) or {}
            return str((p.get("defaultValue") or "parameterized")).lower()
        return "parameterized"
    return str(eff or "").lower()


# --------------------------------------------------------------------------- ARG runner
async def _arg(kql: str, connection: dict[str, Any] | None, session_dir: str | None) -> tuple[list[dict[str, Any]], str]:
    """Run a Resource Graph query, return (rows, error). Salvage-parses a truncated capture so a
    big policy result is never silently turned into zero rows on a REST connection."""
    from app.exec.command_runner import KQL_RESOURCE_CAPTURE_BYTES, parse_kql_rows

    res = await run_kql_capture(kql, connection, output="json", session_config_dir=session_dir,
                                max_bytes=KQL_RESOURCE_CAPTURE_BYTES)
    if not res.ok:
        return [], (res.error or res.stderr or "Query failed.").strip()[:400]
    return parse_kql_rows(res.stdout), ""


# --------------------------------------------------------------------------- queries
_DEFINITIONS_KQL = """
policyresources
| where type =~ 'microsoft.authorization/policydefinitions'
| project id, name, displayName=tostring(properties.displayName),
    policyType=tostring(properties.policyType), mode=tostring(properties.mode),
    category=tostring(properties.metadata.category),
    version=tostring(properties.version),
    effect=tostring(properties.policyRule.then.effect),
    description=tostring(properties.description),
    parameters=properties.parameters
| limit 2000
"""

_INITIATIVES_KQL = """
policyresources
| where type =~ 'microsoft.authorization/policysetdefinitions'
| extend defs=properties.policyDefinitions
| project id, name, displayName=tostring(properties.displayName),
    policyType=tostring(properties.policyType),
    category=tostring(properties.metadata.category),
    description=tostring(properties.description),
    policyCount=array_length(defs), policyDefinitions=defs
| limit 1000
"""

_ASSIGNMENTS_KQL = """
policyresources
| where type =~ 'microsoft.authorization/policyassignments'
| project id, name, displayName=tostring(properties.displayName),
    policyDefinitionId=tostring(properties.policyDefinitionId),
    enforcementMode=tostring(properties.enforcementMode),
    description=tostring(properties.description),
    notScopes=properties.notScopes, parameters=properties.parameters,
    identityType=tostring(identity.type),
    identityPrincipalId=tostring(identity.principalId),
    location=location
| limit 2000
"""

_EXEMPTIONS_KQL = """
policyresources
| where type =~ 'microsoft.authorization/policyexemptions'
| project id, name, displayName=tostring(properties.displayName),
    exemptionCategory=tostring(properties.exemptionCategory),
    expiresOn=tostring(properties.expiresOn),
    policyAssignmentId=tostring(properties.policyAssignmentId),
    description=tostring(properties.description),
    refs=properties.policyDefinitionReferenceIds
| limit 1000
"""

# Subscription id → display name, so policy scopes can resolve to readable subscription names.
_SUBSCRIPTIONS_KQL = """
resourcecontainers
| where type =~ 'microsoft.resources/subscriptions'
| project subscriptionId, name
| limit 1000
"""


def _norm_assignment(row: dict[str, Any], def_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    scope = scope_of(row.get("id", ""))
    def_id = row.get("policyDefinitionId", "") or ""
    is_initiative = "/policysetdefinitions/" in def_id.lower()
    definition = def_by_id.get(def_id.lower(), {})
    ns = row.get("notScopes")
    not_scopes = ns if isinstance(ns, list) else []
    return {
        "id": row.get("id", ""),
        "name": row.get("name", ""),
        "display_name": row.get("displayName") or row.get("name", ""),
        "scope": scope,
        "scope_kind": scope_kind(scope),
        "scope_label": scope_label(scope),
        "policy_definition_id": def_id,
        "definition_name": definition.get("display_name") or _short_def_id(def_id),
        "is_initiative": is_initiative,
        "enforcement_mode": (row.get("enforcementMode") or "Default"),
        "effect": effect_of(definition) if definition else "",
        "category": definition.get("category", ""),
        "description": row.get("description", ""),
        "not_scopes": [str(x) for x in not_scopes],
        "identity_type": row.get("identityType") or "None",
        "identity_principal_id": row.get("identityPrincipalId") or "",
        "location": row.get("location") or "",
        "parameters": row.get("parameters") if isinstance(row.get("parameters"), dict) else {},
    }


# --------------------------------------------------------------------------- public API
async def collect_inventory(connection: dict[str, Any] | None) -> dict[str, Any]:
    """Pull the full policy inventory from Resource Graph. Read-only.

    Returns {definitions, initiatives, assignments, exemptions, errors}. Each list is
    normalized for the UI. ``errors`` collects any per-query failure messages.
    """
    session_dir, login_err = await open_sp_session(connection)
    errors: list[str] = []
    if login_err:
        errors.append(login_err)
    try:
        defs_rows, e1 = await _arg(_DEFINITIONS_KQL, connection, session_dir)
        sets_rows, e2 = await _arg(_INITIATIVES_KQL, connection, session_dir)
        asg_rows, e3 = await _arg(_ASSIGNMENTS_KQL, connection, session_dir)
        exm_rows, e4 = await _arg(_EXEMPTIONS_KQL, connection, session_dir)
        sub_rows, _e5 = await _arg(_SUBSCRIPTIONS_KQL, connection, session_dir)
        for e in (e1, e2, e3, e4):
            if e:
                errors.append(e)
    finally:
        close_sp_session(session_dir)

    # Subscription id (lowercase) → display name, used to resolve readable scope labels.
    sub_names = {
        str(r.get("subscriptionId", "")).lower(): (r.get("name") or r.get("subscriptionId", ""))
        for r in sub_rows
        if r.get("subscriptionId")
    }

    definitions = [
        {
            "id": r.get("id", ""),
            "name": r.get("name", ""),
            "display_name": r.get("displayName") or r.get("name", ""),
            "policy_type": r.get("policyType") or "Custom",
            "mode": r.get("mode") or "All",
            "category": r.get("category") or "Uncategorized",
            "version": r.get("version") or "",
            "effect": effect_of(r),
            "description": r.get("description", ""),
            "parameters": r.get("parameters") if isinstance(r.get("parameters"), dict) else {},
        }
        for r in defs_rows
    ]
    def_by_id = {d["id"].lower(): d for d in definitions if d.get("id")}

    initiatives = [
        {
            "id": r.get("id", ""),
            "name": r.get("name", ""),
            "display_name": r.get("displayName") or r.get("name", ""),
            "policy_type": r.get("policyType") or "Custom",
            "category": r.get("category") or "Uncategorized",
            "description": r.get("description", ""),
            "policy_count": int(r.get("policyCount") or 0),
        }
        for r in sets_rows
    ]
    # Initiatives are also valid assignment targets — index them too.
    for ini in initiatives:
        def_by_id.setdefault(ini["id"].lower(), {"display_name": ini["display_name"], "category": ini["category"]})

    assignments = [_norm_assignment(r, def_by_id) for r in asg_rows]
    # Resolve subscription scopes to readable names (assignments).
    for a in assignments:
        a["scope_label"] = scope_label(a["scope"], sub_names)

    exemptions = []
    for r in exm_rows:
        refs = r.get("refs")
        ex_scope = scope_of(r.get("id", ""))
        exemptions.append({
            "id": r.get("id", ""),
            "name": r.get("name", ""),
            "display_name": r.get("displayName") or r.get("name", ""),
            "scope": ex_scope,
            "scope_kind": scope_kind(ex_scope),
            "scope_label": scope_label(ex_scope, sub_names),
            "category": r.get("exemptionCategory") or "Waiver",
            "expires_on": r.get("expiresOn") or "",
            "policy_assignment_id": r.get("policyAssignmentId") or "",
            "description": r.get("description", ""),
            "reference_ids": [str(x) for x in refs] if isinstance(refs, list) else [],
        })

    return {
        "definitions": definitions,
        "initiatives": initiatives,
        "assignments": assignments,
        "exemptions": exemptions,
        "subscription_names": sub_names,
        "errors": errors,
        "counts": {
            "definitions": len(definitions),
            "custom_definitions": sum(1 for d in definitions if d["policy_type"] == "Custom"),
            "initiatives": len(initiatives),
            "assignments": len(assignments),
            "exemptions": len(exemptions),
        },
    }


def empty_inventory() -> dict[str, Any]:
    """The canonical empty policy inventory (no Azure round-trip), shaped exactly like
    ``collect_inventory``'s result. Used for the 'not loaded yet' page-visit response so a
    first visit to /policy never triggers a (slow) Azure scan — only Refresh / Scan collects."""
    return {
        "definitions": [],
        "initiatives": [],
        "assignments": [],
        "exemptions": [],
        "subscription_names": {},
        "errors": [],
        "counts": {
            "definitions": 0,
            "custom_definitions": 0,
            "initiatives": 0,
            "assignments": 0,
            "exemptions": 0,
        },
    }


def build_scope_tree(assignments: list[dict[str, Any]], exemptions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group assignments + exemptions by scope into a flat, depth-ordered list."""
    by_scope: dict[str, dict[str, Any]] = {}
    for a in assignments:
        node = by_scope.setdefault(a["scope"], {
            "scope": a["scope"], "kind": a["scope_kind"], "label": a["scope_label"],
            "depth": scope_depth(a["scope"]), "assignments": 0, "exemptions": 0,
        })
        node["assignments"] += 1
    for e in exemptions:
        node = by_scope.setdefault(e["scope"], {
            "scope": e["scope"], "kind": e["scope_kind"], "label": e["scope_label"],
            "depth": scope_depth(e["scope"]), "assignments": 0, "exemptions": 0,
        })
        node["exemptions"] += 1
    return sorted(by_scope.values(), key=lambda n: (n["depth"], n["label"]))


def resolve_effective(
    scope_id: str,
    assignments: list[dict[str, Any]],
    exemptions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Effective assignments at ``scope_id`` = every assignment at this scope or any
    ancestor, minus those whose notScopes cover this scope, annotated with exemptions.

    Pure function over the already-collected inventory (no extra Azure calls)."""
    target = (scope_id or "").lower().rstrip("/")
    exm_by_assignment: dict[str, list[dict[str, Any]]] = {}
    for e in exemptions:
        exm_by_assignment.setdefault((e.get("policy_assignment_id") or "").lower(), []).append(e)

    effective: list[dict[str, Any]] = []
    for a in assignments:
        a_scope = (a.get("scope") or "").lower().rstrip("/")
        # Inherited if the target scope is at or below the assignment's scope.
        inherited = target == a_scope or target.startswith(a_scope + "/")
        if not inherited:
            continue
        # notScopes exclusion: target excluded if it is at/below any notScope.
        excluded = any(
            target == ns.lower().rstrip("/") or target.startswith(ns.lower().rstrip("/") + "/")
            for ns in a.get("not_scopes", [])
        )
        if excluded:
            continue
        exms = exm_by_assignment.get((a.get("id") or "").lower(), [])
        effective.append({
            **a,
            "inherited_from": a["scope_label"] if a_scope != target else "(this scope)",
            "is_inherited": a_scope != target,
            "exemptions": exms,
        })
    effective.sort(key=lambda x: (not x["is_inherited"], x["display_name"]))
    return {
        "scope": scope_id,
        "scope_label": scope_label(scope_id),
        "scope_kind": scope_kind(scope_id),
        "effective": effective,
        "count": len(effective),
    }


async def compliance_summary(
    connection: dict[str, Any] | None, subscriptions: list[str], *, limit: int = 6
) -> dict[str, Any]:
    """Best-effort per-assignment compliance via the Policy Insights ``summarize`` API.

    Aggregates non-compliant resource/policy counts per assignment across up to
    ``limit`` subscriptions. Degrades to an empty/partial result when Policy Insights is
    unavailable (e.g. brand-new tenant).

    Uses ARM REST with the connection's own token so it works for EVERY connection type
    (service principal, pasted ARM token, managed identity) — not just those with an ambient
    ``az`` login (a pasted-token connection has none, and the CLI would return nothing)."""
    from app.azure.arm import arm_rest
    from app.azure.credentials import get_arm_token

    by_assignment: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    total_nc_resources = 0
    scanned = 0
    token, terr = await get_arm_token(connection or {})
    if not token:
        return {
            "by_assignment": {}, "subscriptions_scanned": 0,
            "total_non_compliant_resources": 0, "available": False,
            "errors": [(terr or "No Azure token for this connection.")[:160]],
        }
    for sub in subscriptions[:limit]:
        url = (
            f"https://management.azure.com/subscriptions/{sub}"
            "/providers/Microsoft.PolicyInsights/policyStates/latest/summarize"
            "?api-version=2019-10-01&$top=200"
        )
        text, err = await arm_rest(token, "POST", url)
        if err:
            errors.append(f"{sub[:8]}…: {err.strip()[:160]}")
            continue
        scanned += 1
        try:
            payload = json.loads(text or "{}")
        except json.JSONDecodeError:
            continue
        values = payload.get("value") or []
        for entry in values:
            for pa in entry.get("policyAssignments") or []:
                aid = (pa.get("policyAssignmentId") or "").lower()
                results = pa.get("results") or {}
                nc_res = int(results.get("nonCompliantResources") or 0)
                nc_pol = int(results.get("nonCompliantPolicies") or 0)
                total_nc_resources += nc_res
                acc = by_assignment.setdefault(aid, {"non_compliant_resources": 0, "non_compliant_policies": 0})
                acc["non_compliant_resources"] += nc_res
                acc["non_compliant_policies"] += nc_pol
    return {
        "by_assignment": by_assignment,
        "subscriptions_scanned": scanned,
        "total_non_compliant_resources": total_nc_resources,
        "available": scanned > 0,
        "errors": errors,
    }


async def discover_subscriptions(connection: dict[str, Any] | None) -> list[str]:
    """Subscription GUIDs visible to the connection (via Resource Graph)."""
    session_dir, _ = await open_sp_session(connection)
    try:
        rows, _ = await _arg(
            "resourcecontainers | where type =~ 'microsoft.resources/subscriptions' "
            "| project subscriptionId | limit 200",
            connection, session_dir,
        )
    finally:
        close_sp_session(session_dir)
    return [r.get("subscriptionId", "") for r in rows if r.get("subscriptionId")]


def scope_predicate(scope_id: str) -> str:
    """A KQL fragment restricting the ``resources`` table to a scope (sub / RG). Returns
    '' for tenant/MG scopes (no per-resource column to filter on without MG expansion)."""
    if not scope_id:
        return ""
    sub = _SUB_RE.search(scope_id)
    rg = _RG_RE.search(scope_id)
    parts: list[str] = []
    if sub:
        parts.append(f"subscriptionId =~ '{_esc(sub.group(1))}'")
    if rg:
        parts.append(f"resourceGroup =~ '{_esc(rg.group(1))}'")
    return " and ".join(parts)


async def count_resources(
    connection: dict[str, Any] | None,
    where_predicate: str,
    *,
    scope_id: str = "",
    project: str = "id, name, type, resourceGroup, subscriptionId, location",
) -> dict[str, Any]:
    """Run a Resource Graph count + sample for a what-if predicate (read-only).

    ``where_predicate`` is a KQL boolean expression over the ``resources`` table (the AI
    produces this from a candidate policy). When ``scope_id`` is given, the query is
    additionally restricted to that subscription/resource-group so the impact reflects the
    target scope, not the whole tenant. Returns {count, sample, scope_predicate, error}."""
    if not where_predicate.strip():
        return {"count": 0, "sample": [], "scope_predicate": "", "error": "Empty predicate."}
    safe = where_predicate.replace("\n", " ").strip()
    # Guard: only allow a where-clause body (no table switch / management operations).
    if re.search(r"\b(externaldata|print|\.show|invoke)\b", safe, re.IGNORECASE):
        return {"count": 0, "sample": [], "scope_predicate": "", "error": "Predicate rejected."}
    sp = scope_predicate(scope_id)
    where = f"({safe})" + (f" and {sp}" if sp else "")
    session_dir, _ = await open_sp_session(connection)
    try:
        count_rows, e1 = await _arg(
            f"resources | where {where} | summarize c=count()", connection, session_dir
        )
        sample_rows, _ = await _arg(
            f"resources | where {where} | project {project} | limit 25", connection, session_dir
        )
    finally:
        close_sp_session(session_dir)
    if e1:
        return {"count": 0, "sample": [], "scope_predicate": sp, "error": e1}
    count = int(count_rows[0].get("c", 0)) if count_rows else 0
    return {"count": count, "sample": sample_rows, "scope_predicate": sp, "error": ""}


async def get_definition_rule(
    connection: dict[str, Any] | None, definition_id: str
) -> dict[str, Any]:
    """Fetch a single policy (or initiative) definition's full body — including the
    ``policyRule`` and parameters — which the inventory query omits for brevity. Used so
    the rollout planner can run a real what-if on an *existing* policy. Read-only."""
    if not definition_id:
        return {"error": "No definition id."}
    is_set = "/policysetdefinitions/" in definition_id.lower()
    table = "policyresources"
    type_filter = (
        "microsoft.authorization/policysetdefinitions" if is_set
        else "microsoft.authorization/policydefinitions"
    )
    kql = (
        f"{table} | where type =~ '{type_filter}' and tolower(id) == tolower('{_esc(definition_id)}') "
        "| project id, name, displayName=tostring(properties.displayName), "
        "mode=tostring(properties.mode), policyRule=properties.policyRule, "
        "parameters=properties.parameters, policyType=tostring(properties.policyType) | limit 1"
    )
    session_dir, _ = await open_sp_session(connection)
    try:
        rows, err = await _arg(kql, connection, session_dir)
    finally:
        close_sp_session(session_dir)
    if err:
        return {"error": err}
    if not rows:
        return {"error": "Definition not found."}
    r = rows[0]
    return {
        "id": r.get("id", ""),
        "display_name": r.get("displayName") or r.get("name", ""),
        "mode": r.get("mode") or "All",
        "is_initiative": is_set,
        "policy_rule": r.get("policyRule"),
        "parameters": r.get("parameters") if isinstance(r.get("parameters"), dict) else {},
        "error": "",
    }


# --------------------------------------------------------------------------- workload scoping
def scope_governs(policy_scope: str, workload_scope_ids: set[str]) -> bool:
    """True if a policy assigned at ``policy_scope`` is *relevant* to a workload — i.e. its
    scope is equal to, an ancestor of (inheritance down to the workload), or a descendant
    of (a more specific policy inside the workload) any of the workload's scope ids.

    The workload scope set is pre-expanded to include the subscriptions' management-group
    ancestor chain, so MG- and tenant-root-inherited policies are correctly attributed."""
    ps = (policy_scope or "").lower().rstrip("/")
    if ps in ("", "/"):  # tenant-root assignment governs everything
        return True
    for ws in workload_scope_ids:
        w = ws.rstrip("/")
        if ps == w or w.startswith(ps + "/") or ps.startswith(w + "/"):
            return True
    return False


async def resolve_workload_scopes(
    workload: dict[str, Any], connection: dict[str, Any] | None
) -> dict[str, Any]:
    """Resolve a workload into the set of normalized scope-id strings that define its
    governance footprint, plus a human summary. Includes the management-group ancestor
    chain of each subscription so inherited MG/tenant policies are recognized. Read-only."""
    from app.architectures.reverse import resolve_scope

    scope = await resolve_scope(workload, connection)
    ids: set[str] = set()
    subs: set[str] = set(scope.get("subs") or set())
    for guid in subs:
        ids.add(f"/subscriptions/{guid}".lower())
    for guid, rg in scope.get("rg_pairs") or set():
        ids.add(f"/subscriptions/{guid}/resourcegroups/{rg}".lower())
    for rid in scope.get("resource_ids") or set():
        ids.add(rid.lower())
    # Management-group nodes directly on the workload.
    for node in workload.get("nodes", []):
        if node.get("kind") == "mg" and node.get("id"):
            ids.add(node["id"].lower())

    # Add the MG ancestor chain for each subscription (so org/MG/tenant policies match).
    ancestor_mgs: set[str] = set()
    if subs:
        session_dir, _ = await open_sp_session(connection)
        try:
            joined = ", ".join(f"'{_esc(s)}'" for s in sorted(subs))
            rows, _ = await _arg(
                "resourcecontainers | where type =~ 'microsoft.resources/subscriptions' "
                f"and subscriptionId in~ ({joined}) "
                "| project ancestors=properties.managementGroupAncestorsChain",
                connection, session_dir,
            )
        finally:
            close_sp_session(session_dir)
        for r in rows:
            for a in r.get("ancestors") or []:
                name = a.get("name") if isinstance(a, dict) else None
                if name:
                    ids.add(f"/providers/microsoft.management/managementgroups/{name}".lower())
                    ancestor_mgs.add(str(a.get("displayName") or name))

    return {
        "scope_ids": ids,
        "subscriptions": sorted(subs),
        "subscription_count": len(subs),
        "resource_group_count": len(scope.get("rg_pairs") or set()),
        "resource_count": len(scope.get("resource_ids") or set()),
        "ancestor_management_groups": sorted(ancestor_mgs),
        "error": scope.get("error", ""),
    }


async def find_builtin_definitions(
    connection: dict[str, Any] | None, keywords: list[str], *, limit: int = 12
) -> list[dict[str, Any]]:
    """Search Azure for BUILT-IN policy definitions whose display name or description
    matches any keyword — used to map an assessment finding to the real built-in policy
    that enforces it (so the generated assignment targets an authentic definition id).
    Read-only; best-effort (returns [] if built-ins aren't visible at the scope)."""
    kws = [k.strip().lower() for k in keywords if k and len(k.strip()) >= 3][:8]
    if not kws:
        return []
    conds = " or ".join(
        f"displayName contains '{_esc(k)}' or tolower(tostring(properties.description)) contains '{_esc(k)}'"
        for k in kws
    )
    kql = (
        "policyresources | where type =~ 'microsoft.authorization/policydefinitions' "
        "and tostring(properties.policyType) == 'BuiltIn' "
        f"| where {conds} "
        "| project id, displayName=tostring(properties.displayName), "
        "category=tostring(properties.metadata.category), "
        "effect=tostring(properties.policyRule.then.effect), "
        "description=tostring(properties.description) "
        f"| limit {limit}"
    )
    session_dir, _ = await open_sp_session(connection)
    try:
        rows, _ = await _arg(kql, connection, session_dir)
    finally:
        close_sp_session(session_dir)
    return [
        {
            "id": r.get("id", ""),
            "display_name": r.get("displayName", ""),
            "category": r.get("category", ""),
            "effect": effect_of(r),
            "description": (r.get("description", "") or "")[:300],
        }
        for r in rows
        if r.get("id")
    ]


