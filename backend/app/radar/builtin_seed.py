"""Built-in seed for the Retirement & Breaking-Change Radar.

Two editable knowledge sets (versioned in reference.py):

1. ``CLASSIFICATION_RULES`` — keyword rules that tag a raw Service-Health/Advisor event as
   a *retirement* (capability removed) vs a *permanent breaking change* (API-contract or
   default-setting change that doesn't remove a capability but still breaks workloads),
   and supply a recommended-replacement hint + canonical migration link when the source
   feed doesn't. Matched by substring against the event title/service/feature.

2. ``MODEL_LIFECYCLE`` — the Azure OpenAI / Foundry model lifecycle table (there is no
   Resource Graph query for model retirement dates), powering the AI-model lane. Each
   entry carries Preview→GA→Deprecated→Retired dates + the recommended successor.

Dates reflect publicly announced Microsoft deadlines as of mid-2026; admins edit them in
the dashboard when Microsoft revises a date."""
from __future__ import annotations

from typing import Any

BUILTIN_SEED_VERSION = 1

# change_type values used throughout the feature.
RETIREMENT = "retirement"
BREAKING_CHANGE = "breaking_change"


# --------------------------------------------------------------------- classification
# Each rule: match any of ``keywords`` (case-insensitive substring) against the event's
# combined service/feature/title text. ``change_type`` classifies it; ``replacement`` and
# ``migration_url`` fill gaps the source feed leaves blank. ``planned_date`` seeds a known
# public deadline for announcements that don't yet carry one in the tenant feed.
CLASSIFICATION_RULES: list[dict[str, Any]] = [
    {
        "id": "default-outbound-access",
        "keywords": ["default outbound", "default outbound access"],
        "change_type": RETIREMENT,
        "service": "Virtual Network",
        "replacement": "Explicit outbound connectivity (NAT Gateway, Standard Load Balancer outbound rule, or instance-level public IP).",
        "migration_url": "https://learn.microsoft.com/azure/virtual-network/ip-services/default-outbound-access",
        "planned_date": "2026-03-31",
    },
    {
        "id": "classic-resources",
        "keywords": ["classic", "cloud services (classic)", "asm "],
        "change_type": RETIREMENT,
        "service": "Classic (ASM) resources",
        "replacement": "Migrate to the Azure Resource Manager (ARM) equivalent.",
        "migration_url": "https://learn.microsoft.com/azure/cloud-services-extended-support/",
    },
    {
        "id": "tls-1.0-1.1",
        "keywords": ["tls 1.0", "tls 1.1", "minimum tls", "tls version"],
        "change_type": BREAKING_CHANGE,
        "service": "TLS",
        "replacement": "Enforce a minimum TLS version of 1.2 (or 1.3) on the resource.",
        "migration_url": "https://learn.microsoft.com/azure/security/fundamentals/tls-certificate-changes",
    },
    {
        "id": "basic-public-ip",
        "keywords": ["basic sku public ip", "basic public ip", "basic load balancer"],
        "change_type": RETIREMENT,
        "service": "Load Balancer / Public IP",
        "replacement": "Upgrade Basic SKU to Standard SKU.",
        "migration_url": "https://learn.microsoft.com/azure/load-balancer/load-balancer-basic-upgrade-guidance",
    },
    {
        "id": "api-version-deprecation",
        "keywords": ["api version", "api-version", "rest api version", "deprecated api"],
        "change_type": BREAKING_CHANGE,
        "service": "Azure REST API",
        "replacement": "Move clients/templates to a supported API version.",
        "migration_url": "https://learn.microsoft.com/azure/azure-resource-manager/management/breaking-change-policy",
    },
    {
        "id": "aoai-model-retirement",
        "keywords": ["azure openai", "openai model", "model deprecation", "model retirement", "foundry model"],
        "change_type": RETIREMENT,
        "service": "Azure OpenAI",
        "replacement": "Redeploy to a supported model version.",
        "migration_url": "https://learn.microsoft.com/azure/ai-services/openai/concepts/model-retirements",
    },
]

