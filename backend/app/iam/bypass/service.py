"""Collect the bypass surface, join it to who can reach the credential, and roll it up.

One batched Resource Graph query, not one per service. Resource Graph allows 15 queries per 5
seconds per security principal tenant-wide and **each page costs a unit**, so twenty per-service
queries would spend the whole budget before the rest of a refresh started.

Every family still reports its own :class:`CollectorStatus`, so one service that could not be
read never blanks the tab — and a family with nothing to report says whether that is because
there is nothing there or because nothing could be seen.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.iam import effective, schema
from app.iam.bypass import specs as sp
from app.iam.collectors import CollectorStatus

log = logging.getLogger("app.iam.bypass")

MAX_RESOURCE_ROWS = 50_000

# Every field any spec reads, projected once. Types that do not have a given property simply
# return an empty string, which the detectors treat as "absent" — and absent almost always means
# the bypass is ENABLED, which is why each detector states its default explicitly.
_PROJECTION = """
    id, name, type, subscriptionId, resourceGroup, tags,
    allowSharedKeyAccess              = tostring(properties.allowSharedKeyAccess),
    keyExpirationPeriodInDays         = tostring(properties.keyPolicy.keyExpirationPeriodInDays),
    allowBlobPublicAccess             = tostring(properties.allowBlobPublicAccess),
    allowCrossTenantReplication       = tostring(properties.allowCrossTenantReplication),
    disableLocalAuth                  = tostring(properties.disableLocalAuth),
    disableAccessKeyAuthentication    = tostring(properties.disableAccessKeyAuthentication),
    disableLocalAccounts              = tostring(properties.disableLocalAccounts),
    enableAzureRBAC                   = tostring(properties.aadProfile.enableAzureRBAC),
    aadProfileManaged                 = tostring(properties.aadProfile.managed),
    azureADOnlyAuthentication         = tostring(properties.administrators.azureADOnlyAuthentication),
    adminLogin                        = tostring(properties.administrators.login),
    administratorLogin                = tostring(properties.administratorLogin),
    sqlAdministratorLogin             = tostring(properties.sqlAdministratorLogin),
    adminUserEnabled                  = tostring(properties.adminUserEnabled),
    enableRbacAuthorization           = tostring(properties.enableRbacAuthorization),
    allowedAuthenticationModes        = tostring(properties.allowedAuthenticationModes)
