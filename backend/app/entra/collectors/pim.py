"""Privileged Identity Management collector — policy health, activation history, PIM for Groups.

Splits deliberately from ``collectors/roles.py``:

* ``roles`` owns definitions, active assignments and **eligibility schedules**, because the
  Conditional Access engine needs eligible role holders to resolve a role-scoped policy.
* ``pim`` (this module) owns everything that only the privileged-access screens need:
  ``roleManagementPolicies`` (the configuration health grid), ``roleAssignmentScheduleRequests``
  (activation history) and PIM for Groups.

``roleManagementPolicies`` is the highest-value dataset in the product that the older
identity module never collected: approval-required, MFA-on-activation, justification,
maximum duration and notification recipients all live there, and nothing else exposes them.

Everything is P2-gated. On a tenant without P2 Graph answers with a **400 carrying a license
message** (not a 403), so the domain degrades to ``unlicensed`` rather than ``error``.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.entra import model
from app.entra.collectors import CollectContext, as_dict, as_list, clip, guarded
from app.entra.collectors.roles import _is_licence_error, tier_of
from app.entra.graphclient import GraphClient, GraphError, GraphPermissionError

log = logging.getLogger("app.entra.collectors.pim")

DOMAIN = "pim"

# The scope that unlocks activation history. Shared with the activations collector, which is
# why the same missing permission must deduplicate to one row rather than two sentences.
ACTIVATION_SCOPE = "RoleAssignmentSchedule.Read.Directory"

# Rule ids ending _EndUser_Assignment govern ACTIVATION (what a user must do to turn the
# role on). The _Admin_* variants govern what an administrator may configure, which is a
# different question and deliberately not what the health grid reports.
_ACTIVATION_SUFFIX = "_EndUser_Assignment"

_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def parse_duration_hours(value: str) -> float | None:
    """ISO-8601 duration -> hours (``PT8H`` -> 8.0). Returns None when unparseable."""
    if not value:
        return None
    m = _DURATION_RE.match(str(value).strip())
    if not m:
        return None
    parts = {k: int(v) for k, v in m.groupdict(default="0").items()}
    total = parts["days"] * 24 + parts["hours"] + parts["minutes"] / 60 + parts["seconds"] / 3600
    return round(total, 3) if total else 0.0


def _decode_rules(rules: list[Any]) -> dict[str, Any]:
    """Turn a policy's rule array into the six controls the health grid reports."""
    health: dict[str, Any] = {
        "approval_required": False,
        "approver_count": 0,
        "mfa_on_activation": False,
        "auth_context_required": False,
        "auth_context_value": "",
        "justification_required": False,
        "ticket_required": False,
        "max_activation_hours": None,
        "eligibility_expires": None,
        "assignment_expires": None,
        "notification_recipients": 0,
        "rules_seen": 0,
    }
    for raw in rules or []:
        rule = as_dict(raw)
        rid = str(rule.get("id") or "")
        odata = str(rule.get("@odata.type") or "")
        health["rules_seen"] += 1

        if "ApprovalRule" in odata and rid.endswith(_ACTIVATION_SUFFIX):
            setting = as_dict(rule.get("setting"))
            health["approval_required"] = bool(setting.get("isApprovalRequired"))
            approvers = 0
            for stage in as_list(setting.get("approvalStages")):
                approvers += len(as_list(as_dict(stage).get("primaryApprovers")))
            health["approver_count"] = approvers

        elif "AuthenticationContextRule" in odata and rid.endswith(_ACTIVATION_SUFFIX):
            health["auth_context_required"] = bool(rule.get("isEnabled"))
            health["auth_context_value"] = str(rule.get("claimValue") or "")

        elif "EnablementRule" in odata and rid.endswith(_ACTIVATION_SUFFIX):
            enabled = {str(e) for e in as_list(rule.get("enabledRules"))}
            health["mfa_on_activation"] = "MultiFactorAuthentication" in enabled
            health["justification_required"] = "Justification" in enabled
            health["ticket_required"] = "Ticketing" in enabled

        elif "ExpirationRule" in odata:
            required = bool(rule.get("isExpirationRequired"))
            hours = parse_duration_hours(str(rule.get("maximumDuration") or ""))
            if rid.endswith(_ACTIVATION_SUFFIX):
                health["max_activation_hours"] = hours
            elif rid.endswith("_Admin_Eligibility"):
                health["eligibility_expires"] = required
            elif rid.endswith("_Admin_Assignment"):
                health["assignment_expires"] = required

        elif "NotificationRule" in odata and rid.endswith(_ACTIVATION_SUFFIX):
            health["notification_recipients"] += len(as_list(rule.get("notificationRecipients")))

    return health


