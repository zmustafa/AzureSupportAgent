"""Build the Identity posture snapshot from Microsoft Graph (EntraID MCP) and Azure
Resource Graph.

Five signal groups, each normalised to a common *finding* shape so the dashboard and the
ticket/investigate handoffs can treat them uniformly:

    expiring_credentials  — app/SP client secrets & certs near or past expiry  (cap. 2)
    ownerless_apps        — app registrations with no assigned owner            (cap. 2)
    ca_gaps               — disabled / report-only conditional-access policies  (cap. 2)
    users_without_mfa     — privileged users with MFA not enabled (sampled)     (cap. 2)
    keyvault_expiry       — Key Vault certificate/secret expiry (best-effort)   (Resource Graph)

Every collector is defensive: a permission/parse failure for one group is captured in the
snapshot's ``errors`` map and never sinks the others (stale-while-error)."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("app.identity.collector")

# Finding kinds (stable identifiers used by the frontend + ticket text).
KIND_SECRET = "secret_expiry"
KIND_CERT = "cert_expiry"
KIND_OWNERLESS = "ownerless_app"
KIND_CA_GAP = "ca_gap"
KIND_NO_MFA = "user_no_mfa"
KIND_KV_EXPIRY = "keyvault_expiry"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tool_result_json(result: dict[str, Any]) -> Any:
    """Parse the JSON payload from an MCP ``call_tool`` result.

    Raises ``RuntimeError`` with the server's message when the tool reported an error so
    the calling group collector can record it in ``errors`` (and the other groups proceed).
    """
    content = result.get("content") or []
    if result.get("isError"):
        msg = "\n".join(str(p) for p in content).strip()
        raise RuntimeError(msg[:500] or "EntraID tool returned an error.")
    # FastMCP serialises the whole return value as one JSON text block; be tolerant of
    # multiple blocks by trying the concatenation first, then each block individually.
    joined = "".join(p for p in content if isinstance(p, str)).strip()
    if joined:
        try:
            return json.loads(joined)
        except (ValueError, TypeError):
            pass
    for part in content:
        if isinstance(part, str):
            try:
                return json.loads(part)
            except (ValueError, TypeError):
                continue
    return []


def _severity_for_days(days: int | None) -> str:
    """Urgency tier for a days-until-expiry value (negative = already expired)."""
    if days is None:
        return "info"
    if days < 0:
        return "critical"
    if days <= 30:
        return "error"
    if days <= 60:
        return "warning"
    return "info"


_SEVERITY_RANK = {"critical": 0, "error": 1, "warning": 2, "info": 3}


# --------------------------------------------------------------------------- workloads
def _build_workload_index() -> list[dict[str, Any]]:
    """A lightweight index of active workloads for best-effort mapping.

    Each entry carries the lowercased names/tags/node-names to match a directory object's
    display name against, plus the set of ARM ids (node ids) for Key Vault id matching."""
    from app.workloads.registry import list_workloads

    index: list[dict[str, Any]] = []
    try:
        workloads = list_workloads()
    except Exception:  # noqa: BLE001 - registry optional/empty
        return index
    for w in workloads:
        names: set[str] = set()
        if w.get("name"):
            names.add(str(w["name"]).lower())
        for tag in w.get("tags", []) or []:
            if tag:
                names.add(str(tag).lower())
        arm_ids: set[str] = set()
        for node in w.get("nodes", []) or []:
            if node.get("name"):
                names.add(str(node["name"]).lower())
            if node.get("id"):
                arm_ids.add(str(node["id"]).lower())
        index.append({"id": w.get("id"), "name": w.get("name"), "names": names, "arm_ids": arm_ids})
    return index


def _map_by_name(name: str | None, index: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """Best-effort (workload_id, workload_name) for a directory object's display name.

    Conservative: an exact name/tag/node-name hit, or a containment match where one side
    is a meaningful (≥4 char) substring of the other. Returns (None, None) when unresolved
    so the UI honestly shows "—" rather than guessing."""
    if not name:
        return None, None
    n = name.strip().lower()
    if not n:
        return None, None
    for w in index:
        if n in w["names"]:
            return w["id"], w["name"]
    for w in index:
        for cand in w["names"]:
            if len(cand) >= 4 and (cand in n or (len(n) >= 4 and n in cand)):
                return w["id"], w["name"]
    return None, None


def _map_by_arm_id(arm_id: str | None, index: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """(workload_id, workload_name) for an ARM resource id (exact, or under a node scope)."""
    if not arm_id:
        return None, None
    a = arm_id.strip().lower()
    for w in index:
        for node in w["arm_ids"]:
            if a == node or a.startswith(node + "/"):
                return w["id"], w["name"]
    return None, None


# --------------------------------------------------------------------------- findings
def _finding(**kw: Any) -> dict[str, Any]:
    """Normalise a finding to the common shape consumed by the UI + handoffs."""
    return {
        "id": kw.get("id", ""),
        "kind": kw.get("kind", ""),
        "title": kw.get("title", ""),
        "detail": kw.get("detail", ""),
        "severity": kw.get("severity", "info"),
        "subject": kw.get("subject", ""),
        "subject_id": kw.get("subject_id", ""),
        "expires_at": kw.get("expires_at"),
        "days_left": kw.get("days_left"),
        "workload_id": kw.get("workload_id"),
        "workload_name": kw.get("workload_name"),
        "remediation": kw.get("remediation", ""),
    }


def _sort_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by urgency: severity tier, then soonest-to-expire, then subject name."""
    def key(f: dict[str, Any]):
        days = f.get("days_left")
        return (
            _SEVERITY_RANK.get(f.get("severity", "info"), 3),
            days if isinstance(days, int) else 10**9,
            (f.get("subject") or "").lower(),
        )

    return sorted(findings, key=key)


