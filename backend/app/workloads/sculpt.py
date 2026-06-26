"""Scope Sculptor — the pre-flight input-shaping layer for Workload Autopilot.

Large estates (thousands of resources) make AI grouping slow and noisy: most resources are
low-signal children (NICs, disks, alert rules) or live in platform-managed system resource
groups (``MC_*``, ``NetworkWatcherRG``). This module lets a user SURVEY the estate (free,
no LLM) and SCULPT the input before paying for AI:

* noise/system-RG filters (Tier 1)
* tag-seed pre-partitioning — deterministically bucket well-tagged resources so only the
  untagged remainder reaches the LLM (Tier 1)
* facet tallies + naming-convention detection for the live preview (Tier 1 / Tier 3)
* granularity-aware cost/time estimation (Tier 2 / Tier 3)
* priority ordering so prod / largest groups stream first (Tier 4)
* orphan re-attachment — filtered children rejoin their parent workload AFTER grouping

Everything here is PURE (no Azure / no LLM) so it's fast and fully unit-testable.
"""
from __future__ import annotations

import fnmatch
import re
from collections import Counter
from typing import Any, Iterable

# --------------------------------------------------------------------------- noise model
# Low-signal / child resource types that attach to a parent workload deterministically — they
# don't need to be reasoned about by the LLM. Matched by SUBSTRING against the lowercased ARM
# type, so "microsoft.compute/disks" also catches nothing else accidentally (kept specific).
NOISE_TYPE_SUBSTRINGS: tuple[str, ...] = (
    "microsoft.compute/disks",
    "microsoft.compute/snapshots",
    "microsoft.compute/restorepointcollections",
    "microsoft.compute/virtualmachines/extensions",
    "microsoft.compute/sshpublickeys",
    "microsoft.network/networkinterfaces",
    "microsoft.network/networkwatchers",
    "microsoft.network/networkwatchers/flowlogs",
    "microsoft.insights/activitylogalerts",
    "microsoft.insights/metricalerts",
    "microsoft.insights/scheduledqueryrules",
    "microsoft.insights/webtests",
    "microsoft.insights/autoscalesettings",
    "microsoft.insights/datacollectionrules",
    "microsoft.insights/datacollectionendpoints",
    "microsoft.insights/actiongroups",
    "microsoft.alertsmanagement/",
    "microsoft.guestconfiguration/",
    "microsoft.security/",
    "microsoft.advisor/",
    "microsoft.changeanalysis/",
)

# Platform-managed system resource groups (glob patterns, case-insensitive). These hold
# infrastructure Azure creates on your behalf — never a user "workload".
DEFAULT_SYSTEM_RG_GLOBS: tuple[str, ...] = (
    "MC_*",                       # AKS managed node RGs
    "NetworkWatcherRG",
    "DefaultResourceGroup-*",
    "databricks-rg-*",
    "AzureBackupRG_*",
    "AzureBackupRG-*",
    "LogAnalyticsDefaultResources",
    "cloud-shell-storage-*",
    "microsoft-network",
    "DynamicsDeployments",
    "ai-*-managed-rg",
)

# Default per-LLM-call resource budget (mirrors autopilot._AI_BATCH) and the per-call wall-clock
# estimate used by the cost preview. Tunable; kept here so the estimator and the runner agree.
RESOURCE_BATCH = 500
RG_BATCH = 300            # resource-groups per call in resource-group granularity
SAMPLE_BATCH = 400        # representatives per call in sample-taxonomy granularity
EST_SECONDS_PER_CALL = 9  # rough wall-clock per grouping call (reasoning model)
EST_OVERHEAD_SECONDS = 6  # enumerate + signals + merge


def _rtype(r: dict[str, Any]) -> str:
    return str(r.get("resource_type") or r.get("type") or "").lower()