def _score_health(h: dict[str, Any], max_hours: float) -> tuple[int, list[str]]:
    """0-100 configuration score plus the list of failed controls."""
    checks = [
        ("mfa_on_activation", bool(h.get("mfa_on_activation") or h.get("auth_context_required"))),
        ("approval_required", bool(h.get("approval_required"))),
        ("justification_required", bool(h.get("justification_required"))),
        ("duration_bounded", h.get("max_activation_hours") is not None
         and h["max_activation_hours"] <= max_hours),
        ("eligibility_expires", h.get("eligibility_expires") is not False),
        ("notifications", h.get("notification_recipients", 0) > 0),
    ]
    failed = [name for name, ok in checks if not ok]
    score = round(100 * (len(checks) - len(failed)) / len(checks))
    return score, failed


async def collect(client: GraphClient, ctx: CollectContext) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        notes: list[str] = []
        blockers: list[dict[str, Any]] = []
        licensed = True

        # --- role management policies (the configuration health grid) --------------
        policies: list[dict[str, Any]] = []
        policies_available = False
        await ctx.say("info", "PIM: reading role management policies…")
        try:
            assignments, _ = await client.get_all(
                "/policies/roleManagementPolicyAssignments",
                filter="scopeId eq '/' and scopeType eq 'DirectoryRole'",
                expand="policy($expand=rules)",
                top=0,
            )
            policies_available = True
            for raw in assignments:
                row = as_dict(raw)
                policy = as_dict(row.get("policy"))
                health = _decode_rules(as_list(policy.get("rules")))
                score, failed = _score_health(health, ctx.max_activation_hours)
                policies.append({
                    "role_id": str(row.get("roleDefinitionId") or ""),
                    "policy_id": str(row.get("policyId") or policy.get("id") or ""),
                    "scope_id": str(row.get("scopeId") or "/"),
                    **health,
                    "score": score,
                    "failed_controls": failed,
                })
            await ctx.say("ok", f"PIM: {len(policies)} role management policy/policies")
        except GraphPermissionError as exc:
            notes.append("Role management policies not permitted "
                         f"(needs RoleManagementPolicy.Read.Directory): {clip(exc.message, 110)}")
        except GraphError as exc:
            if _is_licence_error(exc):
                licensed = False
                notes.append("Role management policies unavailable: this tenant is not licensed "
                             "for Entra ID P2 / ID Governance.")
            else:
                notes.append(f"Role management policies: {clip(exc, 150)}")

        # --- activation history ------------------------------------------------------
        activations: list[dict[str, Any]] = []
        activations_available = False
        try:
            requests, trunc = await client.get_all(
                "/roleManagement/directory/roleAssignmentScheduleRequests",
                select=["id", "action", "principalId", "roleDefinitionId", "justification",
                        "ticketInfo", "createdDateTime", "status", "scheduleInfo", "approvalId"],
                top=0, max_items=5000,
            )
            activations_available = True
            for raw in requests:
                req = as_dict(raw)
                info = as_dict(req.get("scheduleInfo"))
                expiration = as_dict(info.get("expiration"))
                ticket = as_dict(req.get("ticketInfo"))
                activations.append({
                    "id": str(req.get("id") or ""),
                    "action": str(req.get("action") or ""),
                    "principal_id": str(req.get("principalId") or ""),
                    "role_id": str(req.get("roleDefinitionId") or ""),
                    "justification": str(req.get("justification") or ""),
                    "ticket_number": str(ticket.get("ticketNumber") or ""),
                    "ticket_system": str(ticket.get("ticketSystem") or ""),
                    "created_at": str(req.get("createdDateTime") or ""),
                    "status": str(req.get("status") or ""),
                    "duration_hours": parse_duration_hours(str(expiration.get("duration") or "")),
                    "approval_id": str(req.get("approvalId") or ""),
                })
            if trunc:
                notes.append("Activation history was capped at 5,000 requests.")
            await ctx.say("ok", f"PIM: {len(activations)} assignment request(s)")
        except GraphPermissionError as exc:
            # Graph names WRITE scopes here (RoleAssignmentSchedule.ReadWrite.Directory,
            # RoleManagement.ReadWrite.Directory, RoleAssignmentSchedule.Remove.Directory) —
            # there is no read-only scope that opens this collection to an app-only token.
            # A read-only product does not ask for directory write access, so this is
            # reported as a source limit, not as something the operator should go and grant.
            # The Activations tab reads the same facts from the PIM audit log instead.
            notes.append(
                "Activation history is not available to a read-only connection: Microsoft "
                "only exposes this collection to write scopes. The Activations tab reads "
                "the same activations from the PIM audit log.")
            log.debug("pim activation history denied: %s", exc.message[:200])
        except GraphError as exc:
            if _is_licence_error(exc):
                licensed = False
                notes.append("Activation history unavailable: this tenant is not licensed for "
                             "Entra ID P2 / ID Governance.")
                blockers.append(model.blocker(
                    model.BLOCKER_LICENCE,
                    "Activation history requires Entra ID P2 or ID Governance.",
                    scope="Entra ID P2 / ID Governance",
                    impact="PIM activity cannot be reviewed on this tenant.",
                ))
            else:
                notes.append(f"Activation history: {clip(exc, 150)}")

        # --- PIM for Groups -----------------------------------------------------------
        # This collection CANNOT be enumerated tenant-wide: without a groupId or principalId
        # filter Graph answers 400 "The required parameters GroupId or PrincipalId is
        # missing." So we ask per role-assignable group, which is both the bounded set and
        # the only set that matters — membership of one of those groups IS a directory role.
        group_eligibilities: list[dict[str, Any]] = []
        groups_available = False
        assignable: list[dict[str, Any]] = []
        try:
            raw_groups, _ = await client.get_all(
                "/groups",
                filter="isAssignableToRole eq true",
                select=["id", "displayName"],
                top=999, max_items=2000, advanced=True,
            )
            assignable = [as_dict(g) for g in raw_groups]
        except GraphError as exc:
            notes.append(f"Role-assignable groups could not be listed for PIM for Groups: "
                         f"{clip(exc, 120)}")

        if assignable:
            await ctx.say("info", f"PIM: checking {len(assignable)} role-assignable group(s) "
                                  "for managed membership…")
            failures = 0
            for group in assignable:
                gid = str(group.get("id") or "")
                if not gid:
                    continue
                try:
                    rows, _ = await client.get_all(
                        "/identityGovernance/privilegedAccess/group/eligibilitySchedules",
                        filter=f"groupId eq '{gid}'", top=0, max_items=500,
                    )
                except GraphPermissionError as exc:
                    notes.append("PIM for Groups not permitted (needs "
                                 f"PrivilegedAccess.Read.AzureADGroup): {clip(exc.message, 90)}")
                    break
                except GraphError as exc:
                    if _is_licence_error(exc):
                        licensed = False
                        notes.append("PIM for Groups unavailable: this tenant is not licensed "
                                     "for Entra ID P2 / ID Governance.")
                        break
                    failures += 1
                    continue
                groups_available = True
                for raw in rows:
                    row = as_dict(raw)
                    group_eligibilities.append({
                        "id": str(row.get("id") or ""),
                        "group_id": gid,
                        "group_name": str(group.get("displayName") or ""),
                        "principal_id": str(row.get("principalId") or ""),
                        "access_id": str(row.get("accessId") or ""),
                        "member_type": str(row.get("memberType") or ""),
                        "status": str(row.get("status") or ""),
                    })
            if failures:
                notes.append(f"{failures} role-assignable group(s) could not be checked for "
                             "PIM-managed membership.")
            if groups_available:
                await ctx.say("ok", f"PIM: {len(group_eligibilities)} group eligibility/"
                                    f"eligibilities across {len(assignable)} assignable group(s)")
        elif not notes or "Role-assignable groups" not in notes[-1]:
            notes.append("No role-assignable groups exist, so PIM for Groups has nothing to "
                         "manage.")
            groups_available = True

        data = {
            "policies": policies,
            "activations": activations,
            "group_eligibilities": group_eligibilities,
            "capabilities": {
                "policies": policies_available,
                "activations": activations_available,
                "group_pim": groups_available,
                "licensed": licensed,
            },
            "counts": {
                "policies": len(policies),
                "activations": len(activations),
                "self_activations": sum(1 for a in activations if a["action"] == "selfActivate"),
                "group_eligibilities": len(group_eligibilities),
                "managed_group_ids": len({g["group_id"] for g in group_eligibilities if g["group_id"]}),
            },
        }
        if not licensed and not (policies or activations or group_eligibilities):
            return model.unlicensed_payload(
                DOMAIN,
                "Privileged Identity Management requires Entra ID P2 or Entra ID Governance.",
            ) | {"data": data, "notes": notes}
        status = model.STATUS_PARTIAL if notes else model.STATUS_OK
        return model.domain_payload(
            DOMAIN, data, status=status,
            item_count=len(policies) + len(activations), notes=notes, blockers=blockers,
        )

    return await guarded(DOMAIN, ctx, _run)