# Fallback when nothing matches: Advisor/Service-Health events are retirements unless the
# title clearly signals a default/contract change.
_BREAKING_HINTS = ("breaking change", "default change", "behavior change", "behaviour change",
                    "api version", "tls", "contract")


def classify_text(text: str) -> dict[str, Any]:
    """Classify a raw event by its combined text. Returns the matched rule fields merged
    with a default fallback (always returns a usable dict)."""
    low = (text or "").lower()
    for rule in CLASSIFICATION_RULES:
        if any(kw in low for kw in rule["keywords"]):
            return dict(rule)
    change_type = BREAKING_CHANGE if any(h in low for h in _BREAKING_HINTS) else RETIREMENT
    return {"id": "", "keywords": [], "change_type": change_type, "service": "", "replacement": "", "migration_url": "", "planned_date": ""}


# --------------------------------------------------------------------- model lifecycle
# stage values for the AI-model lane.
STAGE_PREVIEW = "preview"
STAGE_GA = "ga"
STAGE_DEPRECATED = "deprecated"
STAGE_RETIRED = "retired"

MODEL_LIFECYCLE: list[dict[str, Any]] = [
    {
        "model": "gpt-35-turbo", "version": "0301", "stage": STAGE_RETIRED,
        "ga_date": "2023-03-01", "deprecation_date": "2024-08-01", "retirement_date": "2025-02-13",
        "replacement": "gpt-4o-mini or gpt-4.1-mini",
    },
    {
        "model": "gpt-35-turbo", "version": "0613", "stage": STAGE_DEPRECATED,
        "ga_date": "2023-06-13", "deprecation_date": "2025-04-01", "retirement_date": "2026-08-01",
        "replacement": "gpt-4o-mini or gpt-4.1-mini",
    },
    {
        "model": "gpt-4", "version": "0613", "stage": STAGE_DEPRECATED,
        "ga_date": "2023-06-13", "deprecation_date": "2025-06-06", "retirement_date": "2026-09-30",
        "replacement": "gpt-4o or gpt-4.1",
    },
    {
        "model": "gpt-4", "version": "turbo-2024-04-09", "stage": STAGE_GA,
        "ga_date": "2024-04-09", "deprecation_date": "", "retirement_date": "2026-12-01",
        "replacement": "gpt-4o or gpt-4.1",
    },
    {
        "model": "gpt-4o", "version": "2024-05-13", "stage": STAGE_GA,
        "ga_date": "2024-05-13", "deprecation_date": "", "retirement_date": "2026-10-15",
        "replacement": "gpt-4o 2024-11-20 or gpt-4.1",
    },
    {
        "model": "gpt-4o", "version": "2024-11-20", "stage": STAGE_GA,
        "ga_date": "2024-11-20", "deprecation_date": "", "retirement_date": "2027-03-01",
        "replacement": "Newer gpt-4o / gpt-4.1 when released.",
    },
    {
        "model": "text-embedding-ada-002", "version": "2", "stage": STAGE_GA,
        "ga_date": "2022-12-15", "deprecation_date": "", "retirement_date": "2026-10-03",
        "replacement": "text-embedding-3-small or text-embedding-3-large",
    },
]


def model_lifecycle_index() -> dict[tuple[str, str], dict[str, Any]]:
    """(model, version) → lifecycle entry, for matching live deployments."""
    return {(e["model"].lower(), str(e.get("version", "")).lower()): e for e in MODEL_LIFECYCLE}


def builtin_reference() -> dict[str, Any]:
    """The seed document persisted by reference.py on first load."""
    return {
        "version": 0,
        "builtin_seed_version": BUILTIN_SEED_VERSION,
        "classification_rules": [dict(r) for r in CLASSIFICATION_RULES],
        "model_lifecycle": [dict(m) for m in MODEL_LIFECYCLE],
    }