"""


def _kql() -> str:
    types = ", ".join(f"'{t}'" for t in sp.RESOURCE_TYPES)
    # `| order by id asc` is mandatory: $skipToken paging is only deterministic over an ordered
    # result set. Without it pages overlap and drop rows non-reproducibly.
    return f"resources\n| where type in~ ({types})\n| project {_PROJECTION}\n| order by id asc"


async def collect(
    connection: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, CollectorStatus]]:
    """Every resource in scope for a bypass check, plus a status per service family.

    On failure EVERY family reports the failure rather than zero findings: a service the
    connection cannot read must never render as a service with nothing wrong."""
    from app.exec.command_runner import run_kql_collect

    started = time.monotonic()
    res = await run_kql_collect(_kql(), connection, max_rows=MAX_RESOURCE_ROWS, page_size=1000)
    elapsed = time.monotonic() - started

    if not res.ok:
        err = (res.error or "").lower()
        if any(t in err for t in ("429", "throttl", "toomanyrequests", "rate limit")):
            status = schema.STATUS_THROTTLED
        elif any(t in err for t in ("forbidden", "authorizationfailed", "403", "401")):
            status = schema.STATUS_UNAUTHORIZED
        else:
            status = schema.STATUS_FAILED
        message = (res.error or "Resource Graph query failed.")[:300]
        return [], {
            family: CollectorStatus(f"Bypass{family.title()}", status, 0, elapsed, message)
            for family in sp.FAMILIES
        }

    rows = list(res.rows)
    by_type: dict[str, int] = {}
    for r in rows:
        by_type[str(r.get("type", "")).lower()] = by_type.get(str(r.get("type", "")).lower(), 0) + 1

    statuses: dict[str, CollectorStatus] = {}
    for family in sp.FAMILIES:
        count = sum(by_type.get(t, 0) for t in sp.TYPES_BY_FAMILY[family])
        st = CollectorStatus(f"Bypass{family.title()}", schema.STATUS_SUCCEEDED, count, elapsed, "")
        if count == 0:
            # NOT an attention status: a tenant with no Cosmos accounts genuinely has no Cosmos
            # bypass. What matters is that the denominator is published so a reader can tell
            # "none exist" from "none assessed".
            st.status = schema.STATUS_SKIPPED
            st.message = "No resources of this type were returned."
        if not res.complete:
            st.status = schema.STATUS_PARTIAL
            st.message = f"Result was capped at {MAX_RESOURCE_ROWS} resources."
        statuses[family] = st
    return rows, statuses


# --------------------------------------------------------------------------- reachability
def compute_reachability(
    access_rows: list[dict[str, Any]],
    role_index: dict[str, effective.RoleActionSet],
    actions: list[str],
) -> dict[str, list[dict[str, str]]]:
    """``action -> [{principalId, principalName, scope}]``, evaluated once per assignment scope.

    This is the join that makes shadow access an *access* feature rather than a configuration
    checklist: it answers "and who can actually get that key?".

    Computed per assignment SCOPE rather than per resource. A principal can call ``listKeys`` on
    a resource if they hold the action at or above it, and assignments only exist at a handful of
    scopes — so this is (scopes x actions) evaluations instead of (resources x actions), which on
    a real estate is the difference between instant and unusable.

    EVERY scope at which the principal holds the action is recorded, not just the first. Stopping
    at the first was measurably wrong: the caller filters these scopes against each resource, so
    a principal whose first hit was a scope that does not cover the resource in question
    disappeared entirely. On the live tenant that made 9 of 18 storage accounts report "nobody
    can fetch the key" when two principals could — a false all-clear on a shared-key door, which
    is the worst direction for this error to run in."""
    scopes: list[str] = []
    for r in access_rows:
        s = str(r.get("scope", "")).strip()
        if s and s not in scopes:
            scopes.append(s)

    by_principal: dict[str, list[dict[str, Any]]] = {}
    names: dict[str, str] = {}
    for r in access_rows:
        pid = str(r.get("effectivePrincipalId", "") or r.get("principalId", "")).lower()
        if not pid:
            continue
        by_principal.setdefault(pid, []).append(r)
        names.setdefault(pid, str(r.get("effectivePrincipalName", "") or r.get("principalDisplayName", "") or pid))

    out: dict[str, list[dict[str, str]]] = {}
    for action in actions:
        if not action:
            continue
        holders: list[dict[str, str]] = []
        for pid, mine in by_principal.items():
            for scope in scopes:
                dec = effective.evaluate(
                    mine, role_index, principal_id=pid, scope=scope,
                    action=action, plane=effective.PLANE_CONTROL,
                )
                if dec.verdict == effective.ALLOWED:
                    holders.append({"principalId": pid, "principalName": names.get(pid, pid), "scope": scope})
        out[action] = holders
    return out


# --------------------------------------------------------------------------- assessment
def assess(
    resources: list[dict[str, Any]],
    *,
    reachability: dict[str, list[dict[str, str]]] | None = None,
    reachability_available: bool = True,
    workload_env: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """One row per (resource, enabled bypass).

    Only *enabled* bypasses become rows — a resource where every door is closed contributes to
    the denominator, not to the list."""
    reachability = reachability or {}
    workload_env = workload_env or {}
    out: list[dict[str, Any]] = []

    for res in resources:
        rtype = str(res.get("type", "")).lower()
        rid = str(res.get("id", ""))
        env = workload_env.get(rid.lower(), "")
        for spec in sp.BYPASS_SPECS:
            if spec.resource_type != rtype:
                continue
            try:
                enabled = bool(spec.detect(res))
            except Exception:  # noqa: BLE001 — one malformed resource must not lose the sweep
                log.warning("bypass detector %s failed on %s", spec.key, rid, exc_info=True)
                continue
            if not enabled:
                continue

            holders = reachability.get(spec.credential_action, []) if spec.credential_action else []
            # A principal is now recorded at every scope where they hold the action, so the same
            # person can match a resource more than once. The published count is PEOPLE, not
            # (person, scope) pairs — "12 principals can fetch this key" has to mean 12 people.
            seen_principals: set[str] = set()
            reachable: list[dict[str, str]] = []
            for h in holders:
                if not effective.scope_covers(h["scope"], rid):
                    continue
                if h["principalId"] in seen_principals:
                    continue
                seen_principals.add(h["principalId"])
                reachable.append(h)
            out.append(
                {
                    "key": spec.key,
                    "family": spec.family,
                    "resourceId": rid,
                    "resourceType": rtype,
                    "resourceName": str(res.get("name", "")),
                    "subscriptionId": str(res.get("subscriptionId", "")),
                    "resourceGroup": str(res.get("resourceGroup", "")),
                    "bypassKind": spec.bypass_kind,
                    "title": spec.title,
                    "enabled": True,
                    "detail": spec.detail,
                    "severity": _severity_for(spec, env, len(reachable)),
                    "environment": env,
                    "credentialAction": spec.credential_action,
                    # An empty list when the join could not run is indistinguishable from "nobody
                    # can reach it", so the flag is carried explicitly on every row.
                    "reachableBy": reachable[:50],
                    "reachableCount": len(reachable),
                    "reachabilityAvailable": bool(reachability_available and spec.credential_action),
                    "rbacOnlyPossible": spec.rbac_only_possible,
                    "remediation": spec.remediation.format(
                        name=res.get("name", ""), rg=res.get("resourceGroup", ""), id=rid
                    ),
                    # Never published without the remediation it qualifies.
                    "breaksIf": spec.breaks_if,
                    "frameworks": list(spec.frameworks),
                }
            )
    return out


# Production is where a shared key matters. A dev sandbox with the same setting is not the same
# finding, and treating it as one is how a findings list stops being read.
_PROD_MARKERS = ("prod", "production", "live")
_NONPROD_MARKERS = ("dev", "test", "sandbox", "staging", "qa", "uat", "demo")

_BUMP = {"warning": "error", "error": "critical", "critical": "critical", "info": "warning"}
_DROP = {"critical": "error", "error": "warning", "warning": "info", "info": "info"}


def _severity_for(spec: sp.BypassSpec, environment: str, reachable: int) -> str:
    """Base severity, modulated by environment and by how many principals can get the key."""
    sev = spec.severity
    env = (environment or "").lower()
    if any(m in env for m in _PROD_MARKERS):
        sev = _BUMP[sev]
    elif any(m in env for m in _NONPROD_MARKERS):
        sev = _DROP[sev]
    # A door many people can open is a wider door.
    if reachable >= 10:
        sev = _BUMP[sev]
    return sev


def summarize(
    resources: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    statuses: dict[str, CollectorStatus],
) -> dict[str, Any]:
    """The one number the tab rolls up to: *is RBAC the only door?*

    The denominator is **assessed resources**, published alongside the percentage. A ratio over
    an unknown denominator is the kind of number that gets quoted in a board pack and cannot be
    defended."""
    assessed_ids = {str(r.get("id", "")).lower() for r in resources if r.get("id")}
    bypassed_ids = {str(r["resourceId"]).lower() for r in rows}
    assessed = len(assessed_ids)
    clean = assessed - len(bypassed_ids)

    by_family: dict[str, dict[str, Any]] = {}
    for family in sp.FAMILIES:
        st = statuses.get(family)
        fam_resources = {
            str(r.get("id", "")).lower() for r in resources
            if str(r.get("type", "")).lower() in sp.TYPES_BY_FAMILY[family]
        }
        fam_rows = [r for r in rows if r["family"] == family]
        by_family[family] = {
            "family": family,
            "assessed": len(fam_resources),
            "affected": len({r["resourceId"].lower() for r in fam_rows}),
            "findings": len(fam_rows),
            "status": st.status if st else schema.STATUS_SKIPPED,
            "message": st.message if st else "",
        }

    blind = [f for f, s in statuses.items() if s.status in schema.UNTRUSTWORTHY_STATUSES]
    limitations = [
        "This reports the door, not the room. Kubernetes RBAC objects, in-database SQL users and "
        "Exchange permissions are not read — a cluster shown here has NOT had its internal "
        "authorization assessed.",
    ]
    if blind:
        limitations.append(
            f"{len(blind)} service family/families could not be read ({', '.join(sorted(blind))}), "
            "so their resources are absent from both the findings and the denominator."
        )

    return {
        "assessed": assessed,
        "rbac_only": clean,
        "bypassed": len(bypassed_ids),
        # None rather than 0 when nothing was assessed: 0% would read as "no resource is
        # RBAC-only", which is the opposite of "we have not looked at any".
        "rbac_only_pct": round(100 * clean / assessed) if assessed else None,
        "findings": len(rows),
        "by_family": list(by_family.values()),
        "by_severity": {
            s: sum(1 for r in rows if r["severity"] == s)
            for s in ("critical", "error", "warning", "info")
        },
        "limitations": limitations,
    }
