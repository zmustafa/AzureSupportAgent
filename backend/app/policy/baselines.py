"""Curated policy *baselines* for coverage-gap analysis.

Each baseline is a short list of governance controls (a name, the Azure domain, the
expected effect, and matching hints) that a well-governed tenant typically enforces.
The coverage advisor compares a tenant's live assignments against a chosen baseline and
reports which controls look covered vs missing. These are intentionally lightweight,
human-readable control sets (not full ARM policy JSON) — the AI proposes concrete
built-in policies to close any gap.
"""
from __future__ import annotations

from typing import Any

# A control: id, title, domain, recommended effect, and keyword hints used to fuzzy-match
# existing assignments/definitions by display name or category.
_BASELINES: dict[str, dict[str, Any]] = {
    "waf": {
        "label": "Well-Architected (security baseline)",
        "description": "Core guardrails every workload should have, aligned to the WAF security & operational pillars.",
        "controls": [
            {"id": "encrypt-storage", "title": "Storage accounts require secure transfer (HTTPS)", "domain": "Storage", "effect": "deny", "hints": ["secure transfer", "https", "storage"]},
            {"id": "no-public-storage", "title": "Storage accounts should disallow public blob access", "domain": "Storage", "effect": "deny", "hints": ["public blob", "public access", "storage"]},
            {"id": "kv-soft-delete", "title": "Key Vaults should have soft delete & purge protection", "domain": "Key Vault", "effect": "audit", "hints": ["key vault", "soft delete", "purge"]},
            {"id": "sql-tde", "title": "SQL databases should have transparent data encryption", "domain": "Data", "effect": "auditIfNotExists", "hints": ["transparent data encryption", "tde", "sql"]},
            {"id": "vm-managed-disk", "title": "VMs should use managed disks", "domain": "Compute", "effect": "audit", "hints": ["managed disk", "virtual machine"]},
            {"id": "diag-settings", "title": "Resources should ship diagnostics to Log Analytics", "domain": "Monitoring", "effect": "deployIfNotExists", "hints": ["diagnostic settings", "log analytics", "monitoring"]},
            {"id": "allowed-locations", "title": "Restrict allowed deployment locations", "domain": "General", "effect": "deny", "hints": ["allowed locations", "region"]},
            {"id": "require-tags", "title": "Require an owner / cost-center tag on resources", "domain": "Governance", "effect": "deny", "hints": ["require a tag", "tag on resources", "tagging"]},
        ],
    },
    "mcsb": {
        "label": "Microsoft Cloud Security Benchmark",
        "description": "Security-first controls aligned to MCSB network, identity, data-protection and logging domains.",
        "controls": [
            {"id": "nsg-on-subnets", "title": "Subnets should be associated with a Network Security Group", "domain": "Network", "effect": "audit", "hints": ["network security group", "subnet", "nsg"]},
            {"id": "no-public-ip-vm", "title": "VMs should not have public IPs directly attached", "domain": "Network", "effect": "audit", "hints": ["public ip", "virtual machine"]},
            {"id": "private-endpoints", "title": "PaaS data services should use private endpoints", "domain": "Network", "effect": "audit", "hints": ["private endpoint", "private link"]},
            {"id": "mfa-admins", "title": "Accounts with owner/write perms should have MFA", "domain": "Identity", "effect": "auditIfNotExists", "hints": ["mfa", "multi-factor", "owner permissions"]},
            {"id": "defender-on", "title": "Microsoft Defender for Cloud plans should be enabled", "domain": "Security", "effect": "auditIfNotExists", "hints": ["defender", "security center"]},
            {"id": "no-storage-key-access", "title": "Storage accounts should disable shared key access", "domain": "Identity", "effect": "audit", "hints": ["shared key", "storage", "azure ad"]},
            {"id": "kv-firewall", "title": "Key Vault should disable public network access", "domain": "Network", "effect": "audit", "hints": ["key vault", "public network", "firewall"]},
            {"id": "activity-log-retention", "title": "Activity log should be retained / exported", "domain": "Logging", "effect": "auditIfNotExists", "hints": ["activity log", "retention", "log"]},
        ],
    },
    "cis": {
        "label": "CIS Microsoft Azure Foundations",
        "description": "Foundational hardening controls from the CIS Azure benchmark.",
        "controls": [
            {"id": "cis-secure-transfer", "title": "Ensure 'Secure transfer required' is enabled on storage", "domain": "Storage", "effect": "deny", "hints": ["secure transfer", "storage"]},
            {"id": "cis-sql-auditing", "title": "Ensure auditing is enabled on SQL servers", "domain": "Data", "effect": "auditIfNotExists", "hints": ["sql", "auditing"]},
            {"id": "cis-disk-encryption", "title": "Ensure VM disks are encrypted", "domain": "Compute", "effect": "auditIfNotExists", "hints": ["disk encryption", "virtual machine"]},
            {"id": "cis-nsg-flow-logs", "title": "Ensure NSG flow logs are enabled", "domain": "Network", "effect": "auditIfNotExists", "hints": ["flow log", "network security group"]},
            {"id": "cis-kv-logging", "title": "Ensure logging for Key Vault is enabled", "domain": "Key Vault", "effect": "auditIfNotExists", "hints": ["key vault", "logging", "diagnostic"]},
            {"id": "cis-no-rdp-internet", "title": "Ensure RDP/SSH is not open to the internet", "domain": "Network", "effect": "deny", "hints": ["rdp", "ssh", "internet", "management ports"]},
            {"id": "cis-defender-storage", "title": "Ensure Defender for Storage is on", "domain": "Security", "effect": "auditIfNotExists", "hints": ["defender", "storage"]},
        ],
    },
}


def list_baselines() -> list[dict[str, Any]]:
    return [
        {"id": bid, "label": b["label"], "description": b["description"], "control_count": len(b["controls"])}
        for bid, b in _BASELINES.items()
    ]


def get_baseline(baseline_id: str) -> dict[str, Any] | None:
    b = _BASELINES.get(baseline_id)
    if not b:
        return None
    return {"id": baseline_id, **b}


def _matches(control: dict[str, Any], haystacks: list[str]) -> bool:
    hints = [h.lower() for h in control.get("hints", [])]
    blob = " ".join(haystacks).lower()
    # Covered if at least two distinct hint tokens appear (reduces false positives).
    hit = sum(1 for h in hints if h in blob)
    return hit >= 2 or (len(hints) <= 2 and hit >= 1)


def coverage(baseline_id: str, assignments: list[dict[str, Any]], definitions: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare a baseline's controls against live assignments. Deterministic first pass;
    the AI advisor enriches the ``missing`` set with concrete policy proposals."""
    b = _BASELINES.get(baseline_id)
    if not b:
        return {"error": "Unknown baseline."}
    # Build the searchable text per assignment (its display name, definition name, category).
    asg_blobs = [
        " ".join(filter(None, [a.get("display_name", ""), a.get("definition_name", ""), a.get("category", "")]))
        for a in assignments
    ]
    covered, missing = [], []
    for c in b["controls"]:
        if _matches(c, asg_blobs):
            covered.append(c)
        else:
            missing.append(c)
    total = len(b["controls"])
    return {
        "baseline_id": baseline_id,
        "baseline_label": b["label"],
        "total": total,
        "covered": covered,
        "missing": missing,
        "covered_count": len(covered),
        "missing_count": len(missing),
        "coverage_pct": round(100 * len(covered) / total) if total else 0,
    }
