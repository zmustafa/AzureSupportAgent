"""AMBA Monitoring Coverage computation.

Detects, per resource, which recommended baseline alerts are present (✓), missing (✗),
misconfigured (⚠ — disabled, no action group, wrong criterion, threshold outside tolerance)
or suppressed (🔇 — muted by an enabled alert processing rule). Runs entirely on the
read-only Azure Resource Graph path: resources, alert rules, action groups and alert
processing rules are all ARM resources queryable via KQL, so no gated command-execution or
data-plane access is needed.

Three alert classes are matched, each with its own evidence:

* **metric**      ``microsoft.insights/metricalerts`` — matched on metric name + dimensions,
  including *multi-resource* rules scoped to a resource group / subscription that carry a
  ``targetResourceType`` (these previously read as "missing" on every child resource).
* **log**         ``microsoft.insights/scheduledqueryrules`` — matched on a query signature
  (primary table + the ``Name``/``MetricName``/``CounterName`` operands), never on
  "any rule targeting this resource".
* **activitylog** ``microsoft.insights/activitylogalerts`` — matched on the condition's
  ``category`` plus ``operationName`` / ``incidentType``. Service Health and Resource Health
  live here, scored against a synthetic ``microsoft.resources/subscriptions`` row.

``compute_coverage`` accepts optional pre-fetched ``resources`` and ``alerts`` lists; when
omitted it queries Azure Resource Graph. The injection path makes the logic unit-testable
and powers the demo/dummy-data seed (demo.py)."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from app.amba.reference import load_reference
from app.core.coverage_resources import build_all_resources

log = logging.getLogger("app.amba.collector")

STATUS_PRESENT = "present"
STATUS_MISCONFIGURED = "misconfigured"
STATUS_SUPPRESSED = "suppressed"
STATUS_MISSING = "missing"

SUBSCRIPTION_TYPE = "microsoft.resources/subscriptions"

_SEVERITY_RANK = {"critical": 0, "error": 1, "warning": 2, "info": 3}

# AMBA-ALZ honours this tag to opt a resource out of policy-driven alerting.
MONITOR_DISABLE_TAG = "monitordisable"

_METRIC_RULE = "microsoft.insights/metricalerts"
_LOG_RULE = "microsoft.insights/scheduledqueryrules"
_ACTIVITY_RULE = "microsoft.insights/activitylogalerts"
_ACTION_GROUP = "microsoft.insights/actiongroups"
_ACTION_RULE = "microsoft.alertsmanagement/actionrules"

# Query literals too generic to identify a specific AMBA log alert.
_GENERIC_QUERY_TOKENS = {
    "vm.azm.ms", "c:", "/", "output", "true", "false", "succeeded", "computer",
    "microsoft.compute/virtualmachines", "microsoft.compute/virtualmachinescalesets",
    "microsoft.hybridcompute/machines",
}


@dataclass(frozen=True)
class CoverageOptions:
    """Knobs that shape scoring, sourced from app settings."""

    misconfig_counts_as_gap: bool = True
    tolerance_pct: float = 10.0
    # Which baseline tiers are scored. "optional" alerts are upstream-hidden and off by default.
    tiers: tuple[str, ...] = ("core", "recommended")
    # Restrict scoring to a workload pattern (alz / hpc / avd / rag / avs); empty = all.
    patterns: tuple[str, ...] = ()
    # Flag rules whose severity differs from the baseline as misconfigured.
    severity_counts_as_gap: bool = False
    # Skip resources tagged MonitorDisable=true instead of scoring them as gaps.
    honor_monitor_disable_tag: bool = True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Markers that identify an ARG throttling failure (``429 RateLimiting``) as opposed to a real
# fault like a permission gap. A throttle means "could not evaluate, try again shortly" — very
# different from a scan that genuinely found nothing, and the UI says so.
_THROTTLE_MARKERS = ("429", "ratelimiting", "rate limit", "too many requests", "throttl")


def _is_throttle_error(error: str) -> bool:
    blob = (error or "").lower()
    return any(marker in blob for marker in _THROTTLE_MARKERS)


def _esc(val: str) -> str:
    return (val or "").replace("'", "''")


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


# --------------------------------------------------------------------------- scope helpers
def _scope_kind(scope: str) -> str:
    """Classify an alert scope as resource / group / subscription."""
    parts = [p for p in str(scope or "").strip("/").split("/") if p]
    lowered = [p.lower() for p in parts]
    if "providers" in lowered:
        return "resource"
    if len(parts) >= 4 and lowered[2] == "resourcegroups":
        return "group"
    if len(parts) >= 2 and lowered[0] == "subscriptions":
        return "subscription"
    return "resource"


def _subscription_scope(subscription_id: str) -> str:
    return f"/subscriptions/{_lower(subscription_id)}"


def _group_scope(subscription_id: str, resource_group: str) -> str:
    return f"/subscriptions/{_lower(subscription_id)}/resourcegroups/{_lower(resource_group)}"


def _containers_for(resource: dict[str, Any]) -> list[str]:
    """Container scopes (RG then subscription) that can carry a multi-resource rule."""
    sub = _lower(resource.get("subscriptionId"))
    rg = _lower(resource.get("resourceGroup"))
    out: list[str] = []
    if sub and rg:
        out.append(_group_scope(sub, rg))
    if sub:
        out.append(_subscription_scope(sub))
    return out


# --------------------------------------------------------------------------- rule parsing
def _criteria_clauses(props: dict[str, Any]) -> list[dict[str, Any]]:
    crit = props.get("criteria")
    if not isinstance(crit, dict):
        return []
    return [clause for clause in (crit.get("allOf") or []) if isinstance(clause, dict)]


def _alert_metric_names(props: dict[str, Any]) -> set[str]:
    """Metric names referenced by a metric alert's criteria (lowercased)."""
    names: set[str] = set()
    for clause in _criteria_clauses(props):
        metric = clause.get("metricName") or clause.get("metricname")
        if metric:
            names.add(_lower(metric))
    return names


