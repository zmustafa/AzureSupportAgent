"""Vault security posture — the ransomware / recoverability readiness scorecard.

Backup data is the last line of defense, so the questions that matter are not "is backup on"
but "can an attacker or a mistake destroy the backups, and can we still restore if the region
goes away".  Each control from the editable reference is evaluated per vault into
pass / warn / fail / n-a, weighted into a 0-100 score, and rolled up across the fleet.

Controls flagged ``portal_only`` in the reference are evaluated and reported but never offered
as an action — they are irreversible (immutability lock, CMK) or change who can operate the
vault (Resource Guard).
"""
from __future__ import annotations

from typing import Any

from app.backup_manager import reference

PASS, WARN, FAIL, NA = "pass", "warn", "fail", "na"
_RANK = {FAIL: 0, WARN: 1, PASS: 2, NA: 3}

_SOFT_DELETE_ON = {"on", "enabled", "alwayson"}
_SOFT_DELETE_ALWAYS = {"alwayson"}
_GEO = {"georedundant", "geo-redundant", "geo"}
_ZONE = {"zoneredundant", "zone-redundant", "zone"}
_LOCAL = {"locallyredundant", "locally-redundant", "local"}


def _token(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "").replace("_", "")


def _cell(status: str, value: str, detail: str = "", *, action: str = "") -> dict[str, Any]:
    return {"status": status, "value": value, "detail": detail, "action": action}


def _evaluate(check_id: str, vault: dict[str, Any], min_retention: int) -> dict[str, Any]:
    kind = vault.get("kind", "recovery_services")
    if check_id == "soft_delete":
        state = _token(vault.get("soft_delete_state"))
        if state in _SOFT_DELETE_ALWAYS:
            return _cell(PASS, "Always-on", "Soft delete cannot be turned off for this vault.")
        if state in _SOFT_DELETE_ON:
            return _cell(PASS, "Enabled")
        if not state:
            return _cell(NA, "Unknown", "Soft-delete state could not be read for this vault.")
        return _cell(FAIL, "Disabled", "Backup data can be permanently deleted without a recovery window.", action="enable_soft_delete")

    if check_id == "soft_delete_retention":
        state = _token(vault.get("soft_delete_state"))
        if state not in _SOFT_DELETE_ON and state not in _SOFT_DELETE_ALWAYS:
            return _cell(NA, "—", "Soft delete is off, so retention does not apply.")
        days = vault.get("soft_delete_retention_days")
        try:
            days_int = int(days) if days is not None else 0
        except (TypeError, ValueError):
            days_int = 0
        if not days_int:
            return _cell(NA, "Default", "Retention period not reported; the service default applies.")
        if days_int >= min_retention:
            return _cell(PASS, f"{days_int}d")
        return _cell(WARN, f"{days_int}d", f"Below the {min_retention}-day recommended undelete window.", action="extend_soft_delete_retention")

    if check_id == "immutability":
        state = _token(vault.get("immutability_state"))
        if state == "locked":
            return _cell(PASS, "Locked", "Retention cannot be shortened and recovery points cannot be deleted early.")
        if state == "unlocked":
            return _cell(WARN, "Unlocked", "Immutability is configured but still reversible.")
        if not state or state in ("disabled", "none"):
            return _cell(FAIL, "Disabled", "Recovery points can be deleted early by anyone with vault write access.")
        return _cell(NA, state or "Unknown")

    if check_id == "mua":
        enabled = vault.get("mua_enabled")
        if enabled is None:
            return _cell(NA, "Unknown", "Resource Guard association could not be read.")
        if enabled:
            return _cell(PASS, "Resource Guard attached")
        return _cell(FAIL, "None", "A single compromised identity can perform destructive vault operations.")

    if check_id == "redundancy":
        token = _token(vault.get("redundancy"))
        if token in _GEO:
            return _cell(PASS, "Geo-redundant")
        if token in _ZONE:
            return _cell(PASS, "Zone-redundant")
        if token in _LOCAL:
            detail = "Backup data does not survive a zone or regional failure."
            if vault.get("instance_count"):
                detail += " Redundancy is locked once the first item is protected."
            return _cell(FAIL, "Locally redundant", detail, action="" if vault.get("instance_count") else "set_redundancy")
        return _cell(NA, vault.get("redundancy") or "Unknown")

    if check_id == "cross_region_restore":
        token = _token(vault.get("redundancy"))
        if token not in _GEO:
            return _cell(NA, "—", "Cross Region Restore requires geo-redundant backup storage.")
        crr = _token(vault.get("cross_region_restore"))
        if crr in ("enabled", "true"):
            return _cell(PASS, "Enabled")
        return _cell(WARN, "Disabled", "Geo-redundant data cannot be restored until the primary region returns.", action="enable_crr")

    if check_id == "cmk":
        return _cell(PASS, "Customer-managed") if vault.get("cmk") else _cell(WARN, "Platform-managed")

    if check_id == "private_endpoint":
        access = _token(vault.get("public_network_access"))
        endpoints = int(vault.get("private_endpoints") or 0)
        if endpoints and access in ("disabled", "securedbyperimeter"):
            return _cell(PASS, f"{endpoints} private endpoint(s), public access off")
        if endpoints:
            return _cell(WARN, f"{endpoints} private endpoint(s)", "Public network access is still permitted.")
        return _cell(WARN, "Public", "Backup traffic traverses public endpoints.")

    if check_id == "monitor_alerts":
        token = _token(vault.get("monitor_alerts"))
        if token in ("enabled", "true"):
            return _cell(PASS, "Enabled")
        if not token:
            return _cell(NA, "Unknown", "Alert configuration could not be read for this vault.")
        return _cell(FAIL, "Disabled", "A silent backup failure can go unnoticed indefinitely.", action="enable_vault_alerts")

    if check_id == "diagnostics":
        enabled = vault.get("diagnostics_enabled")
        if enabled is None:
            return _cell(NA, "Unknown")
        if enabled:
            return _cell(PASS, f"{len(vault.get('diagnostics_workspaces') or [])} workspace(s)")
        return _cell(WARN, "Not configured", "Long-horizon job history, storage consumption, and cost reporting are unavailable.", action="enable_diagnostics")

    return _cell(NA, "—", f"Unknown check for a {kind} vault.")


