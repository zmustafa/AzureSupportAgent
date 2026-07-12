"""Tenant-scoped Action Group routing and AMBA blueprint deployment planning.

This module is deliberately control-plane only: it stores direct routing rules, immutable
blueprints, assignments, and draft plans.  Submitting a plan creates approval-ledger rows;
it never writes to Azure or applies a change.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.alerts_manager import service
from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "alerts_manager_planner.json"
_COLLECTIONS = ("routing_rules", "blueprints", "assignments", "plans")
_SCOPE_KINDS = {"subscription", "workload", "workload_group"}
_CLASSIFICATIONS = {"covered", "equivalent", "drifted", "missing", "blocked"}
_SEVERITY = {"critical": 0, "error": 1, "warning": 2, "info": 3}
_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_ACTION_GROUP_ID_RE = re.compile(
    r"^/subscriptions/[^/]+/resourcegroups/[^/]+/providers/microsoft\.insights/actiongroups/[^/]+$",
    re.IGNORECASE,
)
_SUPPORTED_GAP_TYPES = {"baseline_missing": "missing", "baseline_misconfigured": "drifted"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {"version": 1, "tenants": {}})
    data = data if isinstance(data, dict) else {"version": 1, "tenants": {}}
    return _migrate_legacy_catalog(data)


def _write(data: dict[str, Any]) -> None:
    jsonstore.write_json(_PATH, data)


def _migrate_legacy_catalog(data: dict[str, Any]) -> dict[str, Any]:
    """Resolve legacy catalog references once, preserve diagnostics, then remove catalog data."""
    changed = False
    for bucket in (data.get("tenants") or {}).values():
        if not isinstance(bucket, dict) or "catalog" not in bucket:
            continue
        catalog = {
            entry_id: entry
            for entry_id, encrypted in (bucket.get("catalog") or {}).items()
            if (entry := _decode(encrypted))
        }
        for rule_id, encrypted in list((bucket.get("routing_rules") or {}).items()):
            rule = _decode(encrypted)
            if not rule or "catalog_entry_ids" not in rule:
                continue
            resolved: list[str] = []
            unresolved: list[str] = []
            for entry_id in _string_list(rule.pop("catalog_entry_ids"), limit=10):
                action_group_id = str((catalog.get(entry_id) or {}).get("action_group_id") or "").strip()
                if is_action_group_id(action_group_id):
                    if action_group_id.lower() not in {value.lower() for value in resolved}:
                        resolved.append(action_group_id)
                else:
                    unresolved.append(entry_id)
            rule["action_group_ids"] = resolved
            if unresolved:
                rule["migration_diagnostics"] = [
                    f"Legacy routing destination '{entry_id}' could not be resolved to an Action Group resource ID."
                    for entry_id in unresolved
                ]
            bucket["routing_rules"][rule_id] = service.encrypted_json(rule)
        del bucket["catalog"]
        changed = True
    if changed:
        if _PATH.exists():
            backup = _PATH.with_name(f"{_PATH.name}.catalog-backup")
            if not backup.exists():
                shutil.copy2(_PATH, backup)
        _write(data)
    return data


def _bucket(data: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    bucket = data.setdefault("tenants", {}).setdefault(tenant_id or "default", {})
    for name in _COLLECTIONS:
        bucket.setdefault(name, {})
    return bucket


def _decode(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        item = service.decrypted_json(value)
    except (TypeError, ValueError):
        return None
    return item if isinstance(item, dict) else None


def _items(tenant_id: str, collection: str) -> list[dict[str, Any]]:
    raw = _bucket(_read(), tenant_id).get(collection, {})
    out = []
    for item_id, encrypted in raw.items():
        item = _decode(encrypted)
        if item:
            item["id"] = item_id
            out.append(item)
    return out


def _get(tenant_id: str, collection: str, item_id: str) -> dict[str, Any] | None:
    encrypted = _bucket(_read(), tenant_id).get(collection, {}).get(item_id)
    item = _decode(encrypted)
    if item:
        item["id"] = item_id
    return item


def _put(tenant_id: str, collection: str, item: dict[str, Any]) -> dict[str, Any]:
    data = _read()
    item_id = str(item.get("id") or uuid.uuid4())
    stored = copy.deepcopy(item)
    stored.pop("id", None)
    _bucket(data, tenant_id)[collection][item_id] = service.encrypted_json(stored)
    _write(data)
    return {**stored, "id": item_id}


def _delete(tenant_id: str, collection: str, item_id: str) -> bool:
    data = _read()
    values = _bucket(data, tenant_id)[collection]
    if item_id not in values:
        return False
    del values[item_id]
    _write(data)
    return True


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _string_list(value: Any, *, lower: bool = False, limit: int = 50) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for raw in value[:limit]:
        item = str(raw or "").strip()
        if not item:
            continue
        item = item.lower() if lower else item
        if item not in result:
            result.append(item)
    return result


def is_action_group_id(value: Any) -> bool:
    return bool(_ACTION_GROUP_ID_RE.fullmatch(str(value or "").strip().rstrip("/")))


def validate_action_group_ids(
    action_group_ids: list[str], live_action_groups: list[dict[str, Any]] | None = None,
) -> dict[str, list[str]]:
    errors = [f"Invalid Action Group resource ID: {value}" for value in action_group_ids if not is_action_group_id(value)]
    warnings: list[str] = []
    if live_action_groups is not None:
        live = {str(item.get("id") or "").lower().rstrip("/"): item for item in live_action_groups}
        for value in action_group_ids:
            item = live.get(value.lower().rstrip("/"))
            if not item:
                errors.append(f"Action Group was not found in the selected live Azure inventory: {value}")
            elif not item.get("enabled", True):
                warnings.append(f"Action Group is disabled: {item.get('name') or value}")
            elif int(item.get("active_receiver_count", item.get("receiver_count", 0)) or 0) <= 0:
                warnings.append(f"Action Group has no active receivers: {item.get('name') or value}")
    return {"errors": errors, "warnings": warnings}


# ------------------------------------------------------------------------ Routing matrix
def list_routing_rules(tenant_id: str) -> list[dict[str, Any]]:
    return sorted(_items(tenant_id, "routing_rules"), key=lambda item: (int(item.get("order", 0)), item["id"]))


def get_routing_rule(tenant_id: str, rule_id: str) -> dict[str, Any] | None:
    return _get(tenant_id, "routing_rules", rule_id)


def save_routing_rule(
    tenant_id: str, payload: dict[str, Any], *, actor: str, rule_id: str = "",
) -> dict[str, Any]:
    existing = _get(tenant_id, "routing_rules", rule_id) if rule_id else None
    if rule_id and not existing:
        raise KeyError("Routing rule not found.")
    action_group_ids = _string_list(payload.get("action_group_ids"), limit=10)
    if not action_group_ids:
        raise ValueError("At least one Action Group resource ID is required.")
    validation = validate_action_group_ids(action_group_ids)
    if validation["errors"]:
        raise ValueError(validation["errors"][0])
    severities: list[int] = []
    for value in payload.get("severities") or []:
        try:
            severity = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= severity <= 4 and severity not in severities:
            severities.append(severity)
    now = _now()
    item = {
        "id": rule_id or str(uuid.uuid4()),
        "name": _text(payload.get("name"), 160),
        "order": max(0, min(10000, int(payload.get("order") or 0))),
        "enabled": bool(payload.get("enabled", True)),
        "fallback": bool(payload.get("fallback", False)),
        "severities": severities,
        "categories": _string_list(payload.get("categories"), lower=True),
        "environments": _string_list(payload.get("environments"), lower=True),
        "scopes": _string_list(payload.get("scopes")),
        "action_group_ids": action_group_ids,
        "created_at": (existing or {}).get("created_at") or now,
        "created_by": (existing or {}).get("created_by") or actor,
        "updated_at": now,
        "updated_by": actor,
    }
    if not item["name"]:
        raise ValueError("Routing rule name is required.")
    return _put(tenant_id, "routing_rules", item)


def delete_routing_rule(tenant_id: str, rule_id: str) -> bool:
    return _delete(tenant_id, "routing_rules", rule_id)


def _scope_matches(configured: list[str], actual: str) -> bool:
    if not configured:
        return True
    candidate = actual.lower().rstrip("/")
    return any(candidate == value.lower().rstrip("/") or candidate.startswith(value.lower().rstrip("/") + "/") for value in configured)


def resolve_route(
    tenant_id: str, event: dict[str, Any], *, live_action_groups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    severity = event.get("severity")
    if isinstance(severity, str) and not severity.isdigit():
        severity = _SEVERITY.get(severity.lower())
    try:
        severity = int(severity)
    except (TypeError, ValueError):
        severity = None
    category = str(event.get("category") or "").lower()
    environment = str(event.get("environment") or "").lower()
    scope = str(event.get("scope") or event.get("resource_id") or "")
    rules = [rule for rule in list_routing_rules(tenant_id) if rule.get("enabled", True)]
    trace: list[dict[str, Any]] = []
    selected: dict[str, Any] | None = None
    for fallback_pass in (False, True):
        for rule in rules:
            if bool(rule.get("fallback")) != fallback_pass:
                continue
            checks = {
                "severity": not rule.get("severities") or severity in rule["severities"],
                "category": not rule.get("categories") or category in rule["categories"],
                "environment": not rule.get("environments") or environment in rule["environments"],
                "scope": _scope_matches(rule.get("scopes") or [], scope),
            }
            matched = all(checks.values())
            trace.append({"rule_id": rule["id"], "name": rule["name"], "order": rule["order"], "fallback": fallback_pass, "matched": matched, "checks": checks})
            if matched:
                selected = rule
                break
        if selected:
            break
    if not selected:
        return {"matched": False, "rule": None, "action_group_ids": [], "action_groups": [], "diagnostics": [], "explanation": "No enabled routing rule or fallback matched.", "trace": trace}
    action_group_ids = list(selected.get("action_group_ids") or [])
    validation = validate_action_group_ids(action_group_ids, live_action_groups)
    live = {str(item.get("id") or "").lower().rstrip("/"): item for item in (live_action_groups or [])}
    entries = [{
        "id": action_group_id,
        "name": str((live.get(action_group_id.lower().rstrip("/")) or {}).get("name") or action_group_id.rstrip("/").rsplit("/", 1)[-1]),
    } for action_group_id in action_group_ids]
    diagnostics = [*selected.get("migration_diagnostics", []), *validation["errors"], *validation["warnings"]]
    usable = not validation["errors"] and not validation["warnings"] and bool(action_group_ids)
    explanation = (
        f"Matched {'fallback ' if selected.get('fallback') else ''}rule '{selected['name']}' at order {selected['order']}; "
        f"resolved {len(action_group_ids)} direct Action Group ID{'s' if len(action_group_ids) != 1 else ''}."
    )
    if diagnostics:
        explanation += " " + " ".join(diagnostics)
    return {"matched": usable, "rule": selected, "action_group_ids": action_group_ids, "action_groups": entries, "diagnostics": diagnostics, "explanation": explanation, "trace": trace}


# ------------------------------------------------------------------- Immutable blueprints
def list_blueprints(tenant_id: str) -> list[dict[str, Any]]:
    versions = _items(tenant_id, "blueprints")
    by_blueprint: dict[str, dict[str, Any]] = {}
    for item in versions:
        blueprint_id = item.get("blueprint_id", "")
        if blueprint_id not in by_blueprint or int(item.get("version", 0)) > int(by_blueprint[blueprint_id].get("version", 0)):
            by_blueprint[blueprint_id] = item
    return sorted(by_blueprint.values(), key=lambda item: (item.get("name", "").lower(), item.get("blueprint_id", "")))


def list_blueprint_versions(tenant_id: str, blueprint_id: str) -> list[dict[str, Any]]:
    return sorted(
        [item for item in _items(tenant_id, "blueprints") if item.get("blueprint_id") == blueprint_id],
        key=lambda item: int(item.get("version", 0)), reverse=True,
    )


def get_blueprint(tenant_id: str, blueprint_id: str, version: int | None = None) -> dict[str, Any] | None:
    versions = list_blueprint_versions(tenant_id, blueprint_id)
    if version is None:
        return versions[0] if versions else None
    return next((item for item in versions if int(item.get("version", 0)) == version), None)


def create_blueprint_version(
    tenant_id: str, payload: dict[str, Any], *, actor: str, blueprint_id: str = "",
) -> dict[str, Any]:
    existing = list_blueprint_versions(tenant_id, blueprint_id) if blueprint_id else []
    if blueprint_id and not existing:
        raise KeyError("Blueprint not found.")
    included = _string_list(payload.get("included_resource_types"), lower=True, limit=200)
    if not included:
        raise ValueError("At least one included AMBA resource type is required.")
    severity_overrides: dict[str, int] = {}
    for key, value in (payload.get("severity_overrides") or {}).items():
        try:
            severity = int(value)
        except (TypeError, ValueError):
            severity = _SEVERITY.get(str(value).lower(), -1)
        if 0 <= severity <= 4:
            severity_overrides[_text(key, 128)] = severity
    version = max((int(item.get("version", 0)) for item in existing), default=0) + 1
    stable_id = blueprint_id or str(uuid.uuid4())
    item = {
        "id": f"{stable_id}:{version}",
        "blueprint_id": stable_id,
        "version": version,
        "name": _text(payload.get("name") or (existing[0].get("name") if existing else ""), 160),
        "description": _text(payload.get("description"), 1000),
        "amba_version": _text(payload.get("amba_version") or (existing[0].get("amba_version") if existing else ""), 64),
        "included_resource_types": included,
        "severity_overrides": severity_overrides,
        "default_disabled": False,
        "created_at": _now(),
        "created_by": actor,
    }
    if not item["name"] or not item["amba_version"]:
        raise ValueError("Blueprint name and AMBA version are required.")
    return _put(tenant_id, "blueprints", item)


def delete_blueprint_version(tenant_id: str, blueprint_id: str, version: int) -> bool:
    if any(item.get("blueprint_id") == blueprint_id and int(item.get("blueprint_version", 0)) == version for item in _items(tenant_id, "assignments")):
        raise ValueError("Blueprint version is assigned and cannot be deleted.")
    item = get_blueprint(tenant_id, blueprint_id, version)
    return bool(item and _delete(tenant_id, "blueprints", item["id"]))


# -------------------------------------------------------------------------- Assignments
def list_assignments(tenant_id: str) -> list[dict[str, Any]]:
    return sorted(_items(tenant_id, "assignments"), key=lambda item: item.get("created_at", ""), reverse=True)


def get_assignment(tenant_id: str, assignment_id: str) -> dict[str, Any] | None:
    return _get(tenant_id, "assignments", assignment_id)


def save_assignment(
    tenant_id: str, payload: dict[str, Any], *, actor: str, assignment_id: str = "",
) -> dict[str, Any]:
    existing = _get(tenant_id, "assignments", assignment_id) if assignment_id else None
    if assignment_id and not existing:
        raise KeyError("Blueprint assignment not found.")
    scope_kind = _text(payload.get("scope_kind"), 32)
    scope_id = _text(payload.get("scope_id"), 1000)
    if scope_kind not in _SCOPE_KINDS or not scope_id:
        raise ValueError("Assignment scope must be a subscription, workload, or workload_group.")
    blueprint_id = _text(payload.get("blueprint_id"), 64)
    version = int(payload.get("blueprint_version") or 0)
    if not get_blueprint(tenant_id, blueprint_id, version):
        raise ValueError("Assigned blueprint version was not found.")
    now = _now()
    item = {
        "id": assignment_id or str(uuid.uuid4()),
        "blueprint_id": blueprint_id,
        "blueprint_version": version,
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "connection_id": _text(payload.get("connection_id"), 128),
        "environment": _text(payload.get("environment"), 64).lower(),
        "monitoring_resource_group": _text(payload.get("monitoring_resource_group"), 90),
        "enabled": bool(payload.get("enabled", True)),
        "created_at": (existing or {}).get("created_at") or now,
        "created_by": (existing or {}).get("created_by") or actor,
        "updated_at": now,
        "updated_by": actor,
    }
    return _put(tenant_id, "assignments", item)


def delete_assignment(tenant_id: str, assignment_id: str) -> bool:
    if any(item.get("assignment_id") == assignment_id and item.get("status") not in {"draft", "rejected"} for item in _items(tenant_id, "plans")):
        raise ValueError("Assignment has an active submitted deployment plan.")
    return _delete(tenant_id, "assignments", assignment_id)


# ------------------------------------------------------------------------------ Planner
def _snapshot_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for group in snapshot.get("groups") or []:
        resource_type = str(group.get("resource_type") or "").lower()
        for resource in group.get("rows") or []:
            for cell in resource.get("cells") or []:
                rows.append({
                    "resource_id": resource.get("resource_id", ""), "resource_name": resource.get("resource_name", ""),
                    "resource_type": resource_type, "resource_group": resource.get("resource_group", ""),
                    "subscription_id": resource.get("subscription_id", ""), "location": resource.get("location", ""),
                    "alert_key": cell.get("alert_key", ""), "alert_name": cell.get("alert_name", ""),
                    "amba_category": cell.get("amba_category", ""), "severity": cell.get("severity", "warning"),
                    "status": cell.get("status", "missing"), "recommended": cell.get("recommended") or {},
                    "observed": cell.get("observed") or {}, "why": cell.get("why", ""),
                })
    return rows or list(snapshot.get("gaps") or [])


def derive_coverage_items(tenant_id: str, assignment: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    from app.amba import cache

    kind, scope_id = assignment["scope_kind"], assignment["scope_id"]
    snapshots: list[dict[str, Any]] = []
    sources: list[str] = []
    if kind == "workload_group":
        from app.workloads.registry import list_workloads

        for workload in list_workloads():
            if workload.get("group_id") != scope_id or (workload.get("tenant_id") and workload.get("tenant_id") != tenant_id):
                continue
            snapshot = cache.read_snapshot(tenant_id, "workload", workload["id"])
            if snapshot:
                snapshots.append(snapshot)
                sources.append(f"workload:{workload['id']}")
    else:
        snapshot = cache.read_snapshot(tenant_id, kind, scope_id)
        if snapshot:
            snapshots.append(snapshot)
            sources.append(f"{kind}:{scope_id}")
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for snapshot in snapshots:
        for row in _snapshot_rows(snapshot):
            deduped[(str(row.get("resource_id", "")).lower(), str(row.get("alert_key", "")).lower())] = row
    return list(deduped.values()), sources


def _classification(gap: dict[str, Any]) -> str:
    explicit = str(gap.get("classification") or "").lower()
    if explicit in _CLASSIFICATIONS:
        return explicit
    status = str(gap.get("status") or "missing").lower()
    if status in {"present", "covered"}:
        return "covered"
    if status == "equivalent":
        return "equivalent"
    if status in {"misconfigured", "drifted"}:
        return "drifted"
    return "missing"


def _rule_name(gap: dict[str, Any]) -> str:
    resource = _NAME_RE.sub("-", str(gap.get("resource_name") or "resource")).strip("-.")
    key = _NAME_RE.sub("-", str(gap.get("alert_key") or "amba")).strip("-.")
    return f"amba-{resource}-{key}"[:240].rstrip("-.")


def _severity_for(blueprint: dict[str, Any], gap: dict[str, Any]) -> int:
    overrides = blueprint.get("severity_overrides") or {}
    key = str(gap.get("alert_key") or "")
    label = str(gap.get("severity") or "warning").lower()
    return int(overrides.get(key, overrides.get(label, _SEVERITY.get(label, 2))))


def gap_identity(gap: dict[str, Any]) -> str:
    """Return the analysis decision key or a deterministic key from trusted gap fields."""
    decision_key = _text(gap.get("decision_key"), 1000)
    if decision_key:
        return decision_key
    identity = {
        key: str(gap.get(key) or "").strip().lower()
        for key in ("type", "resource_id", "alert_key", "rule_id", "action_group_id", "signal")
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"gap:{digest[:32]}"


def _gap_plan_scope(context: dict[str, Any]) -> tuple[str, str]:
    scopes = [
        ("workload", _text(context.get("workload_id"), 1000)),
        ("subscription", _text(context.get("subscription_id"), 1000)),
        ("management_group", _text(context.get("management_group_id"), 1000)),
    ]
    selected = [(kind, value) for kind, value in scopes if value]
    if len(selected) != 1:
        raise ValueError("Exactly one workload, subscription, or management-group scope is required.")
    return selected[0]


def _normalized_selected_gap(gap: dict[str, Any]) -> dict[str, Any]:
    """Map the public gap-analysis DTO to the planner's AMBA proposal fields."""
    gap_type = _text(gap.get("type"), 64).lower()
    return {
        "decision_key": gap_identity(gap),
        "type": gap_type,
        "resource_id": _text(gap.get("resource_id"), 1000),
        "resource_name": _text(gap.get("resource_name"), 256),
        "resource_type": _text(gap.get("resource_type"), 256).lower(),
        "resource_group": _text(gap.get("resource_group"), 256),
        "subscription_id": _text(gap.get("subscription_id"), 128),
        "location": _text(gap.get("location"), 128),
        "alert_key": _text(gap.get("alert_key"), 128),
        "alert_name": _text(gap.get("signal") or gap.get("alert_name"), 256),
        "amba_category": _text(gap.get("amba_category") or gap.get("category"), 128).lower(),
        "severity": _text(gap.get("risk") or gap.get("severity") or "warning", 32).lower(),
        "status": _SUPPORTED_GAP_TYPES.get(gap_type, _text(gap.get("status") or "unsupported", 32).lower()),
        "recommended": copy.deepcopy(gap.get("recommended") or {}),
        "why": _text(gap.get("explanation") or gap.get("why"), 2048),
    }


