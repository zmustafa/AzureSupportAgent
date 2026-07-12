"""AI for the inventory screen: translate a natural-language query into either a structured
filter (applied client-side over the cached grid) or a read-only Resource Graph KQL query
(executed and validated), and explain a single resource in plain English.

Uses the shared provider (``build_provider().stream``) + ``safe_json_parse`` — never a
``complete_json`` helper. All KQL is validated to be read-only before it can run.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from app.agent.factory import build_provider
from app.core.utils import safe_json_parse

logger = logging.getLogger("app.inventory.ai")

# Hard cap on any single AI call. The configured provider can hang (e.g. GitHub Copilot's
# headless token re-capture takes up to ~45s); bounding it guarantees the request returns
# quickly and degrades to the deterministic local keyword match instead of a "failed to
# fetch" at the browser.
_AI_TIMEOUT_SECONDS = 12.0

# KQL must read from one of these tables and contain no mutating / data-exfil operators.
_ALLOWED_TABLES = ("resources", "resourcecontainers", "policyresources")
_FORBIDDEN = re.compile(r"\b(externaldata|print|invoke|set|append|\.show|\.create|\.drop|\.alter|datatable)\b", re.IGNORECASE)


async def _complete_json(system: str, user: str) -> Any:
    """Stream a completion and parse the JSON object out of it."""
    provider = build_provider()
    text = ""
    async for ev in provider.stream(
        [{"role": "system", "content": system}, {"role": "user", "content": user}], None
    ):
        if ev.type == "token":
            text += ev.text
    t = text.strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
        if m:
            t = m.group(1).strip()
    if not t.startswith("{"):
        m = re.search(r"(\{.*\})", t, re.DOTALL)
        if m:
            t = m.group(1)
    return safe_json_parse(t, default=None)


def validate_kql(kql: str) -> tuple[str, str]:
    """Return (clean_kql, error). Enforces a single read-only query over an allowed table."""
    q = (kql or "").strip().strip("`").strip()
    q = re.sub(r"//[^\n]*", "", q)  # strip line comments
    q = re.sub(r"\s*\n\s*", " ", q).strip()
    if not q:
        return "", "Empty query."
    if ";" in q:
        return "", "Only a single statement is allowed."
    first = q.split("|", 1)[0].strip().split()[0].lower() if q else ""
    if first not in _ALLOWED_TABLES:
        return "", f"Query must start with one of: {', '.join(_ALLOWED_TABLES)}."
    if _FORBIDDEN.search(q):
        return "", "Query contains a disallowed operator."
    return q, ""


# --------------------------------------------------------------------------- local fallback
# Keyword → ARM type map so common searches work WITHOUT the AI (e.g. when the configured
# LLM provider is unavailable). Order matters: more specific phrases first.
_TYPE_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("logic app", "logic apps", "workflow"), "microsoft.logic/workflows"),
    (("storage account",), "microsoft.storage/storageaccounts"),
    (("app service plan", "server farm", "serverfarm"), "microsoft.web/serverfarms"),
    (("function app", "functions ", " function "), "microsoft.web/sites"),
    (("app service", "web app", "website", "web site"), "microsoft.web/sites"),
    (("static web app", "static site"), "microsoft.web/staticsites"),
    (("api connection",), "microsoft.web/connections"),
    (("virtual machine scale set", "vmss", "scale set"), "microsoft.compute/virtualmachinescalesets"),
    (("virtual machine", "virtual machines", " vm ", " vms"), "microsoft.compute/virtualmachines"),
    (("managed disk", "disks"), "microsoft.compute/disks"),
    (("key vault", "keyvault"), "microsoft.keyvault/vaults"),
    (("sql database", "sql db"), "microsoft.sql/servers/databases"),
    (("sql managed instance",), "microsoft.sql/managedinstances"),
    (("sql server",), "microsoft.sql/servers"),
    (("cosmos",), "microsoft.documentdb/databaseaccounts"),
    (("postgres", "postgresql"), "microsoft.dbforpostgresql/flexibleservers"),
    (("mysql",), "microsoft.dbformysql/flexibleservers"),
    (("redis",), "microsoft.cache/redis"),
    (("aks", "kubernetes", "managed cluster"), "microsoft.containerservice/managedclusters"),
    (("container registry", "acr"), "microsoft.containerregistry/registries"),
    (("container app",), "microsoft.app/containerapps"),
    (("cognitive", "openai", "ai service", "ai services"), "microsoft.cognitiveservices/accounts"),
    (("data factory", "adf"), "microsoft.datafactory/factories"),
    (("machine learning", "ml workspace"), "microsoft.machinelearningservices/workspaces"),
    (("cognitive search", "search service", "ai search"), "microsoft.search/searchservices"),
    (("virtual network", "vnet"), "microsoft.network/virtualnetworks"),
    (("network security group", "nsg"), "microsoft.network/networksecuritygroups"),
    (("public ip",), "microsoft.network/publicipaddresses"),
    (("load balancer",), "microsoft.network/loadbalancers"),
    (("application gateway", "app gateway"), "microsoft.network/applicationgateways"),
    (("network interface", "nic"), "microsoft.network/networkinterfaces"),
    (("private endpoint",), "microsoft.network/privateendpoints"),
    (("azure firewall", "firewall"), "microsoft.network/azurefirewalls"),
    (("network watcher",), "microsoft.network/networkwatchers"),
    (("dns zone",), "microsoft.network/dnszones"),
    (("service bus",), "microsoft.servicebus/namespaces"),
    (("event hub",), "microsoft.eventhub/namespaces"),
    (("event grid",), "microsoft.eventgrid/topics"),
    (("log analytics",), "microsoft.operationalinsights/workspaces"),
    (("application insight", "app insight", "appinsights"), "microsoft.insights/components"),
    (("metric alert",), "microsoft.insights/metricalerts"),
    (("action group",), "microsoft.insights/actiongroups"),
    (("automation account",), "microsoft.automation/automationaccounts"),
    (("recovery service", "backup vault", "recovery vault"), "microsoft.recoveryservices/vaults"),
    (("managed identity",), "microsoft.managedidentity/userassignedidentities"),
    (("api management", "apim"), "microsoft.apimanagement/service"),
    (("cdn",), "microsoft.cdn/profiles"),
    (("front door",), "microsoft.network/frontdoors"),
]

# SKU/size keyword → substring(s) matched against sku/size/tier. ("__skip_for_logic__" marks
# "consumption" which, for Logic Apps, is the *type* itself — microsoft.logic/workflows — so we
# must not add an (empty) SKU filter that would yield zero rows.)
_SKU_KEYWORDS: list[tuple[str, list[str]]] = [
    ("d-series", ["Standard_D", "_D"]), ("d series", ["Standard_D", "_D"]),
    ("e-series", ["Standard_E", "_E"]), ("e series", ["Standard_E", "_E"]),
    ("f-series", ["Standard_F", "_F"]), ("f series", ["Standard_F", "_F"]),
    ("b-series", ["Standard_B", "_B"]), ("b series", ["Standard_B", "_B"]),
    ("burstable", ["Standard_B"]),
    ("premium", ["Premium", "_P"]),
    ("basic", ["Basic"]),
    ("free tier", ["Free", "F0"]),
]


def _local_parse(query: str, context: dict[str, Any]) -> dict[str, Any]:
    """Best-effort keyword parse of a query into a structured filter — no AI needed. Powers
    common searches (by resource type, region, workload, SKU) even when the LLM is down."""
    q = f" {query.lower().strip()} "
    qflat = q.replace(" ", "")

    types: list[str] = []
    for keywords, arm in _TYPE_KEYWORDS:
        if any(k in q for k in keywords) and arm not in types:
            types.append(arm)
    is_logic = "microsoft.logic/workflows" in types

    locations: list[str] = []
    for loc in context.get("locations", []):
        lo = str(loc).lower()
        if lo and (lo in q or lo.replace(" ", "") in qflat) and lo not in locations:
            locations.append(lo)

    workloads: list[str] = []
    for w in context.get("workloads", []):
        wl = str(w).lower()
        if wl and wl in q and w not in workloads:
            workloads.append(str(w))

    sku: list[str] = []
    for phrase, vals in _SKU_KEYWORDS:
        if phrase in q:
            for v in vals:
                if v not in sku:
                    sku.append(v)
    # "consumption"/"dynamic" plan: Function Apps use the Y1 SKU; Logic Apps consumption is the
    # type itself (no SKU), so only add a SKU filter when it isn't a Logic App search.
    if ("consumption" in q or "dynamic plan" in q) and not is_logic:
        for v in ("Y1", "Consumption", "Dynamic"):
            if v not in sku:
                sku.append(v)

    has_signal = bool(types or locations or workloads or sku)
    parts: list[str] = []
    if types:
        parts.append(f"{len(types)} resource type{'s' if len(types) > 1 else ''}")
    if locations:
        parts.append(", ".join(locations))
    if workloads:
        parts.append(", ".join(workloads))
    if sku:
        parts.append("SKU ~ " + "/".join(sku[:2]))
    explanation = ("Matched " + " · ".join(parts) + ".") if has_signal else f"Searching resource names for “{query.strip()}”."

    return {
        "mode": "filter",
        "filter": {
            "types": types,
            "locations": locations,
            "subscriptions": [],
            "resource_groups": [],
            "workloads": workloads,
            "tag_key": "",
            "tag_value": "",
            "sku_contains": sku,
            # Only fall back to a name search when no structured signal was found, so a type
            # match like "logic apps" doesn't also require the name to contain "logic apps".
            "text": "" if has_signal else query.strip(),
        },
        "explanation": explanation,
        "source": "local",
    }



_FILTER_SYS = """You translate a user's natural-language request into a filter over an Azure \
resource inventory. Prefer a STRUCTURED FILTER. Only fall back to KQL for predicates the \
structured filter cannot express (deep resource properties, time ranges, joins).