def score_vault(vault: dict[str, Any], *, checks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Evaluate every configured control against one vault and return a weighted score."""
    catalog = checks if checks is not None else reference.vault_checks()
    min_retention = 14
    cells: list[dict[str, Any]] = []
    earned = 0
    possible = 0
    for check in catalog:
        result = _evaluate(str(check.get("id")), vault, min_retention)
        weight = int(check.get("weight") or 0)
        if result["status"] != NA:
            possible += weight
            earned += weight if result["status"] == PASS else (weight // 2 if result["status"] == WARN else 0)
        cells.append({
            "id": check.get("id"),
            "label": check.get("label"),
            "severity": check.get("severity", "info"),
            "why": check.get("why", ""),
            "weight": weight,
            "portal_only": bool(check.get("portal_only")),
            "portal_reason": str(check.get("portal_reason") or ""),
            **result,
        })
    score = round(100 * earned / possible) if possible else 100
    worst = min((_RANK.get(c["status"], 3) for c in cells), default=3)
    return {
        "vault_id": vault.get("id", ""),
        "vault_name": vault.get("name", ""),
        "vault_kind": vault.get("kind", ""),
        "subscription_id": vault.get("subscription_id", ""),
        "resource_group": vault.get("resource_group", ""),
        "location": vault.get("location", ""),
        "instance_count": int(vault.get("instance_count") or 0),
        "replicated_item_count": int(vault.get("replicated_item_count") or 0),
        "score": score,
        "band": "green" if score >= 80 else ("amber" if score >= 50 else "red"),
        "status": {0: FAIL, 1: WARN, 2: PASS, 3: NA}.get(worst, NA),
        "checks": cells,
        "failing": [c["id"] for c in cells if c["status"] == FAIL],
        "warning": [c["id"] for c in cells if c["status"] == WARN],
        # The concrete hardening actions available on this vault (the values the harden API
        # accepts), not the check ids — portal-only controls are excluded by construction.
        "actionable": sorted({c["action"] for c in cells if c.get("action") and not c.get("portal_only")}),
        "portal_only_gaps": [
            {"id": c["id"], "label": c["label"], "reason": c["portal_reason"], "value": c["value"]}
            for c in cells if c.get("portal_only") and c["status"] in (FAIL, WARN)
        ],
    }


def build_posture(vaults: list[dict[str, Any]]) -> dict[str, Any]:
    """Fleet posture: per-vault scorecards plus the aggregate rollup."""
    catalog = reference.vault_checks()
    scored = [score_vault(v, checks=catalog) for v in vaults]
    scored.sort(key=lambda v: (v["score"], v["vault_name"].lower()))
    total = len(scored)
    average = round(sum(v["score"] for v in scored) / total) if total else 100

    by_check: list[dict[str, Any]] = []
    for check in catalog:
        check_id = str(check.get("id"))
        statuses = [next((c for c in v["checks"] if c["id"] == check_id), None) for v in scored]
        present = [s for s in statuses if s]
        by_check.append({
            "id": check_id,
            "label": check.get("label"),
            "severity": check.get("severity", "info"),
            "portal_only": bool(check.get("portal_only")),
            "portal_reason": str(check.get("portal_reason") or ""),
            "pass": sum(1 for s in present if s["status"] == PASS),
            "warn": sum(1 for s in present if s["status"] == WARN),
            "fail": sum(1 for s in present if s["status"] == FAIL),
            "na": sum(1 for s in present if s["status"] == NA),
        })

    return {
        "generated_at": vaults[0].get("_generated_at") if vaults and vaults[0].get("_generated_at") else None,
        "vault_count": total,
        "average_score": average,
        "band": "green" if average >= 80 else ("amber" if average >= 50 else "red"),
        "red_vaults": sum(1 for v in scored if v["band"] == "red"),
        "amber_vaults": sum(1 for v in scored if v["band"] == "amber"),
        "green_vaults": sum(1 for v in scored if v["band"] == "green"),
        "vaults": scored,
        "by_check": by_check,
        "actionable_count": sum(len(v["actionable"]) for v in scored),
    }


def capacity(vaults: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-vault headroom against published service limits — the ceiling nobody watches until
    a backup enrolment suddenly fails."""
    limits = reference.limits()
    warn_pct = int(limits.get("warn_at_pct", 80))
    out: list[dict[str, Any]] = []
    for vault in vaults:
        if vault.get("kind") == "backup":
            item_limit = int(limits.get("backup_vault_instances_per_vault", 5000))
        else:
            item_limit = int(limits.get("rsv_protected_items_per_vault", 2000))
        used = int(vault.get("instance_count") or 0)
        pct = round(100 * used / item_limit) if item_limit else 0
        policy_limit = int(limits.get("rsv_policies_per_vault", 200))
        policies_used = int(vault.get("policy_count") or 0)
        policy_pct = round(100 * policies_used / policy_limit) if policy_limit else 0
        out.append({
            "vault_id": vault.get("id", ""),
            "vault_name": vault.get("name", ""),
            "vault_kind": vault.get("kind", ""),
            "subscription_id": vault.get("subscription_id", ""),
            "instances": used,
            "instance_limit": item_limit,
            "instance_pct": pct,
            "policies": policies_used,
            "policy_limit": policy_limit,
            "policy_pct": policy_pct,
            "at_risk": pct >= warn_pct or policy_pct >= warn_pct,
        })
    out.sort(key=lambda v: -v["instance_pct"])
    return out