# --------------------------------------------------------------------------- collectors
async def _collect_expiring_credentials(
    client, days: int, index: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    raw = _tool_result_json(
        await client.call_tool(
            "find_expiring_credentials",
            {"within_days": days, "include_expired": True, "limit": 400},
        )
    )
    out: list[dict[str, Any]] = []
    for c in raw if isinstance(raw, list) else []:
        d = c.get("daysUntilExpiry")
        ctype = c.get("credentialType")  # "secret" | "certificate"
        name = c.get("displayName") or c.get("appId") or "(unnamed)"
        owner_type = c.get("ownerType") or "application"
        wid, wname = _map_by_name(c.get("displayName"), index)
        kind = KIND_CERT if ctype == "certificate" else KIND_SECRET
        when = "expired" if (isinstance(d, int) and d < 0) else "expires"
        rel = (
            f"{abs(d)} day(s) ago" if (isinstance(d, int) and d < 0)
            else (f"in {d} day(s)" if isinstance(d, int) else "soon")
        )
        out.append(
            _finding(
                id=f"cred:{c.get('ownerId')}:{c.get('keyId') or c.get('credentialName')}",
                kind=kind,
                title=f"{ctype.capitalize()} {when} on {owner_type} “{name}”",
                detail=f"{ctype.capitalize()} {when} {rel}.",
                severity=_severity_for_days(d),
                subject=name,
                subject_id=c.get("appId") or c.get("ownerId") or "",
                expires_at=c.get("endDateTime"),
                days_left=d,
                workload_id=wid,
                workload_name=wname,
                remediation=(
                    "Rotate the credential and update consumers; remove unused "
                    "secrets/certificates. Assign an owner so rotation is accountable."
                ),
            )
        )
    return out


async def _collect_ownerless_apps(
    client, index: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    raw = _tool_result_json(
        await client.call_tool("find_ownerless_applications", {"limit": 400})
    )
    out: list[dict[str, Any]] = []
    for a in raw if isinstance(raw, list) else []:
        name = a.get("displayName") or a.get("appId") or "(unnamed)"
        wid, wname = _map_by_name(a.get("displayName"), index)
        out.append(
            _finding(
                id=f"ownerless:{a.get('id')}",
                kind=KIND_OWNERLESS,
                title=f"App registration “{name}” has no owner",
                detail=(
                    "No directory owner is assigned, so nobody is accountable for "
                    "rotating its credentials or decommissioning it."
                ),
                severity="warning",
                subject=name,
                subject_id=a.get("appId") or a.get("id") or "",
                workload_id=wid,
                workload_name=wname,
                remediation="Assign at least one owner (a user or group) to the app registration.",
            )
        )
    return out


async def _collect_ca_gaps(client) -> list[dict[str, Any]]:
    raw = _tool_result_json(await client.call_tool("get_conditional_access_policies", {}))
    policies = raw if isinstance(raw, list) else []
    out: list[dict[str, Any]] = []
    enabled_count = 0
    for p in policies:
        state = (p.get("state") or "").lower()
        name = p.get("displayName") or "(unnamed policy)"
        if state == "enabled":
            enabled_count += 1
            continue
        if state in ("disabled", "enabledforreportingbutnotenforced"):
            report_only = state != "disabled"
            out.append(
                _finding(
                    id=f"ca:{p.get('id')}",
                    kind=KIND_CA_GAP,
                    title=f"Conditional-access policy “{name}” is {'report-only' if report_only else 'disabled'}",
                    detail=(
                        "This policy is not enforcing access controls, leaving a gap in "
                        "your conditional-access coverage (e.g. MFA, device compliance)."
                    ),
                    severity="warning" if report_only else "error",
                    subject=name,
                    subject_id=p.get("id") or "",
                    remediation="Review and enable the policy (move report-only to enforced) once validated.",
                )
            )
    # No enforced CA policy at all is the highest-impact gap.
    if enabled_count == 0:
        out.insert(
            0,
            _finding(
                id="ca:none-enabled",
                kind=KIND_CA_GAP,
                title="No enabled conditional-access policies",
                detail=(
                    "The tenant has no enforced conditional-access policy. Sign-ins are not "
                    "protected by MFA / device / location controls."
                ),
                severity="critical",
                subject="Tenant conditional access",
                subject_id="",
                remediation="Create and enable a baseline conditional-access policy requiring MFA.",
            ),
        )
    return out


async def _collect_users_without_mfa(
    client, cap: int
) -> tuple[list[dict[str, Any]], bool, int]:
    """Privileged users whose MFA is not enabled. Returns (findings, sampled, scanned)."""
    raw = _tool_result_json(await client.call_tool("get_privileged_users", {}))
    users = raw if isinstance(raw, list) else []
    total = len(users)
    sampled = total > cap
    scan = users[:cap]
    out: list[dict[str, Any]] = []
    for u in scan:
        uid = u.get("id")
        if not uid:
            continue
        name = u.get("displayName") or u.get("userPrincipalName") or uid
        try:
            mfa = _tool_result_json(await client.call_tool("get_user_mfa_status", {"user_id": uid}))
        except RuntimeError as exc:
            log.info("MFA status unavailable for %s: %s", uid, exc)
            continue
        status = (mfa or {}).get("mfaStatus") if isinstance(mfa, dict) else None
        if status == "Enabled":
            continue
        roles = u.get("roles") or []
        role_txt = ", ".join(roles) if isinstance(roles, list) else str(roles)
        out.append(
            _finding(
                id=f"mfa:{uid}",
                kind=KIND_NO_MFA,
                title=f"Privileged user “{name}” has MFA not enabled",
                detail=f"Holds privileged role(s): {role_txt or 'unknown'}. MFA status: {status or 'unknown'}.",
                severity="critical",
                subject=name,
                subject_id=u.get("userPrincipalName") or uid,
                remediation="Require and register a strong MFA method (Authenticator/FIDO2) for this account.",
            )
        )
    return out, sampled, len(scan)


async def _collect_keyvault_expiry(
    connection, index: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], str | None]:
    """Best-effort Key Vault certificate/secret expiry via Resource Graph + data-plane az.

    Resource Graph enumerates the vaults (always available, read-only). Reading actual
    expiry needs data-plane ``az keyvault`` calls, which require command execution to be
    enabled AND the connection's identity to have data-plane access — so this is graceful:
    when expiry can't be read we still map the vaults and explain why in the note."""
    from app.core.app_settings import load_settings
    from app.exec.command_runner import (
        close_sp_session,
        open_sp_session,
        run_az_json_capture,
        run_kql_capture,
    )

    kql = (
        "resources | where type =~ 'microsoft.keyvault/vaults' "
        "| project id, name, resourceGroup, subscriptionId, location"
    )
    cap = await run_kql_capture(kql, connection)
    if not cap.ok:
        return [], f"Could not enumerate Key Vaults: {cap.error[:200]}"
    try:
        vaults = json.loads(cap.stdout) if cap.stdout.strip() else []
    except (ValueError, TypeError):
        vaults = []
    if not isinstance(vaults, list) or not vaults:
        return [], None

    if not load_settings().get("command_execution_enabled", False):
        return [], (
            f"Found {len(vaults)} Key Vault(s). Enable command execution (Admin → General) "
            "and grant the connection data-plane read access to surface certificate/secret expiry."
        )

    out: list[dict[str, Any]] = []
    errors = 0
    config_dir, _ = await open_sp_session(connection)
    try:
        for v in vaults:
            vname = v.get("name")
            vid = v.get("id")
            if not vname:
                continue
            wid, wname = _map_by_arm_id(vid, index)
            for obj_kind, sub in (("certificate", "certificate"), ("secret", "secret")):
                res = await run_az_json_capture(
                    [
                        "keyvault", sub, "list", "--vault-name", vname,
                        "--query", "[].{name:name, expires:attributes.expires}", "-o", "json",
                    ],
                    connection,
                    label=f"az keyvault {sub} list",
                    session_config_dir=config_dir,
                )
                if not res.ok:
                    errors += 1
                    continue
                try:
                    items = json.loads(res.stdout) if res.stdout.strip() else []
                except (ValueError, TypeError):
                    items = []
                for it in items if isinstance(items, list) else []:
                    expires = it.get("expires")
                    days = _days_until(expires)
                    if days is None:
                        continue
                    out.append(
                        _finding(
                            id=f"kv:{vid}:{obj_kind}:{it.get('name')}",
                            kind=KIND_KV_EXPIRY,
                            title=f"Key Vault {obj_kind} “{it.get('name')}” in {vname}",
                            detail=(
                                f"{obj_kind.capitalize()} "
                                + ("expired" if days < 0 else "expires")
                                + (f" {abs(days)} day(s) ago." if days < 0 else f" in {days} day(s).")
                            ),
                            severity=_severity_for_days(days),
                            subject=f"{vname}/{it.get('name')}",
                            subject_id=vid or "",
                            expires_at=expires,
                            days_left=days,
                            workload_id=wid,
                            workload_name=wname,
                            remediation="Renew/rotate the Key Vault object before it expires; update consumers.",
                        )
                    )
    finally:
        close_sp_session(config_dir)

    note = None
    if errors:
        note = f"Read expiry for some vaults; {errors} vault query(ies) failed (data-plane access?)."
    return out, note


def _days_until(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int((dt - datetime.now(timezone.utc)).total_seconds() // 86400)


# --------------------------------------------------------------------------- orchestrator
async def collect_identity(
    connection: dict[str, Any] | None,
    *,
    days: int,
    mfa_cap: int,
    include_keyvault: bool,
    tenant_id: str,
) -> dict[str, Any]:
    """Build the full identity snapshot. Never raises — per-group errors land in ``errors``."""
    from app.core.config import get_settings
    from app.mcp.client import build_entra_mcp_client

    settings = get_settings()
    index = _build_workload_index()
    groups: dict[str, list[dict[str, Any]]] = {
        "expiring_credentials": [],
        "ownerless_apps": [],
        "ca_gaps": [],
        "users_without_mfa": [],
        "keyvault_expiry": [],
    }
    errors: dict[str, str] = {}
    meta: dict[str, Any] = {}

    client = build_entra_mcp_client(settings, connection=connection)

    async def _run(group: str, coro):
        try:
            groups[group] = _sort_findings(await coro)
        except Exception as exc:  # noqa: BLE001 - isolate per-group failures
            errors[group] = str(exc)[:300]
            log.info("identity group %s failed: %s", group, exc)

    async def _run_mfa():
        try:
            findings, sampled, scanned = await _collect_users_without_mfa(client, mfa_cap)
            groups["users_without_mfa"] = _sort_findings(findings)
            meta["mfa_sampled"] = sampled
            meta["mfa_scanned"] = scanned
        except Exception as exc:  # noqa: BLE001
            errors["users_without_mfa"] = str(exc)[:300]
            log.info("identity group users_without_mfa failed: %s", exc)

    async def _run_kv():
        try:
            findings, note = await _collect_keyvault_expiry(connection, index)
            groups["keyvault_expiry"] = _sort_findings(findings)
            if note:
                errors["keyvault_expiry"] = note
        except Exception as exc:  # noqa: BLE001
            errors["keyvault_expiry"] = str(exc)[:300]
            log.info("identity group keyvault_expiry failed: %s", exc)

    try:
        tasks = [
            _run("expiring_credentials", _collect_expiring_credentials(client, days, index)),
            _run("ownerless_apps", _collect_ownerless_apps(client, index)),
            _run("ca_gaps", _collect_ca_gaps(client)),
            _run_mfa(),
        ]
        if include_keyvault:
            tasks.append(_run_kv())
        await asyncio.gather(*tasks)
    finally:
        client.close()

    def _worst(items: list[dict[str, Any]]) -> str:
        if not items:
            return "ok"
        return min((f.get("severity", "info") for f in items), key=lambda s: _SEVERITY_RANK.get(s, 3))

    kpis = {
        "expiring_secrets": sum(
            1 for f in groups["expiring_credentials"] if f["kind"] == KIND_SECRET
        ),
        "expiring_certs": sum(
            1 for f in groups["expiring_credentials"] if f["kind"] == KIND_CERT
        ),
        "ownerless_apps": len(groups["ownerless_apps"]),
        "users_without_mfa": len(groups["users_without_mfa"]),
        "ca_gaps": len(groups["ca_gaps"]),
        "keyvault_expiring": len(groups["keyvault_expiry"]),
    }
    severities = {g: _worst(items) for g, items in groups.items()}

    return {
        "generated_at": _now_iso(),
        "days": int(days),
        "tenant_id": tenant_id,
        "connection_configured": connection is not None,
        "kpis": kpis,
        "group_severity": severities,
        "groups": groups,
        "errors": errors,
        "meta": meta,
    }
