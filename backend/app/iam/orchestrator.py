"""Drive a per-scope (or directory) RBAC refresh against an Azure connection.

The orchestrator is the write path behind ``POST /rbac/refresh``: it acquires the connection's
ARM / Graph tokens, runs the relevant collectors continue-on-error (the scanner model), and
writes the result into the per-scope cache. Each step emits a progress line so the SSE endpoint
can stream live status while neighbours stay served from cache.

It never raises on a collector failure — failures are captured as collector statuses and the
scope's slice is still written (stale-while-error per scope)."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.iam import cache, collectors, cpu, schema

log = logging.getLogger("app.iam.orchestrator")

ProgressFn = Callable[[str, str], Awaitable[None]]

# Above this fraction of assignments whose role could not be named, the Resource Graph sweep is
# discarded entirely. An unnamed role is also an UNCLASSIFIED role, so it reads as ordinary
# access — a tenant full of Owners would report zero privileged access. A small tolerance
# absorbs a genuinely deleted role definition, which is a real (and separately reported) state.
UNNAMED_ROLE_TOLERANCE = 0.05

_GUID_LEN = 36


def _is_bare_guid(role_name: str) -> bool:
    """Did this row fall back to the role's GUID because no definition matched it?"""
    name = (role_name or "").strip()
    return len(name) == _GUID_LEN and name.count("-") == 4 and " " not in name


# The job layer uses these as sentinel keys for "every scope" / "the directory layer". They are
# not ARM scopes, and a `mode=scope` refresh that arrives without a scope falls back to one of
# them — which used to write a real cache slice for a scope literally called "__all__". It then
# sat in the freshness table forever as a permanently-stale row with no data, inflating the scope
# count and the delta-refresh statistics.
SENTINEL_SCOPES = frozenset({"__all__", "directory", ""})


@dataclass
class BulkAccess:
    """One tenant-wide Resource Graph sweep, bucketed by subscription scope.

    ``refresh_all`` collects this once and hands it to every subscription, replacing four ARM
    round trips per subscription (role definitions, role assignments, deny assignments, Key Vault
    vault listing) with four queries for the whole tenant.

    ``subscriptions`` is the set of subscription ids ARG actually returned assignments for. A
    subscription absent from it is NOT treated as "has no access" — see
    :func:`_bulk_covers`, which is what stops an ARG indexing gap from silently emptying a
    scope."""

    role_defs: dict[str, dict[str, Any]] = field(default_factory=dict)
    assignments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    deny: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    keyvault: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    lighthouse: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    statuses: list[collectors.CollectorStatus] = field(default_factory=list)
    usable: bool = False

    def covers(self, scope: str) -> bool:
        """Whether the bulk sweep can stand in for the per-scope ARM calls at ``scope``.

        Requires the sweep to have succeeded AND to have returned at least one assignment for
        this subscription. A real subscription always has at least one role assignment (its own
        owner), so zero rows means ARG could not see it — a permission difference, or the
        indexing lag ARG is prone to right after a change. Trusting that zero would write an
        empty slice and report the subscription as cleanly collected with no access at all."""
        return self.usable and bool(self.assignments.get(scope))


class PimLicence:
    """Per-run memo of whether PIM is licensed on this tenant.

    PIM answers "no Entra ID P2 licence" with a 400 **per scope**, and there are three PIM
    endpoints. On a 26-subscription unlicensed tenant that is 78 round trips to learn the same
    fact 78 times — and after the Resource Graph pivot it is the single largest cost in a
    refresh. The licence is a tenant-wide property, so once one scope reports it missing the
    rest can be skipped with the same honest ``Skipped`` status they would have got anyway.

    Deliberately narrow: **only** the licence verdict is memoised. A 403 on one scope says
    nothing about another scope's permissions, so those are never cached."""

    __slots__ = ("_unlicensed", "_message")

    def __init__(self) -> None:
        self._unlicensed = False
        self._message = ""

    @property
    def known_unlicensed(self) -> bool:
        return self._unlicensed

    @property
    def message(self) -> str:
        return self._message or "PIM is not licensed on this tenant (needs Entra ID P2 / Governance)."

    def observe(self, *statuses: collectors.CollectorStatus) -> None:
        for st in statuses:
            if st.status == schema.STATUS_SKIPPED and "licen" in (st.message or "").lower():
                self._unlicensed = True
                self._message = st.message

    def skipped(self, collector: str) -> collectors.CollectorStatus:
        return collectors.CollectorStatus(collector, schema.STATUS_SKIPPED, 0, 0.0, self.message)


async def _noop(_level: str, _message: str) -> None:
    return None


def _scope_label(scope: str, scope_type: str) -> str:
    if scope_type == schema.SCOPE_SUBSCRIPTION:
        return scope.rstrip("/").split("/")[-1]
    if scope_type == schema.SCOPE_MANAGEMENT_GROUP:
        return scope.rstrip("/").split("/")[-1]
    return scope


def _annotate_pim(rows: list[dict[str, Any]], active_map: dict[str, dict[str, Any]]) -> int:
    """Stamp PIM facts onto the active role-assignment rows an activation produced.

    A PIM activation creates a REAL role assignment, so the same access is in both
    ``roleAssignments`` and ``roleAssignmentScheduleInstances``. Joining on ARM's
    ``originRoleAssignmentId`` lets one row carry both truths — that it is active now, and that
    it expires — instead of double-counting or reporting a time-boxed elevation as permanent.

    Returns the number of rows annotated."""
    if not active_map:
        return 0
    hits = 0
    for row in rows:
        aid = str(row.get("assignmentId", "")).strip().lower()
        info = active_map.get(aid)
        if not info:
            continue
        row["pimManaged"] = True
        row["memberType"] = row.get("memberType") or info.get("memberType", "")
        if info.get("activated"):
            row["activationExpiresOn"] = info.get("activationExpiresOn", "")
            row["assignmentType"] = "ActivatedRoleAssignment"
        hits += 1
    return hits