def _proposal(assignment: dict[str, Any], blueprint: dict[str, Any], gap: dict[str, Any], action_group_ids: list[str]) -> tuple[dict[str, Any] | None, list[str]]:
    recommended = gap.get("recommended") or {}
    errors: list[str] = [str(value) for value in recommended.get("metric_validation_errors") or [] if str(value)]
    if str(recommended.get("signal") or gap.get("signal") or "metric").lower() != "metric":
        errors.append("Only metric-rule proposals are supported by this MVP.")
    if not str(recommended.get("metric") or "").strip():
        errors.append("AMBA recommendation has no metric name.")
    if not isinstance(recommended.get("threshold"), (int, float)):
        errors.append("AMBA recommendation has no numeric threshold.")
    for field in ("resource_id", "subscription_id", "resource_group"):
        if not str(gap.get(field) or "").strip():
            errors.append(f"Coverage item is missing {field}.")
    monitoring_group = assignment.get("monitoring_resource_group") or gap.get("resource_group")
    if not monitoring_group:
        errors.append("A monitoring resource group is required.")
    if errors:
        return None, errors
    desired = {
        "name": _rule_name(gap), "enabled": True, "severity": _severity_for(blueprint, gap),
        "description": _text(gap.get("why") or f"AMBA baseline: {gap.get('alert_name', '')}", 2048),
        "scopes": [gap["resource_id"]], "subscription_id": gap["subscription_id"],
        "resource_group": monitoring_group, "location": "global", "evaluation_frequency": "PT5M",
        "window_size": recommended.get("window") or "PT5M", "auto_mitigate": True,
        "action_group_ids": action_group_ids, "target_resource_type": "", "target_resource_region": "",
        "tags": {"managed-by": "alerts-manager", "amba-blueprint": blueprint["blueprint_id"], "amba-version": str(blueprint["amba_version"])},
        "conditions": [{
            "name": str(gap.get("alert_key") or "amba-condition")[:128],
            "metric_name": recommended.get("metric"), "metric_namespace": gap.get("resource_type"),
            "threshold_type": "static", "operator": recommended.get("operator") or "GreaterThan",
            "threshold": recommended.get("threshold"), "aggregation": recommended.get("aggregation") or "Average",
            "dimensions": recommended.get("dimensions") or [], "min_failing_periods": 1, "evaluation_periods": 1,
        }],
    }
    from app.alerts_manager import rules

    errors.extend(rules.validate_rule_payload("metric", desired, create=True))
    if errors:
        return None, list(dict.fromkeys(errors))
    target_id = f"/subscriptions/{gap['subscription_id']}/resourceGroups/{monitoring_group}/providers/Microsoft.Insights/metricAlerts/{desired['name']}"
    return {"family": "metric", "operation": "create", "target_id": target_id, "desired": desired, "body": rules.build_rule_body("metric", desired)}, []