Return ONLY a JSON object:
{
  "mode": "filter" | "kql",
  "filter": {
    "types": [full ARM types, lowercase, e.g. "microsoft.compute/virtualmachines"],
    "locations": [azure region codes, lowercase, e.g. "eastus"],
    "subscriptions": [subscription GUIDs],
    "resource_groups": [resource group names],
    "workloads": [workload names],
    "tag_key": "", "tag_value": "",
    "sku_contains": [substrings matched against sku/size/tier, e.g. "Standard_D" or "_D"],
    "text": "free-text matched against resource name"
  },
  "kql": "Resources | where ... | project id",   // only when mode = "kql"; MUST project id; read-only
  "explanation": "one short sentence describing how you interpreted the request"
}

Rules:
- Map friendly names to ARM types ("virtual machines" -> microsoft.compute/virtualmachines, \
"storage accounts" -> microsoft.storage/storageaccounts, "key vaults" -> microsoft.keyvault/vaults, \
"app services"/"web apps" -> microsoft.web/sites, "sql databases" -> microsoft.sql/servers/databases, \
"logic apps" -> microsoft.logic/workflows, "aks"/"kubernetes" -> microsoft.containerservice/managedclusters).
- "D series"/"D-series"/"Dv5" SKU -> sku_contains like ["Standard_D"] or ["_D"]. "E series" -> ["Standard_E"].
- Consumption Logic Apps ARE the type microsoft.logic/workflows (they have NO sku) — for \
"logic apps in consumption plan" just set types=["microsoft.logic/workflows"] and DO NOT add a sku filter. \
Standard Logic Apps are microsoft.web/sites. Function Apps on a consumption plan use sku "Y1".
- Only use the provided known types/locations/workloads/subscriptions when they match; otherwise infer the ARM type.
- KQL must read from the `resources` table, be read-only, and project at least `id`.
- Omit empty arrays/strings. Keep it minimal."""


def _filter_has_signal(flt: dict[str, Any]) -> bool:
    return bool(
        flt.get("types") or flt.get("locations") or flt.get("subscriptions")
        or flt.get("resource_groups") or flt.get("workloads") or flt.get("sku_contains")
        or flt.get("tag_key") or flt.get("text")
    )


async def nl_to_query(query: str, context: dict[str, Any]) -> dict[str, Any]:
    """Translate NL into {mode, filter?, kql?, explanation}. Validates any KQL.

    AI-first, with a local keyword fallback so common searches keep working even when the
    configured LLM provider is unavailable (expired token, network error, etc.)."""
    local = _local_parse(query, context)
    user = (
        f"Request: {query}\n\n"
        f"Known resource types present (lowercase ARM): {json.dumps(context.get('types', [])[:120])}\n"
        f"Known locations: {json.dumps(context.get('locations', [])[:60])}\n"
        f"Known workloads (name): {json.dumps(context.get('workloads', [])[:60])}\n"
        f"Known subscriptions (guid:name): {json.dumps(context.get('subscriptions', [])[:60])}\n"
    )
    try:
        parsed = await asyncio.wait_for(_complete_json(_FILTER_SYS, user), timeout=_AI_TIMEOUT_SECONDS)
    except (Exception, asyncio.TimeoutError) as exc:  # noqa: BLE001 — any provider error/timeout degrades to local
        logger.warning("inventory NL search AI unavailable, using local keyword match: %s", exc)
        return local
    if not isinstance(parsed, dict):
        return local

    mode = parsed.get("mode") or "filter"
    explanation = str(parsed.get("explanation") or "")[:300]
    if mode == "kql":
        clean, err = validate_kql(str(parsed.get("kql") or ""))
        if err:
            # Reject unsafe/invalid KQL → prefer a local keyword match over a bare text search.
            return local
        return {"mode": "kql", "kql": clean, "explanation": explanation or "Running a Resource Graph query."}

    flt = parsed.get("filter") if isinstance(parsed.get("filter"), dict) else {}
    # Normalize to lists/strings the frontend expects.
    norm = {
        "types": [str(t).lower() for t in (flt.get("types") or [])],
        "locations": [str(t).lower() for t in (flt.get("locations") or [])],
        "subscriptions": [str(t) for t in (flt.get("subscriptions") or [])],
        "resource_groups": [str(t) for t in (flt.get("resource_groups") or [])],
        "workloads": [str(t) for t in (flt.get("workloads") or [])],
        "tag_key": str(flt.get("tag_key") or ""),
        "tag_value": str(flt.get("tag_value") or ""),
        "sku_contains": [str(t) for t in (flt.get("sku_contains") or [])],
        "text": str(flt.get("text") or ""),
    }
    # If the AI shrugged but local keywords found something, prefer the local result.
    if not _filter_has_signal(norm) and local.get("source") == "local" and _filter_has_signal(local["filter"]):
        return local
    return {"mode": "filter", "filter": norm, "explanation": explanation or local["explanation"]}


_EXPLAIN_SYS = """You are an Azure expert. In 2-4 short sentences, explain what the given \
Azure resource is, what it is typically used for, and one practical governance or \
cost/security consideration for it. Be concrete and reference the resource's type, SKU, \
and location where relevant. Plain text, no markdown headers."""


