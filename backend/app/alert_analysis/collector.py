"""Collect and analyze Azure Monitor alert-rule and notification proliferation.

The collection path is read-only Azure Resource Graph. ``compute_analysis`` is pure so
normalization, overlap classification, recipient privacy, and AMBA-backed gap logic can be
exercised without Azure access and reused by deterministic demo data.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from app.amba.collector import compute_coverage
from app.alert_analysis.pricing import empty_cost_summary, estimate_rule_cost, summarize_rule_costs

log = logging.getLogger("app.alert_analysis.collector")
SNAPSHOT_SCHEMA_VERSION = 3
ProgressCallback = Callable[[str, str], Awaitable[None]]

_ALERT_TYPES = {
    "microsoft.insights/metricalerts",
    "microsoft.insights/scheduledqueryrules",
    "microsoft.insights/activitylogalerts",
    "microsoft.alertsmanagement/smartdetectoralertrules",
    "microsoft.alertsmanagement/prometheusrules",
}
_SEVERITY_LABELS = {0: "critical", 1: "error", 2: "warning", 3: "informational", 4: "verbose"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _ci_get(value: dict[str, Any], *names: str, default: Any = None) -> Any:
    wanted = {name.lower() for name in names}
    for key, item in value.items():
        if str(key).lower() in wanted:
            return item
    return default


def _norm_id(value: Any) -> str:
    return str(value or "").strip().rstrip("/").lower()


def _parse_rows(stdout: str) -> list[dict[str, Any]]:
    from app.exec.command_runner import parse_kql_rows

    return parse_kql_rows(stdout)


def _esc(value: str) -> str:
    return (value or "").replace("'", "''")


def _scope_matches(scope: str, resource_id: str) -> bool:
    scope_id = _norm_id(scope)
    target_id = _norm_id(resource_id)
    return bool(scope_id and target_id and (target_id == scope_id or target_id.startswith(scope_id + "/")))


def _action_group_ids(properties: dict[str, Any]) -> list[str]:
    """Extract action-group resource IDs across metric/log/activity alert schemas."""
    found: list[str] = []

    def visit(value: Any, parent: str = "") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                lower = str(key).lower()
                if lower == "actiongroupid" and isinstance(item, str):
                    found.append(item)
                elif lower == "actiongroups":
                    visit(item, "actiongroups")
                else:
                    visit(item, lower)
        elif isinstance(value, list):
            for item in value:
                visit(item, parent)
        elif isinstance(value, str) and parent == "actiongroups":
            found.append(value)

    visit(_ci_get(properties, "actions", default=[]), "actions")
    visit(_ci_get(properties, "actionGroups", default=[]), "actiongroups")
    return list(dict.fromkeys(_norm_id(item) for item in found if _norm_id(item)))


def _rule_scopes(properties: dict[str, Any]) -> list[str]:
    raw = _ci_get(properties, "scopes", "scope", default=[])
    if isinstance(raw, str):
        raw = [raw]
    return list(dict.fromkeys(str(item).rstrip("/") for item in raw if item)) if isinstance(raw, list) else []


def _duration_minutes(value: Any) -> float | None:
    raw = str(value or "").upper()
    match = re.fullmatch(r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?", raw)
    if not match:
        return None
    return float(match.group(1) or 0) * 60 + float(match.group(2) or 0) + float(match.group(3) or 0) / 60


def _query_fingerprint(query: str, signal_type: str = "") -> str:
    if not query:
        return ""
    from app.alerts_manager.advisory import _kql_semantic_key, _promql_semantic_key

    return _promql_semantic_key(query) if signal_type == "prometheus" else _kql_semantic_key(query)


def _dimensions(clause: dict[str, Any]) -> list[str]:
    raw = _ci_get(clause, "dimensions", default=[])
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for dim in raw:
        item = _dict(dim)
        name = str(_ci_get(item, "name", default=""))
        operator = str(_ci_get(item, "operator", default=""))
        values = _ci_get(item, "values", default=[])
        if not isinstance(values, list):
            values = [values]
        out.append(f"{name.lower()}:{operator.lower()}:{','.join(sorted(str(v).lower() for v in values))}")
    return sorted(out)


def _normalize_conditions(rule_type: str, properties: dict[str, Any]) -> list[dict[str, Any]]:
    criteria = _dict(_ci_get(properties, "criteria", "condition", default={}))
    clauses = _ci_get(criteria, "allOf", default=[])
    if not isinstance(clauses, list) or not clauses:
        clauses = [criteria] if criteria else [{}]
    window = str(_ci_get(properties, "windowSize", "window_size", default=""))
    frequency = str(_ci_get(properties, "evaluationFrequency", "frequency", default=""))
    query = str(_ci_get(properties, "query", default="") or _ci_get(criteria, "query", default=""))
    out: list[dict[str, Any]] = []
    for raw_clause in clauses:
        clause = _dict(raw_clause)
        dynamic = "dynamic" in str(_ci_get(criteria, "odata.type", default="")).lower() or "dynamic" in str(_ci_get(clause, "criterionType", default="")).lower()
        detector = _dict(_ci_get(properties, "detector", default={}))
        metric = str(
            _ci_get(
                clause,
                "metricName",
                "metricMeasureColumn",
                "field",
                default=("log query" if "scheduledqueryrules" in rule_type else "activity log" if "activitylogalerts" in rule_type else ""),
            )
        )
        if "smartdetector" in rule_type:
            metric = str(_ci_get(detector, "id", default="smart detector")).rstrip("/").split("/")[-1]
        clause_query = str(_ci_get(clause, "query", default=query))
        threshold = _ci_get(clause, "threshold", default=None)
        try:
            threshold = float(threshold) if threshold is not None else None
        except (TypeError, ValueError):
            threshold = str(threshold)
        operator = str(_ci_get(clause, "operator", default=""))
        aggregation = str(_ci_get(clause, "timeAggregation", "timeAggregationMethod", default=""))
        signal_type = (
            "prometheus" if "prometheusrules" in rule_type
            else "smart" if "smartdetector" in rule_type
            else "metric" if "metricalerts" in rule_type
            else "log" if "scheduledqueryrules" in rule_type
            else "activity"
        )
        signal_key = _query_fingerprint(clause_query, signal_type) if signal_type in {"log", "prometheus"} and clause_query else metric.strip().lower()
        condition = {
            "signal_type": signal_type,
            "signal_name": metric or signal_type,
            "signal_key": signal_key or signal_type,
            "operator": operator,
            "threshold": threshold,
            "aggregation": aggregation,
            "window": window,
            "frequency": frequency,
            "window_minutes": _duration_minutes(window),
            "dimensions": _dimensions(clause),
            "dynamic": dynamic,
            "query_fingerprint": _query_fingerprint(clause_query, signal_type),
        }
        exact_payload = {
            key: condition[key]
            for key in ("signal_type", "signal_key", "operator", "threshold", "aggregation", "window", "dimensions", "dynamic")
        }
        near_payload = {
            key: condition[key]
            for key in ("signal_type", "signal_key", "operator", "aggregation", "dynamic")
        }
        condition["exact_signature"] = json.dumps(exact_payload, sort_keys=True, default=str)
        condition["near_signature"] = json.dumps(near_payload, sort_keys=True, default=str)
        out.append(condition)
    return out


def _prometheus_threshold(expression: str) -> tuple[str, float | None]:
    """Best-effort terminal comparison extraction; raw PromQL remains authoritative."""
    match = re.search(r"(?:^|\s)(>=|<=|==|!=|>|<)\s*(-?\d+(?:\.\d+)?)\s*$", expression or "")
    if not match:
        return "", None
    return match.group(1), float(match.group(2))


def _expand_alert_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expand Prometheus rule-group resources into one normal alert record per rule."""
    expanded: list[dict[str, Any]] = []
    for raw in rows:
        rule_type = str(raw.get("type", "")).lower()
        if "prometheusrulegroups" not in rule_type:
            expanded.append(raw)
            continue
        props = _dict(raw.get("properties"))
        embedded = _ci_get(props, "rules", default=[])
        if not isinstance(embedded, list):
            continue
        group_scopes = _rule_scopes(props)
        group_frequency = str(_ci_get(props, "interval", "evaluationInterval", default=""))
        for index, raw_rule in enumerate(embedded):
            rule = _dict(raw_rule)
            alert_name = str(_ci_get(rule, "alert", "record", default=f"rule-{index + 1}"))
            expression = str(_ci_get(rule, "expr", "expression", default=""))
            operator, threshold = _prometheus_threshold(expression)
            labels = _dict(_ci_get(rule, "labels", default={}))
            severity_text = str(_ci_get(labels, "severity", default="warning")).lower()
            severity = {"critical": 0, "error": 1, "warning": 2, "info": 3, "informational": 3}.get(severity_text, 2)
            expanded.append(
                {
                    **raw,
                    "id": f"{str(raw.get('id', '')).rstrip('/')}/rules/{index}",
                    "name": alert_name,
                    "type": "microsoft.alertsmanagement/prometheusrules",
                    "properties": {
                        "enabled": bool(_ci_get(props, "enabled", default=True)) and not bool(_ci_get(rule, "enabled", default=True) is False),
                        "severity": severity,
                        "scopes": _ci_get(rule, "scopes", default=group_scopes),
                        "windowSize": _ci_get(rule, "for", default=""),
                        "evaluationFrequency": group_frequency,
                        "query": expression,
                        "criteria": {
                            "allOf": [
                                {
                                    "metricName": alert_name,
                                    "operator": operator,
                                    "threshold": threshold,
                                    "query": expression,
                                }
                            ]
                        },
                        "actions": _ci_get(rule, "actions", default=_ci_get(props, "actions", default=[])),
                    },
                }
            )
    return expanded


