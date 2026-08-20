"""Read-only Entra identity agent tools — the dossier and the CA verdict, from cache.

Companion to :mod:`app.iam.agent_tool`, which already exposes the RBAC half (``who_can_access``,
``can_principal_do``, ``escalation_paths_to`` and seven more). This module deliberately adds
only what has no equivalent there:

  identity_investigate     one principal, everything already collected, with provenance
  ca_evaluate              what happens when this person signs in here, from this device
  identity_group_members   who is in a group, and which groups a principal belongs to
  ca_policies_for_app      which policies actually reach an application, and are they enforced
  identity_findings        the signals that fired against one principal

Two rules this module exists to honour, and which are the reason the handlers do not simply
call the HTTP routes:

* **Permission parity.** A question answered in chat must be as permissioned as the same
  question answered by clicking. ``investigate.activity`` is granted separately because
  reading a named individual's behavioral history is a different act from reading their
  access, and the agent must not become the way around that.
* **No paraphrasing.** Each handler returns the engine's own structure. The moment a handler
  summarizes, provenance and the four resolution states collapse into prose and every
  guarantee the screens make is lost.
"""
from __future__ import annotations

import json
from typing import Any

from app.connectors.base import ConnectorTool, err, ok

# Per-tool switches. The two that answer the questions people actually ask are on by default;
# the rest are opt-in because the combined Azure + Graph catalog is already large enough to
# be trimmed for request size (see app/agent/github_copilot.py), and every added tool costs
# every turn.
TOOL_DEFAULTS: dict[str, bool] = {
    "identity_investigate": True,
    "ca_evaluate": True,
    "identity_group_members": False,
    "ca_policies_for_app": False,
    "identity_findings": False,
}


def _enabled(settings: dict[str, Any], name: str) -> bool:
    configured = settings.get("entra_identity_tools") or {}
    if isinstance(configured, dict) and name in configured:
        return bool(configured[name])
    return TOOL_DEFAULTS.get(name, False)


# Raw Microsoft Graph tools (from the EntraID MCP server) that return a named individual's
# BEHAVIORAL history. Gating `identity_investigate` behind `investigate.activity` while these
# stay open to the same caller is theatre: the agent just calls these instead and assembles the
# same answer with a thinner audit trail. They are withheld together or the split is not real.
BEHAVIOURAL_GRAPH_TOOLS: frozenset[str] = frozenset({
    "get_user_sign_ins",
    "get_user_audit_logs",
})


def behavioural_graph_tools_blocked(principal: Any) -> frozenset[str]:
    """Graph tool names to withhold from this caller.

    Empty when the caller holds ``investigate.activity``, or when an admin has deliberately
    opted back into the raw tools for everyone.
    """
    from app.core.app_settings import load_settings

    if bool(load_settings().get("entra_mcp_behavioural_tools_enabled", False)):
        return frozenset()
    if _allowed(principal, "investigate.activity"):
        return frozenset()
    return BEHAVIOURAL_GRAPH_TOOLS


def _allowed(principal: Any, permission: str) -> bool:
    """Same test ``require_permission`` applies, without the FastAPI dependency."""
    from app.auth.permissions import accepted_permission_keys

    if principal is None:
        return False
    if getattr(principal, "is_admin", False):
        return True
    return any(principal.has(p) for p in accepted_permission_keys(permission))


def _json(payload: Any, summary: str) -> dict[str, Any]:
    return ok(json.dumps(payload, default=str), summary)


# --------------------------------------------------------------------------- audit
async def _audit(principal: Any, action: str, target: str, meta: dict[str, Any]) -> None:
    """Write the SAME audit row the HTTP route writes, marked as chat-originated.

    Without this a chat investigation is invisible to the "who has been looking at whom"
    record that protects both the investigator and the person investigated — and the
    "recently investigated" strip, which reads back from this log, would silently omit it.
    """
    from app.core.db import SessionLocal
    from app.models import AuditLog

    try:
        async with SessionLocal() as db:
            db.add(AuditLog(
                tenant_id=getattr(principal, "tenant_id", ""),
                actor_id=getattr(principal, "subject", ""),
                action=action,
                target=target,
                metadata_json={**meta, "via": "chat"},
            ))
            await db.commit()
    except Exception:  # noqa: BLE001 - a failed audit write must not break the turn
        pass


