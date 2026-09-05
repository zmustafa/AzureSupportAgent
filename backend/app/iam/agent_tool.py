"""Read-only RBAC agent tools — answer access questions from the cached scan.

Exposes the access-review data to the LLM through the same ``ConnectorTool`` shape the other
built-in tools use, so the orchestrator's tool loop dispatches them uniformly. All three are
strictly READ-ONLY (no approval pause): they query the per-scope cache via :mod:`compose`, never
Azure directly, so they're instant and side-effect free. Gated by the ``iam_tools_enabled``
admin setting."""
from __future__ import annotations

from typing import Any

from app.connectors.base import ConnectorTool, err, ok
from app.iam import cache, compose, effective, schema


def _resolve_principal(tenant_id: str, query: str) -> tuple[str, str, str]:
    """(principalId, displayName, error) for a name / UPN / object id.

    An ambiguous match is an ERROR, not a guess. Silently picking the first "John" and reporting
    his access as somebody else's is the kind of wrong answer that gets acted on."""
    q = (query or "").strip().lower()
    if not q:
        return "", "", "No principal supplied."
    seen: dict[str, str] = {}
    for r in compose.build_master_rows(tenant_id):
        pid = str(r.get("effectivePrincipalId", "") or r.get("principalId", ""))
        if not pid:
            continue
        name = str(r.get("effectivePrincipalName", "") or r.get("principalDisplayName", ""))
        upn = str(r.get("effectivePrincipalUserPrincipalName", "") or r.get("principalUserPrincipalName", ""))
        if q == pid.lower() or q == upn.lower() or q == name.lower() or (len(q) > 3 and q in name.lower()):
            seen.setdefault(pid, name or upn or pid)
    if not seen:
        return "", "", f"No principal matching {query!r} appears in the cached access scan."
    if len(seen) > 1:
        listed = ", ".join(f"{n} ({p})" for p, n in list(seen.items())[:5])
        return "", "", f"{query!r} matches {len(seen)} principals: {listed}. Use the object id."
    pid, name = next(iter(seen.items()))
    return pid, name, ""