async def explain_resource(resource: dict[str, Any]) -> str:
    """A short plain-text explanation of a single resource (streamed, then returned). Returns
    an empty string if the AI provider is unavailable so the caller can degrade gracefully."""
    facts = {
        "name": resource.get("name", ""),
        "type": resource.get("type", ""),
        "kind": resource.get("kind", ""),
        "sku": resource.get("sku", ""),
        "tier": resource.get("tier", ""),
        "size": resource.get("size", ""),
        "location": resource.get("location", ""),
        "resource_group": resource.get("resource_group", ""),
        "tags": resource.get("tags", {}),
        "workloads": [w.get("name") for w in resource.get("workloads", [])],
    }

    async def _run() -> str:
        provider = build_provider()
        text = ""
        async for ev in provider.stream(
            [{"role": "system", "content": _EXPLAIN_SYS},
             {"role": "user", "content": json.dumps(facts, separators=(",", ":"))}], None
        ):
            if ev.type == "token":
                text += ev.text
        return text.strip()

    try:
        return await asyncio.wait_for(_run(), timeout=_AI_TIMEOUT_SECONDS * 2)
    except (Exception, asyncio.TimeoutError) as exc:  # noqa: BLE001
        logger.warning("inventory explain AI unavailable: %s", exc)
        return ""