# --------------------------------------------------------------------------- helpers
def policy_for_role(pim_data: dict[str, Any], role_id: str) -> dict[str, Any] | None:
    return next((p for p in pim_data.get("policies") or [] if p.get("role_id") == role_id), None)


def last_activation(pim_data: dict[str, Any], principal_id: str, role_id: str) -> str:
    """Most recent successful activation timestamp for a (principal, role) pair."""
    stamps = [
        a["created_at"] for a in pim_data.get("activations") or []
        if a.get("principal_id") == principal_id and a.get("role_id") == role_id
        and a.get("created_at") and str(a.get("status") or "").lower() in ("provisioned", "granted", "")
    ]
    return max(stamps) if stamps else ""


def privileged_policies(pim_data: dict[str, Any], roles_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Policy health rows joined to role names, restricted to privileged roles."""
    defs = {d.get("id"): d for d in roles_data.get("definitions") or []}
    out = []
    for p in pim_data.get("policies") or []:
        definition = defs.get(p.get("role_id")) or {}
        name = str(definition.get("display_name") or "")
        if not definition or not definition.get("privileged", tier_of(name) != "tier2"):
            continue
        out.append({**p, "role_name": name, "role_tier": definition.get("tier", tier_of(name))})
    return sorted(out, key=lambda r: (r["score"], r["role_name"]))