def _make_can_principal_do(tenant_id: str):
    async def _handler(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        action = str(args.get("action") or "").strip()
        scope = str(args.get("scope") or "").strip()
        if not action or not scope:
            return err("Both 'action' and 'scope' are required.")
        pid, name, perr = _resolve_principal(tenant_id, str(args.get("principal") or ""))
        if perr:
            return err(perr)

        rows = compose.build_master_rows(tenant_id)
        role_index = effective.build_role_index(cache.read_directory(tenant_id).get("role_defs", []))
        dec = effective.evaluate(
            rows, role_index, principal_id=pid, scope=scope,
            action=action, plane=str(args.get("plane") or ""),
        )

        headline = {
            effective.ALLOWED: f"YES - {name} can perform {action} on {scope}.",
            effective.DENIED: f"NO - {name} is DENIED {action} on {scope}.",
            effective.NOT_GRANTED: f"NO - {name} has no grant for {action} on {scope}.",
            # Never phrased as a yes or a no: the whole point of this verdict is that the answer
            # is not known, and a model asked for a verdict will otherwise round it to one.
            effective.INDETERMINATE: f"UNKNOWN - {name}'s access to {action} on {scope} cannot be determined.",
        }[dec.verdict]

        lines = [headline, "", dec.reason]
        if dec.decided_by:
            d = dec.decided_by
            lines.append(f"Deciding assignment: {d.get('roleName')} at {d.get('scopeDisplayName')} ({d.get('assignmentId')})")
        if dec.via_groups:
            lines.append("Via group(s): " + ", ".join(g["groupName"] for g in dec.via_groups))
        if dec.not_action_exclusions:
            for ex in dec.not_action_exclusions[:3]:
                lines.append(f"Excluded by notActions on {ex['roleName']}: {ex['notAction']}")
        if dec.condition_unevaluated:
            lines.append(
                f"{len(dec.condition_unevaluated)} assignment(s) carry an ABAC condition that is "
                "not evaluated here, so the answer may change per resource."
            )
        if dec.unknown_roles:
            lines.append("Unresolved role definition(s): " + ", ".join(dec.unknown_roles))
        return ok("\n".join(lines))

    return _handler


def _fmt_rows(rows: list[dict[str, Any]], *, limit: int = 50) -> str:
    """Compact, model-friendly rendering of access rows."""
    lines: list[str] = []
    for r in rows[:limit]:
        who = r.get("effectivePrincipalName") or r.get("principalDisplayName") or r.get("effectivePrincipalId") or "(unknown)"
        path = r.get("accessPath", "")
        via = f" via {r.get('sourceGroupName')}" if path == schema.PATH_GROUP else (" (owner)" if path == schema.PATH_OWNER else "")
        scope = r.get("scopeDisplayName") or r.get("subscriptionName") or r.get("scope") or "directory"
        flag = " ⚠privileged" if r.get("roleIsPrivileged") else ""
        lines.append(f"- {who}: {r.get('roleName','')} @ {scope}{via}{flag} [{r.get('surface','')}]")
    more = f"\n…and {len(rows) - limit} more." if len(rows) > limit else ""
    return ("\n".join(lines) + more) if lines else "(no matching access found)"


def _make_who_can_access(tenant_id: str):
    async def _handler(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        scope_q = str(args.get("scope") or "").strip().lower()
        privileged_only = bool(args.get("privileged_only", False))
        rows = compose.build_master_rows(tenant_id)
        if scope_q:
            rows = [
                r
                for r in rows
                if scope_q in str(r.get("scope", "")).lower()
                or scope_q in str(r.get("scopeDisplayName", "")).lower()
                or scope_q in str(r.get("subscriptionName", "")).lower()
                or scope_q in str(r.get("resourceName", "")).lower()
            ]
        if privileged_only:
            rows = [r for r in rows if r.get("roleIsPrivileged")]
        if not rows:
            return ok("No cached access matches that scope. Run an access refresh on the RBAC page first.")
        rows.sort(key=lambda r: (not r.get("roleIsPrivileged"), r.get("roleName", "")))
        return ok(f"{len(rows)} access grant(s) matching '{scope_q or 'any scope'}':\n\n{_fmt_rows(rows)}")

    return _handler


def _make_privileged_review(tenant_id: str):
    async def _handler(_config: dict[str, Any], _args: dict[str, Any]) -> dict[str, Any]:
        rows = [r for r in compose.build_master_rows(tenant_id) if r.get("roleIsPrivileged")]
        if not rows:
            return ok("No privileged access in the cached scan (or nothing scanned yet).")
        owners = [r for r in rows if r.get("accessPath") == schema.PATH_OWNER]
        group = [r for r in rows if r.get("accessPath") == schema.PATH_GROUP]
        principals = sorted({r.get("effectivePrincipalName") or r.get("principalDisplayName") or r.get("effectivePrincipalId") for r in rows})
        head = (
            f"{len(rows)} privileged grant(s) across {len(principals)} principal(s); "
            f"{len(group)} via group membership, {len(owners)} via service-principal ownership.\n\n"
        )
        return ok(head + _fmt_rows(rows))

    return _handler


def _make_effective_for_principal(tenant_id: str):
    async def _handler(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        q = str(args.get("principal") or "").strip().lower()
        if not q:
            return err("Provide a principal name, UPN, or id.")
        rows = [
            r
            for r in compose.build_master_rows(tenant_id)
            if q in str(r.get("effectivePrincipalName", "")).lower()
            or q in str(r.get("effectivePrincipalUserPrincipalName", "")).lower()
            or q in str(r.get("effectivePrincipalId", "")).lower()
            or q in str(r.get("principalDisplayName", "")).lower()
        ]
        if not rows:
            return ok(f"No cached access found for '{q}'.")
        return ok(f"Effective access for '{q}' ({len(rows)} grant(s)):\n\n{_fmt_rows(rows)}")

    return _handler


def _make_why_access(tenant_id: str):
    """*Why* a principal has access to a scope — the path and the deciding assignment.

    Distinct from `can_principal_do`, which answers a yes/no about one action. This one answers
    "where did this come from", which is the question asked when somebody is trying to REMOVE
    access and needs to know which assignment to touch."""

    async def _handler(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        scope = str(args.get("scope") or "").strip()
        if not scope:
            return err("'scope' is required.")
        pid, name, perr = _resolve_principal(tenant_id, str(args.get("principal") or ""))
        if perr:
            return err(perr)

        rows = [
            r for r in compose.build_master_rows(tenant_id)
            if str(r.get("effectivePrincipalId", "")).lower() == pid
            and effective.scope_covers(str(r.get("scope", "")), scope)
        ]
        if not rows:
            return ok(
                f"{name} has no cached grant reaching {scope}. That is not proof of no access: "
                f"a scope that was never scanned contributes nothing here."
            )
        lines = [f"{name} reaches {scope} through {len(rows)} grant(s):", ""]
        for r in rows[:25]:
            path = str(r.get("accessPath", ""))
            how = (
                f"via group {r.get('sourceGroupName')}" if path == schema.PATH_GROUP
                else "as service-principal owner" if path == schema.PATH_OWNER
                else "assigned directly"
            )
            where = r.get("scopeDisplayName") or r.get("scope")
            deny = " [DENY]" if r.get("effect") == schema.EFFECT_DENY else ""
            state = " (eligible, not active)" if r.get("assignmentState") == schema.STATE_ELIGIBLE else ""
            lines.append(
                f"- {r.get('roleName')}{deny}{state} at {where} — {how}. "
                f"Assignment: {r.get('assignmentId') or '(none recorded)'}"
            )
        if len(rows) > 25:
            lines.append(f"…and {len(rows) - 25} more.")
        lines += [
            "",
            "To remove access, change the assignment at the scope it was MADE at — which also "
            "affects every other resource under that scope.",
        ]
        return ok("\n".join(lines))

    return _handler


def _make_escalation_paths(tenant_id: str):
    async def _handler(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        from app.iam import escalation

        rows = compose.build_master_rows(tenant_id)
        directory = cache.read_directory(tenant_id)
        # The memoised entry point, not `detect`: the same graph is wanted by /iam/escalation,
        # /iam/findings and /iam/score, and recomputing it costs ~30s on a 45-scope tenant.
        graph = escalation.graph_for_tenant(
            tenant_id, rows,
            effective.build_role_index(directory.get("role_defs", [])),
            identities=directory.get("identities", {}),
            federated=directory.get("federated", []),
        )
        paths = graph.get("paths", []) or []
        target = str(args.get("target_role") or "").strip()
        if target:
            paths = [p for p in paths if target.lower() in str(p.get("toLabel", "")).lower()]
        limits = graph.get("limitations", []) or []
        if not paths:
            body = f"No escalation path found{f' to {target}' if target else ''} in the cached scan."
            if limits:
                # An escalation map that could not see managed identities reporting no paths is
                # the most dangerous false negative in the product.
                body += "\n\nThis is NOT an all-clear — the analysis could not see:\n" + "\n".join(
                    f"- {limitation}" for limitation in limits[:6]
                )
            return ok(body)
        lines = [f"{len(paths)} escalation path(s):", ""]
        for p in paths[:20]:
            lines.append(
                f"- {p.get('fromLabel')} → {p.get('toLabel')} in {len(p.get('steps', []))} step(s) "
                f"[confidence: {p.get('confidence')}]"
            )
        if limits:
            lines += ["", "Not visible to this analysis:"] + [f"- {limitation}" for limitation in limits[:6]]
        return ok("\n".join(lines))

    return _handler


def _make_unused_permissions(tenant_id: str):
    async def _handler(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        analysis = cache.read_rightsizing(tenant_id)
        if not analysis.get("measured"):
            # "Nothing unused" and "we never measured usage" must not read the same, especially
            # to a model that will summarize this into a recommendation.
            reasons = analysis.get("limitations") or ["Usage has not been collected."]
            return ok(
                "UNMEASURED — usage has not been collected for this tenant, so nothing here is a "
                "claim about what is unused:\n" + "\n".join(f"- {r}" for r in reasons[:5])
            )
        recs = analysis.get("recommendations") or []
        who = str(args.get("principal") or "").strip().lower()
        if who:
            recs = [
                r for r in recs
                if who in str(r.get("principalName", "")).lower()
                or who == str(r.get("principalId", "")).lower()
            ]
        if not recs:
            return ok(
                f"No over-privileged assignment found{f' for {who}' if who else ''} over the "
                f"{analysis.get('window_days')}-day window, measured against "
                f"{analysis.get('action_universe_size')} distinct actions."
            )
        lines = [
            f"{len(recs)} over-privileged assignment(s) over {analysis.get('window_days')} days, "
            f"measured against {analysis.get('action_universe_size')} distinct actions:",
            "",
        ]
        for r in recs[:20]:
            prop = r.get("recommendation") or {}
            narrower = (
                f" → propose {', '.join(prop.get('roles', []))}" if prop else " (no safe narrower role)"
            )
            lines.append(
                f"- {r.get('principalName')}: {', '.join(r.get('currentRoles', []))} "
                f"[{r.get('confidence')} confidence]{narrower}"
            )
        for limitation in (analysis.get("limitations") or [])[:4]:
            lines.append(f"! {limitation}")
        return ok("\n".join(lines))

    return _handler


def _make_simulate_revoke(tenant_id: str):
    async def _handler(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        from app.iam import simulator

        assignment_id = str(args.get("assignment_id") or "").strip()
        if not assignment_id:
            return err("'assignment_id' is required.")
        rows = compose.build_master_rows(tenant_id)
        directory = cache.read_directory(tenant_id)
        try:
            result = simulator.simulate(
                rows, [{"kind": simulator.REMOVE_ASSIGNMENT, "assignment_id": assignment_id}],
                role_index=effective.build_role_index(directory.get("role_defs", [])),
            )
        except simulator.MissingReferent as exc:
            return err(f"{exc} — the assignment may already be gone.")
        except simulator.InvalidChange as exc:
            return err(str(exc))
        lost = result.get("access_lost") or []
        retained = result.get("access_retained_via_other_path") or []
        orphaned = result.get("orphaned_resources") or []
        lines = [
            f"Simulated revoke of {assignment_id} (nothing was changed in Azure):",
            "",
            f"- principals affected: {result.get('principals_affected', 0)}",
            f"- grants lost: {len(lost)}",
            f"- grants that survive by another path: {len(retained)}",
            f"- resources left with no access at all: {len(orphaned)}",
            f"- standing privilege: {result.get('standing_privilege_before')} → "
            f"{result.get('standing_privilege_after')}",
        ]
        if result.get("unchanged"):
            # The most important thing this tool can say. A revoke that changes nothing gets
            # signed off as "access removed" and the access is still there.
            lines.append(
                "- NOTE: this changes NOTHING — the same access is still granted by another "
                "assignment or path."
            )
        for limitation in (result.get("limitations") or [])[:5]:
            lines.append(f"! {limitation}")
        return ok("\n".join(lines))

    return _handler


def _make_access_changed_since(tenant_id: str):
    async def _handler(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        drift = cache.read_drift(tenant_id)
        if not drift.get("available"):
            # `note`, not `reason`: the cached drift slice carries the explanation under `note`,
            # and reading a key that does not exist would drop the one sentence that stops this
            # reading as "nothing changed".
            return ok(
                "No comparison is available: this tenant has fewer than two retained scans, so "
                "what changed is unknown — not nothing. "
                + str(drift.get("note") or drift.get("reason") or "")
            )
        changes = drift.get("changes") or []
        worsening = [c for c in changes if c.get("worsens")]
        if not changes:
            return ok(
                "No authorization change since the previous scan. Both snapshots exist and were "
                "compared, so this is a measured result rather than an absence of data."
            )
        lines = [
            f"{drift.get('total', len(changes))} authorization change(s) since the previous scan, "
            f"{drift.get('worsening', len(worsening))} of which increased access:",
            "",
        ]
        for c in (worsening or changes)[:20]:
            actor = c.get("actor") or {}
            who = actor.get("actor") if isinstance(actor, dict) else None
            lines.append(
                f"- [{c.get('class')}] {c.get('principalName') or c.get('principalId')}: "
                f"{c.get('roleName')} at {c.get('scopeName') or c.get('scope')}"
                + (f" — by {who}" if who else "")
            )
        if drift.get("truncated"):
            lines.append("…the change list was truncated; open the Compare tab for the rest.")
        return ok("\n".join(lines))

    return _handler


def _make_who_can_reach_resource(tenant_id: str):
    async def _handler(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        from app.iam import resource_access

        resource_id = str(args.get("resource_id") or "").strip()
        if not resource_id:
            return err("'resource_id' is required.")
        out = resource_access.for_resource(tenant_id, resource_id)
        if not out.get("measured"):
            return ok(f"UNKNOWN — {out.get('reason')}")
        lines = [
            f"{out['total']} principal(s) can reach {resource_id}, "
            f"{out['privilegedTotal']} with a privileged role:",
            "",
        ]
        for p in out["principals"][:25]:
            grants = "; ".join(
                f"{g['roleName']} (granted at {g['grantedAt']})" for g in p["grants"][:3]
            )
            flag = " ⚠privileged" if p["privileged"] else ""
            dead = " [DELETED PRINCIPAL]" if p["principalExists"] == schema.EXISTS_FALSE else ""
            off = (
                " [DISABLED ACCOUNT — dormant, restored on re-enable]"
                if p.get("principalAccountEnabled") == schema.ENABLED_FALSE
                else ""
            )
            lines.append(f"- {p['principalName'] or p['principalId']}{flag}{dead}{off}: {grants}")
        bypass = out.get("bypass") or {}
        if not bypass.get("measured"):
            lines += ["", f"! {bypass.get('reason')}"]
        elif bypass.get("openDoors"):
            lines += ["", "RBAC is not the only door into this resource:"]
            lines += [f"- {d['title']}" for d in bypass["openDoors"]]
        for limitation in out.get("limitations", [])[:4]:
            lines.append(f"! {limitation}")
        return ok("\n".join(lines))

    return _handler


def build_iam_tools(tenant_id: str) -> list[ConnectorTool]:
    """The read-only access-review tools bound to a tenant's cached scan."""
    return [
        ConnectorTool(
            name="who_can_access",
            description=(
                "List who has access to an Azure scope (subscription, resource group, or resource) "
                "from the latest cached RBAC access scan. Includes effective access via group "
                "membership and service-principal ownership. Read-only."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "description": "Scope to filter by — a subscription name/id, resource group, or resource name substring."},
                    "privileged_only": {"type": "boolean", "description": "Only return privileged (Owner/Contributor/UAA/data-owner) grants."},
                },
            },
            kind="read",
            handler=_make_who_can_access(tenant_id),
        ),
        ConnectorTool(
            name="privileged_access_review",
            description=(
                "Summarize all privileged access (Owner, Contributor, User Access Administrator, "
                "data-plane owner roles, Entra admin roles) from the latest cached RBAC scan, "
                "including access granted via groups and service-principal ownership. Read-only."
            ),
            parameters={"type": "object", "properties": {}},
            kind="read",
            handler=_make_privileged_review(tenant_id),
        ),
        ConnectorTool(
            name="effective_access_for_principal",
            description=(
                "Show every Azure/Entra access grant a given user, group, or service principal "
                "effectively has, from the latest cached RBAC scan (direct + via group + as owner). "
                "Read-only."
            ),
            parameters={
                "type": "object",
                "properties": {"principal": {"type": "string", "description": "Principal display name, UPN, or object id."}},
                "required": ["principal"],
            },
            kind="read",
            handler=_make_effective_for_principal(tenant_id),
        ),
        ConnectorTool(
            name="can_principal_do",
            description=(
                "Answer whether a specific principal can perform a specific Azure action on a "
                "specific scope, and explain why, from the latest cached access scan. Honours "
                "deny assignments, notActions, control-plane vs data-plane separation, and scope "
                "inheritance. Returns UNKNOWN rather than guessing when an ABAC condition or an "
                "unresolved role definition is in the path. Read-only."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "principal": {"type": "string", "description": "Display name, UPN, or object id."},
                    "action": {
                        "type": "string",
                        "description": "Azure action string, e.g. Microsoft.Storage/storageAccounts/delete.",
                    },
                    "scope": {
                        "type": "string",
                        "description": "Full ARM scope or resource id, e.g. /subscriptions/<id>/resourceGroups/prod.",
                    },
                    "plane": {
                        "type": "string",
                        "description": "'control' or 'data'. Omit to infer from the action string.",
                    },
                },
                "required": ["principal", "action", "scope"],
            },
            kind="read",
            handler=_make_can_principal_do(tenant_id),
        ),
        ConnectorTool(
            name="why_does_principal_have_access",
            description=(
                "Explain WHERE a principal's access to a scope comes from: every grant that "
                "reaches it, whether it was assigned directly, inherited from a broader scope, "
                "or held via a group or service-principal ownership, and the assignment id to "
                "change. Use this when asked how to REMOVE access. Read-only."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "principal": {"type": "string", "description": "Display name, UPN, or object id."},
                    "scope": {"type": "string", "description": "Full ARM scope or resource id."},
                },
                "required": ["principal", "scope"],
            },
            kind="read",
            handler=_make_why_access(tenant_id),
        ),
        ConnectorTool(
            name="escalation_paths_to",
            description=(
                "List paths by which a principal could gain a more powerful role (typically "
                "Owner) from the cached scan — managed identities, federated credentials, "
                "role-assignment rights, Key Vault pivots. Always reports what the analysis "
                "could NOT see; an empty result is not an all-clear. Read-only."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "target_role": {
                        "type": "string",
                        "description": "Optional filter, e.g. 'Owner'. Omit for all paths.",
                    },
                },
            },
            kind="read",
            handler=_make_escalation_paths(tenant_id),
        ),
        ConnectorTool(
            name="unused_permissions_for",
            description=(
                "Report granted-versus-used access from the cached usage scan, optionally for "
                "one principal, with a narrower role proposal where one is defensible. Returns "
                "UNMEASURED when usage was never collected rather than implying nothing is "
                "unused. Read-only."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "principal": {"type": "string", "description": "Optional principal name or object id."},
                },
            },
            kind="read",
            handler=_make_unused_permissions(tenant_id),
        ),
        ConnectorTool(
            name="simulate_revoke",
            description=(
                "Model removing one role assignment over the cached snapshot: who loses access, "
                "who is left with none at all, and whether the revocation actually changes "
                "anything. Changes NOTHING in Azure — this is a what-if. Read-only."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "assignment_id": {"type": "string", "description": "Full role assignment resource id."},
                },
                "required": ["assignment_id"],
            },
            kind="read",
            handler=_make_simulate_revoke(tenant_id),
        ),
        ConnectorTool(
            name="access_changed_since",
            description=(
                "Summarize authorization changes since the previous cached scan — new access, "
                "widened scope, escalated privilege. Says so when there is no baseline to "
                "compare against instead of reporting no changes. Read-only."
            ),
            parameters={"type": "object", "properties": {}},
            kind="read",
            handler=_make_access_changed_since(tenant_id),
        ),
        ConnectorTool(
            name="who_can_reach_resource",
            description=(
                "List everyone who can reach ONE Azure resource, including access inherited "
                "from its resource group, subscription and management groups, plus whether the "
                "resource can be reached WITHOUT a role assignment (shared keys, local auth). "
                "Read-only."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "resource_id": {"type": "string", "description": "Full ARM resource id."},
                },
                "required": ["resource_id"],
            },
            kind="read",
            handler=_make_who_can_reach_resource(tenant_id),
        ),
    ]


def register_iam_tools(toolset, *, tenant_id: str) -> None:
    """Add the IAM tools to a connector toolset when enabled (mirrors register_profiler_tool)."""
    from app.core.app_settings import load_settings

    s = load_settings()
    if not bool(s.get("iam_tools_enabled", True)):
        return
    try:
        toolset.add_connector({"tenant_id": tenant_id}, build_iam_tools(tenant_id))
    except Exception:  # noqa: BLE001 - never let tool registration break a turn
        pass