def is_noise(resource: dict[str, Any]) -> bool:
    """True when the resource is a low-signal child that should attach to its parent
    workload rather than be reasoned about individually."""
    t = _rtype(resource)
    return any(sub in t for sub in NOISE_TYPE_SUBSTRINGS)


def rg_matches_globs(rg: str, globs: Iterable[str]) -> bool:
    """Case-insensitive glob match of a resource-group name against any pattern."""
    name = (rg or "").lower()
    for pat in globs:
        if pat and fnmatch.fnmatch(name, pat.lower()):
            return True
    return False


# --------------------------------------------------------------------------- environment
_ENV_TOKENS: list[tuple[str, tuple[str, ...]]] = [
    ("production", ("prod", "prd", "live")),
    ("staging", ("stag", "stg", "uat", "preprod", "pre-prod")),
    ("development", ("dev", "sandbox", "sbx")),
    ("test", ("test", "qa", "tst")),
    ("dr", ("failover", "secondary")),
]


def resource_environment(resource: dict[str, Any]) -> str:
    """Best-effort environment for ONE resource from its tags + name (for env scoping)."""
    tags = resource.get("tags") or {}
    if isinstance(tags, dict):
        for k in ("environment", "env", "stage", "Environment", "Env"):
            v = str(tags.get(k, "")).strip().lower()
            if v:
                for env, tokens in _ENV_TOKENS:
                    if any(tok in v for tok in tokens):
                        return env
    name = str(resource.get("name", "")).lower()
    rg = str(resource.get("resource_group", "")).lower()
    hay = f"{name} {rg}"
    for env, tokens in _ENV_TOKENS:
        if any(re.search(rf"(^|[-_/]){re.escape(tok)}([-_/]|\d|$)", hay) for tok in tokens):
            return env
    return "unknown"


# --------------------------------------------------------------------------- filtering
class FilterConfig:
    """The sculpt selections that reduce the enumerated estate before grouping."""

    __slots__ = (
        "exclude_noise", "exclude_system_rgs", "rg_globs", "include_types", "exclude_types",
        "environments", "regions", "subscriptions", "name_contains",
    )

    def __init__(
        self,
        *,
        exclude_noise: bool = True,
        exclude_system_rgs: bool = True,
        rg_globs: Iterable[str] | None = None,
        include_types: Iterable[str] | None = None,
        exclude_types: Iterable[str] | None = None,
        environments: Iterable[str] | None = None,
        regions: Iterable[str] | None = None,
        subscriptions: Iterable[str] | None = None,
        name_contains: str = "",
    ) -> None:
        self.exclude_noise = exclude_noise
        self.exclude_system_rgs = exclude_system_rgs
        self.rg_globs = tuple(rg_globs) if rg_globs is not None else DEFAULT_SYSTEM_RG_GLOBS
        self.include_types = {t.lower() for t in (include_types or [])}
        self.exclude_types = {t.lower() for t in (exclude_types or [])}
        self.environments = {e.lower() for e in (environments or [])}
        self.regions = {r.lower() for r in (regions or [])}
        self.subscriptions = {s.lower() for s in (subscriptions or [])}
        self.name_contains = (name_contains or "").strip().lower()


