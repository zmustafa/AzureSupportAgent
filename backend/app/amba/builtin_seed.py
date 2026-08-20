"""Built-in AMBA reference: the vendored upstream catalog plus local enrichment.

Three layers are merged, in order:

1. ``data/amba_catalog.json`` — the upstream Azure Monitor Baseline Alerts catalog,
   imported verbatim from ``services/<Provider>/<type>/alerts.yaml`` at a pinned release
   tag by ``scripts/import_amba_catalog.py``. Never hand-edit this file.
2. ``data/enrichment.json`` — operator-facing "why this matters" copy and display units
   keyed by (arm_type, alert key). Hand-maintained.
3. ``data/local_extensions.json`` — resource types AMBA does not publish (e.g. SQL
   *databases*, managed disks, Static Web Apps) and extra alerts we add on top of
   AMBA-published types. Hand-maintained; every entry is marked ``source: "local"``.

The merged result seeds ``reference.py``, which persists an admin-editable copy.

Alert schema (a superset of the upstream ``alerts.yaml`` properties):

    key / guid              stable identity; ``guid`` is AMBA's and survives renames
    name / description      upstream label + upstream description
    why                     local explanation shown in the UI drawer
    alert_type              metric | log | activitylog
    amba_category           availability | performance | security  (derived)
    severity / severity_num string bucket + AMBA's numeric 0-4
    tier                    core | recommended | optional
    patterns                alz | hpc | avd | rag | avs
    metric / metric_namespace / counter_name
    operator / threshold / unit
    criterion_type          StaticThresholdCriterion | DynamicThresholdCriterion | ''
    alert_sensitivity       Low | Medium | High   (dynamic thresholds)
    failing_periods         {number_of_evaluation_periods, min_failing_periods_to_alert}
    auto_mitigate
    time_aggregation / window_size / evaluation_frequency
    dimensions              [{name, operator, values}]
    activity_log            {category, incidentType, operationName, status, …}
    log_query               KQL for log-search alerts
    visible / verified / default_enabled
    requires_action_group / deployable
    threshold_override_tag  AMBA-ALZ per-resource override tag name
    policy_alert_name / policy_scope / deployments / references
    source                  amba | local
"""
from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_DATA = Path(__file__).resolve().parent / "data"
_CATALOG_PATH = _DATA / "amba_catalog.json"
_ENRICHMENT_PATH = _DATA / "enrichment.json"
_LOCAL_PATH = _DATA / "local_extensions.json"

# Bumped whenever the merged baseline changes so the registry can offer "reset to builtin vN".
# v9 = full upstream AMBA import (release 2026-06-03): 76 types / 675 alerts, plus local
#      extensions. Supersedes the hand-curated 37-type seed.
BUILTIN_SEED_VERSION = 9

ALERT_TYPES = ("metric", "log", "activitylog")
TIERS = ("core", "recommended", "optional")
PATTERNS = ("alz", "hpc", "avd", "rag", "avs")
SEVERITIES = ("critical", "error", "warning", "info")
AMBA_CATEGORIES = ("availability", "performance", "security")
OPERATORS = (
    "GreaterThan", "GreaterThanOrEqual", "LessThan", "LessThanOrEqual",
    "GreaterOrLessThan", "Equals",
)

_SEV_LABEL = {0: "critical", 1: "error", 2: "warning", 3: "info", 4: "info"}
_SEV_NUM = {"critical": 0, "error": 1, "warning": 2, "info": 3}

# Metric-alert recommendations that reference a derived/composite figure rather than a real
# Azure Monitor metric definition, so they must never become deployable alert proposals.
_NON_METRIC_NAMES = {"disk iops saturation", "disk throughput saturation"}


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _is_deployable(alert: dict[str, Any]) -> bool:
    """Can this recommendation be emitted as a real Azure alert rule?"""
    if alert.get("deployable") is False:
        return False
    kind = alert.get("alert_type")
    if kind == "activitylog":
        return bool(alert.get("activity_log"))
    if kind == "log":
        return bool(alert.get("log_query") or alert.get("counter_name"))
    metric = str(alert.get("metric") or "").strip()
    if not metric or metric.lower() in _NON_METRIC_NAMES:
        return False
    if str(alert.get("criterion_type") or "") == "DynamicThresholdCriterion":
        return True
    return alert.get("threshold") is not None