def _receiver_destination(receiver_type: str, receiver: dict[str, Any]) -> str:
    candidates: dict[str, tuple[str, ...]] = {
        "email": ("emailAddress", "email"),
        "sms": ("phoneNumber",),
        "voice": ("phoneNumber",),
        "webhook": ("serviceUri", "uri"),
        "azurefunction": ("functionAppResourceId", "functionName"),
        "logicapp": ("resourceId", "callbackUrl"),
        "eventhub": ("eventHubNameSpace", "eventHubName"),
        "armrole": ("roleId",),
        "automationrunbook": ("automationAccountId", "runbookName"),
        "itsm": ("workspaceId", "connectionId"),
        "azureapppush": ("emailAddress",),
    }
    values = [str(_ci_get(receiver, key, default="")) for key in candidates.get(receiver_type, ("name",))]
    return "|".join(value.strip().lower() for value in values if value.strip())


def _display_destination(receiver_type: str, destination: str) -> str:
    if not destination:
        return "configured destination"
    if receiver_type in {"email", "azureapppush", "sms", "voice"}:
        return destination
    if receiver_type in {"webhook", "logicapp"}:
        try:
            host = urlsplit(destination.split("|", 1)[0]).hostname
            return host or "masked endpoint"
        except ValueError:
            return "masked endpoint"
    tail = destination.split("|", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    return f"…{tail[-12:]}" if tail else "configured destination"


def _normalize_action_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for raw in rows:
        props = _dict(raw.get("properties"))
        receivers: list[dict[str, Any]] = []
        for key, raw_receivers in props.items():
            lower = str(key).lower()
            if not lower.endswith("receivers") or not isinstance(raw_receivers, list):
                continue
            receiver_type = lower.removesuffix("receivers").replace("_", "")
            for raw_receiver in raw_receivers:
                receiver = _dict(raw_receiver)
                destination = _receiver_destination(receiver_type, receiver)
                if not destination:
                    continue
                fingerprint = hashlib.sha256(f"{receiver_type}:{destination}".encode("utf-8")).hexdigest()[:12]
                enabled = bool(_ci_get(receiver, "enabled", default=True))
                displayed = _display_destination(receiver_type, destination)
                receivers.append(
                    {
                        "type": receiver_type,
                        "name": str(_ci_get(receiver, "name", default="")),
                        "destination": displayed,
                        "masked": displayed,
                        "fingerprint": fingerprint,
                        "enabled": enabled,
                    }
                )
        enabled = bool(_ci_get(props, "enabled", default=True))
        groups.append(
            {
                "id": str(raw.get("id", "")),
                "id_norm": _norm_id(raw.get("id")),
                "name": str(raw.get("name", "")),
                "subscription_id": str(raw.get("subscriptionId", "")),
                "resource_group": str(raw.get("resourceGroup", "")),
                "enabled": enabled,
                "receivers": receivers,
                "receiver_count": len(receivers),
                "active_receiver_count": sum(1 for receiver in receivers if receiver["enabled"] and enabled),
            }
        )
    return groups


def _refresh_rule_routing(rule: dict[str, Any], action_group_index: dict[str, dict[str, Any]]) -> None:
    groups = [
        action_group_index[group_id]
        for group_id in rule["action_group_ids"]
        if group_id in action_group_index
    ]
    receiver_paths = [
        {"action_group_id": group["id_norm"], "fingerprint": receiver["fingerprint"]}
        for group in groups
        for receiver in group["receivers"]
        if group["enabled"] and receiver["enabled"]
    ]
    receiver_path_counts: dict[str, int] = defaultdict(int)
    for path in receiver_paths:
        receiver_path_counts[path["fingerprint"]] += 1
    rule["action_group_names"] = [group["name"] for group in groups]
    rule["missing_action_group_ids"] = [
        group_id for group_id in rule["action_group_ids"] if group_id not in action_group_index
    ]
    rule["receiver_fingerprints"] = sorted(receiver_path_counts)
    rule["receiver_count"] = len(receiver_path_counts)
    rule["duplicate_receiver_fingerprints"] = sorted(
        fingerprint for fingerprint, count in receiver_path_counts.items() if count > 1
    )


def _normalize_rules(
    rows: list[dict[str, Any]],
    resources: list[dict[str, Any]],
    action_group_index: dict[str, dict[str, Any]],
    *,
    scope_kind: str,
) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    for raw in rows:
        rule_type = str(raw.get("type", "")).lower()
        if rule_type not in _ALERT_TYPES:
            continue
        props = _dict(raw.get("properties"))
        scopes = _rule_scopes(props)
        effective_targets = [
            {"id": str(resource.get("id", "")), "name": str(resource.get("name", "")), "type": str(resource.get("type", ""))}
            for resource in resources
            if any(_scope_matches(scope, str(resource.get("id", ""))) for scope in scopes)
        ]
        if scope_kind == "workload" and not effective_targets:
            continue
        action_group_ids = _action_group_ids(props)
        severity_raw = _ci_get(props, "severity", default=3)
        try:
            severity_number = int(severity_raw)
        except (TypeError, ValueError):
            severity_number = 3
        enabled = bool(_ci_get(props, "enabled", default=True))
        activity_category = ""
        if "activitylogalerts" in rule_type:
            activity_category = next((
                str(_ci_get(_dict(clause), "equals", default=""))
                for clause in (_ci_get(_dict(_ci_get(props, "condition", default={})), "allOf", default=[]) or [])
                if str(_ci_get(_dict(clause), "field", default="")).lower() == "category"
            ), "")
        rules.append(
            {
                "id": str(raw.get("id", "")),
                "name": str(raw.get("name", "")),
                "type": rule_type,
                "activity_category": activity_category,
                "subscription_id": str(raw.get("subscriptionId", "")),
                "resource_group": str(raw.get("resourceGroup", "")),
                "enabled": enabled,
                "severity": severity_number,
                "severity_label": _SEVERITY_LABELS.get(severity_number, str(severity_number)),
                "scopes": scopes,
                "effective_targets": effective_targets,
                "effective_target_count": len(effective_targets),
                "conditions": _normalize_conditions(rule_type, props),
                "action_group_ids": action_group_ids,
                "action_group_names": [],
                "missing_action_group_ids": [],
                "receiver_fingerprints": [],
                "receiver_count": 0,
                "duplicate_receiver_fingerprints": [],
                "overlap_group_ids": [],
                "finding_status": "ok",
                "issues": [],
            }
        )
        _refresh_rule_routing(rules[-1], action_group_index)
        rules[-1]["cost"] = estimate_rule_cost(rules[-1])
    return rules


def _target_atoms(rule: dict[str, Any]) -> list[str]:
    targets = [_norm_id(target["id"]) for target in rule["effective_targets"] if target.get("id")]
    if targets:
        return targets
    return [_norm_id(scope) for scope in rule["scopes"] if scope] or [f"rule:{_norm_id(rule['id'])}"]


def _is_layered(a: dict[str, Any], b: dict[str, Any], ca: dict[str, Any], cb: dict[str, Any], tolerance_pct: float = 10.0) -> bool:
    if a["severity"] == b["severity"]:
        return False
    ta, tb = ca.get("threshold"), cb.get("threshold")
    if not isinstance(ta, (int, float)) or not isinstance(tb, (int, float)) or ta == tb:
        return False
    return abs(float(ta) - float(tb)) / max(abs(float(ta)), abs(float(tb)), 1.0) >= max(0, tolerance_pct) / 100


def _dimension_overlap(left: list[str], right: list[str]) -> str:
    from app.alerts_manager.advisory import classify_dimension_overlap

    def parse(values: list[str]) -> list[dict[str, Any]]:
        result = []
        for value in values:
            name, operator, raw = (str(value).split(":", 2) + ["", ""])[:3]
            result.append({"name": name, "operator": operator.title(), "values": [item for item in raw.split(",") if item]})
        return result

    return classify_dimension_overlap(parse(left), parse(right))


def _overlaps(rules: list[dict[str, Any]], *, tolerance_pct: float = 20.0) -> list[dict[str, Any]]:
    exact_buckets: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    near_buckets: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for rule in rules:
        for target in _target_atoms(rule):
            for condition in rule["conditions"]:
                exact_buckets[(target, condition["exact_signature"])].append((rule, condition))
                near_buckets[(target, condition["near_signature"])].append((rule, condition))

    results: list[dict[str, Any]] = []
    exact_pairs: set[tuple[str, str, str]] = set()

    def append_group(kind: str, confidence: str, target: str, members: list[tuple[dict[str, Any], dict[str, Any]]]) -> None:
        unique: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {item[0]["id"]: item for item in members}
        if len(unique) < 2:
            return
        ordered = list(unique.values())
        fingerprints = [set(rule["receiver_fingerprints"]) for rule, _ in ordered]
        shared = set.intersection(*fingerprints) if fingerprints and all(fingerprints) else set()
        group_id = f"OV-{len(results) + 1:04d}"
        signal = ordered[0][1]
        results.append(
            {
                "id": group_id,
                "type": kind,
                "confidence": confidence,
                "target_id": target,
                "signal_type": signal["signal_type"],
                "signal_name": signal["signal_name"],
                "rule_ids": [rule["id"] for rule, _ in ordered],
                "rule_names": [rule["name"] for rule, _ in ordered],
                "shared_recipient_count": len(shared),
                "shared_recipient_fingerprints": sorted(shared),
                "notification_overlap": bool(shared),
                "explanation": "Equivalent conditions can fire for the same target" if kind == "exact" else "Similar conditions can fire for the same target",
                "recommendation": "Consolidate the rules or separate their escalation intent and recipients.",
            }
        )
        for rule, _ in ordered:
            rule["overlap_group_ids"].append(group_id)
            rule["finding_status"] = "overlap"

    for (target, _signature), members in exact_buckets.items():
        unique_ids = sorted({rule["id"] for rule, _ in members})
        if len(unique_ids) < 2:
            continue
        append_group("exact", "high", target, members)
        for i, left in enumerate(unique_ids):
            for right in unique_ids[i + 1 :]:
                exact_pairs.add((target, left, right))

    for (target, _signature), members in near_buckets.items():
        unique: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {item[0]["id"]: item for item in members}
        items = list(unique.values())
        for i, (left, left_condition) in enumerate(items):
            for right, right_condition in items[i + 1 :]:
                pair = (target, *sorted((left["id"], right["id"])))
                dimension_status = _dimension_overlap(left_condition.get("dimensions") or [], right_condition.get("dimensions") or [])
                if pair in exact_pairs or dimension_status == "disjoint" or _is_layered(left, right, left_condition, right_condition, tolerance_pct):
                    continue
                lt, rt = left_condition.get("threshold"), right_condition.get("threshold")
                threshold_close = (
                    isinstance(lt, (int, float))
                    and isinstance(rt, (int, float))
                    and abs(float(lt) - float(rt)) / max(abs(float(lt)), abs(float(rt)), 1.0) <= max(0, tolerance_pct) / 100
                )
                same_threshold = lt == rt
                windows_overlap = left_condition.get("window") == right_condition.get("window") or same_threshold
                if threshold_close or windows_overlap:
                    append_group("near", "medium", target, [(left, left_condition), (right, right_condition)])

    # A single rule can still duplicate notifications when it links two action groups
    # containing the same effective destination. Surface that routing fan-out even when
    # there is no second rule to compare.
    for rule in rules:
        duplicate_destinations = rule.get("duplicate_receiver_fingerprints") or []
        if not duplicate_destinations:
            continue
        group_id = f"OV-{len(results) + 1:04d}"
        condition = (rule.get("conditions") or [{}])[0]
        results.append(
            {
                "id": group_id,
                "type": "notification",
                "confidence": "high",
                "target_id": _target_atoms(rule)[0],
                "signal_type": condition.get("signal_type", ""),
                "signal_name": condition.get("signal_name", ""),
                "rule_ids": [rule["id"]],
                "rule_names": [rule["name"]],
                "shared_recipient_count": len(duplicate_destinations),
                "shared_recipient_fingerprints": duplicate_destinations,
                "notification_overlap": True,
                "explanation": "The rule reaches the same destination through multiple action groups.",
                "recommendation": "Remove the duplicate recipient path or consolidate the linked action groups.",
            }
        )
        rule["overlap_group_ids"].append(group_id)
        rule["finding_status"] = "overlap"
    return results


def _gaps(
    resources: list[dict[str, Any]],
    raw_rules: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    action_group_index: dict[str, dict[str, Any]],
    *,
    tolerance_pct: float,
) -> list[dict[str, Any]]:
    # Expand parent scopes to direct resource IDs before handing alerts to AMBA's exact-ID
    # matcher. This recognizes RG/subscription-level baseline rules for workload resources.
    expanded_alerts: list[dict[str, Any]] = []
    resource_ids = [str(resource.get("id", "")) for resource in resources]
    in_scope_rule_ids = {_norm_id(rule["id"]) for rule in rules}
    for raw in raw_rules:
        if _norm_id(raw.get("id")) not in in_scope_rule_ids:
            continue
        props = dict(_dict(raw.get("properties")))
        scopes = _rule_scopes(props)
        matched = [rid for rid in resource_ids if any(_scope_matches(scope, rid) for scope in scopes)]
        props["scopes"] = matched or scopes
        expanded_alerts.append({**raw, "properties": props})

    coverage = compute_coverage(
        resources,
        expanded_alerts,
        misconfig_counts_as_gap=True,
        tolerance_pct=tolerance_pct,
    )
    gaps: list[dict[str, Any]] = []
    for item in coverage.get("gaps", []):
        gaps.append(
            {
                "type": "baseline_missing" if item.get("status") == "missing" else "baseline_misconfigured",
                "risk": item.get("severity", "warning"),
                "resource_id": item.get("resource_id", ""),
                "resource_name": item.get("resource_name", ""),
                "resource_type": item.get("resource_type", ""),
                "subscription_id": item.get("subscription_id", ""),
                "resource_group": item.get("resource_group", ""),
                "location": item.get("location", ""),
                "alert_key": item.get("alert_key", ""),
                "amba_category": item.get("amba_category", ""),
                "rule_id": (item.get("observed") or {}).get("rule_id", ""),
                "rule_name": (item.get("observed") or {}).get("rule_name", ""),
                "action_group_id": "",
                "signal": item.get("alert_name", ""),
                "recommended": item.get("recommended") or {},
                "explanation": item.get("why", "") or f"AMBA baseline alert is {item.get('status', 'missing')}.",
                "recommendation": f"Add or correct the recommended {item.get('alert_name', 'baseline alert')}.",
            }
        )

    for rule in rules:
        issues: list[tuple[str, str, str]] = []
        if not rule["enabled"]:
            issues.append(("disabled_rule", "warning", "Review whether the rule should be enabled or explicitly retired."))
        if not rule["action_group_ids"]:
            issues.append(("no_action_group", "error", "Connect the rule to an action group with an owned response path."))
        if rule["missing_action_group_ids"]:
            issues.append(("missing_action_group", "error", "Replace the missing action-group reference."))
        linked = [action_group_index[group_id] for group_id in rule["action_group_ids"] if group_id in action_group_index]
        if linked and sum(group["active_receiver_count"] for group in linked) == 0:
            issues.append(("no_active_receivers", "error", "Enable at least one receiver in the linked action group."))
        for issue, risk, recommendation in issues:
            rule["issues"].append(issue)
            if rule["finding_status"] == "ok":
                rule["finding_status"] = "gap"
            gaps.append(
                {
                    "type": issue,
                    "risk": risk,
                    "resource_id": rule["effective_targets"][0]["id"] if rule["effective_targets"] else "",
                    "resource_name": rule["effective_targets"][0]["name"] if rule["effective_targets"] else "",
                    "resource_type": rule["effective_targets"][0]["type"] if rule["effective_targets"] else "",
                    "rule_id": rule["id"],
                    "rule_name": rule["name"],
                    "action_group_id": "; ".join(rule["missing_action_group_ids"]),
                    "signal": rule["conditions"][0]["signal_name"] if rule["conditions"] else "",
                    "explanation": issue.replace("_", " ").capitalize(),
                    "recommendation": recommendation,
                }
            )

    referenced_groups = {group_id for rule in rules for group_id in rule["action_group_ids"]}
    for group in action_group_index.values():
        if group["active_receiver_count"] == 0:
            gaps.append(
                {
                    "type": "action_group_no_receivers",
                    "risk": "error",
                    "resource_id": "",
                    "resource_name": "",
                    "resource_type": "",
                    "rule_id": "",
                    "rule_name": "",
                    "action_group_id": group["id"],
                    "signal": group["name"],
                    "explanation": "Action group has no effective enabled receiver.",
                    "recommendation": "Enable an owned receiver or retire the unused action group.",
                }
            )
        if group["id_norm"] not in referenced_groups:
            gaps.append(
                {
                    "type": "orphaned_action_group",
                    "risk": "informational",
                    "resource_id": "",
                    "resource_name": "",
                    "resource_type": "",
                    "rule_id": "",
                    "rule_name": "",
                    "action_group_id": group["id"],
                    "signal": group["name"],
                    "explanation": "No in-scope alert rule references this action group.",
                    "recommendation": "Confirm whether the action group is still needed before retiring it.",
                }
            )
    return gaps


def _recipient_summary(action_groups: list[dict[str, Any]], rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_fp: dict[str, dict[str, Any]] = {}
    for group in action_groups:
        for receiver in group["receivers"]:
            if not receiver["enabled"] or not group["enabled"]:
                continue
            entry = by_fp.setdefault(
                receiver["fingerprint"],
                {
                    "fingerprint": receiver["fingerprint"],
                    "type": receiver["type"],
                    "destination": receiver.get("destination") or receiver["masked"],
                    "masked": receiver["masked"],
                    "action_group_ids": [],
                    "action_group_names": [],
                    "rule_ids": [],
                },
            )
            entry["action_group_ids"].append(group["id"])
            entry["action_group_names"].append(group["name"])
    for rule in rules:
        for fingerprint in rule["receiver_fingerprints"]:
            if fingerprint in by_fp:
                by_fp[fingerprint]["rule_ids"].append(rule["id"])
    for item in by_fp.values():
        item["action_group_ids"] = sorted(set(item["action_group_ids"]))
        item["action_group_names"] = sorted(set(item["action_group_names"]))
        item["rule_ids"] = sorted(set(item["rule_ids"]))
        item["action_group_count"] = len(item["action_group_ids"])
        item["rule_count"] = len(item["rule_ids"])
        item["proliferation"] = item["action_group_count"] > 1
    return sorted(by_fp.values(), key=lambda item: (-item["action_group_count"], -item["rule_count"], item["destination"]))


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _apply_firing_history(rules: list[dict[str, Any]], firing_rows: list[dict[str, Any]]) -> None:
    now = datetime.now(timezone.utc)
    by_id = {_norm_id(rule["id"]): rule for rule in rules}
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rule in rules:
        by_name[rule["name"].strip().lower()].append(rule)
        rule["firing_7d"] = 0
        rule["firing_30d"] = 0
        rule["last_fired"] = ""
    for raw in firing_rows:
        essentials = _dict(raw.get("essentials") or _ci_get(_dict(raw.get("properties")), "essentials", default={}))
        reference = str(_ci_get(essentials, "alertRule", "alertRuleId", default=""))
        candidates = []
        if _norm_id(reference) in by_id:
            candidates = [by_id[_norm_id(reference)]]
        elif reference.strip().lower() in by_name:
            candidates = by_name[reference.strip().lower()]
        fired_raw = _ci_get(essentials, "firedDateTime", "startDateTime", default=raw.get("firedDateTime"))
        fired = _parse_time(fired_raw)
        if not fired:
            continue
        age_days = (now - fired).total_seconds() / 86400
        for rule in candidates:
            if age_days <= 30:
                rule["firing_30d"] += 1
            if age_days <= 7:
                rule["firing_7d"] += 1
            if not rule["last_fired"] or str(fired_raw) > rule["last_fired"]:
                rule["last_fired"] = str(fired_raw)


def compute_analysis(
    resources: list[dict[str, Any]],
    alert_rows: list[dict[str, Any]],
    action_group_rows: list[dict[str, Any]],
    firing_rows: list[dict[str, Any]] | None = None,
    *,
    scope_kind: str,
    scope_id: str,
    scope_name: str,
    tolerance_pct: float = 10.0,
) -> dict[str, Any]:
    """Pure Phase-1 analysis over pre-fetched ARG rows."""
    action_groups = _normalize_action_groups(action_group_rows)
    action_group_index = {group["id_norm"]: group for group in action_groups}
    expanded_alert_rows = _expand_alert_rows(alert_rows)
    rules = _normalize_rules(expanded_alert_rows, resources, action_group_index, scope_kind=scope_kind)
    _apply_firing_history(rules, firing_rows or [])
    overlaps = _overlaps(rules, tolerance_pct=tolerance_pct)
    gaps = _gaps(resources, expanded_alert_rows, rules, action_group_index, tolerance_pct=tolerance_pct)
    recipients = _recipient_summary(action_groups, rules)
    overlap_rule_ids = {rule_id for overlap in overlaps for rule_id in overlap["rule_ids"]}
    notification_overlaps = sum(1 for overlap in overlaps if overlap["notification_overlap"])
    denominator = max(1, len(rules) + len(resources))
    penalty = len(gaps) + len(overlaps) + (2 * notification_overlaps)
    rationalization_score = max(0, round(100 * (1 - min(denominator, penalty) / denominator)))
    cost_summary = summarize_rule_costs(rules)
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "scope_name": scope_name,
        "source": "azure_resource_graph",
        "demo": False,
        "error": "",
        "partial": False,
        "report_exists": True,
        "rationalization_score": rationalization_score,
        "cost_summary": cost_summary,
        "kpis": {
            "total_rules": len(rules),
            "enabled_rules": sum(1 for rule in rules if rule["enabled"]),
            "disabled_rules": sum(1 for rule in rules if not rule["enabled"]),
            "overlap_groups": len(overlaps),
            "overlapping_rules": len(overlap_rule_ids),
            "notification_overlaps": notification_overlaps,
            "gap_count": len(gaps),
            "action_groups": len(action_groups),
            "unique_recipients": len(recipients),
            "recipient_proliferation": sum(1 for recipient in recipients if recipient["proliferation"]),
            "resources_evaluated": len(resources),
            "smart_detector_rules": sum(1 for rule in rules if "smartdetector" in rule["type"]),
            "prometheus_rules": sum(1 for rule in rules if "prometheus" in rule["type"]),
            "firings_7d": sum(rule["firing_7d"] for rule in rules),
            "firings_30d": sum(rule["firing_30d"] for rule in rules),
        },
        "rules": rules,
        "action_groups": action_groups,
        "recipients": recipients,
        "overlaps": overlaps,
        "gaps": gaps,
    }


async def _query(predicates: list[str], projection: str, connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    from app.assessments.runner import query_resources_batched

    return await query_resources_batched(predicates, connection, projection=projection)


def _subscription_predicates(subscriptions: list[str], *, chunk_size: int = 100) -> list[str]:
    return [
        "subscriptionId in~ (" + ", ".join(f"'{_esc(sub)}'" for sub in subscriptions[index : index + chunk_size]) + ")"
        for index in range(0, len(subscriptions), chunk_size)
    ]


async def _query_alerts(subscriptions: list[str], connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not subscriptions:
        return []
    return await _query(
        [
            predicate + " and type in~ ('microsoft.insights/metricalerts', "
            "'microsoft.insights/scheduledqueryrules', 'microsoft.insights/activitylogalerts', "
            "'microsoft.alertsmanagement/smartdetectoralertrules', "
            "'microsoft.alertsmanagement/prometheusrulegroups', "
            "'microsoft.monitor/accounts/prometheusrulegroups')"
            for predicate in _subscription_predicates(subscriptions)
        ],
        "id, name, type, subscriptionId, resourceGroup, location, properties, tags",
        connection,
    )


async def _query_action_groups(subscriptions: list[str], connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not subscriptions:
        return []
    return await _query(
        [predicate + " and type =~ 'microsoft.insights/actiongroups'" for predicate in _subscription_predicates(subscriptions)],
        "id, name, type, subscriptionId, resourceGroup, location, properties, tags",
        connection,
    )


async def _query_firings(subscriptions: list[str], connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    from app.exec.command_runner import run_kql_collect

    rows: list[dict[str, Any]] = []
    for predicate in _subscription_predicates(subscriptions):
        query = (
            "AlertsManagementResources "
            "| where type =~ 'microsoft.alertsmanagement/alerts' "
            f"| where {predicate} "
            "| extend essentials=properties.essentials "
            "| extend fired=todatetime(essentials.firedDateTime) "
            "| where fired >= ago(7d) "
            "| project id, subscriptionId, essentials, firedDateTime=tostring(fired) "
            "| order by firedDateTime desc"
        )
        result = await run_kql_collect(query, connection, max_rows=10_000)
        if not result.ok:
            raise RuntimeError(result.error or "Alert firing-history query failed.")
        rows.extend(result.rows)
    return rows


async def collect_analysis(
    connection: dict[str, Any] | None,
    *,
    scope_kind: str,
    scope_id: str,
    workload: dict[str, Any] | None,
    tolerance_pct: float = 10.0,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Resolve a workload/subscription, collect ARG rows, and compute the snapshot."""
    from app.amba.collector import _query_resources
    from app.assessments.runner import _resolve_scope, scope_predicate_batches

    async def emit(phase: str, message: str) -> None:
        if progress is not None:
            await progress(phase, message)

    await emit("scope", f"Resolving {scope_kind.replace('_', ' ')} scope…")

    if scope_kind == "workload" and workload is not None:
        scope = await _resolve_scope(workload, connection)
        if scope.get("error") and not scope.get("predicate"):
            await emit("error", f"Scope resolution failed: {scope['error']}")
            return empty_snapshot(scope_kind, scope_id, str(scope["error"]))
        predicates = scope_predicate_batches(scope)
        subscriptions = list(scope.get("effective_subscriptions") or scope.get("subscriptions") or [])
        scope_name = str(workload.get("name") or scope_id)
    elif scope_kind == "subscription" and scope_id:
        predicates = [f"subscriptionId =~ '{_esc(scope_id)}'"]
        subscriptions = [scope_id]
        scope_name = scope_id
    elif scope_kind == "management_group" and scope_id:
        from app.workloads.discovery import subscriptions_under_mg

        await emit("subscriptions", f"Discovering subscriptions under management group {scope_id}…")
        subscriptions = await subscriptions_under_mg(connection, scope_id)
        if not subscriptions:
            await emit("error", "No visible subscriptions were found under this management group.")
            return empty_snapshot(scope_kind, scope_id, "No visible subscriptions were found under this management group.")
        predicates = _subscription_predicates(subscriptions)
        scope_name = scope_id
    else:
        await emit("error", "No resolvable scope was supplied.")
        return empty_snapshot(scope_kind, scope_id, "No resolvable scope.")

    await emit("subscriptions", f"Scope resolved to {len(subscriptions)} subscription(s).")
    try:
        async def tracked(label: str, query: Awaitable[list[dict[str, Any]]]) -> list[dict[str, Any]]:
            rows = await query
            await emit("query", f"Received {len(rows):,} {label} row(s).")
            return rows

        await emit("query", "Launching resource inventory query…")
        resources_task = tracked("resource", _query_resources(predicates, connection))
        await emit("query", "Launching alert rules query…")
        alerts_task = tracked("alert rule", _query_alerts(subscriptions, connection))
        await emit("query", "Launching action groups query…")
        groups_task = tracked("action group", _query_action_groups(subscriptions, connection))
        await emit("query", "Launching 7-day firing history query…")
        firings_task = tracked("firing history", _query_firings(subscriptions, connection))
        resources, alerts, action_groups, firings = await asyncio.gather(
            resources_task, alerts_task, groups_task, firings_task
        )
        await emit(
            "normalize",
            f"Normalizing {len(alerts):,} alert rows, routes, recipients, overlaps, gaps, and costs…",
        )
        snapshot = compute_analysis(
            resources,
            alerts,
            action_groups,
            firings,
            scope_kind=scope_kind,
            scope_id=scope_id,
            scope_name=scope_name,
            tolerance_pct=tolerance_pct,
        )
        await emit("normalize", f"Normalized {snapshot['kpis']['total_rules']:,} effective alert rule(s).")
        await emit("normalize", f"Resolved {snapshot['kpis']['action_groups']:,} action group route(s).")
        await emit("normalize", f"Deduplicated {snapshot['kpis']['unique_recipients']:,} recipient destination(s).")
        await emit("compute", f"Computed {snapshot['kpis']['overlap_groups']:,} exact/near notification overlap group(s).")
        await emit("compute", f"Computed {snapshot['kpis']['gap_count']:,} AMBA baseline and routing gap(s).")
        await emit("compute", f"Estimated rule costs and rationalization score {snapshot['rationalization_score']}.")
        snapshot["connection_configured"] = connection is not None
        snapshot["partial"] = len(alerts) >= 10_000 or len(action_groups) >= 10_000
        if snapshot["partial"]:
            snapshot["error"] = "Azure Resource Graph returned the configured result cap; findings may be partial."
        await emit(
            "compute",
            f"Analysis computed: {snapshot['kpis']['total_rules']:,} rules, "
            f"{snapshot['kpis']['overlap_groups']:,} overlap groups, {snapshot['kpis']['gap_count']:,} gaps.",
        )
        return snapshot
    except Exception as exc:  # noqa: BLE001 - return an actionable cached snapshot shape
        log.warning("Alert-proliferation collection failed: %s", exc)
        await emit("error", f"Collection failed: {str(exc)[:300]}")
        return empty_snapshot(scope_kind, scope_id, str(exc)[:500])


def empty_snapshot(scope_kind: str, scope_id: str, error: str = "") -> dict[str, Any]:
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "scope_name": scope_id,
        "source": "azure_resource_graph",
        "demo": False,
        "error": error,
        "partial": False,
        "report_exists": False,
        "rationalization_score": 0,
        "connection_configured": False,
        "cost_summary": empty_cost_summary(),
        "kpis": {
            "total_rules": 0,
            "enabled_rules": 0,
            "disabled_rules": 0,
            "overlap_groups": 0,
            "overlapping_rules": 0,
            "notification_overlaps": 0,
            "gap_count": 0,
            "action_groups": 0,
            "unique_recipients": 0,
            "recipient_proliferation": 0,
            "resources_evaluated": 0,
            "smart_detector_rules": 0,
            "prometheus_rules": 0,
            "firings_7d": 0,
            "firings_30d": 0,
        },
        "rules": [],
        "action_groups": [],
        "recipients": [],
        "overlaps": [],
        "gaps": [],
    }