async def refresh_scope(
    tenant_id: str,
    connection: dict[str, Any] | None,
    scope: str,
    *,
    display_name: str = "",
    progress: ProgressFn | None = None,
    bulk: BulkAccess | None = None,
    pim_licence: "PimLicence | None" = None,
    role_def_sink: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Refresh a single subscription/management-group scope and write its cache slice.

    When ``bulk`` covers this scope, the role-definition, role-assignment, deny-assignment and
    Key Vault collectors are served from the tenant-wide Resource Graph sweep instead of four ARM
    round trips. PIM and classic administrators are not in Resource Graph, so they stay on ARM —
    but ``pim_licence`` lets a whole-tenant refresh stop re-asking the PIM endpoints once one
    scope has established that the tenant has no Entra ID P2 licence."""
    progress = progress or _noop
    started = time.monotonic()
    if scope.strip() in SENTINEL_SCOPES:
        # Never persist a sentinel as if it were a scope; see SENTINEL_SCOPES.
        await progress("warning", f"{scope!r} is not an Azure scope — nothing to refresh.")
        return {"scope": scope, "skipped": True, "error": "not an Azure scope"}
    parts = schema.parse_scope(scope)
    scope_type = parts.get("scopeType", schema.SCOPE_SUBSCRIPTION)
    subscription_id = parts.get("subscriptionId", "")
    label = display_name or _scope_label(scope, scope_type)
    from_bulk = bulk is not None and bulk.covers(scope)
    await progress("info", f"Refreshing access for {label}…")

    from app.azure.credentials import get_arm_token

    token, terr = (None, "no connection") if not connection else await get_arm_token(connection)
    if not token:
        await progress("warning", f"No Azure token for this connection — skipped ({terr}).")
        meta = {
            "scopeType": scope_type,
            "displayName": label,
            "subscriptionId": subscription_id,
            "managementGroupId": parts.get("managementGroupId", ""),
            "status": schema.STATUS_SKIPPED,
            "collectors": [collectors.CollectorStatus("AzureSubscriptionRbac", schema.STATUS_SKIPPED, 0, 0.0, terr or "").public()],
            "coverage": {},
            "demo": False,
        }
        return await asyncio.to_thread(cache.write_scope, tenant_id, scope, meta=meta, rows=[])

    statuses: list[collectors.CollectorStatus] = []
    if from_bulk:
        assert bulk is not None
        role_defs = bulk.role_defs
        rows = list(bulk.assignments.get(scope, []))
        await progress("ok", f"{len(rows)} role assignment(s) from Resource Graph.")
        statuses.append(
            collectors.CollectorStatus(
                "ArgRoleAssignments", schema.STATUS_SUCCEEDED, len(rows), 0.0,
                "Served from the tenant-wide Resource Graph sweep.",
            )
        )
    else:
        await progress("info", "Collecting role definitions…")
        role_defs, rd_status = await collectors.collect_role_definitions(token, scope)
        await progress("info", f"{rd_status.rows_added} role definition(s) [{rd_status.status}].")

        collector_name = "AzureSubscriptionRbac" if scope_type == schema.SCOPE_SUBSCRIPTION else "ManagementGroupRbac"
        await progress("info", "Collecting role assignments…")
        rows, ra_status = await collectors.collect_azure_rbac(
            token,
            scope=scope,
            subscription_id=subscription_id,
            subscription_name=label,
            tenant_id=tenant_id,
            role_defs=role_defs,
            collector=collector_name,
        )
        await progress("ok", f"{ra_status.rows_added} role assignment(s) [{ra_status.status}].")
        statuses.extend([rd_status, ra_status])

    if role_def_sink is not None:
        # Accumulated across every scope so the directory blob ends up with the COMPLETE set.
        # A custom role defined at a management group is not indexed by the Resource Graph
        # sweep, and a role the engine cannot resolve makes it answer "indeterminate" forever.
        role_def_sink.update(role_defs)

    # --- PIM / JIT ----------------------------------------------------------------------
    # Activation controls first: the eligibility rows carry them, so they must be resolved
    # before the eligibility collector runs.
    if pim_licence is not None and pim_licence.known_unlicensed:
        # An earlier scope already established the tenant has no Entra ID P2 licence. Asking the
        # other three endpoints on every remaining scope re-learns the same fact at ~1.5s a call
        # and, after the Resource Graph pivot, dominates the whole refresh. The reported status
        # is identical to what the calls would have produced.
        statuses.extend(
            pim_licence.skipped(c)
            for c in ("PimPolicies", "PimEligibility", "PimActiveSchedules")
        )
        active_map: dict[str, dict[str, Any]] = {}
        await progress("info", "PIM is not licensed on this tenant — skipped.")
    else:
        await progress("info", "Reading PIM activation policies…")
        pim_policies, pol_status = await collectors.collect_pim_policies(token, scope=scope)
        statuses.append(pol_status)

        await progress("info", "Collecting PIM eligibility…")
        eligible_rows, elig_status = await collectors.collect_pim_eligibility(
            token,
            scope=scope,
            subscription_id=subscription_id,
            subscription_name=label,
            tenant_id=tenant_id,
            role_defs=role_defs,
            policies=pim_policies,
        )
        rows.extend(eligible_rows)
        statuses.append(elig_status)
        await progress("ok", f"{elig_status.rows_added} eligible assignment(s) [{elig_status.status}].")

        # Active PIM schedules MIRROR rows roleAssignments already returned, so they are folded
        # in as annotations rather than emitted — otherwise every JIT elevation is double-counted.
        await progress("info", "Correlating active PIM elevations…")
        active_map, act_status = await collectors.collect_pim_active_schedules(token, scope=scope)
        statuses.append(act_status)
        if pim_licence is not None:
            pim_licence.observe(pol_status, elig_status, act_status)
    annotated = _annotate_pim(rows, active_map)
    if annotated:
        await progress("ok", f"{annotated} active assignment(s) are PIM-governed elevations.")

    # Deny assignments — evaluated BEFORE role assignments and not overridable, so a report
    # without them can say the opposite of the truth. Collected at every scope level.
    if from_bulk:
        assert bulk is not None
        deny_rows = list(bulk.deny.get(scope, []))
        rows.extend(deny_rows)
        statuses.append(
            collectors.CollectorStatus(
                "ArgDenyAssignments", schema.STATUS_SUCCEEDED, len(deny_rows), 0.0,
                "Served from the tenant-wide Resource Graph sweep.",
            )
        )
        await progress("info", f"{len(deny_rows)} deny assignment(s) from Resource Graph.")
    else:
        await progress("info", "Collecting deny assignments…")
        deny_rows, deny_status = await collectors.collect_deny_assignments(
            token,
            scope=scope,
            subscription_id=subscription_id,
            subscription_name=label,
            tenant_id=tenant_id,
        )
        rows.extend(deny_rows)
        statuses.append(deny_status)
        await progress("info", f"{deny_status.rows_added} deny assignment(s) [{deny_status.status}].")

    # Lighthouse delegations. Only from the bulk sweep — there is no per-scope ARM fallback yet,
    # and the collector status is what the signal reads, so emitting a fake Succeeded here would
    # turn "we never looked" into "no other tenant has access".
    if from_bulk:
        assert bulk is not None
        lh_rows = list(bulk.lighthouse.get(scope, []))
        rows.extend(lh_rows)
        statuses.append(
            collectors.CollectorStatus(
                "AzureLighthouseDelegations", schema.STATUS_SUCCEEDED, len(lh_rows), 0.0,
                "Served from the tenant-wide Resource Graph sweep.",
            )
        )
        if lh_rows:
            await progress("warning", f"{len(lh_rows)} Lighthouse delegation(s) at {label}.")

    # Subscription-only surfaces. Neither exists at management-group or tenant-root scope, so
    # querying there would just manufacture a misleading Failed status.
    if scope_type == schema.SCOPE_SUBSCRIPTION and subscription_id:
        if from_bulk:
            assert bulk is not None
            kv_rows = list(bulk.keyvault.get(scope, []))
            rows.extend(kv_rows)
            statuses.append(
                collectors.CollectorStatus(
                    "ArgKeyVaultAccessPolicies", schema.STATUS_SUCCEEDED, len(kv_rows), 0.0,
                    "Served from the tenant-wide Resource Graph sweep.",
                )
            )
            await progress("info", f"{len(kv_rows)} Key Vault access policy grant(s) from Resource Graph.")
        else:
            await progress("info", "Collecting Key Vault access policies…")
            kv_rows, kv_status = await collectors.collect_keyvault_policies(
                token, subscription_id=subscription_id, subscription_name=label, tenant_id=tenant_id
            )
            rows.extend(kv_rows)
            statuses.append(kv_status)
            await progress("info", f"{kv_status.rows_added} Key Vault access policy grant(s) [{kv_status.status}].")

        # Classic administrators are not in Resource Graph at all — always ARM.
        await progress("info", "Collecting classic administrators…")
        classic_rows, classic_status = await collectors.collect_classic_admins(
            token, subscription_id=subscription_id, subscription_name=label, tenant_id=tenant_id
        )
        rows.extend(classic_rows)
        statuses.append(classic_status)
        await progress("info", f"{classic_status.rows_added} classic administrator(s) [{classic_status.status}].")

    overall = schema.STATUS_SUCCEEDED
    for s in statuses:
        if s.status in schema.ATTENTION_STATUSES:
            overall = schema.STATUS_PARTIAL
    meta = {
        "scopeType": scope_type,
        "displayName": label,
        "subscriptionId": subscription_id,
        "managementGroupId": parts.get("managementGroupId", ""),
        "status": overall,
        "collectors": [s.public() for s in statuses],
        "coverage": {"roleAssignments": len(rows), "roleDefinitions": len(role_defs)},
        "demo": False,
        # Which path produced this slice. Diagnostics surfaces it: an ARG-sourced scope and an
        # ARM-sourced one have different blind spots, and a reader comparing two scopes needs
        # to know they were not collected the same way.
        "source": "arg" if from_bulk else "arm",
        "duration_seconds": round(time.monotonic() - started, 2),
    }
    written = await asyncio.to_thread(cache.write_scope, tenant_id, scope, meta=meta, rows=rows)
    await progress("ok", f"Cached {len(rows)} assignment(s) for {label}.")
    return written


async def refresh_directory(
    tenant_id: str,
    connection: dict[str, Any] | None,
    *,
    progress: ProgressFn | None = None,
    role_defs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Refresh the tenant directory layer: Entra roles, group expansion, SP owners.

    Group ids and service-principal ids are derived from the assignments already cached in the
    scope slices, so the directory layer enriches whatever Azure RBAC has discovered."""
    progress = progress or _noop
    await progress("info", "Refreshing Entra directory layer…")

    from app.azure.credentials import get_arm_token, get_graph_token

    # Resolve management-group names via ARM first — this works even when the connection lacks
    # Microsoft Graph directory permissions, so the scope tree shows MG names regardless.
    mg_names: dict[str, str] = {}
    mg_status: collectors.CollectorStatus | None = None
    identities: dict[str, dict[str, Any]] = {}
    federated: list[dict[str, Any]] = []
    arm_statuses: list[collectors.CollectorStatus] = []
    if connection:
        arm_token, _aerr = await get_arm_token(connection)
        if arm_token:
            await progress("info", "Resolving management-group names…")
            mg_names, mg_status = await collectors.collect_management_groups(arm_token)
            await progress("info", f"Resolved {len(mg_names)} management group name(s) [{mg_status.status}].")

            # Managed identities and their federated credentials come from ARM/Resource Graph,
            # NOT Microsoft Graph — so they are collected here, before the Graph token is
            # required. A connection with no directory permissions still gets the identity
            # inventory, which is what makes "which resource is this GUID?" answerable.
            from app.iam import arg as _arg

            await progress("info", "Inventorying managed identities…")
            identities, mi_status = await _arg.collect_managed_identities(connection)
            arm_statuses.append(mi_status)
            await progress("info", f"{mi_status.rows_added} managed identity/identities [{mi_status.status}].")

            uami_ids = [
                str(i["identityResourceId"]) for i in identities.values()
                if i.get("identityKind") == "UserAssigned" and i.get("identityResourceId")
            ]
            await progress("info", f"Reading federated credentials on {len(uami_ids)} identity/identities…")
            federated, fic_status = await collectors.collect_federated_credentials(arm_token, uami_ids)
            arm_statuses.append(fic_status)
            await progress("info", f"{fic_status.rows_added} federated credential(s) [{fic_status.status}].")

    token, terr = (None, "no connection") if not connection else await get_graph_token(connection)
    if not token:
        await progress("warning", f"No Microsoft Graph token — directory skipped ({terr}).")
        collector_list = [collectors.CollectorStatus("EntraRoleAssignments", schema.STATUS_SKIPPED, 0, 0.0, terr or "").public()]
        if mg_status is not None:
            collector_list.append(mg_status.public())
        collector_list.extend(s.public() for s in arm_statuses)
        meta = {
            "status": schema.STATUS_SKIPPED,
            "collectors": collector_list,
            "demo": False,
        }
        return await asyncio.to_thread(
            cache.write_directory,
            tenant_id, meta=meta, rows=[], role_defs=_preserve_role_defs(tenant_id, role_defs),
            principals=[], groups={}, management_groups=mg_names,
            identities=identities, federated=federated,
        )

    statuses: list[collectors.CollectorStatus] = []
    if mg_status is not None:
        statuses.append(mg_status)
    statuses.extend(arm_statuses)
    await progress("info", "Collecting Entra directory roles…")
    entra_rows, entra_status = await collectors.collect_entra_roles(token, tenant_id)
    statuses.append(entra_status)
    await progress("info", f"{entra_status.rows_added} directory role assignment(s) [{entra_status.status}].")

    # Derive the group + SP ids that actually appear in cached assignments (only expand what's used).
    scope_rows = await asyncio.to_thread(cache.all_scope_rows, tenant_id)
    group_ids = sorted({r.get("principalId", "") for r in scope_rows if r.get("principalType") == "Group" and r.get("principalId")})
    sp_ids = sorted({r.get("principalId", "") for r in scope_rows if r.get("principalType") == "ServicePrincipal" and r.get("principalId")})

    await progress("info", f"Expanding {len(group_ids)} group(s)…")
    groups, grp_status = await collectors.collect_group_expansion(token, group_ids)
    statuses.append(grp_status)

    await progress("info", f"Resolving owners for {len(sp_ids)} service principal(s)…")
    owner_rows, owner_status = await collectors.collect_sp_owners(token, sp_ids, tenant_id)
    statuses.append(owner_status)

    # Resolve every distinct principal GUID seen in the cached Azure-RBAC/KV/classic assignments
    # AND the Entra directory-role rows to a friendly name (ARM only returns the object id, and
    # the Entra query expands roleDefinition not principal). This populates the principal directory
    # used by compose to backfill names across every tab + export.
    principal_ids = sorted(
        {r.get("principalId", "") for r in scope_rows if r.get("principalId")}
        | {r.get("effectivePrincipalId", "") for r in scope_rows if r.get("effectivePrincipalId")}
        | {r.get("principalId", "") for r in entra_rows if r.get("principalId")}
        | {r.get("effectivePrincipalId", "") for r in owner_rows if r.get("effectivePrincipalId")}
    )
    await progress("info", f"Resolving {len(principal_ids)} principal name(s)…")
    principals, prin_status = await collectors.collect_principal_directory(token, principal_ids)
    statuses.append(prin_status)
    await progress("info", f"Resolved {prin_status.rows_added} principal name(s) [{prin_status.status}].")

    # Backfill group display names (the expansion graph stores members but not the group's own
    # name) from the resolved principal directory, so group rows read with names too.
    _pmap = {p["principalId"].lower(): p.get("displayName", "") for p in principals if p.get("principalId")}
    for gid, grp in groups.items():
        if not grp.get("name"):
            grp["name"] = _pmap.get(str(gid).lower(), "")

    overall = schema.STATUS_SUCCEEDED
    for s in statuses:
        if s.status in schema.ATTENTION_STATUSES:
            overall = schema.STATUS_PARTIAL
    meta = {
        "status": overall,
        "collectors": [s.public() for s in statuses],
        "demo": False,
    }
    written = await asyncio.to_thread(
        cache.write_directory,
        tenant_id,
        meta=meta,
        rows=[*entra_rows, *owner_rows],
        role_defs=_preserve_role_defs(tenant_id, role_defs),
        principals=principals,
        groups=groups,
        management_groups=mg_names,
        identities=identities,
        federated=federated,
    )
    await progress("ok", "Directory layer cached.")
    return written


def _role_def_list(role_defs: dict[str, dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Flatten the GUID-keyed role map for the directory blob.

    These carry the four action lists, which is what makes ``app.iam.effective`` able to answer
    "can P do A" from cache. Without them the engine sees empty permission sets and cannot tell
    "Owner grants everything" apart from "Owner grants nothing"."""
    return sorted(
        (dict(rd) for rd in (role_defs or {}).values()),
        key=lambda rd: str(rd.get("roleName", "")),
    )


def _preserve_role_defs(
    tenant_id: str, role_defs: dict[str, dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Role definitions for the directory blob, carrying forward the cached set when the caller
    collected none.

    ``refresh_directory`` does not collect role definitions — only ``refresh_all`` does. But it
    rewrites the whole directory blob, and role definitions live in that blob, so passing no
    ``role_defs`` used to persist an empty list and DELETE the definitions a previous full
    refresh had collected. Two of the three callers (the standalone directory job and the
    missions system) pass none, so an ordinary directory refresh silently destroyed them.

    Measured on the live `lu` tenant: the action universe collapsed from 5,055 actions to 125
    and right-sizing went from 2,185 over-privileged assignments to **zero**, which the UI then
    rendered as "Nothing crossed the over-privilege threshold" — a clean bill of health produced
    by having lost the data. Effective Access, escalation, the simulator and the agent tool read
    the same index and degrade the same silent way.

    A refresh that did not look at role definitions has no business deleting them, so the cached
    set is carried forward. Only a caller that actually collected some replaces them."""
    if role_defs:
        return _role_def_list(role_defs)
    existing = cache.read_directory(tenant_id).get("role_defs") or []
    return sorted(
        (dict(rd) for rd in existing),
        key=lambda rd: str(rd.get("roleName", "")),
    )


async def collect_bulk(
    tenant_id: str,
    connection: dict[str, Any] | None,
    *,
    arm_token: str = "",
    role_def_scope: str = "",
    subscription_names: dict[str, str] | None = None,
    progress: ProgressFn | None = None,
) -> BulkAccess:
    """One tenant-wide Resource Graph sweep: role definitions, assignments, denies, vaults.

    Never returns a half-trusted result. If the sweep fails, is throttled, or cannot name the
    roles it found, ``usable`` stays False and every scope falls back to its ARM collectors —
    degraded to the old speed, but never to wrong data. The failure statuses ride along so
    Diagnostics can explain why the fast path was not taken."""
    from app.iam import arg

    progress = progress or _noop
    out = BulkAccess()
    started = time.monotonic()

    # Role definitions come from BOTH sources, and ARM is the one that matters.
    # `authorizationresources` indexes CUSTOM role definitions; built-ins are largely absent
    # from it. Measured on a live tenant: ARG returned 3 definitions and 66 of 67 assignments
    # fell back to a bare role GUID — which also means roleIsPrivileged=False, so a tenant with
    # 39 Owner grants would have reported zero privileged access. Built-in definitions are
    # identical tenant-wide, so one ARM call at any scope supplies them all.
    if arm_token and role_def_scope:
        await progress("info", "Reading built-in role definitions…")
        builtin, bi_st = await collectors.collect_role_definitions(arm_token, role_def_scope)
        out.role_defs.update(builtin)
        out.statuses.append(bi_st)

    await progress("info", "Sweeping custom role definitions via Resource Graph…")
    custom, rd_st = await arg.collect_role_definitions_arg(connection)
    out.statuses.append(rd_st)
    if rd_st.status in schema.ATTENTION_STATUSES and not out.role_defs:
        await progress("warning", f"Resource Graph role definitions unavailable ({rd_st.status}): {rd_st.message}")
        return out
    # Custom definitions win: a tenant can define a custom role whose GUID collides with nothing,
    # but if it ever did, the scope-specific one is the more accurate answer.
    out.role_defs.update(custom)
    if not out.role_defs:
        await progress("warning", "No role definitions could be resolved — using per-subscription ARM collection.")
        return out
    await progress("ok", f"{len(out.role_defs)} role definition(s) tenant-wide.")

    await progress("info", "Sweeping role assignments via Resource Graph…")
    out.assignments, ra_st = await arg.collect_assignments_arg(
        connection, tenant_id=tenant_id, role_defs=out.role_defs, subscription_names=subscription_names
    )
    out.statuses.append(ra_st)
    if ra_st.status in schema.ATTENTION_STATUSES:
        await progress("warning", f"Resource Graph assignments unavailable ({ra_st.status}): {ra_st.message}")
        return out

    # A role we cannot name is a role we cannot classify, and an unclassified privileged role
    # reads as ordinary access. Past a small tolerance that is a silent falsification, not a
    # degradation, so the whole sweep is discarded in favour of ARM.
    unnamed = sum(
        1 for rows in out.assignments.values() for r in rows if _is_bare_guid(r.get("roleName", ""))
    )
    total = sum(len(v) for v in out.assignments.values())
    if total and unnamed / total > UNNAMED_ROLE_TOLERANCE:
        pct = round(100 * unnamed / total)
        out.statuses.append(
            collectors.CollectorStatus(
                "ArgRoleAssignments", schema.STATUS_PARTIAL, total, 0.0,
                f"{pct}% of assignments could not be matched to a role definition; "
                "falling back to per-subscription ARM collection.",
            )
        )
        await progress(
            "warning",
            f"{pct}% of Resource Graph assignments have unresolved role names — using ARM instead.",
        )
        return out
    await progress("ok", f"{ra_st.rows_added} role assignment(s) across {len(out.assignments)} subscription(s).")

    # Denies and vaults are additive: a failure here degrades those two surfaces to ARM without
    # giving up the assignment sweep, which is where nearly all the time is saved.
    out.deny, dn_st = await arg.collect_deny_assignments_arg(
        connection, tenant_id=tenant_id, subscription_names=subscription_names
    )
    out.statuses.append(dn_st)
    out.keyvault, kv_st = await arg.collect_keyvault_policies_arg(
        connection, tenant_id=tenant_id, subscription_names=subscription_names
    )
    out.statuses.append(kv_st)

    # Lighthouse is collected even when it finds nothing, because the STATUS is the product:
    # `ext.lighthouse_delegation` reports "not measured" until a successful run exists, and a
    # tenant carrying a partner's standing Owner access has no other way to discover it — these
    # grants do not appear in the portal's Access control blade at all.
    out.lighthouse, lh_st = await arg.collect_lighthouse_arg(
        connection, tenant_id=tenant_id, subscription_names=subscription_names,
        role_defs=out.role_defs,
    )
    out.statuses.append(lh_st)
    if lh_st.rows_added:
        await progress("warning", f"{lh_st.rows_added} Lighthouse delegation(s) from another tenant.")

    if dn_st.status in schema.ATTENTION_STATUSES or kv_st.status in schema.ATTENTION_STATUSES:
        # Do not silently serve empty deny/KV buckets from a failed query — that would report
        # "no deny assignments" for a tenant that has them. Drop back to ARM for everything.
        await progress(
            "warning",
            "Resource Graph deny/Key Vault sweep incomplete — using per-subscription ARM collection.",
        )
        return out

    out.usable = True
    await progress("ok", f"Resource Graph sweep complete in {time.monotonic() - started:.1f}s.")
    return out


async def refresh_bypass(
    tenant_id: str,
    connection: dict[str, Any] | None,
    *,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Sweep the RBAC-bypass surface and join it to who can reach each credential.

    Runs LAST, after the access rows and the directory exist, because the ``reachableBy`` join
    needs both — and that join is what makes this an access feature rather than a configuration
    checklist."""
    from app.iam import bypass, compose, effective

    progress = progress or _noop
    await progress("info", "Sweeping non-RBAC access paths (shared keys, local auth, admin users)…")
    resources, statuses = await bypass.collect(connection)

    readable = [s for s in statuses.values() if s.status not in schema.UNTRUSTWORTHY_STATUSES]
    await progress(
        "info" if readable else "warning",
        f"{len(resources)} resource(s) assessed across {len(readable)} of {len(statuses)} service families.",
    )

    access_rows = await asyncio.to_thread(compose.build_master_rows, tenant_id)
    directory = await asyncio.to_thread(cache.read_directory, tenant_id)
    role_index = await asyncio.to_thread(effective.build_role_index, directory.get("role_defs", []))
    actions = sorted({s.credential_action for s in bypass.BYPASS_SPECS if s.credential_action})

    reachability: dict[str, list[dict[str, str]]] = {}
    reachability_available = bool(role_index)
    if reachability_available:
        await progress("info", "Resolving who can obtain each credential…")
        # OFF THE LOOP. This join is every principal x every granting scope x every credential
        # action; it is the single most expensive thing a refresh does, and run inline it froze
        # the entire product — login included — for its whole duration. Measured: the same class
        # of work costs 0.98s of loop lag inline and 0.04s in a thread.
        reachability = await cpu.run(
            bypass.compute_reachability, access_rows, role_index, actions, label="bypass reachability"
        )
    else:
        # An empty reachable list and an unavailable join look identical to a reader, so the rows
        # carry the distinction explicitly rather than implying nobody can get the key.
        await progress(
            "warning",
            "Role definitions are not cached, so 'who can obtain the credential' could not be computed.",
        )

    rows = await asyncio.to_thread(
        bypass.assess, resources, reachability=reachability, reachability_available=reachability_available
    )
    summary = await asyncio.to_thread(bypass.summarize, resources, rows, statuses)

    overall = schema.STATUS_SUCCEEDED
    for st in statuses.values():
        if st.status in schema.ATTENTION_STATUSES:
            overall = schema.STATUS_PARTIAL
    written = await asyncio.to_thread(
        cache.write_bypass,
        tenant_id,
        meta={
            "status": overall,
            "collectors": [s.public() for s in statuses.values()],
            "demo": False,
            "reachability_available": reachability_available,
        },
        resources=resources,
        rows=rows,
        summary=summary,
    )
    pct = summary.get("rbac_only_pct")
    await progress(
        "ok",
        f"{summary['findings']} bypass finding(s); RBAC is the only door for "
        + (f"{pct}% of {summary['assessed']} assessed resource(s)." if pct is not None
           else "an unknown share (nothing was assessed)."),
    )
    return written


async def refresh_usage(
    tenant_id: str,
    connection: dict[str, Any] | None,
    *,
    days: int = 90,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Collect exercised actions per principal — a SEPARATE job from the access refresh.

    Deliberately not part of `refresh_all`. The Activity Log is queried per subscription and is
    slow enough that a 26-subscription tenant would double or triple the refresh; worse, bolting
    it on would give usage the access snapshot's freshness, and the whole point is that usage is
    allowed to be weeks old while access is minutes old — as long as the UI says which is which."""
    from app.azure.credentials import get_arm_token
    from app.azure.arm import list_subscriptions
    from app.iam import usage as usage_mod

    progress = progress or _noop
    token, terr = await get_arm_token(connection)
    if not token:
        payload = usage_mod._empty(days, f"Usage collection needs an Azure token: {terr}",
                                   status=schema.STATUS_UNAUTHORIZED)
        await asyncio.to_thread(cache.write_usage, tenant_id, payload)
        await progress("warning", payload["notes"][0])
        return payload
    subs_raw, serr = await list_subscriptions(token)
    if serr:
        await progress("warning", f"Subscription listing failed: {serr}")
    subs = [str(s.get("id", "")) for s in subs_raw if s.get("id")]
    await progress("info", f"Collecting exercised actions across {len(subs)} subscription(s)…")

    payload = await usage_mod.collect(subs, connection, days=days)
    written = await asyncio.to_thread(cache.write_usage, tenant_id, payload)

    # The analysis is written with the usage it derives from, so the findings endpoint reads a
    # cached result instead of paying two seconds of CPU per request.
    #
    # OFF THE LOOP. This is the analysis whose inline execution starved the event loop hard
    # enough that SQLite began reporting "database is locked" on unrelated session writes and
    # login hung. That was fixed at the /iam/rightsizing endpoint and MISSED here — the same
    # defect surviving in a second call site, which is why the freeze came back during refreshes.
    from app.iam import rightsize

    analysis = await cpu.run(rightsize.analyse_for_tenant, tenant_id, force=True, label="right-sizing")

    if not usage_mod.is_measured(payload):
        await progress("warning", "; ".join(payload.get("notes") or ["Usage could not be collected."]))
        return written
    await progress(
        "ok",
        f"{payload['event_count']} operation(s) by {len(payload['principals'])} principal(s) over "
        f"{payload['window_days']} days; {len(analysis['recommendations'])} over-privileged "
        f"assignment(s). Data-plane activity is NOT in this data, so data-plane roles are "
        f"excluded from right-sizing.",
    )
    return written


# How many scopes a whole-tenant refresh collects at once.
#
# Three, not more. The ceiling is not CPU — it is Azure Resource Graph, which allows
# 15 queries / 5s PER SECURITY PRINCIPAL, shared tenant-wide (see app/azure/arg_throttle.py).
# The pacer smooths admissions and retries what slips through, so a wider fan-out does not go
# faster; it just queues behind the same quota while holding more ARM connections open and
# making the progress log unreadable. Three keeps every worker busy through the slow serial
# part of a scope (PIM + classic admins on ARM) without crowding the window.
_SCOPE_FANOUT = 3


async def _fan_out(
    targets: list[tuple[str, str]],
    run: "Callable[[str, str], Awaitable[Any]]",
    *,
    progress: ProgressFn,
    label: str,
) -> list[str]:
    """Collect ``targets`` ``_SCOPE_FANOUT`` at a time; return the scopes that SUCCEEDED.

    Fan out, then fan back in: the caller may not continue until every target has been written,
    because the phase that follows depends on this one being complete.

    One scope failing must not lose the rest of the run, so exceptions are gathered rather than
    raised — a whole-tenant refresh that dies on the twentieth of twenty-six subscriptions is
    worse than one that reports which one broke. Returning the succeeded scopes rather than a
    count lets the caller keep its per-path statistics honest: a scope that raised must not be
    counted as collected by either path.
    """
    if not targets:
        return []
    if len(targets) > 1:
        await progress("info", f"Collecting {len(targets)} {label}(s), {_SCOPE_FANOUT} at a time…")

    sem = asyncio.Semaphore(_SCOPE_FANOUT)

    async def _one(scope: str, name: str) -> Any:
        async with sem:
            return await run(scope, name)

    results = await asyncio.gather(
        *(_one(scope, name) for scope, name in targets), return_exceptions=True
    )

    ok: list[str] = []
    for (scope, name), result in zip(targets, results, strict=True):
        if isinstance(result, BaseException):
            log.warning("iam refresh: %s %s failed: %s", label, scope, result)
            await progress("error", f"{name or scope} failed: {result}")
            continue
        ok.append(scope)
    return ok


async def refresh_all(
    tenant_id: str,
    connection: dict[str, Any] | None,
    *,
    progress: ProgressFn | None = None,
    mode: str = "full",
) -> dict[str, Any]:
    """Refresh every visible scope, then the directory layer.

    Order matters: management groups (and the tenant root) are collected FIRST, then
    subscriptions. An assignment made once at a management group covering 26 subscriptions is
    returned by all 26 subscription queries as an inherited row; collecting the MG scope in its
    own right gives compose an authoritative copy to dedupe against, so the grant is reported
    once and attributed to the MG rather than 26 times to arbitrary subscriptions.

    ``mode="delta"`` asks Resource Graph which subscriptions had authorization activity since
    their last collection and refreshes only those. If that question cannot be answered the run
    degrades to a full refresh — never to "nothing changed"."""
    progress = progress or _noop
    from app.azure.arm import list_all_management_groups, list_subscriptions
    from app.azure.credentials import get_arm_token

    token, terr = (None, "no connection") if not connection else await get_arm_token(connection)
    if not token:
        await progress("warning", f"No Azure token — nothing to scan ({terr}).")
        return {"scopes": 0, "skipped": True, "error": terr}

    refreshed = 0
    stats: dict[str, Any] = {"arg_scopes": 0, "arm_scopes": 0, "skipped_unchanged": 0, "discrepancies": []}
    # One memo for the whole run. Scoped to this refresh so a licence bought between runs is
    # picked up on the next one rather than being cached until the process restarts.
    licence = PimLicence()
    all_role_defs: dict[str, dict[str, Any]] = {}

    # Clear any sentinel slices an earlier build wrote; they show up as permanently-stale
    # zero-row scopes and skew both the scope count and the delta statistics.
    for ghost in cache.purge_phantom_scopes(tenant_id):
        log.info("iam refresh: removed phantom cache scope %r", ghost)

    # 1. Management groups. Best-effort: many connections cannot read the MG hierarchy, and that
    #    must not stop the subscription scan.
    await progress("info", "Listing management groups…")
    mgs, mgerr = await list_all_management_groups(token)
    if mgerr:
        await progress("warning", f"Management-group listing failed: {mgerr}")
    await progress("info", f"{len(mgs)} management group(s) visible.")

    mg_targets = [
        (f"/providers/Microsoft.Management/managementGroups/{str(mg.get('id', '')).strip()}",
         mg.get("name", "") or str(mg.get("id", "")).strip())
        for mg in mgs if str(mg.get("id", "")).strip()
    ]
    # Always ARM: Resource Graph indexes authorizationresources per subscription, so an
    # MG-scoped grant appears only as inherited copies under its children. Collecting the MG
    # in its own right is what lets dedupe attribute the grant to the MG.
    #
    # Fanned out, but the PHASE boundary below is not negotiable — see the docstring. Every
    # management group must be written before the first subscription is collected, or dedupe
    # has no authoritative copy to attribute an inherited grant to.
    done = await _fan_out(
        mg_targets,
        lambda scope, name: refresh_scope(
            tenant_id, connection, scope, display_name=name,
            progress=progress, pim_licence=licence, role_def_sink=all_role_defs,
        ),
        progress=progress, label="management group",
    )
    refreshed += len(done)
    stats["arm_scopes"] += len(done)

    # 2. Subscriptions.
    await progress("info", "Listing subscriptions…")
    subs, serr = await list_subscriptions(token)
    if serr:
        await progress("warning", f"Subscription listing failed: {serr}")
    await progress("info", f"{len(subs)} subscription(s) visible.")
    sub_names = {str(s["id"]): str(s.get("name", s["id"])) for s in subs}

    changed: set[str] | None = None
    if mode == "delta":
        changed, why = await _changed_subscriptions(tenant_id, connection, subs)
        if changed is None:
            await progress("warning", f"Delta refresh unavailable ({why}) — running a full refresh.")
        else:
            await progress("ok", f"{len(changed)} subscription(s) changed since their last collection.")

    to_refresh = [s for s in subs if changed is None or str(s["id"]) in changed]
    refresh_ids = {str(s["id"]) for s in to_refresh}
    for sub in subs:
        if str(sub["id"]) not in refresh_ids:
            cache.mark_scope_verified(
                tenant_id, f"/subscriptions/{sub['id']}", reason="no authorization activity since last collection"
            )
            stats["skipped_unchanged"] += 1

    bulk = BulkAccess()
    if to_refresh:
        # Built-in role definitions are identical tenant-wide, so any one real scope supplies
        # them. Using a subscription rather than an MG keeps this working on connections that
        # cannot read the management-group hierarchy at all.
        bulk = await collect_bulk(
            tenant_id, connection,
            arm_token=token,
            role_def_scope=f"/subscriptions/{to_refresh[0]['id']}",
            subscription_names=sub_names, progress=progress,
        )

    sub_targets: list[tuple[str, str]] = []
    used_arg_by_scope: dict[str, bool] = {}
    for sub in to_refresh:
        # arm.list_subscriptions returns the GUID under `id`, NOT `subscriptionId` — reading the
        # raw ARM field name yields 0 subscriptions on a tenant that has plenty.
        scope = f"/subscriptions/{sub['id']}"
        used_arg = bulk.covers(scope)
        used_arg_by_scope[scope] = used_arg
        if bulk.usable and not used_arg:
            # The sweep worked but returned nothing for this subscription. Every live
            # subscription has at least its own owner assignment, so this is a gap in what ARG
            # could see, not an empty subscription. Fall back to ARM and REPORT the difference
            # rather than writing an empty slice that reads as "collected, no access".
            stats["discrepancies"].append({"scope": scope, "name": sub.get("name", ""), "arg_rows": 0})
            await progress(
                "warning",
                f"Resource Graph returned no assignments for {sub.get('name', sub['id'])} — verifying via ARM.",
            )
        sub_targets.append((scope, str(sub.get("name", sub["id"]))))

    collected = await _fan_out(
        sub_targets,
        lambda scope, name: refresh_scope(
            tenant_id, connection, scope, display_name=name, progress=progress,
            bulk=bulk if used_arg_by_scope.get(scope) else None,
            pim_licence=licence, role_def_sink=all_role_defs,
        ),
        progress=progress, label="subscription",
    )
    refreshed += len(collected)
    # Counted from what actually succeeded, so a failed scope inflates neither path.
    for scope in collected:
        stats["arg_scopes" if used_arg_by_scope.get(scope) else "arm_scopes"] += 1

    await refresh_directory(tenant_id, connection, progress=progress, role_defs=all_role_defs)
    await refresh_bypass(tenant_id, connection, progress=progress)
    if stats["discrepancies"]:
        await progress(
            "warning",
            f"{len(stats['discrepancies'])} subscription(s) were missing from Resource Graph and "
            "were collected via ARM instead.",
        )
    await progress("ok", f"Refreshed {refreshed} scope(s) + directory.")
    return {"scopes": refreshed, "skipped": False, "mode": mode, **stats}


async def _changed_subscriptions(
    tenant_id: str,
    connection: dict[str, Any] | None,
    subs: list[dict[str, Any]],
) -> tuple[set[str] | None, str]:
    """Subscription ids to re-collect, or ``None`` meaning "refresh everything".

    A subscription is refreshed if Resource Graph reports authorization activity since **its own**
    last collection, or if it has never been collected, or if its last collection produced no
    trustworthy rows. That last rule uses ``UNTRUSTWORTHY_STATUSES``, not ``ATTENTION_STATUSES``:
    a tenant without an Entra ID P2 licence gets a 400 from every PIM endpoint, so every scope is
    permanently ``PartiallyCollected``, and treating that as untrustworthy makes delta refresh
    re-collect the whole estate while still calling itself a delta."""
    from app.iam import arg

    metas = {str(m.get("scope", "")): m for m in cache.list_scope_meta(tenant_id)}
    never_collected: set[str] = set()
    oldest = ""
    for sub in subs:
        scope = f"/subscriptions/{sub['id']}"
        meta = metas.get(scope)
        if not meta or meta.get("status") in schema.UNTRUSTWORTHY_STATUSES:
            never_collected.add(str(sub["id"]))
            continue
        gen = str(meta.get("generated_at", ""))
        if not gen:
            never_collected.add(str(sub["id"]))
            continue
        if not oldest or gen < oldest:
            oldest = gen

    if not oldest:
        return None, "no scope has a successful previous collection"

    changed, why = await arg.subscriptions_changed_since(connection, oldest)
    if changed is None:
        return None, why
    return changed | never_collected, ""