def preview_plan(
    tenant_id: str, assignment_id: str, *, actor: str,
    coverage_items: list[dict[str, Any]] | None = None,
    live_action_groups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    assignment = get_assignment(tenant_id, assignment_id)
    if not assignment or not assignment.get("enabled", True):
        raise ValueError("Enabled blueprint assignment not found.")
    blueprint = get_blueprint(tenant_id, assignment["blueprint_id"], int(assignment["blueprint_version"]))
    if not blueprint:
        raise ValueError("Assigned blueprint version not found.")
    if coverage_items is None:
        coverage_items, sources = derive_coverage_items(tenant_id, assignment)
    else:
        sources = ["request"]
    items: list[dict[str, Any]] = []
    counts = {key: 0 for key in _CLASSIFICATIONS}
    included_types = set(blueprint.get("included_resource_types") or [])
    for gap in coverage_items:
        classification = _classification(gap)
        reasons: list[str] = []
        route: dict[str, Any] | None = None
        proposal: dict[str, Any] | None = None
        resource_type = str(gap.get("resource_type") or "").lower()
        if resource_type not in included_types:
            classification = "blocked"
            reasons.append("Resource type is not included in this blueprint version.")
        elif classification in {"missing", "drifted"}:
            route = resolve_route(tenant_id, {
                "severity": _severity_for(blueprint, gap), "category": gap.get("amba_category"),
                "environment": assignment.get("environment"), "scope": gap.get("resource_id"),
            }, live_action_groups=live_action_groups)
            if not route["matched"]:
                classification = "blocked"
                reasons.append(route["explanation"])
            else:
                proposal, proposal_errors = _proposal(
                    assignment, blueprint, gap, route["action_group_ids"],
                )
                if proposal_errors:
                    classification = "blocked"
                    reasons.extend(proposal_errors)
        counts[classification] += 1
        actionable = proposal is not None and classification in {"missing", "drifted"}
        items.append({
            "id": str(uuid.uuid4()), "classification": classification, "actionable": actionable,
            "included": actionable, "resource_id": gap.get("resource_id", ""),
            "resource_name": gap.get("resource_name", ""), "resource_type": resource_type,
            "alert_key": gap.get("alert_key", ""), "alert_name": gap.get("alert_name", ""),
            "category": gap.get("amba_category", ""), "severity": _severity_for(blueprint, gap),
            "source_status": gap.get("status", ""), "reasons": reasons,
            "routing": ({"rule_id": route["rule"]["id"], "rule_name": route["rule"]["name"], "action_group_ids": route["action_group_ids"], "action_groups": route["action_groups"], "diagnostics": route["diagnostics"], "explanation": route["explanation"]} if route and route.get("rule") else None),
            "proposal": proposal,
        })
    plan = {
        "id": str(uuid.uuid4()), "assignment_id": assignment_id,
        "blueprint_id": blueprint["blueprint_id"], "blueprint_version": blueprint["version"],
        "amba_version": blueprint["amba_version"], "connection_id": assignment.get("connection_id", ""),
        "scope_kind": assignment["scope_kind"], "scope_id": assignment["scope_id"],
        "status": "draft", "counts": counts, "items": items, "coverage_sources": sources,
        "created_at": _now(), "created_by": actor, "updated_at": _now(), "updated_by": actor,
        "validated_at": "", "submitted_at": "", "submitted_by": "", "batch_id": "", "change_ids": [],
        "decided_at": "", "decided_by": "", "decision_reason": "",
    }
    return _put(tenant_id, "plans", plan)


def preview_gap_plan(
    tenant_id: str, context: dict[str, Any], gaps: list[dict[str, Any]], *, actor: str,
    routing_mode: str, common_action_group_id: str = "",
    live_action_groups: list[dict[str, Any]] | None = None,
    pending_target_ids: set[str] | None = None,
    active_gap_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Persist an enabled-by-default draft generated only from selected, sanitized analysis gaps."""
    if not gaps:
        raise ValueError("Select at least one gap.")
    if routing_mode not in {"common", "rules"}:
        raise ValueError("Routing mode must be common or rules.")
    scope_kind, scope_id = _gap_plan_scope(context)
    selected_by_id: dict[str, dict[str, Any]] = {}
    for raw_gap in gaps:
        normalized = _normalized_selected_gap(raw_gap)
        selected_by_id.setdefault(normalized["decision_key"], normalized)
    selected = list(selected_by_id.values())
    source_gap_ids = [gap["decision_key"] for gap in selected]

    common_id = _text(common_action_group_id, 1000).rstrip("/")
    common_validation = {"errors": [], "warnings": []}
    if routing_mode == "common":
        if not common_id:
            raise ValueError("A common Action Group resource ID is required.")
        common_validation = validate_action_group_ids([common_id], live_action_groups)

    active_plans = [plan for plan in _items(tenant_id, "plans") if plan.get("status") in {"pending", "approved"}]
    registry_active_gap_ids = {
        str(gap_id)
        for plan in active_plans
        for gap_id in (plan.get("source_gap_ids") or [])
    }
    effective_active_gap_ids = registry_active_gap_ids if active_gap_ids is None else active_gap_ids
    registry_active_targets = {
        str((item.get("proposal") or {}).get("target_id") or "").lower()
        for plan in active_plans
        for item in (plan.get("items") or [])
        if item.get("included")
    }
    active_targets = registry_active_targets if active_gap_ids is None else set()
    active_targets.update(value.lower() for value in (pending_target_ids or set()))

    blueprint = {
        "blueprint_id": "selected-gaps", "version": 1, "amba_version": "gap-analysis",
        "severity_overrides": {}, "default_disabled": False,
    }
    assignment = {
        "connection_id": _text(context.get("connection_id"), 128),
        "environment": _text(context.get("environment"), 64).lower(),
        "monitoring_resource_group": _text(context.get("monitoring_resource_group"), 90),
    }
    live_by_id = {str(item.get("id") or "").lower().rstrip("/"): item for item in (live_action_groups or [])}
    items: list[dict[str, Any]] = []
    counts = {key: 0 for key in _CLASSIFICATIONS}
    for gap in selected:
        source_gap_id = gap["decision_key"]
        classification = _SUPPORTED_GAP_TYPES.get(gap["type"], "blocked")
        reasons: list[str] = []
        route: dict[str, Any] | None = None
        proposal: dict[str, Any] | None = None
        if classification == "blocked":
            reasons.append(f"Gap type '{gap['type'] or 'unknown'}' is unsupported; bulk remediation currently supports metric baseline gaps only.")
        elif source_gap_id in effective_active_gap_ids:
            classification = "blocked"
            reasons.append("This gap already belongs to a pending or approved deployment plan.")
        else:
            if routing_mode == "common":
                live_group = live_by_id.get(common_id.lower()) or {}
                diagnostics = [*common_validation["errors"], *common_validation["warnings"]]
                route = {
                    "matched": not diagnostics,
                    "rule": None,
                    "action_group_ids": [common_id],
                    "action_groups": [{"id": common_id, "name": str(live_group.get("name") or common_id.rsplit("/", 1)[-1])}],
                    "diagnostics": diagnostics,
                    "explanation": "Using the common live Azure Action Group selected for this gap batch." + (" " + " ".join(diagnostics) if diagnostics else ""),
                }
            else:
                route = resolve_route(tenant_id, {
                    "severity": _severity_for(blueprint, gap), "category": gap.get("amba_category"),
                    "environment": assignment.get("environment"), "scope": gap.get("resource_id"),
                }, live_action_groups=live_action_groups)
            if not route["matched"]:
                classification = "blocked"
                reasons.append(route["explanation"])
            else:
                proposal, proposal_errors = _proposal(assignment, blueprint, gap, route["action_group_ids"])
                if proposal_errors:
                    classification = "blocked"
                    reasons.extend(proposal_errors)
                elif str(proposal.get("target_id") or "").lower() in active_targets:
                    classification = "blocked"
                    reasons.append("Another active plan or selected gap already targets the generated alert-rule resource ID.")
                    proposal = None
                else:
                    active_targets.add(str(proposal.get("target_id") or "").lower())
        counts[classification] += 1
        actionable = proposal is not None and classification in {"missing", "drifted"}
        items.append({
            "id": str(uuid.uuid4()), "source_gap_id": source_gap_id,
            "classification": classification, "actionable": actionable, "included": actionable,
            "resource_id": gap["resource_id"], "resource_name": gap["resource_name"],
            "resource_type": gap["resource_type"], "alert_key": gap["alert_key"],
            "alert_name": gap["alert_name"], "category": gap["amba_category"],
            "severity": _severity_for(blueprint, gap), "source_status": gap["status"], "reasons": reasons,
            "routing": ({
                "mode": routing_mode,
                "rule_id": str((route.get("rule") or {}).get("id") or ""),
                "rule_name": str((route.get("rule") or {}).get("name") or ("Common Action Group" if routing_mode == "common" else "No matching routing rule")),
                "action_group_ids": route.get("action_group_ids") or [],
                "action_groups": route.get("action_groups") or [],
                "diagnostics": route.get("diagnostics") or [], "explanation": route.get("explanation") or "",
            } if route else None),
            "proposal": proposal,
        })

    now = _now()
    plan = {
        "id": str(uuid.uuid4()), "assignment_id": "", "source": "selected_gaps",
        "source_gap_ids": source_gap_ids, "routing_mode": routing_mode,
        "common_action_group_id": common_id if routing_mode == "common" else "",
        "blueprint_id": "", "blueprint_version": 0, "amba_version": "",
        "connection_id": assignment["connection_id"], "scope_kind": scope_kind, "scope_id": scope_id,
        "workload_id": _text(context.get("workload_id"), 1000),
        "subscription_id": _text(context.get("subscription_id"), 1000),
        "management_group_id": _text(context.get("management_group_id"), 1000),
        "environment": assignment["environment"], "monitoring_resource_group": assignment["monitoring_resource_group"],
        "status": "draft", "counts": counts, "items": items, "coverage_sources": [f"gap-analysis:{scope_kind}:{scope_id}"],
        "created_at": now, "created_by": actor, "updated_at": now, "updated_by": actor,
        "validated_at": "", "submitted_at": "", "submitted_by": "", "batch_id": "", "change_ids": [],
        "decided_at": "", "decided_by": "", "decision_reason": "",
    }
    return _put(tenant_id, "plans", plan)


def list_plans(tenant_id: str, status: str = "", *, compact: bool = False) -> list[dict[str, Any]]:
    rows = _items(tenant_id, "plans")
    if status:
        rows = [item for item in rows if item.get("status") == status]
    rows.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    if compact:
        summary_fields = (
            "id", "status", "scope_kind", "scope_id", "blueprint_id", "blueprint_version",
            "amba_version", "counts", "created_at", "created_by", "updated_at", "updated_by",
        )
        return [
            {key: item.get(key) for key in summary_fields} | {"item_count": len(item.get("items") or [])}
            for item in rows
        ]
    return [{key: value for key, value in item.items() if key != "items"} | {"item_count": len(item.get("items") or [])} for item in rows]


def get_plan(tenant_id: str, plan_id: str) -> dict[str, Any] | None:
    return _get(tenant_id, "plans", plan_id)


def update_plan_items(tenant_id: str, plan_id: str, selections: list[dict[str, Any]], *, actor: str) -> dict[str, Any]:
    plan = get_plan(tenant_id, plan_id)
    if not plan:
        raise KeyError("Deployment plan not found.")
    if plan.get("status") != "draft":
        raise ValueError("Only draft plan items can be changed.")
    wanted = {str(item.get("item_id") or ""): bool(item.get("included")) for item in selections}
    known = {item["id"] for item in plan.get("items") or []}
    if set(wanted) - known:
        raise ValueError("One or more deployment-plan items were not found.")
    for item in plan.get("items") or []:
        if item["id"] in wanted:
            if wanted[item["id"]] and not item.get("actionable"):
                raise ValueError("Blocked, covered, or equivalent items cannot be included.")
            item["included"] = wanted[item["id"]]
    plan.update({"updated_at": _now(), "updated_by": actor, "validated_at": ""})
    return _put(tenant_id, "plans", plan)


def validate_plan(
    tenant_id: str, plan_id: str, *, actor: str, persist: bool = True,
    live_action_groups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from app.alerts_manager import rules

    plan = get_plan(tenant_id, plan_id)
    if not plan:
        raise KeyError("Deployment plan not found.")
    if plan.get("status") != "draft":
        raise ValueError("Only draft deployment plans can be validated.")
    errors: list[dict[str, Any]] = []
    included = [item for item in plan.get("items") or [] if item.get("included")]
    if not included:
        errors.append({"item_id": "", "errors": ["Select at least one actionable item."]})
    target_ids: set[str] = set()
    for item in included:
        proposal = item.get("proposal") or {}
        desired = proposal.get("desired") or {}
        item_errors = rules.validate_rule_payload(str(proposal.get("family") or ""), desired, create=True)
        route_validation = validate_action_group_ids(list(desired.get("action_group_ids") or []), live_action_groups)
        item_errors.extend(route_validation["errors"])
        item_errors.extend(route_validation["warnings"])
        target_id = str(proposal.get("target_id") or "").lower()
        if target_id in target_ids:
            item_errors.append("Another included item has the same desired alert-rule resource ID.")
        target_ids.add(target_id)
        if desired.get("enabled") is not True:
            item_errors.append("Deployment-plan alert rules must be enabled by default.")
        if item_errors:
            errors.append({"item_id": item["id"], "errors": list(dict.fromkeys(item_errors))})
    valid = not errors
    if persist:
        plan["validated_at"] = _now() if valid else ""
        plan["updated_at"] = _now()
        plan["updated_by"] = actor
        _put(tenant_id, "plans", plan)
    return {"valid": valid, "included_count": len(included), "errors": errors}


def mark_plan_submitted(
    tenant_id: str, plan_id: str, *, actor: str, batch_id: str, change_ids: list[str],
) -> dict[str, Any]:
    plan = get_plan(tenant_id, plan_id)
    if not plan:
        raise KeyError("Deployment plan not found.")
    if plan.get("status") != "draft":
        raise ValueError("Only a draft deployment plan can be submitted.")
    plan.update({
        "status": "pending", "batch_id": batch_id, "change_ids": list(change_ids),
        "submitted_at": _now(), "submitted_by": actor, "updated_at": _now(), "updated_by": actor,
    })
    return _put(tenant_id, "plans", plan)


def mark_plan_decided(
    tenant_id: str, plan_id: str, *, actor: str, decision: str, reason: str,
    decision_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    plan = get_plan(tenant_id, plan_id)
    if not plan:
        raise KeyError("Deployment plan not found.")
    allowed_states = {"pending", "approved"} if decision == "rejected" else {"pending"}
    if plan.get("status") not in allowed_states:
        raise ValueError("Only a pending plan can be approved; a pending or approved plan can be cancelled.")
    plan.update({
        "status": decision, "decided_at": _now(), "decided_by": actor,
        "decision_reason": _text(reason, 1000), "decision_summary": decision_summary or {},
        "updated_at": _now(), "updated_by": actor,
    })
    return _put(tenant_id, "plans", plan)
