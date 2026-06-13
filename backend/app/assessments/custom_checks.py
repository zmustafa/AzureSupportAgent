"""Admin-authored custom assessment checks (JSON registry, no secrets).

Each custom check mirrors the shipped catalog shape (a Resource Graph KQL control that
flags violating resources) so the runner can treat shipped and custom checks uniformly.
Persisted under backend/.data/assessment_checks.json."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.assessments import catalog

_PATH = Path(__file__).resolve().parents[2] / ".data" / "assessment_checks.json"

DEFAULTS: dict[str, Any] = {
    "pillar": "security",
    "title": "",
    "description": "",
    "severity": "warning",
    "resource_types": [],
    "kql": "",
    "remediation": "",
    "remediation_command": "",
    "frameworks": {},  # {cis: [], nist: [], iso: []}
    "enabled": True,
    "created_by": "",
    "created_at": "",
    "updated_at": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    if _PATH.exists():
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"checks": {}}


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _merge(cid: str, raw: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(DEFAULTS))
    merged.update(raw)
    merged["id"] = cid
    merged["custom"] = True
    merged["weight"] = catalog.SEVERITY_WEIGHT.get(merged.get("severity", "warning"), 3)
    merged["resource_types"] = [t.lower() for t in (merged.get("resource_types") or [])]
    return merged


def list_custom_checks() -> list[dict[str, Any]]:
    data = _read()
    out = [_merge(cid, c) for cid, c in data.get("checks", {}).items()]
    out.sort(key=lambda c: (c["pillar"], c["title"].lower()))
    return out


def enabled_custom_checks(pillars: list[str]) -> list[dict[str, Any]]:
    want = {p.lower() for p in pillars}
    return [c for c in list_custom_checks() if c.get("enabled") and c["pillar"] in want]


def get_custom_check(check_id: str) -> dict[str, Any] | None:
    data = _read()
    raw = data.get("checks", {}).get(check_id)
    return _merge(check_id, raw) if raw is not None else None


def upsert_custom_check(check: dict[str, Any]) -> dict[str, Any]:
    data = _read()
    checks = data.setdefault("checks", {})
    cid = check.get("id") or ("custom_" + uuid.uuid4().hex[:10])
    existing = checks.get(cid, {})
    merged = dict(existing)
    for key in DEFAULTS:
        if key in check and check[key] is not None:
            merged[key] = check[key]
    if merged.get("pillar") not in catalog.PILLARS:
        merged["pillar"] = "security"
    if merged.get("severity") not in ("critical", "error", "warning", "info"):
        merged["severity"] = "warning"
    merged["created_at"] = existing.get("created_at") or _now()
    merged["updated_at"] = _now()
    merged.pop("id", None)
    merged.pop("custom", None)
    merged.pop("weight", None)
    checks[cid] = merged
    _write(data)
    result = get_custom_check(cid)
    assert result is not None
    return result


def delete_custom_check(check_id: str) -> bool:
    data = _read()
    if check_id in data.get("checks", {}):
        del data["checks"][check_id]
        _write(data)
        return True
    return False


# Sample custom controls shipped to demonstrate the feature across every pillar. Seeded
# once (by stable id) if absent; never overwritten, so user edits/deletes are respected.
_SAMPLE_CHECKS: dict[str, dict[str, Any]] = {
    "sample_sec_no_tls12": {
        "pillar": "security",
        "title": "Storage accounts not enforcing TLS 1.2 (sample)",
        "description": "Sample custom control: storage accounts whose minimum TLS version is "
        "below 1.2 accept weak, deprecated TLS connections.",
        "severity": "error",
        "resource_types": ["microsoft.storage/storageaccounts"],
        "kql": "| where type =~ 'microsoft.storage/storageaccounts' "
        "| where tostring(properties.minimumTlsVersion) !in~ ('TLS1_2', 'TLS1_3') "
        "| project id, name, type, resourceGroup, subscriptionId",
        "remediation": "Set the minimum TLS version to 1.2 (or later) on each storage account.",
        "remediation_command": "az storage account update --name <name> --resource-group <rg> --min-tls-version TLS1_2",
        "frameworks": {"nist": ["SC-8"], "cis": ["CIS Azure 3.15"]},
    },
    "sample_rel_no_locks": {
        "pillar": "reliability",
        "title": "Production resource groups without a delete lock (sample)",
        "description": "Sample custom control: resource groups tagged environment=prod that "
        "have no management lock are vulnerable to accidental deletion.",
        "severity": "warning",
        "resource_types": ["microsoft.resources/subscriptions/resourcegroups"],
        "kql": "| where type =~ 'microsoft.resources/subscriptions/resourcegroups' "
        "| where tostring(tags['environment']) =~ 'prod' "
        "| project id, name, type, resourceGroup, subscriptionId",
        "remediation": "Apply a CanNotDelete lock to production resource groups.",
        "remediation_command": "az lock create --name noDelete --lock-type CanNotDelete --resource-group <rg>",
        "frameworks": {"nist": ["CP-9"]},
    },
    "sample_cost_premium_ssd_unattached": {
        "pillar": "cost",
        "title": "Premium SSD disks that are unattached (sample)",
        "description": "Sample custom control: unattached Premium SSD managed disks are "
        "expensive idle capacity that should be reclaimed.",
        "severity": "warning",
        "resource_types": ["microsoft.compute/disks"],
        "kql": "| where type =~ 'microsoft.compute/disks' "
        "| where tostring(sku.name) startswith 'Premium' and tostring(properties.diskState) =~ 'Unattached' "
        "| project id, name, type, resourceGroup, subscriptionId",
        "remediation": "Delete or downgrade unattached Premium SSD disks.",
        "remediation_command": "az disk delete --name <name> --resource-group <rg> --yes",
        "frameworks": {},
    },
    "sample_ops_missing_costcenter_tag": {
        "pillar": "operations",
        "title": "Resources missing a 'CostCenter' tag (sample)",
        "description": "Sample custom control: resources without a CostCenter tag can't be "
        "charged back or governed by FinOps policy.",
        "severity": "info",
        "resource_types": [
            "microsoft.compute/virtualmachines",
            "microsoft.storage/storageaccounts",
            "microsoft.web/sites",
        ],
        "kql": "| where type in~ ('microsoft.compute/virtualmachines', "
        "'microsoft.storage/storageaccounts', 'microsoft.web/sites') "
        "| where isempty(tostring(tags['CostCenter'])) and isempty(tostring(tags['costcenter'])) "
        "| project id, name, type, resourceGroup, subscriptionId",
        "remediation": "Apply a 'CostCenter' tag and enforce it with Azure Policy.",
        "remediation_command": "",
        "frameworks": {"nist": ["CM-8"]},
    },
    "sample_perf_vm_low_sku": {
        "pillar": "performance",
        "title": "Virtual machines on Basic/A-series sizes (sample)",
        "description": "Sample custom control: VMs on legacy Basic or A-series sizes have "
        "low, non-burstable performance unsuitable for production.",
        "severity": "info",
        "resource_types": ["microsoft.compute/virtualmachines"],
        "kql": "| where type =~ 'microsoft.compute/virtualmachines' "
        "| extend vmSize = tostring(properties.hardwareProfile.vmSize) "
        "| where vmSize has 'Basic_' or vmSize matches regex 'Standard_A[0-9]' "
        "| project id, name, type, resourceGroup, subscriptionId",
        "remediation": "Resize VMs to a current general-purpose or compute-optimized series.",
        "remediation_command": "az vm resize --name <name> --resource-group <rg> --size Standard_D2s_v5",
        "frameworks": {},
    },
}


def seed_sample_checks() -> int:
    """Insert the sample custom controls (by stable id) if they don't already exist.

    Returns the number of samples newly added. Existing checks (including user edits or
    deletions of a sample) are never overwritten."""
    data = _read()
    checks = data.setdefault("checks", {})
    added = 0
    now = _now()
    for cid, sample in _SAMPLE_CHECKS.items():
        if cid in checks:
            continue
        entry = json.loads(json.dumps(DEFAULTS))
        entry.update(sample)
        entry["enabled"] = True
        entry["created_by"] = "system:sample"
        entry["created_at"] = now
        entry["updated_at"] = now
        checks[cid] = entry
        added += 1
    if added:
        _write(data)
    return added