# --------------------------------------------------------------------------- helpers
def _snapshot(tenant_id: str) -> dict[str, Any]:
    from app.entra import snapshot as snapshot_mod

    return snapshot_mod.analyze(tenant_id)


# --------------------------------------------------------------------------- tools
def _make_identity_investigate(tenant_id: str, principal: Any, connection_id: str = ""):
    async def _handler(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        import asyncio

        from app.entra import investigate

        needle = str(args.get("principal") or "").strip()
        if not needle:
            return err("A principal is required (object id, UPN or appId).")
        if not _allowed(principal, "investigate.read"):
            return err("You do not have the 'investigate.read' permission.")

        snapshot = await asyncio.to_thread(_snapshot, tenant_id)
        env, sections = await investigate.build_dossier(snapshot, tenant_id, needle)
        subject = env["principal"]

        wants_activity = bool(args.get("include_activity"))
        activity_note = ""
        if wants_activity and not _allowed(principal, "investigate.activity"):
            # Answered, not thrown. And NOT silently dropped: a reader who asked for
            # behavioral history and got a dossier without it would read the absence as
            # "nothing happened".
            activity_note = (
                "Behavioral history was requested but not returned: it needs the "
                "'investigate.activity' permission, which is granted separately from "
                "'investigate.read' because reading a named person's sign-in and audit "
                "history is a different act from reading their access."
            )

        await _audit(principal, "investigate.view", str(subject.get("id") or needle),
                     {"kind": subject.get("kind"), "resolution": subject.get("resolution"),
                      "name": subject.get("display_name") or "",
                      # REQUIRED. `investigate.recent_entries` drops any row whose
                      # `connection_id` does not match the caller's, so omitting it here
                      # made a chat investigation invisible in the "recently investigated"
                      # strip — the one place the operator sees what the agent looked at.
                      "connection_id": connection_id})

        payload = {
            "principal": subject,
            "capabilities": env.get("capabilities"),
            "notes": env.get("notes"),
            "sections": sections,
            "activity": None,
            "activity_note": activity_note,
            "how_to_read": (
                "`resolution` distinguishes resolved / deleted / cross_tenant / unreadable / "
                "not_found — a deleted principal whose assignments survived is an ANSWER, not "
                "an error. Every section carries `provenance`; when `unreadable` is true the "
                "section could not be read, which is the opposite of it being empty. "
                "In access, every row has `assignment_kind`: `active` means the role is held "
                "now; `eligible` means PIM eligibility that must still be ACTIVATED and is "
                "NOT standing access — never call it permanent or standing. On an eligible "
                "row `eligibility_permanent` only means the eligibility does not lapse. "
                "`directory_roles` is 'privileged by any path'; use `directory_roles_active` "
                "and `directory_roles_eligible_only` to say whether a role is actually held."
            ),
        }
        name = subject.get("display_name") or subject.get("id") or needle
        return _json(payload, f"Investigated {name}")

    return _handler


def _make_ca_evaluate(tenant_id: str, principal: Any, connection_id: str = ""):
    async def _handler(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        import asyncio

        from app.entra import ca_simulator as sim

        if not _allowed(principal, "investigate.read"):
            return err("You do not have the 'investigate.read' permission.")
        who = str(args.get("principal") or "").strip()
        if not who:
            return err("A principal is required.")

        snapshot = await asyncio.to_thread(_snapshot, tenant_id)
        data = snapshot.get("data") or {}
        # `_ca_analysis`, NOT `_analysis`, and its `policies` rather than the raw collector
        # output under `data["ca"]`. The analyzed set is what carries `effective_ids`,
        # `app_classes` and `is_enforced` — everything `matches()` needs. Reading the raw set
        # made every policy fail to match, so the tool answered "granted, no policy applies"
        # for a Global Administrator in a tenant with 37 policies. A confidently wrong answer
        # to the exact question the tool exists to answer.
        ca_analysis = snapshot.get("_ca_analysis") or {}
        analysis = snapshot.get("_analysis") or {}

        principals = await asyncio.to_thread(sim.build_principals, data, analysis)
        needle = who.lower()
        matches = [p for p in principals
                   if needle in (p.label or "").lower() or needle == (p.id or "").lower()]
        if not matches:
            return err(f"No principal matching {who!r} in the collected directory.")
        if len(matches) > 1:
            listed = ", ".join(f"{p.label} ({p.id})" for p in matches[:5])
            return err(f"{who!r} matches {len(matches)} principals: {listed}. Use the object id.")
        subject = matches[0]

        policies = sim._prepare(ca_analysis.get("policies") or [])
        ctx = sim.SignInContext(
            key="chat",
            label="Asked in chat",
            client_app=str(args.get("client_app") or "browser"),
            platform=str(args.get("platform") or "windows"),
            location=str(args.get("location") or "untrusted"),
            device_compliant=bool(args.get("device_compliant")),
            device_hybrid_joined=bool(args.get("device_hybrid_joined")),
            app_class=str(args.get("app_class") or "all_cloud_apps"),
        )
        verdict = sim.evaluate(policies, subject, ctx)

        await _audit(principal, "investigate.view", subject.id,
                     {"kind": "ca_evaluate", "name": subject.label,
                      "connection_id": connection_id})

        payload = {
            "principal": {"id": subject.id, "label": subject.label},
            "context": {
                "client_app": ctx.client_app, "platform": ctx.platform,
                "location": ctx.location, "device_compliant": ctx.device_compliant,
                "device_hybrid_joined": ctx.device_hybrid_joined, "app_class": ctx.app_class,
            },
            "verdict": verdict,
            "limitations": list(sim.LIMITATIONS),
            "how_to_read": (
                "`verdict` answers whether the sign-in proceeds. `verdict.session` answers "
                "what the session may then DO — they are separate questions. "
                "`session.egress_restricted` is the one that answers 'can they download the "
                "file': it is true only when application-enforced restrictions or a cloud "
                "app security proxy applies. A sign-in frequency does NOT restrict download."
            ),
        }
        return _json(payload, f"Conditional Access verdict for {subject.label}")

    return _handler


def _make_identity_group_members(tenant_id: str, principal: Any, connection: dict[str, Any] | None):
    async def _handler(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        import asyncio

        from app.entra import investigate, investigate_members

        if not _allowed(principal, "investigate.read"):
            return err("You do not have the 'investigate.read' permission.")
        needle = str(args.get("group") or args.get("principal") or "").strip()
        if not needle:
            return err("A group or principal is required (object id, name or UPN).")

        snapshot = await asyncio.to_thread(_snapshot, tenant_id)
        subject = await investigate.resolve(snapshot.get("data") or {}, tenant_id, needle)
        direction = "up" if str(args.get("direction") or "down") == "up" else "down"
        kind = str(subject.get("kind") or "")
        # Downward needs a group; upward works for anything that can be IN one, which is how
        # "which groups is Alice in" gets answered at all.
        if direction == "down" and kind != investigate.KIND_GROUP:
            return err(f"{subject.get('display_name') or needle} is a "
                       f"{kind or 'principal'}, not a group — only groups have members. "
                       "Ask with direction='up' for the groups it belongs to.")
        if direction == "up" and subject.get("resolution") != investigate.RESOLVED:
            return err(f"{needle} could not be resolved in this directory, so the groups it "
                       "belongs to cannot be read. That is not a claim that it belongs to none.")

        gid = str(subject.get("id") or needle)
        result = await investigate_members.expand(
            connection, gid, expand_ids=list(args.get("expand") or []), direction=direction,
            root_kind=kind or investigate_members.TYPE_GROUP,
            transitive=bool(args.get("transitive")) and direction == "up")

        await _audit(principal, "investigate.members", gid,
                     {"direction": direction, "name": subject.get("display_name") or ""})

        return _json(
            {"subject": {"id": gid, "name": subject.get("display_name"), "kind": kind},
             "direction": direction, **result,
             "how_to_read": (
                 "Downward these are DIRECT members, one level per branch; upward they are "
                 "the groups and directory roles this principal belongs to. A child with "
                 "`expandable: true` is a nested group — pass its id in `expand` to open it. "
                 "A branch listed in `notes` with no children was unreadable, not empty."
             )},
            f"Membership of {subject.get('display_name') or gid}")

    return _handler


def _make_ca_policies_for_app(tenant_id: str, principal: Any):
    async def _handler(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        import asyncio

        if not _allowed(principal, "investigate.read"):
            return err("You do not have the 'investigate.read' permission.")
        app = str(args.get("app") or "").strip().lower()

        snapshot = await asyncio.to_thread(_snapshot, tenant_id)
        # The analyzed set, for the same reason `ca_evaluate` uses it: the raw collector rows
        # lack the resolved targeting this filter reads.
        policies = (snapshot.get("_ca_analysis") or {}).get("policies") or []
        out = []
        for p in policies:
            conds = p.get("conditions") or {}
            targets = [str(x).lower() for x in (conds.get("include_apps") or [])]
            hit = (not app) or any(app in t for t in targets) or "all" in targets
            if not hit:
                continue
            out.append({
                "id": p.get("id"), "display_name": p.get("display_name"),
                "state": p.get("state"),
                # `is_enforced` is the analyzed verdict and already accounts for report-only;
                # deriving it from `state` here would disagree with every other screen.
                "enforced": bool(p.get("is_enforced")),
                "report_only": bool(p.get("is_report_only")),
                "app_classes": p.get("app_classes") or [],
                "include_apps": conds.get("include_apps") or [],
                "exclude_apps": conds.get("exclude_apps") or [],
                "grant": p.get("grant"),
                "session": p.get("session"),
            })
        return _json(
            {"app": args.get("app") or "(any)", "policies": out, "count": len(out),
             "how_to_read": (
                 "`enforced: false` means the policy is disabled or report-only and blocks "
                 "nothing. 'All cloud apps' policies are included because they govern this "
                 "app too."
             )},
            f"{len(out)} policy(ies) reach {args.get('app') or 'any app'}")

    return _handler


def _make_identity_findings(tenant_id: str, principal: Any):
    async def _handler(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        import asyncio

        from app.entra import investigate

        if not _allowed(principal, "investigate.read"):
            return err("You do not have the 'investigate.read' permission.")
        needle = str(args.get("principal") or "").strip()
        if not needle:
            return err("A principal is required.")

        snapshot = await asyncio.to_thread(_snapshot, tenant_id)
        subject = await investigate.resolve(snapshot.get("data") or {}, tenant_id, needle)
        sid = str(subject.get("id") or needle)
        findings = [f for f in ((snapshot.get("_analysis") or {}).get("findings") or [])
                    if str(f.get("object_id") or "") == sid]
        return _json(
            {"principal": {"id": sid, "name": subject.get("display_name")},
             "findings": findings, "count": len(findings)},
            f"{len(findings)} finding(s) against {subject.get('display_name') or sid}")

    return _handler


# --------------------------------------------------------------------------- registry
def build_entra_identity_tools(
    tenant_id: str, principal: Any, connection: dict[str, Any] | None,
) -> list[ConnectorTool]:
    from app.core.app_settings import load_settings

    s = load_settings()
    # The audit rows the recents strip reads are filtered by this; see `_make_identity_investigate`.
    connection_id = str((connection or {}).get("id") or "")
    specs: list[tuple[str, str, dict[str, Any], Any]] = [
        (
            "identity_investigate",
            "Everything already known about ONE identity (user, guest, group, service "
            "principal or managed identity): its Azure and directory access, the signals "
            "that fired against it, how its access changed over time, its privilege "
            "activations, and for a group its members. Reads caches only. Use this instead "
            "of assembling an answer from raw Microsoft Graph calls.",
            {
                "type": "object",
                "properties": {
                    "principal": {"type": "string",
                                  "description": "Object id, UPN, mail or appId."},
                    "include_activity": {"type": "boolean",
                                         "description": "Ask for sign-in/audit history. "
                                                        "Requires the investigate.activity "
                                                        "permission."},
                },
                "required": ["principal"],
            },
            _make_identity_investigate(tenant_id, principal, connection_id),
        ),
        (
            "ca_evaluate",
            "What Conditional Access does to a specific sign-in: which policies apply, "
            "whether it is blocked or challenged, and — separately — what the SESSION is "
            "then allowed to do. Answers questions like 'can someone reach SharePoint from "
            "an unmanaged device, and can they download files?'",
            {
                "type": "object",
                "properties": {
                    "principal": {"type": "string"},
                    "app_class": {"type": "string",
                                  "description": "Application class, e.g. collaboration_content "
                                                 "for SharePoint/OneDrive/Exchange, "
                                                 "admin_planes, all_cloud_apps (default)."},
                    "client_app": {"type": "string",
                                   "enum": ["browser", "mobileAppsAndDesktopClients",
                                            "exchangeActiveSync", "other"]},
                    "platform": {"type": "string"},
                    "location": {"type": "string", "enum": ["trusted", "untrusted", "unknown"]},
                    "device_compliant": {"type": "boolean"},
                    "device_hybrid_joined": {"type": "boolean"},
                },
                "required": ["principal"],
            },
            _make_ca_evaluate(tenant_id, principal, connection_id),
        ),
        (
            "identity_group_members",
            "Direct members of a group, one level at a time, keeping nested groups as "
            "openable nodes. Also works upward with direction='up': which groups and "
            "directory roles a user, guest, group or workload identity belongs to.",
            {
                "type": "object",
                "properties": {
                    "group": {"type": "string",
                              "description": "Object id, name or UPN. With direction='up' "
                                             "this may be any principal, not just a group."},
                    "direction": {"type": "string", "enum": ["down", "up"]},
                    "transitive": {"type": "boolean",
                                   "description": "Upward only: include groups reached "
                                                  "through nesting, not just direct ones."},
                    "expand": {"type": "array", "items": {"type": "string"},
                               "description": "Nested group ids to open in the same call."},
                },
                "required": ["group"],
            },
            _make_identity_group_members(tenant_id, principal, connection),
        ),
        (
            "ca_policies_for_app",
            "Which Conditional Access policies reach a given application, and whether they "
            "are actually enforced (a disabled or report-only policy blocks nothing).",
            {
                "type": "object",
                "properties": {"app": {"type": "string"}},
            },
            _make_ca_policies_for_app(tenant_id, principal),
        ),
        (
            "identity_findings",
            "The Entra posture signals that fired against one identity.",
            {
                "type": "object",
                "properties": {"principal": {"type": "string"}},
                "required": ["principal"],
            },
            _make_identity_findings(tenant_id, principal),
        ),
    ]
    return [
        ConnectorTool(name=n, description=d, parameters=p, kind="read", handler=h)
        for n, d, p, h in specs
        if _enabled(s, n)
    ]


def register_entra_identity_tools(
    toolset, *, tenant_id: str, principal: Any, connection: dict[str, Any] | None = None,
) -> None:
    """Add the Entra identity tools to a connector toolset (mirrors register_iam_tools)."""
    from app.core.app_settings import load_settings

    s = load_settings()
    if not bool(s.get("entra_identity_tools_enabled", True)):
        return
    try:
        tools = build_entra_identity_tools(tenant_id, principal, connection)
        if tools:
            toolset.add_connector({"tenant_id": tenant_id}, tools)
    except Exception:  # noqa: BLE001 - never let tool registration break a turn
        pass