def _normalize(alert: dict[str, Any], *, source: str) -> dict[str, Any]:
    """Fill in derived fields and guarantee every key exists with a sane type."""
    out = copy.deepcopy(alert)
    out.setdefault("source", source)

    kind = str(out.get("alert_type") or "metric").lower()
    out["alert_type"] = kind if kind in ALERT_TYPES else "metric"

    sev_num = out.get("severity_num")
    if not isinstance(sev_num, int) or not 0 <= sev_num <= 4:
        sev_num = _SEV_NUM.get(str(out.get("severity") or "warning"), 2)
    out["severity_num"] = sev_num
    if out.get("severity") not in SEVERITIES:
        out["severity"] = _SEV_LABEL.get(sev_num, "info")

    if out.get("amba_category") not in AMBA_CATEGORIES:
        out["amba_category"] = "availability"
    if out.get("tier") not in TIERS:
        out["tier"] = "recommended"
    out["patterns"] = [p for p in (out.get("patterns") or []) if p in PATTERNS]

    for field, default in (
        ("guid", ""), ("description", ""), ("why", ""), ("metric", ""),
        ("metric_namespace", ""), ("counter_name", ""), ("unit", ""),
        ("criterion_type", ""), ("time_aggregation", ""), ("window_size", "PT5M"),
        ("evaluation_frequency", ""), ("log_query", ""), ("policy_alert_name", ""),
        ("policy_scope", ""), ("threshold_override_tag", ""), ("dimension_filter", ""),
    ):
        out[field] = str(out.get(field) or default)

    if out.get("operator") not in OPERATORS:
        out["operator"] = "GreaterThan" if not out.get("operator") else str(out["operator"])
    try:
        out["threshold"] = float(out["threshold"]) if out.get("threshold") is not None else None
    except (TypeError, ValueError):
        out["threshold"] = None

    for field in ("dimensions", "references", "deployments", "amba_tags"):
        value = out.get(field)
        out[field] = value if isinstance(value, list) else []
    out["activity_log"] = out.get("activity_log") if isinstance(out.get("activity_log"), dict) else {}
    out["failing_periods"] = (
        out.get("failing_periods") if isinstance(out.get("failing_periods"), dict) else None
    )

    for field, default in (("visible", True), ("verified", False), ("default_enabled", True)):
        out[field] = bool(out.get(field, default))
    # Activity-log and Resource/Service-Health alerts are useless without a notification
    # target, so an action group is part of "good" for every alert class.
    out["requires_action_group"] = bool(out.get("requires_action_group", True))
    # Evaluation frequency defaults to the aggregation window when upstream omits it.
    if not out["evaluation_frequency"]:
        out["evaluation_frequency"] = out["window_size"]
    out["deployable"] = _is_deployable(out)
    return out


@lru_cache(maxsize=1)
def _merged() -> dict[str, Any]:
    catalog = _load(_CATALOG_PATH)
    enrichment = _load(_ENRICHMENT_PATH).get("types") or {}
    local = _load(_LOCAL_PATH).get("types") or {}

    types: dict[str, Any] = {}
    for arm_type, spec in (catalog.get("types") or {}).items():
        enrich = enrichment.get(arm_type) or {}
        alerts = []
        for raw in spec.get("alerts") or []:
            alert = _normalize(raw, source="amba")
            extra = enrich.get(alert["key"]) or {}
            if extra.get("why"):
                alert["why"] = extra["why"]
            if extra.get("unit"):
                alert["unit"] = extra["unit"]
            if not alert["why"]:
                alert["why"] = alert["description"]
            alerts.append(alert)
        types[arm_type] = {
            "display": spec.get("display") or arm_type,
            "category": spec.get("category") or "other",
            "source": "amba",
            "provider": spec.get("provider", ""),
            "service": spec.get("service", ""),
            "alerts": alerts,
        }

    for arm_type, spec in local.items():
        local_alerts = [_normalize(a, source="local") for a in (spec.get("alerts") or [])]
        target = types.get(arm_type)
        if target is None:
            types[arm_type] = {
                "display": spec.get("display") or arm_type,
                "category": spec.get("category") or "other",
                "source": "local",
                "provider": "",
                "service": "",
                "alerts": local_alerts,
            }
            continue
        have = {a["key"] for a in target["alerts"]}
        target["alerts"].extend(a for a in local_alerts if a["key"] not in have)

    return {
        "amba_release": catalog.get("amba_release", ""),
        "amba_source": catalog.get("source", ""),
        "amba_imported_at": catalog.get("imported_at", ""),
        "types": dict(sorted(types.items())),
    }


def amba_release() -> str:
    """The upstream AMBA release tag the vendored catalog was imported from."""
    return str(_merged().get("amba_release") or "")


def builtin_types() -> dict[str, Any]:
    """The merged type→alerts map (deep copy; safe for the caller to mutate)."""
    return copy.deepcopy(_merged()["types"])


def builtin_reference() -> dict[str, Any]:
    """A fresh reference document seeded from the built-in baseline."""
    merged = _merged()
    return {
        "version": 0,
        "builtin_seed_version": BUILTIN_SEED_VERSION,
        "amba_release": merged.get("amba_release", ""),
        "amba_source": merged.get("amba_source", ""),
        "amba_imported_at": merged.get("amba_imported_at", ""),
        "updated_at": "",
        "updated_by": "",
        "types": copy.deepcopy(merged["types"]),
    }