def _alert_criterion_types(props: dict[str, Any]) -> set[str]:
    """Static vs dynamic threshold criteria declared by a metric alert."""
    kinds: set[str] = set()
    for clause in _criteria_clauses(props):
        kind = clause.get("criterionType") or clause.get("criteriontype")
        if kind:
            kinds.add(str(kind))
        elif clause.get("alertSensitivity") is not None:
            kinds.add("DynamicThresholdCriterion")
        elif clause.get("threshold") is not None:
            kinds.add("StaticThresholdCriterion")
    crit = props.get("criteria")
    if isinstance(crit, dict) and "dynamic" in _lower(crit.get("odata.type")):
        kinds.add("DynamicThresholdCriterion")
    return kinds


def _alert_sensitivities(props: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for clause in _criteria_clauses(props):
        sensitivity = clause.get("alertSensitivity")
        if sensitivity:
            out.add(str(sensitivity))
    return out


def _alert_thresholds(props: dict[str, Any]) -> list[float]:
    """Numeric thresholds declared in an alert's criteria."""
    out: list[float] = []
    for clause in _criteria_clauses(props):
        if clause.get("threshold") is not None:
            try:
                out.append(float(clause["threshold"]))
            except (TypeError, ValueError):
                pass
    return out


def _alert_operators(props: dict[str, Any]) -> set[str]:
    return {str(c["operator"]) for c in _criteria_clauses(props) if c.get("operator")}


def _alert_dimensions(props: dict[str, Any]) -> set[tuple[str, str]]:
    """Dimension name/value pairs referenced by metric alert criteria."""
    out: set[tuple[str, str]] = set()
    for clause in _criteria_clauses(props):
        for dimension in clause.get("dimensions") or []:
            if not isinstance(dimension, dict):
                continue
            name = _lower(dimension.get("name"))
            for value in dimension.get("values") or []:
                if name and str(value).strip():
                    out.add((name, _lower(value)))
    return out


def _action_group_ids(props: dict[str, Any]) -> list[str]:
    """Action group resource ids wired to a rule, across all three rule shapes."""
    out: list[str] = []
    actions = props.get("actions")
    if isinstance(actions, list):
        for action in actions:
            if isinstance(action, dict):
                gid = (
                    action.get("actionGroupId")
                    or action.get("actiongroupid")
                    or action.get("actionGroupID")
                )
                if gid:
                    out.append(_lower(gid))
                for gid2 in action.get("actionGroups") or []:
                    if gid2:
                        out.append(_lower(gid2))
            elif isinstance(action, str) and action:
                out.append(_lower(action))
    elif isinstance(actions, dict):
        for gid in actions.get("actionGroups") or actions.get("actiongroups") or []:
            if isinstance(gid, str) and gid:
                out.append(_lower(gid))
            elif isinstance(gid, dict) and gid.get("actionGroupId"):
                out.append(_lower(gid["actionGroupId"]))
    return out


def _log_query_text(props: dict[str, Any]) -> str:
    """The KQL of a scheduled query rule (the schema differs across API versions)."""
    for clause in _criteria_clauses(props):
        if clause.get("query"):
            return str(clause["query"])
    source = props.get("source")
    if isinstance(source, dict) and source.get("query"):
        return str(source["query"])
    if props.get("query"):
        return str(props["query"])
    return ""


_TABLE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\||$)", re.MULTILINE)
_OPERAND_RE = re.compile(
    r"\b(?:Name|MetricName|CounterName|ObjectName|InstanceName|Namespace|Category|RunbookName_s)\b"
    r"\s*(?:==|=~|has|contains)\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)
_LITERAL_RE = re.compile(r"[\"']([^\"']{3,})[\"']")
_KQL_KEYWORDS = {
    "let", "where", "extend", "summarize", "project", "sort", "order", "union", "join",
    "print", "range", "datatable", "search", "parse", "mv", "top", "limit", "take", "count",
}


def _query_signature(query: str) -> tuple[str, frozenset[str]]:
    """(primary table, discriminating literals) used to match a log alert to a baseline entry."""
    text = query or ""
    table = ""
    for match in _TABLE_RE.finditer(text):
        candidate = match.group(1)
        if candidate.lower() in _KQL_KEYWORDS:
            continue
        table = candidate.lower()
        break

    tokens = {_lower(m) for m in _OPERAND_RE.findall(text)}
    if not tokens:
        tokens = {_lower(m) for m in _LITERAL_RE.findall(text)}
    tokens = {t for t in tokens if t and t not in _GENERIC_QUERY_TOKENS}
    return table, frozenset(tokens)


def _activity_condition(props: dict[str, Any]) -> dict[str, list[str]]:
    """Flatten an activity log alert's condition into {field: [accepted values]}."""
    out: dict[str, list[str]] = {}

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            field_name = node.get("field")
            if field_name:
                values: list[str] = []
                if node.get("equals") is not None:
                    values.append(_lower(node["equals"]))
                for value in node.get("containsAny") or []:
                    values.append(_lower(value))
                if values:
                    out.setdefault(_lower(field_name), []).extend(values)
            for key in ("allOf", "anyOf"):
                for child in node.get(key) or []:
                    _walk(child)
        elif isinstance(node, list):
            for child in node:
                _walk(child)

    _walk(props.get("condition"))
    return out


@dataclass
class RuleDescriptor:
    """A normalized alert rule, independent of which of the three ARM shapes it came from."""

    id: str
    name: str
    arm_type: str
    kind: str  # metric | log | activitylog
    enabled: bool
    scopes: list[str] = field(default_factory=list)
    severity: int | None = None
    metric_names: set[str] = field(default_factory=set)
    criterion_types: set[str] = field(default_factory=set)
    sensitivities: set[str] = field(default_factory=set)
    thresholds: list[float] = field(default_factory=list)
    operators: set[str] = field(default_factory=set)
    dimensions: set[tuple[str, str]] = field(default_factory=set)
    action_group_ids: list[str] = field(default_factory=list)
    target_resource_type: str = ""
    target_resource_region: str = ""
    query_table: str = ""
    query_tokens: frozenset[str] = frozenset()
    activity: dict[str, list[str]] = field(default_factory=dict)
    window_size: str = ""
    evaluation_frequency: str = ""


def _describe_rule(raw: dict[str, Any]) -> RuleDescriptor | None:
    props = raw.get("properties")
    if not isinstance(props, dict):
        return None
    arm_type = _lower(raw.get("type"))
    kind = {_METRIC_RULE: "metric", _LOG_RULE: "log", _ACTIVITY_RULE: "activitylog"}.get(arm_type)
    if kind is None:
        return None

    scopes = props.get("scopes") or []
    if isinstance(scopes, str):
        scopes = [scopes]
    scopes = [_lower(s) for s in scopes if s]

    enabled = props.get("enabled")
    enabled = True if enabled is None else bool(enabled)

    severity = props.get("severity")
    try:
        severity = int(severity) if severity is not None else None
    except (TypeError, ValueError):
        severity = None

    descriptor = RuleDescriptor(
        id=str(raw.get("id") or ""),
        name=str(raw.get("name") or ""),
        arm_type=arm_type,
        kind=kind,
        enabled=enabled,
        scopes=scopes,
        severity=severity,
        action_group_ids=_action_group_ids(props),
        window_size=str(props.get("windowSize") or ""),
        evaluation_frequency=str(props.get("evaluationFrequency") or ""),
    )

    if kind == "metric":
        descriptor.metric_names = _alert_metric_names(props)
        descriptor.criterion_types = _alert_criterion_types(props)
        descriptor.sensitivities = _alert_sensitivities(props)
        descriptor.thresholds = _alert_thresholds(props)
        descriptor.operators = _alert_operators(props)
        descriptor.dimensions = _alert_dimensions(props)
        descriptor.target_resource_type = _lower(props.get("targetResourceType"))
        descriptor.target_resource_region = _lower(props.get("targetResourceRegion"))
    elif kind == "log":
        descriptor.query_table, descriptor.query_tokens = _query_signature(_log_query_text(props))
        descriptor.thresholds = _alert_thresholds(props)
        descriptor.operators = _alert_operators(props)
    else:
        descriptor.activity = _activity_condition(props)

    return descriptor


@dataclass
class SuppressionRule:
    """An enabled alert processing rule that removes action groups (i.e. mutes notifications)."""

    id: str
    name: str
    scopes: list[str]
    unconditional: bool
    conditions: dict[str, list[str]]


def _describe_action_rule(raw: dict[str, Any]) -> SuppressionRule | None:
    props = raw.get("properties")
    if not isinstance(props, dict):
        return None
    if props.get("enabled") is False:
        return None

    suppresses = False
    for action in props.get("actions") or []:
        if isinstance(action, dict) and _lower(action.get("actionType")) == "removeallactiongroups":
            suppresses = True
        elif isinstance(action, str) and _lower(action) == "removeallactiongroups":
            suppresses = True
    if not suppresses:
        return None

    conditions: dict[str, list[str]] = {}
    for cond in props.get("conditions") or []:
        if not isinstance(cond, dict):
            continue
        field_name = _lower(cond.get("field"))
        values = [_lower(v) for v in (cond.get("values") or [])]
        if field_name and values:
            conditions.setdefault(field_name, []).extend(values)

    scopes = props.get("scopes") or []
    if isinstance(scopes, str):
        scopes = [scopes]
    return SuppressionRule(
        id=str(raw.get("id") or ""),
        name=str(raw.get("name") or ""),
        scopes=[_lower(s) for s in scopes if s],
        unconditional=not conditions,
        conditions=conditions,
    )


_RECEIVER_KEYS = (
    "emailReceivers", "smsReceivers", "webhookReceivers", "azureAppPushReceivers",
    "itsmReceivers", "automationRunbookReceivers", "voiceReceivers", "logicAppReceivers",
    "azureFunctionReceivers", "armRoleReceivers", "eventHubReceivers",
)


def _action_group_usable(raw: dict[str, Any]) -> bool:
    """An action group only notifies if it is enabled and has at least one receiver."""
    props = raw.get("properties")
    if not isinstance(props, dict):
        return False
    if props.get("enabled") is False:
        return False
    return any(
        isinstance(props.get(key), list) and props.get(key) for key in _RECEIVER_KEYS
    )


@dataclass
class AlertIndex:
    """Alert rules bucketed by the scope they target, plus notification-plumbing facts."""

    by_resource: dict[str, list[RuleDescriptor]] = field(default_factory=dict)
    by_container: dict[str, list[RuleDescriptor]] = field(default_factory=dict)
    suppressions: list[SuppressionRule] = field(default_factory=list)
    # action group id -> whether it is enabled and has at least one receiver
    action_groups: dict[str, bool] = field(default_factory=dict)


def _index_alerts(alerts: list[dict[str, Any]]) -> AlertIndex:
    """Bucket every rule by the scope it targets so per-resource lookup is cheap."""
    index = AlertIndex()
    for raw in alerts:
        arm_type = _lower(raw.get("type"))
        if arm_type == _ACTION_GROUP:
            index.action_groups[_lower(raw.get("id"))] = _action_group_usable(raw)
            continue
        if arm_type == _ACTION_RULE:
            rule = _describe_action_rule(raw)
            if rule is not None:
                index.suppressions.append(rule)
            continue

        descriptor = _describe_rule(raw)
        if descriptor is None:
            continue
        for scope in descriptor.scopes:
            if _scope_kind(scope) == "resource":
                index.by_resource.setdefault(scope, []).append(descriptor)
            else:
                index.by_container.setdefault(scope, []).append(descriptor)
    return index


def _rules_for_resource(
    resource: dict[str, Any], resource_type: str, index: AlertIndex
) -> list[RuleDescriptor]:
    """Rules that can fire for this resource: directly scoped, plus multi-resource rules."""
    rid = _lower(resource.get("id"))
    location = _lower(resource.get("location"))
    out: list[RuleDescriptor] = list(index.by_resource.get(rid, []))
    seen = {id(r) for r in out}

    for container in _containers_for(resource):
        for rule in index.by_container.get(container, []):
            if id(rule) in seen:
                continue
            if rule.kind == "metric":
                # A container-scoped metric alert only covers resources of its declared
                # targetResourceType (and region, when pinned).
                if not rule.target_resource_type or rule.target_resource_type != resource_type:
                    continue
                if rule.target_resource_region and location and rule.target_resource_region != location:
                    continue
            seen.add(id(rule))
            out.append(rule)
    return out


def _suppressions_for(resource: dict[str, Any], index: AlertIndex) -> list[SuppressionRule]:
    """Enabled suppression rules whose scope covers this resource."""
    covering = {_lower(resource.get("id")), *_containers_for(resource)}
    return [rule for rule in index.suppressions if any(scope in covering for scope in rule.scopes)]


# --------------------------------------------------------------------------- matching
def _recommended_dimensions(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Baseline dimensions, accepting either the structured list or the legacy filter string."""
    dims = rec.get("dimensions")
    if isinstance(dims, list) and dims:
        out = []
        for dim in dims:
            if isinstance(dim, dict) and dim.get("name"):
                out.append(
                    {
                        "name": str(dim["name"]),
                        "operator": str(dim.get("operator") or "Include"),
                        "values": [str(v) for v in (dim.get("values") or [])],
                    }
                )
        return out
    expression = str(rec.get("dimension_filter") or "").strip()
    match = re.fullmatch(r"\s*([\w.]+)\s+eq\s+'([^']+)'\s*", expression, re.IGNORECASE)
    if not match:
        return []
    return [{"name": match.group(1), "operator": "Include", "values": [match.group(2)]}]


def _recommended_aggregation(resource_type: str, rec: dict[str, Any]) -> str:
    if rec.get("time_aggregation"):
        return str(rec["time_aggregation"])
    if rec.get("aggregation"):
        return str(rec["aggregation"])
    if str(rec.get("alert_type") or "metric") != "metric" or not rec.get("metric"):
        return ""
    from app.perfprofile.metrics_map import metric_semantics

    return str(
        metric_semantics(resource_type, str(rec.get("metric") or ""), str(rec.get("unit") or ""))[
            "aggregation"
        ]
    )


def _effective_threshold(rec: dict[str, Any], tags: dict[str, Any]) -> tuple[float | None, str]:
    """Baseline threshold, honouring an AMBA-ALZ ``_amba-<metric>-threshold-Override_`` tag.

    Returns (threshold, the tag name when an override was applied)."""
    baseline = rec.get("threshold")
    tag_name = str(rec.get("threshold_override_tag") or "").strip().lower()
    if not tag_name or not tags:
        return baseline, ""
    for key, value in tags.items():
        if str(key).strip().lower() != tag_name:
            continue
        try:
            return float(str(value).strip()), str(key)
        except (TypeError, ValueError):
            return baseline, ""
    return baseline, ""


def _candidates(rec: dict[str, Any], rules: Iterable[RuleDescriptor]) -> list[RuleDescriptor]:
    """Rules that plausibly implement this recommendation — never 'any rule on the resource'."""
    kind = str(rec.get("alert_type") or "metric")

    if kind == "metric":
        metric = _lower(rec.get("metric"))
        if not metric:
            return []
        found = [r for r in rules if r.kind == "metric" and metric in r.metric_names]
        for dimension in _recommended_dimensions(rec):
            required = {(_lower(dimension["name"]), _lower(v)) for v in dimension.get("values") or []}
            if required:
                found = [r for r in found if required.issubset(r.dimensions)]
        return found

    if kind == "log":
        table, tokens = _query_signature(str(rec.get("log_query") or ""))
        if not table:
            return []
        found = []
        for rule in rules:
            if rule.kind != "log" or rule.query_table != table:
                continue
            # Require the discriminating operands to line up; a bare table match would let
            # an unrelated query satisfy the recommendation (the old "any rule" bug).
            if tokens and not tokens.issubset(rule.query_tokens):
                continue
            found.append(rule)
        return found

    wanted = rec.get("activity_log") or {}
    category = _lower(wanted.get("category"))
    if not category:
        return []
    operation = _lower(wanted.get("operationName"))
    incident = _lower(wanted.get("incidentType"))
    found = []
    for rule in rules:
        if rule.kind != "activitylog":
            continue
        observed = rule.activity
        if category not in observed.get("category", []):
            continue
        if operation and operation not in observed.get("operationname", []):
            continue
        if incident:
            seen_incident = observed.get("properties.incidenttype", []) + observed.get(
                "properties.incidenttype_", []
            )
            if incident not in seen_incident:
                continue
        found.append(rule)
    return found


def _match_status(
    rec: dict[str, Any],
    rules: list[RuleDescriptor],
    index: AlertIndex,
    suppressions: list[SuppressionRule],
    tags: dict[str, Any],
    options: CoverageOptions,
) -> tuple[str, dict[str, Any]]:
    """Classify a recommended alert against the rules that can fire for a resource.

    Returns (status, observed) where observed carries the matched rule's facts for the UI
    drawer (name, enabled, action group, observed threshold, suppression, …)."""
    candidates = _candidates(rec, rules)
    if not candidates:
        return STATUS_MISSING, {}

    def _wired(rule: RuleDescriptor) -> bool:
        # Unknown action groups (outside the scanned subscriptions) are assumed usable.
        return any(index.action_groups.get(gid, True) for gid in rule.action_group_ids)

    # Prefer an enabled rule with a usable action group as the "best" observed state.
    best = next((r for r in candidates if r.enabled and _wired(r)), None)
    best = best or next((r for r in candidates if r.enabled), None) or candidates[0]

    has_action_group = bool(best.action_group_ids)
    usable_action_group = _wired(best)
    threshold, override_tag = _effective_threshold(rec, tags)

    observed: dict[str, Any] = {
        "rule_id": best.id,
        "rule_name": best.name,
        "rule_type": best.arm_type,
        "enabled": best.enabled,
        "has_action_group": has_action_group,
        "action_group_usable": usable_action_group,
        "observed_thresholds": best.thresholds,
        "observed_criterion_types": sorted(best.criterion_types),
        "observed_sensitivities": sorted(best.sensitivities),
        "observed_severity": best.severity,
        "observed_window": best.window_size,
        "observed_frequency": best.evaluation_frequency,
        "matched_rules": len(candidates),
        "threshold_override_tag": override_tag,
        "effective_threshold": threshold,
    }

    issues: list[str] = []
    if not best.enabled:
        issues.append("disabled")
    if rec.get("requires_action_group", True):
        if not has_action_group:
            issues.append("no action group")
        elif not usable_action_group:
            issues.append("action group has no receivers")

    expected_criterion = str(rec.get("criterion_type") or "")
    if expected_criterion and best.criterion_types and expected_criterion not in best.criterion_types:
        issues.append(
            "dynamic threshold recommended"
            if expected_criterion == "DynamicThresholdCriterion"
            else "static threshold recommended"
        )
    elif threshold is not None and best.thresholds:
        tol = abs(float(threshold)) * (options.tolerance_pct / 100.0)
        if not any(abs(t - float(threshold)) <= tol for t in best.thresholds):
            issues.append("threshold differs from override tag" if override_tag else "threshold differs from baseline")

    if options.severity_counts_as_gap:
        expected_sev = rec.get("severity_num")
        if isinstance(expected_sev, int) and best.severity is not None and best.severity != expected_sev:
            issues.append(f"severity {best.severity} differs from baseline {expected_sev}")

    if suppressions:
        observed["suppressed_by"] = [
            {"id": s.id, "name": s.name, "unconditional": s.unconditional} for s in suppressions
        ]
        if any(s.unconditional for s in suppressions):
            observed["issues"] = issues + ["notifications muted by an alert processing rule"]
            return STATUS_SUPPRESSED, observed
        issues.append("may be muted by an alert processing rule")

    observed["issues"] = issues
    return (STATUS_MISCONFIGURED if issues else STATUS_PRESENT), observed


# --------------------------------------------------------------------------- ARG queries
# Ceiling for the paged alert-rule collection. Matches the estate-wide ceiling used by
# ``query_resources_batched``; large tenants legitimately hold thousands of rules.
_ALERT_QUERY_MAX_ROWS = 10_000


async def _query_resources(predicates: list[str], connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    from app.assessments.runner import query_resources_batched

    return await query_resources_batched(
        predicates,
        connection,
        projection="id, name, type, resourceGroup, subscriptionId, location, tags",
    )


async def _query_alerts(subscriptions: list[str], connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Alert rules, action groups and alert processing rules across the in-scope subscriptions.

    Uses the PAGED, retrying collector rather than a single capture. Two reasons:

    - **Correctness.** The old ``take 5000`` was never honoured: both the REST and CLI capture
      paths cap a single page at 1000 rows (``KQL_MAX_ROWS`` / ``--first 1000``, and ARG itself
      caps a page at 1000). A tenant with more than 1000 alert rules + action groups +
      processing rules silently lost the remainder, and every rule that fell off the end made
      its resource read as MISSING. Adding action groups and processing rules to this query made
      hitting that ceiling considerably more likely.
    - **Resilience.** The capture path has no retry, so one 429 aborted the entire scan into an
      empty errored snapshot. ``run_kql_collect`` retries throttling with backoff.

    Ordered by id so ``$skipToken`` paging is deterministic.
    """
    from app.exec.command_runner import run_kql_collect

    if not subscriptions:
        return []
    joined = ", ".join(f"'{_esc(s)}'" for s in subscriptions)
    kql = (
        "resources "
        f"| where type in~ ('{_METRIC_RULE}', '{_LOG_RULE}', '{_ACTIVITY_RULE}', "
        f"'{_ACTION_GROUP}', '{_ACTION_RULE}') "
        f"| where subscriptionId in~ ({joined}) "
        "| project id, name, type, properties | order by id asc"
    )
    res = await run_kql_collect(kql, connection, max_rows=_ALERT_QUERY_MAX_ROWS)
    if not res.ok:
        raise RuntimeError(res.error or "Alert-rule query failed.")
    return res.rows


# --------------------------------------------------------------------------- public API
def subscription_rows(subscriptions: Iterable[str]) -> list[dict[str, Any]]:
    """Synthetic ``microsoft.resources/subscriptions`` rows.

    Service Health, Resource Health and subscription-scoped Activity Log alerts have no
    resource to hang off, so the baseline scores them against one row per subscription."""
    rows: list[dict[str, Any]] = []
    for sub in subscriptions:
        sub_id = _lower(sub)
        if not sub_id:
            continue
        rows.append(
            {
                "id": _subscription_scope(sub_id),
                "name": f"Subscription {sub_id[:8]}…",
                "type": SUBSCRIPTION_TYPE,
                "resourceGroup": "",
                "subscriptionId": sub_id,
                "location": "global",
                "tags": {},
            }
        )
    return rows


def _is_scorable(rec: dict[str, Any]) -> bool:
    """Can this baseline entry be evaluated against real Azure alert rules?

    ``builtin_seed`` computes ``deployable`` when it merges the catalog, but an admin can
    hand-edit the reference set, so the same invariants are re-checked at scoring time:
    guidance-only entries (no metric name, or a static metric alert with no threshold) are
    kept in the reference for context but never scored as a gap."""
    if rec.get("deployable") is False:
        return False
    kind = str(rec.get("alert_type") or "metric")
    if kind == "activitylog":
        return bool((rec.get("activity_log") or {}).get("category"))
    if kind == "log":
        return bool(rec.get("log_query"))
    if not str(rec.get("metric") or "").strip():
        return False
    if str(rec.get("criterion_type") or "") == "DynamicThresholdCriterion":
        return True
    return rec.get("threshold") is not None


def _scored_alerts(spec: dict[str, Any], options: CoverageOptions) -> list[dict[str, Any]]:
    """Baseline entries in scope for scoring: scorable, in an enabled tier, in the pattern."""
    out: list[dict[str, Any]] = []
    for rec in spec.get("alerts") or []:
        if not _is_scorable(rec):
            continue
        if options.tiers and str(rec.get("tier") or "recommended") not in options.tiers:
            continue
        if options.patterns and not any(p in options.patterns for p in (rec.get("patterns") or [])):
            continue
        out.append(rec)
    return out


def _is_monitor_disabled(resource: dict[str, Any]) -> bool:
    tags = resource.get("tags")
    if not isinstance(tags, dict):
        return False
    return any(
        str(key).strip().lower() == MONITOR_DISABLE_TAG
        and str(value).strip().lower() in {"true", "yes", "1"}
        for key, value in tags.items()
    )


def compute_coverage(
    resources: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    *,
    misconfig_counts_as_gap: bool = True,
    tolerance_pct: float = 10.0,
    reference: dict[str, Any] | None = None,
    options: CoverageOptions | None = None,
    subscriptions: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Pure coverage computation over already-fetched resources + alert rules.

    ``resources``: [{id,name,type,resourceGroup,subscriptionId,location,tags}]
    ``alerts``: ARG rows [{id,name,type,properties}] covering metric/log/activity-log rules,
    action groups and alert processing rules.
    Returns the full snapshot (kpis, per-type groups, rows with per-alert cells, gaps)."""
    if options is None:
        options = CoverageOptions(
            misconfig_counts_as_gap=misconfig_counts_as_gap, tolerance_pct=tolerance_pct
        )
    ref = reference if reference is not None else load_reference()
    ref_types: dict[str, Any] = ref.get("types", {})
    index = _index_alerts(alerts)

    scoped_resources = list(resources)
    if subscriptions:
        known = {_lower(r.get("id")) for r in scoped_resources}
        scoped_resources.extend(
            row for row in subscription_rows(subscriptions) if _lower(row["id"]) not in known
        )

    groups: dict[str, dict[str, Any]] = {}
    total_present = total_missing = total_misconfig = total_suppressed = 0
    covered_units = 0.0
    total_units = 0
    gaps: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    for res in scoped_resources:
        rtype = _lower(res.get("type"))
        spec = ref_types.get(rtype)
        if not spec:
            continue  # type not in the baseline reference — not scored
        rec_alerts = _scored_alerts(spec, options)
        if not rec_alerts:
            continue

        if options.honor_monitor_disable_tag and _is_monitor_disabled(res):
            excluded.append(
                {
                    "resource_id": res.get("id", ""),
                    "resource_name": res.get("name", ""),
                    "resource_type": rtype,
                    "reason": f"{MONITOR_DISABLE_TAG} tag",
                }
            )
            continue

        tags = res.get("tags") if isinstance(res.get("tags"), dict) else {}
        rules = _rules_for_resource(res, rtype, index)
        suppressions = _suppressions_for(res, index)

        cells: list[dict[str, Any]] = []
        for rec in rec_alerts:
            status, observed = _match_status(rec, rules, index, suppressions, tags, options)
            total_units += 1
            if status == STATUS_PRESENT:
                total_present += 1
                covered_units += 1.0
            elif status == STATUS_MISCONFIGURED:
                total_misconfig += 1
                covered_units += 0.0 if options.misconfig_counts_as_gap else 0.5
            elif status == STATUS_SUPPRESSED:
                total_suppressed += 1
            else:
                total_missing += 1

            recommended = {
                "metric": rec.get("metric", ""),
                "metric_namespace": rec.get("metric_namespace", ""),
                "operator": rec.get("operator", ""),
                "threshold": rec.get("threshold"),
                "unit": rec.get("unit", ""),
                "window": rec.get("window_size") or rec.get("window", ""),
                "window_size": rec.get("window_size", ""),
                "evaluation_frequency": rec.get("evaluation_frequency", ""),
                "aggregation": _recommended_aggregation(rtype, rec),
                "criterion_type": rec.get("criterion_type", ""),
                "alert_sensitivity": rec.get("alert_sensitivity"),
                "failing_periods": rec.get("failing_periods"),
                "dimensions": _recommended_dimensions(rec),
                "requires_action_group": rec.get("requires_action_group", True),
                "activity_log": rec.get("activity_log") or {},
                "log_query": rec.get("log_query", ""),
                "threshold_override_tag": rec.get("threshold_override_tag", ""),
            }
            cell = {
                "alert_key": rec["key"],
                "alert_name": rec["name"],
                "alert_type": rec.get("alert_type", "metric"),
                "amba_category": rec["amba_category"],
                "severity": rec["severity"],
                "severity_num": rec.get("severity_num"),
                "tier": rec.get("tier", "recommended"),
                "patterns": rec.get("patterns") or [],
                "source": rec.get("source", "amba"),
                "guid": rec.get("guid", ""),
                "status": status,
                "recommended": recommended,
                "observed": observed,
                "why": rec.get("why", ""),
                "references": rec.get("references") or [],
            }
            cells.append(cell)
            if status != STATUS_PRESENT:
                gaps.append(
                    {
                        "resource_id": res.get("id", ""),
                        "resource_name": res.get("name", ""),
                        "resource_type": rtype,
                        "resource_group": res.get("resourceGroup", ""),
                        "subscription_id": res.get("subscriptionId", ""),
                        "location": res.get("location", ""),
                        "alert_key": rec["key"],
                        "alert_name": rec["name"],
                        "alert_type": rec.get("alert_type", "metric"),
                        "amba_category": rec["amba_category"],
                        "severity": rec["severity"],
                        "severity_num": rec.get("severity_num"),
                        "tier": rec.get("tier", "recommended"),
                        "status": status,
                        "recommended": recommended,
                        "observed": observed,
                        "why": rec.get("why", ""),
                    }
                )

        g = groups.setdefault(
            rtype,
            {
                "resource_type": rtype,
                "display": spec.get("display", rtype),
                "category": spec.get("category", "other"),
                "source": spec.get("source", "amba"),
                "recommended_alerts": [
                    {
                        "key": a["key"],
                        "name": a["name"],
                        "amba_category": a["amba_category"],
                        "severity": a["severity"],
                        "alert_type": a.get("alert_type", "metric"),
                        "tier": a.get("tier", "recommended"),
                        "source": a.get("source", "amba"),
                    }
                    for a in rec_alerts
                ],
                "rows": [],
                "present": 0,
                "missing": 0,
                "misconfigured": 0,
                "suppressed": 0,
            },
        )
        g["present"] += sum(1 for c in cells if c["status"] == STATUS_PRESENT)
        g["missing"] += sum(1 for c in cells if c["status"] == STATUS_MISSING)
        g["misconfigured"] += sum(1 for c in cells if c["status"] == STATUS_MISCONFIGURED)
        g["suppressed"] += sum(1 for c in cells if c["status"] == STATUS_SUPPRESSED)
        g["rows"].append(
            {
                "resource_id": res.get("id", ""),
                "resource_name": res.get("name", ""),
                "resource_group": res.get("resourceGroup", ""),
                "subscription_id": res.get("subscriptionId", ""),
                "location": res.get("location", ""),
                "tags": res.get("tags") or {},
                "cells": cells,
            }
        )

    def _grp_pct(g: dict[str, Any]) -> int:
        denom = g["present"] + g["missing"] + g["misconfigured"] + g["suppressed"]
        if denom == 0:
            return 100
        cov = g["present"] + (0.0 if options.misconfig_counts_as_gap else 0.5) * g["misconfigured"]
        return round(100 * cov / denom)

    group_list = sorted(groups.values(), key=lambda g: g["display"].lower())
    for g in group_list:
        g["coverage_pct"] = _grp_pct(g)

    coverage_pct = round(100 * covered_units / total_units) if total_units else 100
    gaps.sort(key=lambda x: (_SEVERITY_RANK.get(x["severity"], 3), x["resource_type"], x["resource_name"]))

    return {
        "generated_at": _now_iso(),
        "coverage_pct": coverage_pct,
        "baseline": {
            "amba_release": ref.get("amba_release", ""),
            "version": ref.get("version", 0),
            "tiers": list(options.tiers),
            "patterns": list(options.patterns),
        },
        "kpis": {
            "total_resources_in_baseline": sum(len(g["rows"]) for g in group_list),
            "alerts_present": total_present,
            "alerts_missing": total_missing,
            "alerts_misconfigured": total_misconfig,
            "alerts_suppressed": total_suppressed,
            "recommended_total": total_units,
            "action_groups": len(index.action_groups),
            "action_groups_usable": sum(1 for ok in index.action_groups.values() if ok),
            "suppression_rules": len(index.suppressions),
            "resources_excluded": len(excluded),
        },
        "groups": group_list,
        "gaps": gaps,
        "excluded_resources": excluded,
        "suppression_rules": [
            {"id": s.id, "name": s.name, "scopes": s.scopes, "unconditional": s.unconditional}
            for s in index.suppressions
        ],
        "all_resources": build_all_resources(scoped_resources, ref_types),
    }


async def collect_coverage(
    connection: dict[str, Any] | None,
    *,
    scope_kind: str,
    scope_id: str,
    workload: dict[str, Any] | None,
    misconfig_counts_as_gap: bool = True,
    tolerance_pct: float = 10.0,
    options: CoverageOptions | None = None,
) -> dict[str, Any]:
    """Resolve the scope, query ARG for resources + alert rules, and compute coverage."""
    from app.assessments.runner import _resolve_scope, scope_predicate_batches  # proven scope resolver

    if options is None:
        options = CoverageOptions(
            misconfig_counts_as_gap=misconfig_counts_as_gap, tolerance_pct=tolerance_pct
        )

    subscriptions: list[str] = []
    if scope_kind == "workload" and workload is not None:
        scope = await _resolve_scope(workload, connection)
        predicate = scope.get("predicate") or ""
        subscriptions = list(scope.get("subscriptions") or [])
        for sub, _rg in scope.get("rg_pairs") or []:
            if sub not in subscriptions:
                subscriptions.append(sub)
        if scope.get("error") and not predicate:
            return _empty_snapshot(scope_kind, scope_id, error=scope["error"])
        predicates = scope_predicate_batches(scope)
    elif scope_kind == "subscription" and scope_id:
        predicates = [f"subscriptionId =~ '{_esc(scope_id)}'"]
        subscriptions = [scope_id]
    else:
        return _empty_snapshot(scope_kind, scope_id, error="No resolvable scope.")

    try:
        resources = await _query_resources(predicates, connection)
        # Alert rules can live in any RG within the in-scope subscriptions; collect by sub.
        sub_guids = subscriptions or sorted(
            {str(r.get("subscriptionId", "")) for r in resources if r.get("subscriptionId")}
        )
        alerts = await _query_alerts(sub_guids, connection)
    except RuntimeError as exc:
        return _empty_snapshot(scope_kind, scope_id, error=str(exc)[:300])

    snap = compute_coverage(resources, alerts, options=options, subscriptions=sub_guids)
    snap.update(
        {
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "scope_name": (workload or {}).get("name") if scope_kind == "workload" else scope_id,
            "connection_configured": connection is not None,
            "source": "azure_resource_graph",
            "demo": False,
            "error": "",
            "throttled": False,
        }
    )
    return snap


def _empty_snapshot(scope_kind: str, scope_id: str, *, error: str) -> dict[str, Any]:
    return {
        "generated_at": _now_iso(),
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "scope_name": scope_id,
        "connection_configured": False,
        "source": "azure_resource_graph",
        "demo": False,
        "coverage_pct": 0,
        "baseline": {"amba_release": "", "version": 0, "tiers": [], "patterns": []},
        "kpis": {
            "total_resources_in_baseline": 0,
            "alerts_present": 0,
            "alerts_missing": 0,
            "alerts_misconfigured": 0,
            "alerts_suppressed": 0,
            "recommended_total": 0,
            "action_groups": 0,
            "action_groups_usable": 0,
            "suppression_rules": 0,
            "resources_excluded": 0,
        },
        "groups": [],
        "gaps": [],
        "excluded_resources": [],
        "suppression_rules": [],
        "all_resources": [],
        "error": error,
        "throttled": _is_throttle_error(error),
    }