def apply_filters(
    resources: list[dict[str, Any]], cfg: FilterConfig
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Split the estate into (kept, removed, reasons).

    ``removed`` are the resources excluded from AI grouping (noise/system-RG/scoped-out). The
    noise/system-RG removals are RE-ATTACHED to their parent workload after grouping via
    :func:`reattach_orphans`; the scoped-out ones (type/env/region/sub/name) are dropped
    entirely. ``reasons`` is a per-cause tally for the UI.
    """
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    reasons: dict[str, int] = {
        "noise": 0, "system_rg": 0, "type": 0, "environment": 0,
        "region": 0, "subscription": 0, "name": 0,
    }
    for r in resources:
        t = _rtype(r)
        rg = str(r.get("resource_group", ""))
        loc = str(r.get("location", "")).lower()
        sub = str(r.get("subscription_id", "")).lower()

        # Hard scoping filters first (these resources are dropped, not re-attached).
        if cfg.subscriptions and sub not in cfg.subscriptions:
            reasons["subscription"] += 1
            continue
        if cfg.regions and loc not in cfg.regions:
            reasons["region"] += 1
            continue
        if cfg.include_types and not any(it in t for it in cfg.include_types):
            reasons["type"] += 1
            continue
        if cfg.exclude_types and any(et in t for et in cfg.exclude_types):
            reasons["type"] += 1
            continue
        if cfg.environments and resource_environment(r) not in cfg.environments:
            reasons["environment"] += 1
            continue
        if cfg.name_contains and cfg.name_contains not in str(r.get("name", "")).lower():
            reasons["name"] += 1
            continue

        # Soft filters: removed from grouping but re-attached to a parent workload later.
        if cfg.exclude_system_rgs and rg_matches_globs(rg, cfg.rg_globs):
            reasons["system_rg"] += 1
            removed.append(r)
            continue
        if cfg.exclude_noise and is_noise(r):
            reasons["noise"] += 1
            removed.append(r)
            continue
        kept.append(r)
    return kept, removed, reasons


# --------------------------------------------------------------------------- tag seeding
def tag_seed_partition(
    resources: list[dict[str, Any]], tag_keys: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Deterministically pre-bucket resources by an authoritative grouping tag.

    For each resource the FIRST present key in ``tag_keys`` (priority order) decides its bucket
    (``<key>=<value>``). Returns ``(seeded_groups, remainder)`` where ``remainder`` is every
    resource carrying none of the seed keys — only the remainder needs the LLM. Each seeded
    group is a ready grouping dict (members are the actual resources)."""
    if not tag_keys:
        return [], list(resources)
    keys_lower = [k.lower() for k in tag_keys if k]
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    remainder: list[dict[str, Any]] = []
    for r in resources:
        tags = r.get("tags") or {}
        chosen: tuple[str, str] | None = None
        if isinstance(tags, dict):
            lowered = {k.lower(): (k, v) for k, v in tags.items()}
            for kl in keys_lower:
                if kl in lowered and str(lowered[kl][1]).strip():
                    orig_k, v = lowered[kl]
                    chosen = (orig_k, str(v))
                    break
        if chosen is None:
            remainder.append(r)
        else:
            buckets.setdefault(chosen, []).append(r)
    groups: list[dict[str, Any]] = []
    for (key, val), members in buckets.items():
        groups.append({
            "name": val,
            "description": f"Resources tagged {key}={val}.",
            "reasoning": f"Pre-grouped deterministically by the authoritative tag '{key}' (no AI needed).",
            "confidence": 0.7,
            "members": members,
            "workload_type": "",
            "environment": "",
            "criticality": "",
            "data_classification": "",
            "seeded_by_tag": key,
        })
    groups.sort(key=lambda g: -len(g["members"]))
    return groups, remainder


# --------------------------------------------------------------------------- naming model
_DELIMS = ("-", "_", ".")


def detect_naming_convention(resources: list[dict[str, Any]], sample: int = 400) -> dict[str, Any]:
    """Infer the estate's dominant resource-naming convention.

    Returns ``{delimiter, segments, confidence, pattern, examples}``. The ``pattern`` is a
    human-readable guess (e.g. ``{app}-{env}-{region}-{nn}``) injected into the grouping prompt
    so the model can parse workload identity from names. Heuristic, best-effort."""
    names = [str(r.get("name", "")) for r in resources[:sample] if r.get("name")]
    if not names:
        return {"delimiter": "", "segments": 0, "confidence": 0.0, "pattern": "", "examples": []}

    delim_counts = {d: sum(1 for n in names if d in n) for d in _DELIMS}
    delimiter = max(delim_counts, key=lambda d: delim_counts[d])
    if delim_counts[delimiter] < max(3, len(names) * 0.3):
        return {"delimiter": "", "segments": 0, "confidence": 0.0, "pattern": "", "examples": names[:3]}

    seg_counts = Counter(len(n.split(delimiter)) for n in names if delimiter in n)
    dominant_segments, dominant_n = seg_counts.most_common(1)[0]
    coverage = dominant_n / max(1, sum(seg_counts.values()))

    # Label each positional segment by inspecting the value distribution.
    cols: list[list[str]] = [[] for _ in range(dominant_segments)]
    for n in names:
        parts = n.split(delimiter)
        if len(parts) == dominant_segments:
            for i, p in enumerate(parts):
                cols[i].append(p.lower())

    env_vocab = {tok for _, toks in _ENV_TOKENS for tok in toks}
    region_vocab = {
        "eastus", "eastus2", "westus", "westus2", "westus3", "centralus", "northeurope",
        "westeurope", "uksouth", "ukwest", "eus", "wus", "weu", "neu", "sea", "eas",
    }

    def _label(col: list[str], idx: int) -> str:
        if not col:
            return f"seg{idx + 1}"
        uniq = set(col)
        if uniq & env_vocab:
            return "env"
        if uniq & region_vocab:
            return "region"
        if all(re.fullmatch(r"\d+", c) for c in col):
            return "nn"
        # Mostly-unique low-cardinality leading segment → likely the app/workload token.
        if idx == 0 and len(uniq) > 1:
            return "app"
        if len(uniq) <= 4:
            return "type"
        return f"seg{idx + 1}"

    labels = [_label(cols[i], i) for i in range(dominant_segments)]
    pattern = delimiter.join("{" + lbl + "}" for lbl in labels)
    return {
        "delimiter": delimiter,
        "segments": dominant_segments,
        "confidence": round(coverage, 2),
        "pattern": pattern,
        "examples": names[:3],
    }


# --------------------------------------------------------------------------- facets
def compute_facets(resources: list[dict[str, Any]], *, top: int = 25) -> dict[str, Any]:
    """Tally the estate for the live preview: counts by type / RG / region / subscription /
    tag-key / environment, plus noise & system-RG counts and the naming convention."""
    from app.workloads.summarize import friendly_type

    type_counts: Counter[str] = Counter()
    rg_counts: Counter[str] = Counter()
    region_counts: Counter[str] = Counter()
    sub_counts: Counter[str] = Counter()
    tag_key_counts: Counter[str] = Counter()
    env_counts: Counter[str] = Counter()
    noise = 0
    system_rg = 0

    for r in resources:
        type_counts[friendly_type(r.get("resource_type") or r.get("type"))] += 1
        rg = str(r.get("resource_group", "")) or "(none)"
        rg_counts[rg] += 1
        region_counts[str(r.get("location", "")) or "(none)"] += 1
        sub_counts[str(r.get("subscription_id", "")) or "(none)"] += 1
        env_counts[resource_environment(r)] += 1
        tags = r.get("tags") or {}
        if isinstance(tags, dict):
            for k in tags:
                tag_key_counts[k] += 1
        if is_noise(r):
            noise += 1
        if rg_matches_globs(rg, DEFAULT_SYSTEM_RG_GLOBS):
            system_rg += 1

    def _top(counter: Counter[str]) -> list[dict[str, Any]]:
        return [{"label": k, "count": v} for k, v in counter.most_common(top)]

    return {
        "total": len(resources),
        "types": _top(type_counts),
        "resource_groups": _top(rg_counts),
        "regions": _top(region_counts),
        "subscriptions": _top(sub_counts),
        "tag_keys": _top(tag_key_counts),
        "environments": _top(env_counts),
        "distinct_resource_groups": len(rg_counts),
        "distinct_regions": len(region_counts),
        "distinct_subscriptions": len(sub_counts),
        "noise_count": noise,
        "system_rg_count": system_rg,
        "naming": detect_naming_convention(resources),
    }


# --------------------------------------------------------------------------- cost estimate
def estimate_cost(
    n_resources: int,
    *,
    granularity: str = "resource",
    n_resource_groups: int = 0,
    tag_seeded: int = 0,
    max_ai_calls: int = 0,
) -> dict[str, Any]:
    """Estimate the AI work for a sculpted run: number of grouping calls, wall-clock and a
    rough token count. ``tag_seeded`` resources are pre-grouped and cost nothing."""
    effective = max(0, n_resources - max(0, tag_seeded))
    if granularity == "subscription" or granularity == "resource_group" and n_resource_groups == 0:
        # subscription template = deterministic, no AI.
        pass
    if granularity == "subscription":
        calls = 0
        unit = "subscriptions"
    elif granularity == "resource_group":
        units = n_resource_groups or max(1, effective // 20)
        calls = (units + RG_BATCH - 1) // RG_BATCH if effective else 0
        unit = "resource groups"
    elif granularity == "sample":
        # One stratified sample pass + a refine pass for ambiguous remainder.
        calls = 2 if effective > SAMPLE_BATCH else 1
        calls = calls if effective else 0
        unit = "representative sample"
    else:  # resource
        calls = (effective + RESOURCE_BATCH - 1) // RESOURCE_BATCH if effective else 0
        unit = "resources"

    capped = False
    if max_ai_calls and calls > max_ai_calls:
        calls = max_ai_calls
        capped = True

    est_seconds = EST_OVERHEAD_SECONDS + calls * EST_SECONDS_PER_CALL
    # Rough tokens: ~22 tokens/resource line in the catalog + completion overhead.
    est_tokens = effective * 22 + calls * 600
    return {
        "ai_calls": calls,
        "unit": unit,
        "effective_resources": effective,
        "tag_seeded": max(0, tag_seeded),
        "est_seconds": est_seconds,
        "est_tokens": est_tokens,
        "capped": capped,
    }


# --------------------------------------------------------------------------- priority order
def priority_sort(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order resources so high-value candidates stream first: production before non-prod,
    then larger resource groups before smaller (so a user can stop early with the best
    workloads already in hand). Stable within ties on (sub, rg, name)."""
    rg_size: Counter[tuple[str, str]] = Counter()
    for r in resources:
        rg_size[(str(r.get("subscription_id", "")), str(r.get("resource_group", "")))] += 1

    env_rank = {"production": 0, "staging": 1, "dr": 1, "test": 2, "development": 3, "unknown": 4, "shared": 4}

    def _key(r: dict[str, Any]):
        env = resource_environment(r)
        size = rg_size[(str(r.get("subscription_id", "")), str(r.get("resource_group", "")))]
        return (
            env_rank.get(env, 4),
            -size,
            str(r.get("subscription_id", "")),
            str(r.get("resource_group", "")),
            str(r.get("name", "")),
        )

    return sorted(resources, key=_key)


# --------------------------------------------------------------------------- orphan attach
def reattach_orphans(
    groups: list[dict[str, Any]], orphans: list[dict[str, Any]]
) -> int:
    """Re-attach filtered child resources (noise/system-RG) to the workload that owns their
    resource group. An orphan whose RG matches exactly one group's RG set joins it; orphans
    with no matching group are left out (they were genuinely standalone). Returns the count
    attached. Mutates ``groups`` in place."""
    if not orphans or not groups:
        return 0
    rg_to_group: dict[tuple[str, str], dict[str, Any]] = {}
    for g in groups:
        for m in g["members"]:
            key = (str(m.get("subscription_id", "")).lower(), str(m.get("resource_group", "")).lower())
            # First group claiming an RG owns it (largest groups are processed first if sorted).
            rg_to_group.setdefault(key, g)
    attached = 0
    for o in orphans:
        key = (str(o.get("subscription_id", "")).lower(), str(o.get("resource_group", "")).lower())
        g = rg_to_group.get(key)
        if g is not None:
            g["members"].append(o)
            attached += 1
    return attached