_INSIGHTS_SYS = """You are an Azure cloud estate analyst. Given a JSON summary of a tenant's \
resource inventory (counts by type, location, subscription, workload; tag coverage; hygiene \
flags; unassigned resources), produce concise, actionable insights for a platform/FinOps team.

Reply with ONLY a JSON object:
{
  "headline": "<one sentence overall take>",
  "insights": [
    {"title": "<short title>", "detail": "<1-2 sentence finding>", "severity": "info|warning|critical", "action": "<concrete next step>"}
  ]
}
Cover, where the data supports it: concentration risk (one region/sub holding most resources), \
tag-governance gaps, cleanup/orphan opportunities, unassigned resources that should be put in \
workloads, and proliferation. 4-7 insights. Be specific with numbers from the data. No code fences."""


async def estate_insights(summary: dict[str, Any], facets: dict[str, Any]) -> dict[str, Any]:
    """AI-generated, actionable insights over the inventory roll-up. Degrades to a small
    deterministic set when the provider is unavailable."""
    context = {
        "summary": summary,
        "top_types": (facets.get("types") or [])[:15],
        "locations": (facets.get("locations") or [])[:15],
        "subscriptions": [{"name": s.get("name"), "count": s.get("count")} for s in (facets.get("subscriptions") or [])][:15],
        "workloads": (facets.get("workloads") or [])[:20],
    }

    async def _run() -> Any:
        return await _complete_json(_INSIGHTS_SYS, json.dumps(context, separators=(",", ":")))

    try:
        parsed = await asyncio.wait_for(_run(), timeout=_AI_TIMEOUT_SECONDS * 2)
        if isinstance(parsed, dict) and parsed.get("insights"):
            return {"headline": str(parsed.get("headline") or "")[:300],
                    "insights": parsed["insights"][:8], "source": "ai"}
    except (Exception, asyncio.TimeoutError) as exc:  # noqa: BLE001
        logger.warning("inventory insights AI unavailable: %s", exc)

    return {"headline": "", "insights": _local_insights(summary, facets), "source": "local"}


