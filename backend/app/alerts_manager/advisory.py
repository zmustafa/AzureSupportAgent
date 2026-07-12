"""Read-only Alerts Manager advisory tools: routing simulation, ownership, and noise guard."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from app.alerts_manager import rules, service


def _scope_matches(scope: str, target: str) -> bool:
    left = str(scope or "").lower().rstrip("/")
    right = str(target or "").lower().rstrip("/")
    return bool(left and right and (left == right or left.startswith(right + "/") or right.startswith(left + "/")))


def _parse_timestamp(value: str) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _duration_minutes(value: str) -> float:
    match = re.fullmatch(r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?", str(value or "").upper())
    return (float(match.group(1) or 0) * 60 + float(match.group(2) or 0)) if match else 0


def _receiver_profile(kind: str, common: bool) -> dict[str, Any]:
    profiles = {
        "email": ("Email", ["Address must be active in the Action Group."], True),
        "sms": ("SMS text", ["Country/region support and Azure rate limits apply.", "Common alert schema is not available."], False),
        "voice": ("Voice call", ["Country/region support and Azure rate limits apply.", "Common alert schema is not available."], False),
        "azureapppush": ("Azure mobile app push", ["Recipient must register the same account in the Azure mobile app."], False),
        "webhook": ("HTTPS POST", ["Endpoint must accept Azure Monitor requests; secure webhooks also require valid Entra configuration."], True),
        "azurefunction": ("Azure Function HTTP trigger", ["Signed trigger URL and function must remain valid."], True),
        "logicapp": ("Logic App callback", ["Signed callback URL and workflow trigger must remain valid."], True),
        "eventhub": ("Event Hub event", ["Namespace, hub, tenant, and subscription must remain accessible."], True),
        "automationrunbook": ("Automation Runbook webhook", ["Published runbook and signed webhook must remain active."], True),
        "itsm": ("ITSM work item", ["Service connection, workspace, region, and ticket mapping must be valid."], False),
        "armrole": ("ARM role email", ["Only supported built-in Azure RBAC roles are expanded to recipients."], True),
    }
    channel, constraints, supports_common = profiles.get(kind, (kind, ["Azure receiver constraints apply."], common))
    return {"channel": channel, "constraints": constraints, "supports_common_schema": supports_common, "payload_schema": "common" if common and supports_common else "alert-type-specific"}


def _payload_preview(candidate: dict[str, Any], monitor_condition: str, common: bool) -> dict[str, Any]:
    if not common:
        return {"schema": "alert-type-specific", "alert_family": candidate.get("family"), "monitor_condition": monitor_condition}
    return {
        "schema": "azure-monitor-common-alert-schema",
        "essentials": {
            "alertRule": candidate.get("name"), "severity": f"Sev{candidate.get('severity')}",
            "signalType": candidate.get("family"), "monitorCondition": monitor_condition,
            "alertTargetIDs": candidate.get("scopes") or [],
        },
        "alertContext": "Varies by signal type; sensitive query/callback data is omitted from simulation.",
    }


def build_bulk_notification_simulation(
    inventory: list[dict[str, Any]], groups: list[dict[str, Any]], *,
    monitor_condition: str = "Fired", include_disabled: bool = True,
    families: set[str] | None = None, severities: set[int] | None = None,
) -> dict[str, Any]:
    """Build a read-only estate routing graph from already-loaded Azure inventories."""
    filtered = [
        rule for rule in inventory
        if (include_disabled or rule.get("enabled"))
        and (not families or rule.get("family") in families)
        and (not severities or rule.get("severity") in severities)
    ]
    groups_by_id = {str(group.get("id") or "").lower(): group for group in groups}
    nodes: dict[str, dict[str, Any]] = {}
    links: dict[tuple[str, str, str], dict[str, Any]] = {}
    routes: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    receiver_occurrences: dict[str, set[str]] = {}

    def node(node_id: str, name: str, kind: str, status: str = "ok", **meta: Any) -> None:
        nodes.setdefault(node_id, {"id": node_id, "name": name, "kind": kind, "status": status, **meta})

    def link(source: str, target: str, status: str, **meta: Any) -> None:
        key = (source, target, status)
        current = links.setdefault(key, {"source": source, "target": target, "value": 0, "status": status, **meta})
        current["value"] += 1

    def outcome_node(status: str) -> str:
        labels = {"deliver": "Expected delivery", "disabled": "Disabled", "missing_group": "Missing Action Group", "no_receiver": "No active receiver", "blocked": "Blocked"}
        node_id = f"outcome:{status}"
        node(node_id, labels.get(status, status.replace("_", " ").title()), "outcome", "ok" if status == "deliver" else "error" if status in {"missing_group", "no_receiver"} else "warning")
        return node_id

    for rule in filtered:
        rule_id = str(rule.get("id") or "")
        rule_node = f"alert:{rule_id.lower()}"
        rule_enabled = bool(rule.get("enabled", True))
        node(rule_node, str(rule.get("name") or service._name_from_id(rule_id)), "alert", "ok" if rule_enabled else "disabled", family=rule.get("family"), severity=rule.get("severity"), resource_id=rule_id)
        scopes = [str(value) for value in rule.get("scopes") or []] or ["unscoped"]
        for scope in scopes:
            resource_node = f"resource:{scope.lower()}"
            node(resource_node, service._name_from_id(scope) if scope != "unscoped" else "Unscoped", "resource", "warning" if scope == "unscoped" else "ok", resource_id=scope)
            link(resource_node, rule_node, "ok" if rule_enabled else "disabled")
        action_group_ids = list(dict.fromkeys(str(value) for value in rule.get("action_group_ids") or [] if value))
        if not action_group_ids:
            outcome = outcome_node("no_receiver")
            link(rule_node, outcome, "error")
            diagnostics.append({"code": "no_action_group", "severity": "critical" if rule.get("severity") in {0, 1} else "high", "rule_id": rule_id, "rule_name": rule.get("name"), "message": "Alert has no Action Group destination."})
            routes.append({"resource_ids": scopes, "rule_id": rule_id, "rule_name": rule.get("name"), "family": rule.get("family"), "severity": rule.get("severity"), "rule_enabled": rule_enabled, "action_group_id": "", "action_group_name": "", "receiver_type": "", "receiver_name": "", "receiver_masked": "", "outcome": "no_receiver", "issues": ["no Action Group"]})
            continue
        for group_id in action_group_ids:
            group = groups_by_id.get(group_id.lower())
            group_node = f"group:{group_id.lower()}"
            if not group:
                node(group_node, service._name_from_id(group_id), "action_group", "error", resource_id=group_id)
                link(rule_node, group_node, "error")
                link(group_node, outcome_node("missing_group"), "error")
                diagnostics.append({"code": "missing_action_group", "severity": "high", "rule_id": rule_id, "rule_name": rule.get("name"), "action_group_id": group_id, "message": "Referenced Action Group was not found."})
                routes.append({"resource_ids": scopes, "rule_id": rule_id, "rule_name": rule.get("name"), "family": rule.get("family"), "severity": rule.get("severity"), "rule_enabled": rule_enabled, "action_group_id": group_id, "action_group_name": service._name_from_id(group_id), "receiver_type": "", "receiver_name": "", "receiver_masked": "", "outcome": "missing_group", "issues": ["missing Action Group"]})
                continue
            group_enabled = bool(group.get("enabled", True))
            node(group_node, str(group.get("name") or service._name_from_id(group_id)), "action_group", "ok" if group_enabled else "disabled", resource_id=group_id)
            link(rule_node, group_node, "ok" if rule_enabled and group_enabled else "disabled")
            receivers = list(group.get("receivers") or [])
            if not receivers:
                link(group_node, outcome_node("no_receiver"), "error")
                diagnostics.append({"code": "no_receivers", "severity": "high", "rule_id": rule_id, "rule_name": rule.get("name"), "action_group_id": group_id, "message": "Action Group has no receivers."})
            if len(receivers) > 10:
                diagnostics.append({"code": "excessive_fanout", "severity": "medium", "rule_id": rule_id, "rule_name": rule.get("name"), "action_group_id": group_id, "message": f"Action Group fans out to {len(receivers)} receivers."})
            active_receivers = 0
            for receiver in receivers:
                receiver_type = str(receiver.get("type") or "unknown")
                destination = str(receiver.get("destination") or receiver.get("masked") or receiver.get("name") or "")
                fingerprint = str(receiver.get("fingerprint") or destination or "unknown")
                receiver_key = f"{receiver_type}:{fingerprint}".lower()
                receiver_node = f"receiver:{receiver_key}"
                receiver_enabled = bool(receiver.get("enabled", True))
                active_receivers += int(receiver_enabled)
                receiver_occurrences.setdefault(receiver_key, set()).add(group_id)
                node(receiver_node, f"{receiver_type.title()} · {destination or receiver.get('name') or 'configured'}", "receiver", "ok" if receiver_enabled else "disabled", receiver_type=receiver_type, fingerprint=fingerprint)
                would_run = rule_enabled and group_enabled and receiver_enabled and monitor_condition == "Fired"
                outcome_status = "deliver" if would_run else "disabled"
                link(group_node, receiver_node, "ok" if would_run else "disabled", receiver_type=receiver_type)
                link(receiver_node, outcome_node(outcome_status), "ok" if would_run else "disabled")
                issues = []
                if not rule_enabled: issues.append("rule disabled")
                if not group_enabled: issues.append("Action Group disabled")
                if not receiver_enabled: issues.append("receiver disabled")
                if monitor_condition == "Resolved": issues.append("resolved behavior requires per-rule fidelity check")
                routes.append({"resource_ids": scopes, "rule_id": rule_id, "rule_name": rule.get("name"), "family": rule.get("family"), "severity": rule.get("severity"), "rule_enabled": rule_enabled, "action_group_id": group_id, "action_group_name": group.get("name"), "action_group_enabled": group_enabled, "receiver_type": receiver_type, "receiver_name": receiver.get("name"), "receiver_destination": destination, "receiver_masked": destination, "receiver_fingerprint": fingerprint, "receiver_enabled": receiver_enabled, "payload_schema": "common" if receiver.get("use_common_alert_schema") else "alert-type-specific", "outcome": outcome_status, "would_run": would_run, "issues": issues})
            if rule.get("severity") in {0, 1} and active_receivers < 2:
                diagnostics.append({"code": "critical_single_path", "severity": "high", "rule_id": rule_id, "rule_name": rule.get("name"), "action_group_id": group_id, "message": "Critical alert has fewer than two active receiver paths."})

    for receiver_key, group_ids in receiver_occurrences.items():
        if len(group_ids) > 1:
            diagnostics.append({"code": "duplicate_receiver_path", "severity": "medium", "receiver": receiver_key, "action_group_ids": sorted(group_ids), "message": f"Receiver is reachable through {len(group_ids)} Action Groups."})
    summary = {
        "rules": len(filtered), "resources": len({scope for rule in filtered for scope in (rule.get("scopes") or [])}),
        "action_groups": len({route["action_group_id"] for route in routes if route["action_group_id"]}),
        "receiver_paths": sum(1 for route in routes if route.get("receiver_type")),
        "would_deliver": sum(1 for route in routes if route.get("would_run")),
        "blocked": sum(1 for route in routes if not route.get("would_run")),
        "diagnostics": len(diagnostics),
    }
    return {"summary": summary, "nodes": list(nodes.values()), "links": list(links.values()), "routes": routes, "diagnostics": diagnostics, "warning": "Dry-run only. No alert was fired and no notification was sent."}


async def bulk_simulate_notification_paths(
    connection: dict[str, Any], *, workload_id: str | None = None, subscription_id: str | None = None,
    management_group_id: str | None = None, monitor_condition: str = "Fired",
    include_disabled: bool = True, families: set[str] | None = None, severities: set[int] | None = None,
) -> dict[str, Any]:
    inventory, groups = await asyncio.gather(
        rules.list_rules(connection, workload_id=workload_id, subscription_id=subscription_id, management_group_id=management_group_id),
        service.list_action_groups(connection, workload_id=workload_id, subscription_id=subscription_id, management_group_id=management_group_id),
    )
    return build_bulk_notification_simulation(inventory, groups, monitor_condition=monitor_condition, include_disabled=include_disabled, families=families, severities=severities)


async def simulate_notification_path(connection: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    resource_id = str(event.get("resource_id") or "")
    rule_id = str(event.get("rule_id") or "")
    timestamp = _parse_timestamp(str(event.get("timestamp") or ""))
    inventory = await rules.list_rules(connection, subscription_id=service._subscription_from_id(resource_id or rule_id) or None)
    selected = next((row for row in inventory if row["id"].lower() == rule_id.lower()), None)
    editable: dict[str, Any] = {}
    if selected:
        resource, _status, _error = await rules.get_rule(connection, selected["id"], selected["family"])
        if resource:
            editable = rules.editable_rule(resource)
    family = str(event.get("family") or (selected or {}).get("family") or "metric")
    candidate = {
        "id": rule_id,
        "name": str(event.get("rule_name") or (selected or {}).get("name") or "Hypothetical alert"),
        "family": family,
        "severity": int(event.get("severity", (selected or {}).get("severity") if (selected or {}).get("severity") is not None else 3)),
        "resource_group": service._resource_group_from_id(resource_id),
        "scopes": [resource_id] if resource_id else list((selected or {}).get("scopes") or []),
        "description": str(event.get("description") or (selected or {}).get("description") or ""),
        "monitor_condition": str(event.get("monitor_condition") or "Fired"),
        "alert_context": str(event.get("alert_context") or ""),
        "target_resource_type": str(editable.get("target_resource_type") or ""),
    }
    inherited_groups = [str(value) for value in event.get("action_group_ids") or (selected or {}).get("action_group_ids") or []]
    selected_groups = [str(value) for value in event.get("selected_action_group_ids") or []]
    base_groups = selected_groups if event.get("use_selected_only") else list(dict.fromkeys([*inherited_groups, *selected_groups]))
    monitor_condition = str(event.get("monitor_condition") or "Fired")
    final_groups = list(dict.fromkeys(base_groups))
    fired_rows: list[dict[str, Any]] = []
    try:
        fired_rows = await service.list_fired_alerts(connection, subscription_id=service._subscription_from_id(resource_id or rule_id) or None, days=30)
    except (ValueError, PermissionError):
        pass
    related_history = [
        item for item in fired_rows
        if (rule_id and str(item.get("rule_id") or "").lower() == rule_id.lower())
        or (candidate["name"] and str(item.get("rule_name") or "").lower() == candidate["name"].lower())
    ]
    mute_duration = str(editable.get("mute_actions_duration") or editable.get("throttling_duration") or "")
    mute_minutes = _duration_minutes(mute_duration)
    recent_fired = max(((_parse_timestamp(str(item.get("fired_at") or "")) for item in related_history if item.get("fired_at"))), default=None)
    seconds_since_last = (timestamp - recent_fired).total_seconds() if recent_fired else None
    muted = bool(mute_minutes and seconds_since_last is not None and 0 <= seconds_since_last < mute_minutes * 60)
    resolved_expected = bool(editable.get("auto_mitigate", True))
    if editable.get("family") == "prometheus":
        resolved_expected = any(bool((item.get("resolveConfiguration") or {}).get("autoResolved", True)) for item in editable.get("prometheus_rules") or [])
    if editable.get("family") == "activity":
        resolved_expected = False
    resolved_blocked = monitor_condition == "Resolved" and not resolved_expected
    groups = await service.list_action_groups(connection, subscription_id=service._subscription_from_id(resource_id or rule_id) or None)
    by_id = {str(group["id"]).lower(): group for group in groups}
    paths: list[dict[str, Any]] = []
    receiver_occurrences: dict[str, list[str]] = {}
    for group_id in final_groups:
        group = by_id.get(group_id.lower())
        if not group:
            paths.append({"action_group_id": group_id, "name": service._name_from_id(group_id), "enabled": False, "missing": True, "receivers": []})
            continue
        receivers = []
        for receiver in group.get("receivers") or []:
            destination = receiver.get("destination") or receiver.get("masked")
            key = f"{receiver.get('type')}:{destination}"
            receiver_occurrences.setdefault(key, []).append(group_id)
            common = bool(receiver.get("use_common_alert_schema"))
            profile = _receiver_profile(str(receiver.get("type") or ""), common)
            receivers.append({
                "type": receiver.get("type"), "name": receiver.get("name"), "destination": destination, "masked": destination,
                "enabled": bool(receiver.get("enabled")),
                "use_common_alert_schema": common, **profile,
                "payload_preview": _payload_preview(candidate, monitor_condition, common),
                "would_run": bool((selected or {}).get("enabled", True) and group.get("enabled") and receiver.get("enabled") and not muted and not resolved_blocked),
                "blocked_reason": "alert rule disabled" if selected and not selected.get("enabled") else f"muted/throttled for {mute_duration}" if muted else "resolved notifications disabled" if resolved_blocked else "action group disabled" if not group.get("enabled") else "receiver disabled" if not receiver.get("enabled") else "",
            })
        paths.append({"action_group_id": group_id, "name": group.get("name"), "enabled": group.get("enabled"), "missing": False, "receivers": receivers})
    duplicate_paths = [
        {"receiver": key, "action_group_ids": ids, "count": len(ids)}
        for key, ids in receiver_occurrences.items() if len(set(ids)) > 1
    ]
    would_run = sum(1 for path in paths for receiver in path["receivers"] if receiver["would_run"])
    from app.alerts_manager import delivery_history

    test_history = delivery_history.for_groups(str(event.get("tenant_id") or "default"), final_groups)
    return {
        "event": {**candidate, "timestamp": timestamp.isoformat()}, "base_action_group_ids": base_groups,
        "inherited_action_group_ids": inherited_groups, "selected_action_group_ids": selected_groups,
        "final_action_group_ids": final_groups,
        "paths": paths, "duplicate_paths": duplicate_paths, "receiver_count": sum(len(path["receivers"]) for path in paths),
        "would_run_count": would_run,
        "monitor_condition": monitor_condition, "resolved_notification_expected": resolved_expected,
        "mute_or_throttle_duration": mute_duration, "muted_or_throttled": muted,
        "history": {
            "fired_30d": sum(1 for item in related_history if str(item.get("monitor_condition") or "").lower() == "fired"),
            "resolved_30d": sum(1 for item in related_history if str(item.get("monitor_condition") or "").lower() == "resolved"),
            "last_fired": max((str(item.get("fired_at") or "") for item in related_history), default=""),
            "test_deliveries": test_history,
        },
        "warning": "Simulation uses current Azure configuration; it does not fire an alert or send notifications.",
    }


def _strip_comments(value: str) -> str:
    return re.sub(r"//[^\n]*|/\*.*?\*/", " ", value or "", flags=re.S)


def _kql_semantic_key(query: str) -> str:
    clean = re.sub(r"\s+", " ", _strip_comments(query).strip().lower())
    stages = [stage.strip() for stage in clean.split("|") if stage.strip()]
    table = stages[0].split()[0] if stages else ""
    where = sorted(
        re.sub(r"\bago\([^)]*\)|\bdatetime\([^)]*\)", "<time>", stage)
        for stage in stages[1:] if stage.startswith("where ")
    )
    summarize = sorted(re.sub(r"\b\d+[smhd]\b", "<window>", stage) for stage in stages[1:] if stage.startswith("summarize "))
    project = sorted(stage for stage in stages[1:] if stage.startswith(("extend ", "project ", "parse ", "mv-expand ")))
    canonical = json.dumps({"table": table, "where": where, "summarize": summarize, "shape": project}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:20] if table else ""


def _promql_semantic_key(expression: str) -> str:
    clean = re.sub(r"\s+", " ", _strip_comments(expression).strip().lower())
    clean = re.sub(r"\s*(>=|<=|==|!=|>|<)\s*-?\d+(?:\.\d+)?\s*$", "", clean)
    clean = re.sub(r"\[(?:\d+(?:\.\d+)?[smhdwy])+\]", "[<range>]", clean)
    clean = re.sub(r"\boffset\s+\d+(?:\.\d+)?[smhdwy]", "offset <duration>", clean)

    def labels(match: re.Match[str]) -> str:
        values = sorted(part.strip() for part in match.group(1).split(",") if part.strip())
        return "{" + ",".join(values) + "}"

    clean = re.sub(r"\{([^{}]*)\}", labels, clean)
    return hashlib.sha256(clean.encode()).hexdigest()[:20] if clean else ""


def _normalized_dimensions(values: list[dict[str, Any]]) -> dict[str, dict[str, set[str]]]:
    result: dict[str, dict[str, set[str]]] = {}
    for item in values or []:
        name = str(item.get("name") or "").strip().lower()
        if not name:
            continue
        operator = "exclude" if str(item.get("operator") or "Include").lower() == "exclude" else "include"
        result.setdefault(name, {"include": set(), "exclude": set()})[operator].update(
            str(value).strip().lower() for value in item.get("values") or [] if str(value).strip()
        )
    return result


def classify_dimension_overlap(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> str:
    a, b = _normalized_dimensions(left), _normalized_dimensions(right)
    partial = False
    for name in set(a) | set(b):
        av = a.get(name, {"include": set(), "exclude": set()})
        bv = b.get(name, {"include": set(), "exclude": set()})
        ai, bi = av["include"], bv["include"]
        if ai and bi:
            shared = ai & bi
            if not shared:
                return "disjoint"
            partial = partial or shared != ai or shared != bi
        elif ai and ai <= bv["exclude"]:
            return "disjoint"
        elif bi and bi <= av["exclude"]:
            return "disjoint"
        elif ai or bi or av["exclude"] != bv["exclude"]:
            partial = True
    return "partial" if partial else "exact"


def _normalized_signal(family: str, desired: dict[str, Any]) -> list[dict[str, Any]]:
    if family == "metric":
        return [{
            "key": f"{str(item.get('metric_namespace') or '').lower()}:{str(item.get('metric_name') or '').lower()}",
            "logic": f"{item.get('aggregation')}:{item.get('operator')}:{item.get('threshold_type', 'static')}",
            "threshold": item.get("threshold"),
            "dimensions": item.get("dimensions") or [],
        } for item in desired.get("conditions") or []]
    if family == "log":
        return [{"key": _kql_semantic_key(str(item.get("query") or "")), "logic": f"{item.get('aggregation')}:{item.get('operator')}", "threshold": item.get("threshold"), "dimensions": item.get("dimensions") or []} for item in desired.get("conditions") or []]
    if family == "activity":
        return [{"key": str(sorted((str(item.get("field")), str(item.get("equals") or item.get("containsAny"))) for item in desired.get("activity_conditions") or [])), "logic": "activity", "threshold": None}]
    if family == "smart":
        return [{"key": str(desired.get("detector_id") or "").lower(), "logic": "smart", "threshold": None}]
    return [{"key": _promql_semantic_key(str(item.get("expression") or item.get("expr") or "")), "logic": "prometheus", "threshold": _terminal_threshold(str(item.get("expression") or item.get("expr") or "")), "dimensions": []} for item in desired.get("prometheus_rules") or []]


def _terminal_threshold(value: str) -> float | None:
    match = re.search(r"(?:>=|<=|==|!=|>|<)\s*(-?\d+(?:\.\d+)?)\s*$", value or "")
    return float(match.group(1)) if match else None


def _threshold_delta(left: Any, right: Any) -> float | None:
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return None
    denominator = max(abs(float(left)), abs(float(right)), 1.0)
    return abs(float(left) - float(right)) * 100 / denominator


def _intentional_layer(candidate: dict[str, Any], existing: dict[str, Any], left: dict[str, Any], right: dict[str, Any], tolerance: float) -> bool:
    delta = _threshold_delta(left.get("threshold"), right.get("threshold"))
    if delta is None or delta <= tolerance or candidate.get("severity") == existing.get("severity"):
        return False
    operator = str(left.get("logic") or "").lower()
    candidate_threshold, existing_threshold = float(left["threshold"]), float(right["threshold"])
    candidate_severity, existing_severity = candidate.get("severity"), existing.get("severity")
    if not isinstance(candidate_severity, int) or not isinstance(existing_severity, int):
        return False
    if "greater" in operator or operator.endswith(":>"):
        return (candidate_threshold > existing_threshold) == (candidate_severity < existing_severity)
    if "less" in operator or operator.endswith(":<"):
        return (candidate_threshold < existing_threshold) == (candidate_severity < existing_severity)
    return False


async def noise_guard(
    connection: dict[str, Any], family: str, desired: dict[str, Any], *, workload_id: str | None = None,
    threshold_tolerance_pct: float = 20.0,
) -> dict[str, Any]:
    existing = await rules.list_rules(connection, workload_id=workload_id)
    candidate_scopes = [str(value) for value in desired.get("scopes") or []]
    candidate_signals = _normalized_signal(family, desired)
    findings: list[dict[str, Any]] = []
    firing_counts: dict[str, int] = {}
    try:
        for alert in await service.list_fired_alerts(connection, workload_id=workload_id, days=30):
            for key in (str(alert.get("rule_id") or "").lower(), str(alert.get("rule_name") or "").lower()):
                if key:
                    firing_counts[key] = firing_counts.get(key, 0) + 1
    except (ValueError, PermissionError):
        pass
    group_fingerprints: dict[str, set[str]] = {}
    try:
        for group in await service.list_action_groups(connection, workload_id=workload_id):
            group_fingerprints[str(group.get("id") or "").lower()] = {str(item.get("fingerprint") or "") for item in group.get("receivers") or [] if item.get("enabled")}
    except (ValueError, PermissionError):
        pass
    for row in existing:
        if desired.get("id") and row["id"].lower() == str(desired["id"]).lower():
            continue
        if row.get("family") != family or not any(_scope_matches(left, right) for left in candidate_scopes for right in row.get("scopes") or []):
            continue
        resource, _status, _error = await rules.get_rule(connection, row["id"], family)
        if not resource:
            continue
        editable = rules.editable_rule(resource)
        other_signals = _normalized_signal(family, editable)
        same_signal = bool({item["key"] for item in candidate_signals} & {item["key"] for item in other_signals})
        if not same_signal:
            continue
        pairs = [(left, right) for left in candidate_signals for right in other_signals if left["key"] == right["key"]]
        dimension_statuses = [classify_dimension_overlap(left.get("dimensions") or [], right.get("dimensions") or []) for left, right in pairs]
        if dimension_statuses and all(status == "disjoint" for status in dimension_statuses):
            continue
        deltas = [delta for left, right in pairs if (delta := _threshold_delta(left.get("threshold"), right.get("threshold"))) is not None]
        exact = bool(pairs) and all(left == right for left, right in pairs)
        layered = any(_intentional_layer(desired, editable, left, right, threshold_tolerance_pct) for left, right in pairs)
        within_tolerance = bool(deltas) and all(delta <= threshold_tolerance_pct for delta in deltas)
        shared_groups = sorted(set(desired.get("action_group_ids") or []) & set(row.get("action_group_ids") or []))
        candidate_fingerprints = set().union(*(group_fingerprints.get(str(group).lower(), set()) for group in desired.get("action_group_ids") or []))
        existing_fingerprints = set().union(*(group_fingerprints.get(str(group).lower(), set()) for group in row.get("action_group_ids") or []))
        shared_receiver_count = len(candidate_fingerprints & existing_fingerprints)
        fires = firing_counts.get(str(row["id"]).lower(), firing_counts.get(str(row["name"]).lower(), 0))
        finding_type = "layered" if layered else "exact" if exact else "near"
        findings.append({
            "rule_id": row["id"], "rule_name": row["name"], "type": finding_type,
            "risk": "informational" if layered else "high" if exact and (shared_groups or shared_receiver_count) else "medium" if exact or within_tolerance else "low",
            "same_signal": True, "shared_action_group_count": len(shared_groups),
            "shared_receiver_count": shared_receiver_count, "dimension_overlap": dimension_statuses[0] if dimension_statuses else "exact",
            "threshold_delta_pct": round(max(deltas), 2) if deltas else None, "threshold_tolerance_pct": threshold_tolerance_pct,
            "historical_firings_30d": fires, "projected_duplicate_notifications_30d": fires * shared_receiver_count,
            "explanation": "Intentional severity/threshold escalation pattern." if layered else "Same scope, semantic signal, dimensions, and condition logic." if exact else "Same scope and semantic signal with compatible dimensions but different tuning.",
        })
    findings.sort(key=lambda item: ({"high": 0, "medium": 1, "low": 2, "informational": 3}[item["risk"]], item["rule_name"].lower()))
    actionable = [item for item in findings if item["type"] != "layered"]
    return {
        "overlap": bool(actionable), "count": len(actionable), "layered_count": len(findings) - len(actionable), "findings": findings[:50],
        "projected_duplicate_notifications_30d": sum(item["projected_duplicate_notifications_30d"] for item in actionable),
        "warning": "This draft may generate duplicate incidents or notification fan-out. Review the overlapping rules before requesting the change." if actionable else "",
    }


async def suggest_action_groups(
    connection: dict[str, Any], tenant_id: str, *, subject_kind: str, subject_id: str,
    workload_id: str | None = None,
) -> dict[str, Any]:
    from app.ownership.resolve import build_context, resolve_owner

    context = build_context(tenant_id)
    resolved = resolve_owner(tenant_id, subject_kind, subject_id, ctx=context)
    owners = resolved.get("owners") or []
    owner_emails = {str(owner.get("email") or "").strip().lower() for owner in owners if owner.get("email")}
    owner_names = {str(owner.get("display_name") or "").strip().lower() for owner in owners if owner.get("display_name")}
    subscription = service._subscription_from_id(subject_id)
    groups = await service.list_action_groups(connection, workload_id=workload_id, subscription_id=subscription or None)
    raw_rows = await service._arg(
        connection,
        "resources | where type =~ 'microsoft.insights/actiongroups' | project id,name,subscriptionId,resourceGroup,tags,properties",
        {subscription} if subscription else set(), max_rows=5000,
    )
    raw_by_id = {str(row.get("id") or "").lower(): row for row in raw_rows}
    suggestions = []
    for group in groups:
        raw = raw_by_id.get(group["id"].lower(), {})
        props = raw.get("properties") if isinstance(raw.get("properties"), dict) else {}
        emails = {str(item.get("emailAddress") or "").strip().lower() for item in props.get("emailReceivers") or [] if isinstance(item, dict)}
        name_haystack = " ".join([str(group.get("name") or ""), str(group.get("short_name") or ""), " ".join(f"{key} {value}" for key, value in (group.get("tags") or {}).items())]).lower()
        matched_email = bool(owner_emails & emails)
        matched_name = any(name and name in name_haystack for name in owner_names)
        score = 0.95 if matched_email else 0.65 if matched_name else 0.25 if subscription and group.get("subscription_id", "").lower() == subscription.lower() else 0.0
        if not score:
            continue
        suggestions.append({
            "action_group_id": group["id"], "name": group["name"], "confidence": score,
            "reason": "Active receiver matches the resolved owner." if matched_email else "Action Group name or tags match the resolved owner." if matched_name else "Same subscription fallback.",
            "receiver_count": group["receiver_count"],
        })
    suggestions.sort(key=lambda item: (-item["confidence"], item["name"].lower()))
    return {
        "subject_kind": subject_kind, "subject_id": subject_id, "ownership_source": resolved.get("source"),
        "owners": [{"display_name": owner.get("display_name"), "role": owner.get("role"), "primary": owner.get("primary")} for owner in owners],
        "suggestions": suggestions[:20], "count": len(suggestions),
    }
