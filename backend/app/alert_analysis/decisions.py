"""Tenant-scoped human decisions for Alerts Manager findings.

Decisions never mutate Azure. They capture operator intent (keep, exempt, consolidate) so
future scans stop repeatedly recommending work that has already been reviewed.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "alert_analysis_decisions.json"
_ACTIONS = {"keep_rule", "exempt_rule", "consolidate_to", "dismiss_finding"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    if not _PATH.exists():
        return {}
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(_PATH)


def _bucket(tenant_id: str, connection_id: str) -> str:
    return f"{tenant_id or 'default'}::{connection_id or 'default'}"


def list_decisions(tenant_id: str, connection_id: str) -> list[dict[str, Any]]:
    values = _read().get(_bucket(tenant_id, connection_id), {})
    out = list(values.values()) if isinstance(values, dict) else []
    out.sort(key=lambda item: item.get("decided_at", ""), reverse=True)
    return out


def record_decision(
    tenant_id: str,
    connection_id: str,
    *,
    target_type: str,
    target_id: str,
    action: str,
    actor: str,
    reason: str = "",
    consolidate_to: str = "",
) -> dict[str, Any]:
    if action not in _ACTIONS:
        raise ValueError("Unsupported alert decision action.")
    if target_type not in {"rule", "overlap", "gap"} or not target_id:
        raise ValueError("A valid decision target is required.")
    key = f"{target_type}:{target_id}"
    data = _read()
    bucket = data.setdefault(_bucket(tenant_id, connection_id), {})
    item = {
        "id": key,
        "target_type": target_type,
        "target_id": target_id,
        "action": action,
        "consolidate_to": consolidate_to,
        "reason": reason[:1000],
        "decided_by": actor,
        "decided_at": _now(),
    }
    bucket[key] = item
    _write(data)
    return item


def delete_decision(tenant_id: str, connection_id: str, target_type: str, target_id: str) -> bool:
    data = _read()
    bucket = data.get(_bucket(tenant_id, connection_id), {})
    key = f"{target_type}:{target_id}"
    if key not in bucket:
        return False
    del bucket[key]
    _write(data)
    return True


def apply_decisions(snapshot: dict[str, Any], values: list[dict[str, Any]]) -> dict[str, Any]:
    """Decorate a response copy and exclude accepted findings from actionable KPI counts."""
    result = json.loads(json.dumps(snapshot))
    by_key = {item["id"]: item for item in values}
    rule_decisions = {
        key.removeprefix("rule:"): value for key, value in by_key.items() if key.startswith("rule:")
    }
    overlap_decisions = {
        key.removeprefix("overlap:"): value for key, value in by_key.items() if key.startswith("overlap:")
    }
    gap_decisions = {
        key.removeprefix("gap:"): value for key, value in by_key.items() if key.startswith("gap:")
    }

    for rule in result.get("rules", []):
        decision = rule_decisions.get(rule.get("id", ""))
        rule["decision"] = decision
        if decision and decision["action"] in {"keep_rule", "exempt_rule"}:
            rule["finding_status"] = "accepted"

    active_overlaps: list[dict[str, Any]] = []
    for overlap in result.get("overlaps", []):
        decision = overlap_decisions.get(overlap.get("id", ""))
        rule_decision = next(
            (rule_decisions.get(rule_id) for rule_id in overlap.get("rule_ids", []) if rule_decisions.get(rule_id)),
            None,
        )
        overlap["decision"] = decision or rule_decision
        if overlap["decision"] and overlap["decision"]["action"] in {"keep_rule", "exempt_rule", "dismiss_finding"}:
            overlap["accepted"] = True
        else:
            active_overlaps.append(overlap)

    active_gaps: list[dict[str, Any]] = []
    for index, gap in enumerate(result.get("gaps", [])):
        gap_key = f"{gap.get('type','')}:{gap.get('rule_id') or gap.get('resource_id') or gap.get('action_group_id') or index}"
        decision = gap_decisions.get(gap_key) or rule_decisions.get(gap.get("rule_id", ""))
        gap["decision"] = decision
        gap["decision_key"] = gap_key
        if decision and decision["action"] in {"exempt_rule", "dismiss_finding", "keep_rule"}:
            gap["accepted"] = True
        else:
            active_gaps.append(gap)

    result["decisions"] = values
    result["active_overlaps"] = active_overlaps
    result["active_gaps"] = active_gaps
    result.setdefault("kpis", {})["accepted_findings"] = (
        len(result.get("overlaps", [])) - len(active_overlaps)
        + len(result.get("gaps", [])) - len(active_gaps)
    )
    result["kpis"]["actionable_overlap_groups"] = len(active_overlaps)
    result["kpis"]["actionable_gap_count"] = len(active_gaps)
    return result