def _local_insights(summary: dict[str, Any], facets: dict[str, Any]) -> list[dict[str, Any]]:
    """Deterministic fallback insights computed straight from the roll-up."""
    out: list[dict[str, Any]] = []
    total = summary.get("total_resources", 0) or 1
    locs = facets.get("locations") or []
    if locs and locs[0]["count"] / total > 0.6:
        out.append({"title": "Region concentration", "severity": "warning",
                    "detail": f"{locs[0]['count']} of {total} resources ({round(locs[0]['count'] / total * 100)}%) are in {locs[0]['key']}.",
                    "action": "Consider a multi-region strategy for resilience-critical workloads."})
    cov = summary.get("tag_coverage_pct", 0)
    if cov < 80:
        out.append({"title": "Tag governance gap", "severity": "warning" if cov < 50 else "info",
                    "detail": f"Only {cov}% of resources carry any tag.",
                    "action": "Define and enforce required tags (owner, environment, cost-center) via Azure Policy."})
    flags = summary.get("flag_counts") or {}
    cleanup = sum(v for k, v in flags.items() if k != "untagged")
    if cleanup:
        out.append({"title": "Cleanup candidates", "severity": "info",
                    "detail": f"{cleanup} likely-orphaned resources detected (unattached disks, idle public IPs, orphaned NICs).",
                    "action": "Review the 'Cleanup candidates' filter and delete confirmed waste."})
    if summary.get("unassigned_count"):
        out.append({"title": "Unassigned resources", "severity": "info",
                    "detail": f"{summary['unassigned_count']} resources don't belong to any workload.",
                    "action": "Assign them to workloads so governance and assessments cover them."})
    return out
