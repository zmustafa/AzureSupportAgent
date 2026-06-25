"""Documented Azure service limits that are NOT exposed by any dynamic quota/usage API.

These are surfaced as ``StaticServiceLimit`` rows so an operator sees the ceiling and whether it
is adjustable, even when Azure offers no live counter. Figures are well-documented platform
limits; they change over time, so each entry carries a short note and the limits are intentionally
conservative. This is metadata only — no Azure calls."""
from __future__ import annotations

from typing import Any

from app.quota.model import AdjustableStatus


# Each entry: category, provider, service, quota_name, limit, unit, adjustable, note, scope.
# scope: "subscription" (count once) or "region" (emitted per scanned region).
STATIC_LIMITS: list[dict[str, Any]] = [
    # --- Storage account performance (per-account hard limits) --------------------
    {
        "category": "storage", "provider": "Microsoft.Storage", "service": "Storage account",
        "quota_name": "Max ingress per general-purpose v2 account", "limit": 60, "unit": "Gbps",
        "adjustable": AdjustableStatus.SUPPORT_REQUIRED, "scope": "subscription",
        "note": "Per-account throughput ceiling; request an increase via support for higher.",
    },
    {
        "category": "storage", "provider": "Microsoft.Storage", "service": "Storage account",
        "quota_name": "Max request rate per account", "limit": 20000, "unit": "requests/s",
        "adjustable": AdjustableStatus.HARD_LIMIT, "scope": "subscription",
        "note": "Throttling (503) begins above this; shard across accounts to scale.",
    },
    # --- App Service (plan-level scaling hard limits) ------------------------------
    {
        "category": "appservice", "provider": "Microsoft.Web", "service": "App Service plan",
        "quota_name": "Max instances per Premium v3 plan", "limit": 30, "unit": "instances",
        "adjustable": AdjustableStatus.SUPPORT_REQUIRED, "scope": "subscription",
        "note": "Scale-out ceiling per plan; higher needs a support request.",
    },
    {
        "category": "appservice", "provider": "Microsoft.Web", "service": "App Service (Free/Shared)",
        "quota_name": "Free tier CPU per day", "limit": 60, "unit": "minutes/day",
        "adjustable": AdjustableStatus.HARD_LIMIT, "scope": "subscription",
        "note": "Free apps are hard-capped on daily CPU; move to Basic+ to remove the cap.",
    },
    # --- Key Vault throttling (per-vault, per-10s) --------------------------------
    {
        "category": "keyvault", "provider": "Microsoft.KeyVault", "service": "Key Vault",
        "quota_name": "Secrets/keys GET transactions", "limit": 4000, "unit": "ops/10s",
        "adjustable": AdjustableStatus.HARD_LIMIT, "scope": "subscription",
        "note": "Per-vault throttling ceiling (HTTP 429). Cache secrets and back off on 429.",
    },
    {
        "category": "keyvault", "provider": "Microsoft.KeyVault", "service": "Key Vault",
        "quota_name": "RSA 2048 software key operations", "limit": 2000, "unit": "ops/10s",
        "adjustable": AdjustableStatus.HARD_LIMIT, "scope": "subscription",
        "note": "Crypto-op throttling per vault; HSM and key types have separate ceilings.",
    },
    # --- Azure Monitor / Log Analytics -------------------------------------------
    {
        "category": "monitor", "provider": "Microsoft.Insights", "service": "Azure Monitor",
        "quota_name": "Log Analytics query rate", "limit": 200, "unit": "queries/30s",
        "adjustable": AdjustableStatus.HARD_LIMIT, "scope": "subscription",
        "note": "Per-user query throttling; batch queries and widen intervals to stay under.",
    },
    {
        "category": "monitor", "provider": "Microsoft.Insights", "service": "Azure Monitor",
        "quota_name": "Action groups per subscription", "limit": 2000, "unit": "Count",
        "adjustable": AdjustableStatus.SUPPORT_REQUIRED, "scope": "subscription",
        "note": "Soft ceiling; counted live where ARG is available, else treat as the documented cap.",
    },
    # --- ARM / governance hard limits --------------------------------------------
    {
        "category": "governance", "provider": "Microsoft.Resources", "service": "Resource Manager",
        "quota_name": "Resource groups per subscription", "limit": 980, "unit": "Count",
        "adjustable": AdjustableStatus.HARD_LIMIT, "scope": "subscription",
        "note": "Hard ARM limit; live count is collected via Resource Graph when available.",
    },
    {
        "category": "governance", "provider": "Microsoft.Resources", "service": "Resource Manager",
        "quota_name": "Deployments in history per resource group", "limit": 800, "unit": "Count",
        "adjustable": AdjustableStatus.HARD_LIMIT, "scope": "subscription",
        "note": "Oldest deployments are auto-pruned at the limit; not adjustable.",
    },
    {
        "category": "governance", "provider": "Microsoft.Authorization", "service": "RBAC",
        "quota_name": "Role assignments per subscription", "limit": 4000, "unit": "Count",
        "adjustable": AdjustableStatus.SUPPORT_REQUIRED, "scope": "subscription",
        "note": "Hard-ish limit; live count collected via Resource Graph. Use groups to reduce.",
    },
    {
        "category": "governance", "provider": "Microsoft.Authorization", "service": "RBAC",
        "quota_name": "Custom roles per tenant", "limit": 5000, "unit": "Count",
        "adjustable": AdjustableStatus.HARD_LIMIT, "scope": "subscription",
        "note": "Tenant-wide custom role ceiling.",
    },
    {
        "category": "governance", "provider": "Microsoft.Authorization", "service": "Azure Policy",
        "quota_name": "Policy assignments per scope", "limit": 200, "unit": "Count",
        "adjustable": AdjustableStatus.HARD_LIMIT, "scope": "subscription",
        "note": "Per-scope assignment ceiling; live count collected via Resource Graph.",
    },
    {
        "category": "governance", "provider": "Microsoft.Management", "service": "Management groups",
        "quota_name": "Management group hierarchy depth", "limit": 6, "unit": "levels",
        "adjustable": AdjustableStatus.HARD_LIMIT, "scope": "subscription",
        "note": "Max 6 levels of MG nesting below the root; not adjustable.",
    },
    # --- AI / OpenAI (model deployment posture often needs manual review) --------
    {
        "category": "ai", "provider": "Microsoft.CognitiveServices", "service": "Azure OpenAI",
        "quota_name": "Default tokens-per-minute per deployment", "limit": None, "unit": "TPM",
        "adjustable": AdjustableStatus.SUPPORT_REQUIRED, "scope": "region",
        "note": "TPM/RPM are per-model/region and may not be retrievable; request via the Quota blade.",
    },
]


def static_results(category_filter: set[str] | None, region: str | None) -> list[dict[str, Any]]:
    """Return the static entries matching the category filter, expanded for a region when the
    entry is region-scoped. ``region`` is None for subscription-scoped passes."""
    out: list[dict[str, Any]] = []
    for e in STATIC_LIMITS:
        if category_filter and e["category"] not in category_filter:
            continue
        if region is None and e["scope"] != "subscription":
            continue
        if region is not None and e["scope"] != "region":
            continue
        out.append(e)
    return out
