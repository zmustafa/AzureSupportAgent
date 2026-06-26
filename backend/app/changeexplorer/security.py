"""Security intelligence for the Change Explorer (features C1/C2/C3).

Turns a normalized change stream into security signal:

- ``flag_event``        — C1: per-change security-sensitive flags (public exposure, RBAC grant,
                          secret/key access, logging disabled, lock removal, exemption created…).
- ``rollback_hint``     — C3: a READ-ONLY ``az`` command an operator could run to revert/inspect a
                          risky change (copy-only — never executed).
- ``suspicious_patterns`` — C2: run-level heuristics (off-hours, first-time actor in scope,
                          mass-delete, disable-logging-then-change, privilege escalation).

Everything is deterministic and best-effort: it reads the fields the normalizer already produced
(category, operation, details, actor, eventTime) and never calls Azure.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

# Severity ranks for ordering / rollups.
_SEV_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}


def _paths(event: dict[str, Any]) -> list[str]:
    return [str(d.get("propertyPath", "")).lower() for d in (event.get("details") or [])]


def _detail_values(event: dict[str, Any]) -> str:
    """All before/after values of a change, lowercased + concatenated, for substring checks."""
    parts: list[str] = []
    for d in event.get("details") or []:
        parts.append(str(d.get("beforeValue", "")))
        parts.append(str(d.get("afterValue", "")))
    return " ".join(parts).lower()


def _op(event: dict[str, Any]) -> str:
    return str(event.get("operation", "")).lower()


def _is_delete(event: dict[str, Any]) -> bool:
    op = _op(event)
    return "delete" in op or (event.get("changeType", "") or "").lower() == "delete"


# --------------------------------------------------------------------------- C1: per-change flags
def flag_event(event: dict[str, Any]) -> list[dict[str, str]]:
    """Return the list of security flags for one change. Each flag = {code, label, severity}.

    Empty when the change carries no notable security signal."""
    flags: list[dict[str, str]] = []
    cat = event.get("category", "")
    op = _op(event)
    paths = _paths(event)
    vals = _detail_values(event)
    rtype = str(event.get("resourceType", "")).lower()

    def add(code: str, label: str, severity: str) -> None:
        flags.append({"code": code, "label": label, "severity": severity})

    # Public network exposure — an NSG rule / firewall opened to the Internet, or a new public IP.
    opened_internet = any(t in vals for t in ("0.0.0.0/0", "internet", "/0")) and any(
        p for p in paths if "sourceaddressprefix" in p or "securityrule" in p or "addressprefix" in p)
    if opened_internet:
        add("public_exposure", "Opened to the Internet (0.0.0.0/0 / Internet)", "critical")
    if "publicipaddresses" in rtype and not _is_delete(event):
        add("public_ip", "Public IP address created/modified", "high")
    if any("publicnetworkaccess" in p for p in paths) and ("enabled" in vals):
        add("public_network_access", "Public network access enabled", "high")

    # RBAC / privilege grant.
    if cat in ("RBAC", "PIM") and not _is_delete(event):
        add("rbac_grant", "Role assignment / privileged access granted", "high")
    if cat == "RBAC" and "owner" in vals:
        add("owner_grant", "Owner / privileged role involved", "critical")

    # Secret / key / certificate access or change.
    if cat in ("Secret", "Certificate", "KeyVault"):
        if "listsecret" in op or "getsecret" in op or "/secrets/" in op or "listkeys" in op:
            add("secret_access", "Secret / key material accessed", "high")
        elif not _is_delete(event):
            add("secret_change", "Secret / key / certificate changed", "medium")
    if any(t in op for t in ("listkeys", "listaccountsas", "listservicesas")):
        add("key_listing", "Account keys / SAS listed", "high")

    # Logging / diagnostics disabled — a classic "cover your tracks" move.
    if cat == "Monitoring" or "diagnosticsetting" in rtype or any("diagnosticsetting" in p for p in paths):
        if _is_delete(event):
            add("logging_disabled", "Diagnostic / logging setting deleted", "critical")
        elif any("logs" in p or "enabled" in p for p in paths) and ("false" in vals):
            add("logging_disabled", "Logging disabled", "high")

    # Resource lock removed.
    if "/locks" in rtype or "authorization/locks" in rtype:
        if _is_delete(event):
            add("lock_removed", "Resource lock removed", "high")

    # Policy exemption created — waives a guardrail.
    if "policyexemption" in rtype and not _is_delete(event):
        add("policy_exemption", "Policy exemption created", "high")
    if cat == "Policy" and _is_delete(event):
        add("policy_deleted", "Policy assignment / definition deleted", "high")

    # Firewall / NSG / network security weakened by deletion.
    if cat == "Network" and _is_delete(event) and any(
            t in rtype for t in ("networksecuritygroup", "firewall", "securityrule")):
        add("security_control_deleted", "Network security control deleted", "high")

    return flags


def highest_flag_severity(flags: list[dict[str, str]]) -> str:
    if not flags:
        return ""
    return max(flags, key=lambda f: _SEV_RANK.get(f.get("severity", "low"), 0)).get("severity", "")


# --------------------------------------------------------------------------- C3: rollback hints
def rollback_hint(event: dict[str, Any]) -> str:
    """A READ-ONLY az command to inspect/revert a risky change (copy-only; never executed).

    For deletes we can't reconstruct the resource, so we suggest restore/inspection paths; for
    writes we suggest showing the current state so the operator can compare + revert by hand."""
    rid = event.get("resourceId", "")
    cat = event.get("category", "")
    if not rid:
        return ""
    if _is_delete(event):
        if cat in ("RBAC", "PIM"):
            return f'# Re-create the role assignment if the deletion was unintended:\naz role assignment list --scope "{rid.rsplit("/providers/",1)[0]}" -o table'
        return f'# Resource was DELETED — inspect activity log / restore from backup:\naz monitor activity-log list --resource-id "{rid}" --offset 7d -o table'
    if cat == "Network" and any("securityrule" in str(d.get("propertyPath", "")).lower() for d in event.get("details") or []):
        return f'# Review the NSG rules and revert the rule by hand if unintended:\naz network nsg show --ids "{rid}" --query "securityRules" -o table'
    if cat in ("Secret", "Certificate", "KeyVault"):
        return f'# Review Key Vault access + rotate if needed:\naz keyvault show --ids "{rid}"'
    if cat == "RBAC":
        return f'# Review who has access at this scope and remove the grant if unintended:\naz role assignment list --scope "{rid}" -o table'
    return f'# Show the resource\'s current state to compare against the change and revert by hand:\naz resource show --ids "{rid}"'


# --------------------------------------------------------------------------- C2: suspicious patterns
def _parse_ts(iso: str) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _off_hours(dt: datetime) -> bool:
    # Off-hours = weekend, or before 07:00 / after 19:00 (local-naive heuristic on the event ts).
    if dt.weekday() >= 5:
        return True
    return dt.hour < 7 or dt.hour >= 19


def suspicious_patterns(events: list[dict[str, Any]],
                        prior_actor_resource_types: set[tuple[str, str]] | None = None) -> list[dict[str, Any]]:
    """Run-level suspicious-pattern detections. Returns ChangeInsight-shaped dicts (without ids;
    the caller stamps run id / makes them insights). ``prior_actor_resource_types`` (from earlier
    runs) enables 'first-time actor for this resource type' — optional.

    Each detection: {patternType, title, summary, severity, relatedChangeIds}.
    """
    out: list[dict[str, Any]] = []
    if not events:
        return out

    def _actor_label(e: dict[str, Any]) -> str:
        return e.get("actorDisplay") or e.get("actor", "") or "unknown"

    # --- Off-hours changes (only flag the risky ones to avoid noise).
    off = [e for e in events
           if (dt := _parse_ts(e.get("eventTime", ""))) and _off_hours(dt)
           and e.get("riskLabel") in ("Critical", "High")]
    if off:
        actors = sorted({_actor_label(e) for e in off})
        out.append({
            "patternType": "off_hours", "severity": "Medium",
            "title": f"{len(off)} high-risk change(s) made outside business hours",
            "summary": ("Changes occurred on a weekend or outside 07:00–19:00 — worth confirming they "
                        f"were planned. Actor(s): {', '.join(actors[:5])}."),
            "relatedChangeIds": [e.get("changeId", "") for e in off[:25]],
        })

    # --- Mass delete by a single actor.
    dels_by_actor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        if _is_delete(e):
            dels_by_actor[_actor_label(e)].append(e)
    for actor, dels in dels_by_actor.items():
        if len(dels) >= 5:
            out.append({
                "patternType": "mass_delete", "severity": "High",
                "title": f"{actor} deleted {len(dels)} resource(s)",
                "summary": ("A burst of deletions by one actor — confirm this was an intended teardown "
                            "and not accidental or malicious."),
                "relatedChangeIds": [e.get("changeId", "") for e in dels[:25]],
            })

    # --- Disable-logging-then-change: a logging/diagnostic disable followed by other changes by
    #     the same actor within the window (classic evasion).
    log_disables = [e for e in events
                    if any(f["code"] == "logging_disabled" for f in (e.get("securityFlags") or []))]
    for ld in log_disables:
        actor = _actor_label(ld)
        ld_ts = _parse_ts(ld.get("eventTime", ""))
        if not ld_ts:
            continue
        after = [e for e in events
                 if _actor_label(e) == actor and e.get("changeId") != ld.get("changeId")
                 and (ts := _parse_ts(e.get("eventTime", ""))) and ts >= ld_ts]
        if after:
            out.append({
                "patternType": "disable_logging_then_change", "severity": "Critical",
                "title": f"{actor} disabled logging, then made {len(after)} further change(s)",
                "summary": ("Logging/diagnostics was turned off and more changes followed — a common "
                            "track-covering pattern. Review urgently."),
                "relatedChangeIds": [ld.get("changeId", "")] + [e.get("changeId", "") for e in after[:24]],
            })

    # --- Privilege escalation: an Owner / privileged RBAC grant.
    esc = [e for e in events if any(f["code"] in ("owner_grant", "rbac_grant") for f in (e.get("securityFlags") or []))]
    if esc:
        out.append({
            "patternType": "privilege_escalation", "severity": "High",
            "title": f"{len(esc)} privilege grant(s) detected",
            "summary": ("Role assignments / privileged access were granted in this window — verify the "
                        "grantees and that the elevation was approved."),
            "relatedChangeIds": [e.get("changeId", "") for e in esc[:25]],
        })

    # --- First-time actor for a resource type (needs prior-run history).
    if prior_actor_resource_types is not None:
        seen_now: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for e in events:
            key = (_actor_label(e), str(e.get("resourceType", "")).lower())
            if key[1]:
                seen_now[key].append(e)
        firsts = [(k, evs) for k, evs in seen_now.items()
                  if k not in prior_actor_resource_types and any(x.get("riskLabel") in ("Critical", "High") for x in evs)]
        if firsts:
            related: list[str] = []
            for _k, evs in firsts:
                related += [e.get("changeId", "") for e in evs]
            out.append({
                "patternType": "first_time_actor", "severity": "Medium",
                "title": f"{len(firsts)} actor/resource-type combination(s) seen for the first time",
                "summary": ("An actor touched a resource type they haven't changed in prior runs — unusual "
                            "access worth a glance."),
                "relatedChangeIds": related[:25],
            })

    return out


def summarize_security(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Security rollup for the Security tab header: counts by flag code + by severity."""
    by_code: dict[str, int] = defaultdict(int)
    by_sev: dict[str, int] = defaultdict(int)
    flagged = 0
    for e in events:
        fl = e.get("securityFlags") or []
        if fl:
            flagged += 1
        for f in fl:
            by_code[f.get("code", "")] += 1
            by_sev[f.get("severity", "")] += 1
    return {
        "flagged_changes": flagged,
        "by_code": dict(by_code),
        "by_severity": dict(by_sev),
    }
