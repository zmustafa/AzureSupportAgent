"""Remediation artifacts — generated, never executed.

The product does not write to Azure. It emits scripts a human reads, understands and runs. That
constraint is what makes the following non-negotiable rather than nice-to-have:

**Every artifact has a rollback.** A revoke script without the matching create is not shippable.
The operator who runs one of these at 3am needs the undo in the same file, not in a docs page.

**Ordering is explained, not implied.** Removing a group membership before revoking the group's
assignment leaves a different end state than the reverse. The bundle is ordered and each step
says why it sits where it does.

**`breaksIf` travels with the command.** "Disable shared key auth" without "this breaks every
client using a connection string" is how a read-only tool causes an outage.

**Nothing is stored.** Artifacts are generated on demand from the current decision set. A stored
script goes stale against a moving estate and becomes a hazard — the assignment id it references
may already belong to something else.

**No secrets, ever.** These scripts reference resources; they never carry a key, a token or a
connection string. `assert_no_secrets` is applied to every bundle before it leaves the module,
because the day someone adds a helpful `--connection-string` is the day this becomes a leak.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

from app.iam import schema

AZ_CLI = "az"
POWERSHELL = "powershell"
BICEP = "bicep"
TERRAFORM = "terraform"
FORMATS = (AZ_CLI, POWERSHELL, BICEP, TERRAFORM)

GENERATOR_VERSION = "iam-remediation/1"

# Anything matching these must never appear in generated output. The check is a tripwire on our
# own templates, not a sanitiser for user input — a hit means a template is wrong and the bundle
# is refused rather than scrubbed.
_SECRET_PATTERNS = (
    re.compile(r"connectionstring\s*=", re.I),
    re.compile(r"accountkey\s*=", re.I),
    re.compile(r"\bsharedaccesskey\b", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{20,}", re.I),
    re.compile(r"\bpassword\s*=", re.I),
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\."),  # a JWT
)


class SecretLeak(RuntimeError):
    """Raised when a generated artifact would contain a credential."""


def assert_no_secrets(text: str) -> None:
    for pat in _SECRET_PATTERNS:
        if pat.search(text):
            raise SecretLeak(f"generated artifact matched {pat.pattern!r} — refusing to emit")


# --------------------------------------------------------------------------- quoting
def _q(value: str) -> str:
    """Single-quote for shells and Bicep, escaping embedded quotes."""
    return "'" + str(value).replace("'", "''") + "'"


def _dq(value: str) -> str:
    """Double-quote for PowerShell and Terraform/JSON."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _tf_name(value: str) -> str:
    """A Terraform-legal identifier derived from an arbitrary id."""
    slug = re.sub(r"[^a-zA-Z0-9_]", "_", str(value)).strip("_") or "item"
    return ("r_" + slug)[:60]


def _sub_arg(row: dict[str, Any], fmt: str) -> str:
    """Pin a command to the subscription the row is in.

    Neither tool infers it. `az keyvault ...` resolves the resource group inside whatever
    subscription happens to be active, and Az cmdlets act on the current context. One script
    routinely spans several subscriptions, so "it worked when I tested it" means only that the
    first step happened to match the operator's default. Reported from a real run as
    "(ResourceGroupNotFound) Resource group '...' could not be found" — for a group that exists.

    Returns a fragment to APPEND for the CLI and one to PREPEND for PowerShell, or "" when the
    row has no subscription (a directory-scoped row, where there is nothing to target)."""
    sub = str(row.get("subscriptionId") or "")
    if not sub:
        return ""
    if fmt == AZ_CLI:
        return f" --subscription {_q(sub)}"
    if fmt == POWERSHELL:
        return f"Set-AzContext -Subscription {_dq(sub)} | Out-Null; "
    return ""


def short_role(role: str) -> str:
    """Collapse a Key Vault access-policy role name to something a script line can hold.

    The stored value is the actual grant — `Access Policy: keys(get,list,update,create,...)
    secrets(...) certificates(...)` — which on a real vault runs to several hundred characters.
    Printed verbatim in a step title and again in its `breaks if` line it produces two unreadable
    lines, and an operator who cannot read the step does not check it.

    Counts, not verbs. The full string stays in the export, which has no width limit. Mirrors
    `shortRole` in the UI so the screen and the script agree."""
    if not role.startswith("Access Policy"):
        return role
    families = [
        f"{name} {len([v for v in verbs.split(',') if v.strip()])}"
        for name, verbs in re.findall(r"(\w+)\(([^)]*)\)", role)
    ]
    return f"Access Policy ({', '.join(families)})" if families else "Access Policy"


# --------------------------------------------------------------------------- planes
# WHICH API ACTUALLY REMOVES THIS ACCESS. Not cosmetic: `az role assignment delete` only ever
# touches Azure RBAC assignments held DIRECTLY by the principal named in `--assignee`. Pointed at
# anything else it exits 0 having done nothing ("No matched assignments were found to delete") or
# fails looking up a role definition that does not exist in ARM ("Role 'Global Reader' doesn't
# exist"). Both were reported from a real run of a generated script.
#
# A command that silently matches nothing is WORSE than no command at all: the operator reads a
# clean exit, ticks the line off, and the access is still there.
PLANE_AZURE_RBAC = "azure_rbac"
PLANE_GROUP_MEMBERSHIP = "group_membership"
PLANE_ENTRA_ROLE = "entra_directory_role"
PLANE_SP_OWNER = "service_principal_owner"
PLANE_PIM_ELIGIBLE = "pim_eligible"
PLANE_KV_POLICY = "key_vault_access_policy"
PLANE_CLASSIC = "classic_admin"
PLANE_LIGHTHOUSE = "lighthouse"
PLANE_DENY = "deny_assignment"


def plane_of(row: dict[str, Any]) -> str:
    """Which removal API this row needs.

    Order matters. The access PATH is checked before the surface because it decides *what you
    remove*, not merely where: a directory role held through a group is fixed by removing the
    membership, not by touching the role assignment (which belongs to the group and is serving
    everyone else in it)."""
    if str(row.get("accessPath", "")) == schema.PATH_GROUP:
        return PLANE_GROUP_MEMBERSHIP
    if str(row.get("accessPath", "")) == schema.PATH_OWNER:
        return PLANE_SP_OWNER

    surface = str(row.get("surface", ""))
    if surface == schema.SURFACE_ENTRA:
        return PLANE_ENTRA_ROLE
    if surface == schema.SURFACE_KEY_VAULT:
        return PLANE_KV_POLICY
    if surface == schema.SURFACE_CLASSIC:
        return PLANE_CLASSIC
    if surface == schema.SURFACE_LIGHTHOUSE:
        return PLANE_LIGHTHOUSE
    if surface == schema.SURFACE_DENY:
        return PLANE_DENY
    if str(row.get("assignmentState", "")) == schema.STATE_ELIGIBLE:
        return PLANE_PIM_ELIGIBLE
    return PLANE_AZURE_RBAC


def _manual(reason: str, steps: str, comment: str = "#") -> str:
    """A step that has no safe command, rendered so it cannot be mistaken for one.

    Every line is commented out. Emitting a plausible-looking command for access that this API
    cannot remove is how an operator ends up believing a revocation happened."""
    body = "\n".join(f"{comment} {line}" for line in steps.strip().splitlines())
    return f"{comment} MANUAL STEP — no safe command exists. {reason}\n{body}"


# --------------------------------------------------------------------------- one action
def revoke_assignment(row: dict[str, Any], fmt: str) -> dict[str, Any]:
    """Revoke one grant, with its rollback, using the API that actually governs it.

    The rollback recreates the assignment from the *snapshot* — principal, role and scope — not
    from the assignment id, because a recreated assignment gets a new id. Someone restoring
    access needs the grant back, not the GUID."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}")

    plane = plane_of(row)
    if plane != PLANE_AZURE_RBAC:
        return _revoke_other_plane(row, fmt, plane)

    principal = str(row.get("effectivePrincipalId") or row.get("principalId") or "")
    role = str(row.get("roleName", ""))
    scope = str(row.get("scope", ""))
    assignment_id = str(row.get("assignmentId", ""))
    label = f"{row.get('effectivePrincipalName') or principal} — {role} @ {scope}"

    if fmt == AZ_CLI:
        dry = f"az role assignment list --assignee {_q(principal)} --scope {_q(scope)} -o table"
        cmd = f"az role assignment delete --assignee {_q(principal)} --role {_q(role)} --scope {_q(scope)}"
        back = f"az role assignment create --assignee {_q(principal)} --role {_q(role)} --scope {_q(scope)}"
    elif fmt == POWERSHELL:
        dry = f"Get-AzRoleAssignment -ObjectId {_dq(principal)} -Scope {_dq(scope)} | Format-Table"
        cmd = f"Remove-AzRoleAssignment -ObjectId {_dq(principal)} -RoleDefinitionName {_dq(role)} -Scope {_dq(scope)}"
        back = f"New-AzRoleAssignment -ObjectId {_dq(principal)} -RoleDefinitionName {_dq(role)} -Scope {_dq(scope)}"
    elif fmt == BICEP:
        # Bicep has no delete verb — a revocation is expressed by REMOVING the resource block from
        # the template that declares it. Emitting a fake "delete" resource would be a lie that
        # deploys cleanly and changes nothing, which is the worst possible outcome.
        dry = f"// Search your templates for a roleAssignment with scope {scope} and principalId {principal}."
        cmd = (
            "// Bicep is declarative: DELETE the resource block below from the template that\n"
            "// owns this assignment, then redeploy in Complete mode for the scope.\n"
            "// Incremental mode will NOT remove it.\n"
            f"//   principalId: {principal}\n//   roleDefinition: {role}\n//   scope: {scope}"
        )
        back = _bicep_assignment(principal, role, scope)
    elif fmt == TERRAFORM:
        dry = f"terraform state list | Select-String {_dq(_tf_name(assignment_id or principal))}"
        cmd = (
            f"# Remove the azurerm_role_assignment resource below from your configuration and apply.\n"
            f"# If it was created outside Terraform, import it first or the plan will show no change:\n"
            f"#   terraform import azurerm_role_assignment.{_tf_name(assignment_id or principal)} {assignment_id}"
        )
        back = _terraform_assignment(principal, role, scope, assignment_id)
    else:
        raise ValueError(f"unknown format {fmt!r}")

    return {
        "action": "revoke_assignment",
        "plane": PLANE_AZURE_RBAC,
        "label": label,
        "format": fmt,
        "dry_run": dry,
        "command": cmd,
        # Never optional. A revoke with no way back is not a remediation, it is an outage waiting
        # for a change-advisory board.
        "rollback": back,
        "breaks_if": (
            f"any automation authenticating as {row.get('effectivePrincipalName') or principal} and relying on "
            f"{role} at {scope} stops working immediately — check pipelines and scheduled jobs first"
        ),
        "order_hint": _order_hint(row),
    }


# --------------------------------------------------------------------------- the other planes
_GRAPH = "https://graph.microsoft.com/v1.0"


def _group_props_dry(group: str, fmt: str, comment: str, group_name: str, why: str) -> str:
    """The read that shows WHY a group refuses the removal, in the requested format.

    `isAssignableToRole` and `groupTypes` are only returned when asked for by name, which is why
    an operator staring at a bare 403 has no way to discover either of them."""
    props = "id,displayName,isAssignableToRole,groupTypes,onPremisesSyncEnabled"
    if fmt == AZ_CLI:
        return (
            f"az rest --method GET --url "
            f"{_q(f'{_GRAPH}/groups/{group}?$select={props}')}  # {why}"
        )
    if fmt == POWERSHELL:
        return (
            f"Invoke-MgGraphRequest -Method GET -Uri "
            f"{_q(f'{_GRAPH}/groups/{group}?$select={props}')}  # {why}"
        )
    return f"{comment} {group_name} ({group}): {why}."


def _revoke_group_membership(row: dict[str, Any], fmt: str) -> tuple[str, str, str, str]:
    """Remove the MEMBER from the GROUP — never the group's role assignment.

    Deleting the assignment would revoke the access for every other member of the group, most of
    whom are not being reviewed. The assignment is the group's; only the membership is this
    person's."""
    member = str(row.get("effectivePrincipalId") or "")
    source_gid = str(row.get("sourceGroupId") or row.get("principalId") or "")
    source_name = str(row.get("sourceGroupName") or source_gid)
    resolution = str(row.get("membershipGroupResolution") or "")
    role = short_role(str(row.get("roleName", "")))
    comment = "//" if fmt == BICEP else "#"

    # WHICH GROUP TO TARGET. The assignment belongs to `source_gid`, but the MEMBERSHIP may be in
    # a child of it, and `az ad group member remove` only ever deletes a direct membership.
    # Reported from a real run: "Resource '<group>' does not exist or one of its queried
    # reference-property objects are not present" — a 404 on a $ref that was never there,
    # because the person is a transitive member, not a direct one.
    group = str(row.get("membershipGroupId") or "")
    group_name = str(row.get("membershipGroupName") or "") or group
    if resolution in ("ambiguous", "unknown"):
        # No single removal is correct, so no single removal is emitted. Guessing one here is
        # how you get a script that exits 0, removes one of two memberships and leaves the
        # access in place — the exact failure this module exists to prevent.
        listed = (
            f"The member sits DIRECTLY in more than one group nested inside {source_name}:\n"
            f"  {row.get('membershipGroupName') or '(unnamed)'}\n"
            f"Removing only one of them leaves the access in place, so pick deliberately."
            if resolution == "ambiguous"
            else (
                f"Part of the nesting under {source_name} could not be expanded, so the group\n"
                f"holding this membership is not known. Any group named here would be a guess."
            )
        )
        step = _manual(
            f"The membership is not directly in {source_name}, and which group it IS in cannot "
            f"be stated from this scan.",
            f"{listed}\n"
            f"List the member's DIRECT groups and remove them from the right one:\n"
            f"  az rest --method GET --url "
            f"'{_GRAPH}/directoryObjects/{member}/memberOf?$select=id,displayName'\n"
            f"Do NOT delete the {source_name} {role} assignment instead — it serves every other "
            f"member.",
            comment,
        )
        dry = (
            f"az ad group member check --group {_q(source_gid)} --member-id {_q(member)} "
            f"--query value  # false: the membership is in a nested group, not this one"
        )
        back = _manual("Re-add the membership.", f"Add {member} back to the group it was in.", comment)
        return dry, step, back, source_name

    if not group:
        group, group_name = source_gid, source_name
    synced_state = str(row.get("membershipGroupOnPremSynced") or "")
    # What the step is TITLED. A bare child-group name in the label reads like the wrong row to
    # anyone who searched for the group that holds the assignment.
    display = f"{group_name} (nested in {source_name})" if resolution == "nested" else group_name

    # A DYNAMIC group has no membership to delete: it is recomputed from a rule every time the
    # rule or the member changes. Removing "the membership" is not a thing you can do.
    if str(row.get("membershipGroupDynamic") or "") == schema.ENABLED_TRUE:
        step = _manual(
            f"Group {group_name!r} has DYNAMIC membership. Its members are computed from a "
            f"membership rule, so there is no membership object to delete.",
            f"Change the rule so it no longer matches {member}, or remove the attribute the\n"
            f"rule keys on. Entra ID > Groups > {group_name} > Dynamic membership rules.\n"
            f"Editing the member list directly is not possible on this group at all.\n"
            f"Do NOT delete the group's {role} assignment instead — it serves every other member.",
            comment,
        )
        dry = _group_props_dry(group, fmt, comment, group_name, "membership is computed from a rule")
        back = _manual("Restore the rule.", f"Put the rule back so {member} matches again.", comment)
        return dry, step, back, display

    # A ROLE-ASSIGNABLE group is protected: Entra requires the CALLING APPLICATION to hold
    # `RoleManagement.ReadWrite.Directory` before it will let anyone change the membership.
    if str(row.get("membershipGroupRoleAssignable") or "") == schema.ENABLED_TRUE:
        # The sign-in is emitted ONCE, as a preamble at the top of the bundle (see
        # `_graph_signin_preamble`). Repeating a six-line connect block on every one of these
        # steps buries the single line that actually differs between them.
        removal = (
            f"Invoke-MgGraphRequest -Method DELETE -Uri "
            f"{_q(f'{_GRAPH}/groups/{group}/members/{member}/$ref')}"
        )
        if fmt == POWERSHELL:
            # The requested format can express it, so it stays runnable.
            dry = _group_props_dry(group, fmt, comment, group_name, "role-assignable: needs an extra permission")
            cmd = (
                f"# {group_name} is ROLE-ASSIGNABLE. Your directory role is not the blocker — the\n"
                f"# app you signed in with must hold RoleManagement.ReadWrite.Directory.\n"
                f"# Sign in first using the block at the top of this script.\n"
                f"{removal}"
            )
            back = (
                f"Invoke-MgGraphRequest -Method POST -Uri {_q(f'{_GRAPH}/groups/{group}/members/$ref')} "
                f"-Body @{{'@odata.id' = {_q(f'{_GRAPH}/directoryObjects/{member}')}}}"
            )
            return dry, cmd, back, display
        step = _manual(
            f"Group {group_name!r} is ROLE-ASSIGNABLE, and the Azure CLI cannot change the "
            f"membership of one — its Microsoft Graph token has no "
            f"RoleManagement.ReadWrite.Directory scope, which Entra requires for these groups.",
            f"This is a limit of the TOOL, not of your access: a Global Administrator gets the\n"
            f"same 403 Authorization_RequestDenied, and activating a PIM role does not change it.\n"
            f"Sign in with Microsoft Graph PowerShell using the block at the top of this\n"
            f"script — interactively, or as a service principal — then run:\n"
            f"  {removal}\n"
            f"Or remove the member in the portal: Entra ID > Groups > {group_name} > Members.\n"
            f"Do NOT delete the group's {role} assignment instead — it serves every other member.",
            comment,
        )
        dry = _group_props_dry(group, fmt, comment, group_name, "role-assignable: the CLI cannot write to it")
        back = _manual(
            "Re-add the membership with the same tool.",
            f"Add {member} back to {group_name} via Graph PowerShell or the portal.",
            comment,
        )
        return dry, step, back, display

    # Membership is collected TRANSITIVELY, so a person can sit in a NESTED group rather than in
    # the one holding the assignment. The group targeted below is the one the scan found the
    # membership in; the check is kept in the dry run so the operator can confirm it.
    nested = (
        f"the membership is in {group_name!r}"
        f"{' — a group nested inside ' + repr(source_name) if resolution == 'nested' else ''}; "
        f"this confirms it before anything is deleted"
    )

    # A group mastered in on-premises AD cannot be edited in Entra at all: the removal fails with
    # "Unable to update the specified properties for objects that have originated within an
    # external service". Reported from a real run — and the scan ALREADY KNEW the group was
    # synced, it just was not consulted. Emitting a runnable command that provably cannot work
    # is the same sin as emitting one that silently does nothing.
    if synced_state == schema.ENABLED_TRUE:
        step = _manual(
            f"Group {group_name!r} is mastered in on-premises Active Directory. Entra rejects "
            f"every membership change to it, so there is no command that can work here.",
            f"Remove {member} from {group_name} IN ACTIVE DIRECTORY (ADUC, or whatever manages\n"
            f"the group on-premises), then wait for Entra Connect to sync the change.\n"
            f"Removing it in Entra would fail; changing it only in Entra would be reverted by\n"
            f"the next sync cycle even if it appeared to succeed.\n"
            f"Do NOT delete the group's {role} assignment instead — it serves every other member.",
            comment,
        )
        known = "already known to be true — this is the check that was skipped before"
        if fmt == AZ_CLI:
            dry = f"az ad group show --group {_q(group)} --query onPremisesSyncEnabled  # {known}"
        elif fmt == POWERSHELL:
            dry = f"(Get-AzADGroup -ObjectId {_dq(group)}).OnPremisesSyncEnabled  # {known}"
        else:
            dry = f"{comment} Group {group_name} ({group}) is mastered in on-premises AD."
        back = _manual(
            "Re-add the membership on-premises.",
            f"Add {member} back to {group_name} in Active Directory and let it sync.",
            comment,
        )
        return dry, step, back, display

    # Sync state UNKNOWN. Graph omits `onPremisesSyncEnabled` for cloud-only objects, so most of
    # these are fine — but "most" is not "all", and the operator should not discover the
    # difference from a raw API error halfway down a script. The group id is therefore resolved
    # AT RUN TIME through a JMESPath fallback that yields a sentinel when the group turns out to
    # be synced, so the removal cannot execute against one. `$(...)` is used rather than an `if`
    # block on purpose: it is valid in bash AND in PowerShell, and this script is routinely run
    # in both (Cloud Shell offers each).
    guard_sentinel = "GROUP-IS-ON-PREM-SYNCED--REMOVE-THE-MEMBER-IN-ACTIVE-DIRECTORY"
    guard_query = f"onPremisesSyncEnabled && '{guard_sentinel}' || '{group}'"
    guarded = (
        f"# The group id is resolved at run time: if the group is mastered in on-prem AD this\n"
        f"# yields {guard_sentinel!r} instead, so the removal cannot touch it.\n"
        f"az ad group member remove --member-id {_q(member)} "
        f'--group $(az ad group show --group {_q(group)} --query "{guard_query}" -o tsv)'
    )
    synced = "if this returns true the group is mastered in on-prem AD — remove the member THERE, not here"

    if fmt == AZ_CLI:
        dry = (
            f"az ad group show --group {_q(group)} --query onPremisesSyncEnabled  # {synced}\n"
            f"az ad group member check --group {_q(group)} --member-id {_q(member)} "
            f"--query value  # {nested}"
        )
        cmd = guarded
        back = f"az ad group member add --group {_q(group)} --member-id {_q(member)}"
    elif fmt == POWERSHELL:
        dry = (
            f"(Get-AzADGroup -ObjectId {_dq(group)}).OnPremisesSyncEnabled  # {synced}\n"
            f"Get-AzADGroupMember -GroupObjectId {_dq(group)} | "
            f"Where-Object Id -eq {_dq(member)}  # {nested}"
        )
        # PowerShell is unambiguous about its own syntax, so the guard is a plain `if`.
        cmd = (
            f"if ((Get-AzADGroup -ObjectId {_dq(group)}).OnPremisesSyncEnabled) {{\n"
            f"    Write-Warning {_dq(f'{group_name} is mastered in on-prem AD - remove {member} in Active Directory, not here.')}\n"
            f"}} else {{\n"
            f"    Remove-AzADGroupMember -GroupObjectId {_dq(group)} -MemberObjectId {_dq(member)}\n"
            f"}}"
        )
        back = f"Add-AzADGroupMember -TargetGroupObjectId {_dq(group)} -MemberObjectId {_dq(member)}"
    else:
        dry = f"{comment} Inspect membership of group {group} in Entra ID."
        cmd = _manual(
            "Group membership is directory state, not infrastructure this template owns.",
            f"Remove member {member} from group {group_name} ({group}).\n"
            f"Portal: Entra ID > Groups > {group_name} > Members > Remove.\n"
            f"Do NOT delete the group's {role} assignment — it serves every other member.\n"
            f"{nested}\n{synced}",
            comment,
        )
        back = _manual("Re-add the membership.", f"Add member {member} back to group {group}.", comment)
    return dry, cmd, back, display


def _revoke_entra_role(row: dict[str, Any], fmt: str) -> tuple[str, str, str]:
    """Entra directory roles live in Graph, not ARM.

    `az role assignment delete --role 'Global Reader'` fails with "Role 'Global Reader' doesn't
    exist" because it looks the name up among ARM role definitions, where directory roles are not
    published at all."""
    principal = str(row.get("effectivePrincipalId") or row.get("principalId") or "")
    role = str(row.get("roleName", ""))
    role_def = str(row.get("roleDefinitionId", ""))
    assignment_id = str(row.get("assignmentId", ""))
    directory_scope = str(row.get("scope") or "/") or "/"
    url = f"{_GRAPH}/roleManagement/directory/roleAssignments"
    body = (
        f'{{"@odata.type":"#microsoft.graph.unifiedRoleAssignment",'
        f'"roleDefinitionId":"{role_def}","principalId":"{principal}",'
        f'"directoryScopeId":"{directory_scope}"}}'
    )

    if not assignment_id:
        comment = "//" if fmt == BICEP else "#"
        step = _manual(
            f"The scan did not capture an assignment id for this {role} grant.",
            f"Portal: Entra ID > Roles and administrators > {role} > remove {principal}.",
            comment,
        )
        return f"{comment} Look up the {role} assignment for {principal}.", step, step

    if fmt == AZ_CLI:
        dry = f"az rest --method GET --url {_q(f'{url}/{assignment_id}')}"
        cmd = f"az rest --method DELETE --url {_q(f'{url}/{assignment_id}')}"
        back = f"az rest --method POST --url {_q(url)} --headers 'Content-Type=application/json' --body {_q(body)}"
    elif fmt == POWERSHELL:
        dry = f"Get-MgRoleManagementDirectoryRoleAssignment -UnifiedRoleAssignmentId {_dq(assignment_id)}"
        cmd = f"Remove-MgRoleManagementDirectoryRoleAssignment -UnifiedRoleAssignmentId {_dq(assignment_id)}"
        back = (
            f"New-MgRoleManagementDirectoryRoleAssignment -RoleDefinitionId {_dq(role_def)} "
            f"-PrincipalId {_dq(principal)} -DirectoryScopeId {_dq(directory_scope)}"
        )
    else:
        comment = "//" if fmt == BICEP else "#"
        dry = f"{comment} Directory role assignment {assignment_id} ({role})."
        cmd = _manual(
            "Entra directory roles are not ARM resources; this template language cannot express them.",
            f"Remove the {role} assignment {assignment_id} from {principal} via Graph or the portal.",
            comment,
        )
        back = _manual("Re-create the directory role assignment.", f"Grant {role} back to {principal}.", comment)
    return dry, cmd, back


def _revoke_sp_owner(row: dict[str, Any], fmt: str) -> tuple[str, str, str]:
    """Service-principal ownership is a Graph relationship, and NO CLI verb removes it.

    `az ad sp owner` exposes `list` and nothing else — `az ad sp owner remove` answers "'remove' is
    misspelled or not recognized by the system", reported from a real run. Az.Resources ships no
    *-AzADServicePrincipalOwner cmdlets either. The only owner verbs that exist anywhere are
    `az ad app owner add|remove`, and those edit the APPLICATION object, which keeps a SEPARATE
    owner list from its service principal. So both formats go straight at the Graph relationship."""
    owner = str(row.get("effectivePrincipalId") or "")
    sp = str(row.get("principalId") or "")
    sp_name = str(row.get("principalDisplayName") or sp)
    owners_url = f"{_GRAPH}/servicePrincipals/{sp}/owners"
    # `$ref` and `$select` must stay inside SINGLE quotes. Retyped as double quotes, both bash and
    # PowerShell expand them to nothing, and the URL silently loses its last segment: the DELETE
    # then addresses the owner ENTITY instead of the relationship and is rejected.
    quoting = "keep the single quotes: in double quotes the shell eats $ref and the URL is wrong"
    # An owner of the paired APPLICATION can add a credential to it and authenticate as this very
    # service principal, so removing the SP owner alone can leave the impersonation path open. The
    # scan only reads servicePrincipals/owners, so the operator gets the check rather than a guess.
    paired = "the app registration holds its OWN owner list — remove them there too if listed"

    if fmt == AZ_CLI:
        dry = (
            f"az rest --method GET --url {_q(owners_url + '?$select=id,displayName')}\n"
            f"az ad app owner list --id $(az ad sp show --id {_q(sp)} --query appId -o tsv) "
            f"-o table  # {paired}"
        )
        cmd = f"az rest --method DELETE --url {_q(f'{owners_url}/{owner}/$ref')}  # {quoting}"
        back = (
            f"az rest --method POST --url {_q(owners_url + '/$ref')} "
            f"--headers 'Content-Type=application/json' "
            f"--body {_q(f'{{\"@odata.id\":\"{_GRAPH}/directoryObjects/{owner}\"}}')}"
        )
    elif fmt == POWERSHELL:
        # Invoke-MgGraphRequest rather than Remove-MgServicePrincipalOwner*ByRef: that generated
        # cmdlet was renamed between Graph SDK v1 and v2, so its name is not safe to hard-code
        # into a script somebody runs unattended.
        dry = (
            f"Invoke-MgGraphRequest -Method GET -Uri {_q(owners_url + '?$select=id,displayName')}\n"
            f"# {paired}"
        )
        cmd = f"Invoke-MgGraphRequest -Method DELETE -Uri {_q(f'{owners_url}/{owner}/$ref')}  # {quoting}"
        back = (
            f"Invoke-MgGraphRequest -Method POST -Uri {_q(owners_url + '/$ref')} "
            f"-Body @{{'@odata.id' = {_q(f'{_GRAPH}/directoryObjects/{owner}')}}}"
        )
    else:
        comment = "//" if fmt == BICEP else "#"
        dry = f"{comment} Owners of service principal {sp_name} ({sp})."
        cmd = _manual(
            "Application ownership is directory state, not infrastructure.",
            f"Remove {owner} from the owners of {sp_name} ({sp}).\n"
            f"Portal: Entra ID > Enterprise applications > {sp_name} > Owners > Remove.\n"
            f"{paired}",
            comment,
        )
        back = _manual("Restore ownership.", f"Add {owner} back as an owner of {sp}.", comment)
    return dry, cmd, back


def _revoke_pim_eligible(row: dict[str, Any], fmt: str) -> tuple[str, str, str]:
    """An ELIGIBLE assignment is a schedule, not an assignment.

    `az role assignment delete` does not see it — it lists active assignments only — so the
    generated command exits cleanly while the person keeps the ability to activate the role."""
    principal = str(row.get("effectivePrincipalId") or row.get("principalId") or "")
    role_def = str(row.get("roleDefinitionId", ""))
    scope = str(row.get("scope", ""))
    role = str(row.get("roleName", ""))
    instance_id = str(row.get("assignmentId") or "")
    api = "2020-10-01"
    # The request NAME is any unused GUID, and it is minted here rather than left as a
    # `<new-guid>` placeholder. Reported from a real run: the placeholder went to ARM verbatim
    # and came back as a 400 carrying an ASP.NET error page, which tells the operator nothing.
    # There is no GUID generator that works in both bash and PowerShell, and re-running the same
    # script with the same name is a no-op rather than a second removal — which is the safer of
    # the two behaviours anyway.
    request_name = str(uuid.uuid4())
    list_url = (
        f"{scope}/providers/Microsoft.Authorization/roleEligibilitySchedules"
        f"?api-version={api}&$filter=principalId eq '{principal}'"
    )
    req = (
        f"{scope}/providers/Microsoft.Authorization/roleEligibilityScheduleRequests/"
        f"{request_name}?api-version={api}"
    )
    # `roleDefinitionId` has to be the FULL ARM id, and AdminRemove needs to say WHICH
    # eligibility it is removing — a principal can hold more than one for the same role.
    props = [
        f'"principalId":"{principal}"',
        f'"roleDefinitionId":"{role_def}"',
        '"requestType":"AdminRemove"',
    ]
    if instance_id:
        props.append(f'"targetRoleEligibilityScheduleInstanceId":"{instance_id}"')
    body = "{\"properties\":{" + ",".join(props) + "}}"

    if fmt == AZ_CLI:
        dry = f"az rest --method GET --url {_q(f'https://management.azure.com{list_url}')}"
        cmd = (
            f"# PIM eligibility is removed with an AdminRemove request, not by deleting an assignment.\n"
            f"# The request name below is a fresh GUID; re-running this step is a no-op, not a repeat.\n"
            f"az rest --method PUT --url {_q('https://management.azure.com' + req)} "
            f"--body {_q(body)}"
        )
        back = (
            f"# Re-create the eligibility with requestType AdminAssign and the original schedule.\n"
            f"# Review the PIM policy for {role} at {scope} before restoring — the original\n"
            f"# expiry is not recoverable from the assignment itself."
        )
    elif fmt == POWERSHELL:
        pim_filter = _dq(f"principalId eq '{principal}'")
        dry = f"Get-AzRoleEligibilitySchedule -Scope {_dq(scope)} -Filter {pim_filter}"
        cmd = (
            f"New-AzRoleEligibilityScheduleRequest -Name {_dq(request_name)} -Scope {_dq(scope)} "
            f"-PrincipalId {_dq(principal)} -RoleDefinitionId {_dq(role_def)} -RequestType AdminRemove"
        )
        back = (
            f"# Re-create with -RequestType AdminAssign. The original expiry is not recoverable\n"
            f"# from the assignment; check the PIM policy for {role}."
        )
    else:
        comment = "//" if fmt == BICEP else "#"
        dry = f"{comment} Eligible (not active) {role} for {principal} at {scope}."
        cmd = _manual(
            "PIM eligibility is a schedule request, not a declarative resource.",
            f"Portal: PIM > Azure resources > {scope} > Eligible assignments > remove {role}.",
            comment,
        )
        back = _manual("Restore the eligibility.", f"Re-assign {role} as eligible to {principal}.", comment)
    return dry, cmd, back


def _revoke_kv_policy(row: dict[str, Any], fmt: str) -> tuple[str, str, str]:
    """Key Vault access policies are a property of the vault, not a role assignment."""
    principal = str(row.get("effectivePrincipalId") or row.get("principalId") or "")
    vault = str(row.get("resourceName") or "")
    rg = str(row.get("resourceGroup") or "")
    # `az keyvault` resolves the resource group inside the ACTIVE subscription. Reported from a
    # real run: "(ResourceGroupNotFound) Resource group 'RG-...' could not be found" — the group
    # exists, in another subscription. One script routinely spans several.
    sub = _sub_arg(row, fmt)

    if fmt == AZ_CLI:
        dry = f"az keyvault show --name {_q(vault)} --resource-group {_q(rg)}{sub} --query 'properties.accessPolicies'"
        cmd = f"az keyvault delete-policy --name {_q(vault)} --resource-group {_q(rg)}{sub} --object-id {_q(principal)}"
        back = (
            f"# Restore the exact permission sets captured in the export for this row:\n"
            f"az keyvault set-policy --name {_q(vault)} --resource-group {_q(rg)}{sub} "
            f"--object-id {_q(principal)} --key-permissions <keys> --secret-permissions <secrets> "
            f"--certificate-permissions <certs>"
        )
    elif fmt == POWERSHELL:
        # Az cmdlets act on the CURRENT context, and one script routinely spans several
        # subscriptions, so the context is set per step rather than assumed.
        dry = f"{_sub_arg(row, fmt)}(Get-AzKeyVault -VaultName {_dq(vault)}).AccessPolicies"
        cmd = (
            f"{_sub_arg(row, fmt)}Remove-AzKeyVaultAccessPolicy -VaultName {_dq(vault)} "
            f"-ResourceGroupName {_dq(rg)} -ObjectId {_dq(principal)}"
        )
        back = (
            f"# Restore with the permission sets captured in the export for this row:\n"
            f"{_sub_arg(row, fmt)}Set-AzKeyVaultAccessPolicy -VaultName {_dq(vault)} "
            f"-ResourceGroupName {_dq(rg)} -ObjectId {_dq(principal)} "
            f"-PermissionsToKeys <keys> -PermissionsToSecrets <secrets>"
        )
    else:
        comment = "//" if fmt == BICEP else "#"
        dry = f"{comment} Access policies on vault {vault}."
        cmd = _manual(
            "The access policy is a property of the vault resource.",
            f"Remove the accessPolicies entry for objectId {principal} from vault {vault} and redeploy.",
            comment,
        )
        back = _manual("Restore the policy entry.", f"Re-add the accessPolicies entry for {principal}.", comment)
    return dry, cmd, back


def _revoke_other_plane(row: dict[str, Any], fmt: str, plane: str) -> dict[str, Any]:
    """Build the action for every plane that is not an Azure RBAC assignment."""
    principal = str(row.get("effectivePrincipalId") or row.get("principalId") or "")
    who = str(row.get("effectivePrincipalName") or principal)
    role = str(row.get("roleName", ""))
    # Titles and prose only. Every COMMAND below is built from ids, never from this string, so
    # shortening it cannot change what the step does.
    role = short_role(role)
    scope = str(row.get("scope", ""))
    comment = "//" if fmt == BICEP else "#"
    label = f"{who} — {role} @ {scope}"
    breaks = (
        f"any automation authenticating as {who} and relying on {role} at {scope} stops working "
        f"immediately — check pipelines and scheduled jobs first"
    )

    if plane == PLANE_GROUP_MEMBERSHIP:
        dry, cmd, back, group_name = _revoke_group_membership(row, fmt)
        label = f"{who} — remove from group {group_name} (grants {role} @ {scope})"
        # The blast radius of a membership removal is the WHOLE group, not this one role. An
        # operator who reads only "revoke Reader" will not expect the other twelve things the
        # group grants to disappear at the same time.
        breaks = (
            f"{who} loses EVERYTHING group {group_name!r} grants, not just {role} — review the "
            f"group's other assignments first. Any automation running as {who} breaks immediately"
        )
        if str(row.get("membershipGroupOnPremSynced") or "") == schema.ENABLED_TRUE:
            # Nothing runs, so nothing breaks here — and saying otherwise would send the operator
            # looking for a blast radius in the wrong directory.
            label = f"{who} — remove from group {group_name} IN ACTIVE DIRECTORY (grants {role} @ {scope})"
            breaks = (
                f"nothing yet: this group is mastered on-premises, so no command here can change "
                f"it. Once the removal is made in AD and syncs, {who} loses EVERYTHING "
                f"{group_name!r} grants, not just {role}"
            )
    elif plane == PLANE_ENTRA_ROLE:
        dry, cmd, back = _revoke_entra_role(row, fmt)
    elif plane == PLANE_SP_OWNER:
        dry, cmd, back = _revoke_sp_owner(row, fmt)
        sp_name = str(row.get("principalDisplayName") or row.get("principalId") or "")
        label = f"{who} — remove as owner of {sp_name}"
        # Stated at the point of action because it is the single most misunderstood step here.
        breaks = (
            f"removing the owner does NOT stop the service principal {sp_name}: it signs in with "
            f"its own secret or certificate. Roll the credential as well, or nothing changes"
        )
    elif plane == PLANE_PIM_ELIGIBLE:
        dry, cmd, back = _revoke_pim_eligible(row, fmt)
        label = f"{who} — {role} @ {scope} (ELIGIBLE, not active)"
        breaks = f"{who} can no longer activate {role} at {scope}. Nothing breaks until they try to"
    elif plane == PLANE_KV_POLICY:
        dry, cmd, back = _revoke_kv_policy(row, fmt)
    elif plane == PLANE_CLASSIC:
        dry = f"{comment} az role assignment list --include-classic-administrators --scope {scope}"
        cmd = _manual(
            "Classic administrators cannot be removed by the ARM role APIs.",
            f"Portal: Subscription > Access control (IAM) > Classic administrators > remove {who}.\n"
            f"Classic administrator access is being retired by Azure; confirm the subscription\n"
            f"still has a modern Owner before removing the last classic admin.",
            comment,
        )
        back = _manual("Classic administrators cannot be re-added.", "Grant an equivalent RBAC role instead.", comment)
    elif plane == PLANE_LIGHTHOUSE:
        dry = f"{comment} az managedservices assignment list --scope {scope}"
        cmd = _manual(
            "Lighthouse access comes from a delegation owned by the MANAGING tenant.",
            f"Removing {who} here is not possible from this tenant's role APIs. Either remove the\n"
            f"registration assignment for the whole delegation, or ask the managing tenant to\n"
            f"remove {who} from the authorization list:\n"
            f"  az managedservices assignment delete --assignment <id> --scope {scope}\n"
            f"Deleting the assignment removes access for EVERY principal in the delegation.",
            comment,
        )
        back = _manual("Re-create the delegation.", "Re-onboard the delegation from the managing tenant.", comment)
    elif plane == PLANE_DENY:
        dry = f"{comment} az rest --method GET --url 'https://management.azure.com{scope}/providers/Microsoft.Authorization/denyAssignments?api-version=2022-04-01'"
        cmd = _manual(
            "Deny assignments are read-only — they are created by Azure, not by users.",
            f"This row DENIES access; it is not a grant and must not be 'revoked'.\n"
            f"It comes from a Blueprint or a Managed Application. Remove the owning resource\n"
            f"if the denial is genuinely unwanted.",
            comment,
        )
        back = _manual("Nothing to undo.", "No action was taken.", comment)
    else:  # pragma: no cover - plane_of never returns anything else
        raise ValueError(f"unhandled plane {plane!r}")

    return {
        "action": "revoke_assignment",
        "plane": plane,
        "label": label,
        "format": fmt,
        "dry_run": dry,
        "command": cmd,
        "rollback": back,
        "breaks_if": breaks,
        "order_hint": _order_hint(row),
        # What this row grants, without the who/how prefix the label carries. When several rows
        # fold into one step the label repeats verbatim per grant, which on a group holding 43
        # assignments produced a single unreadable line — the blast radius became invisible by
        # being printed too loudly.
        "grant": f"{role} @ {scope}",
        # Whether this step needs the Microsoft Graph PowerShell sign-in emitted once at the top
        # of the bundle. Carried on the action because `build_bundle` only ever sees actions.
        "needs_graph_ps": (
            plane == PLANE_GROUP_MEMBERSHIP
            and str(row.get("membershipGroupRoleAssignable") or "") == schema.ENABLED_TRUE
        ),
        "tenant_id": str(row.get("tenantId") or ""),
        # Lets the caller fold the N grants one membership removal covers into a single step.
        # Keyed on the MEMBERSHIP group, not the assignment group: several groups can hold
        # assignments and all reach this person through the SAME nested child, which makes them
        # one removal wearing three hats. Keyed on the source group they stayed three steps —
        # the first succeeds and the rest fail on a membership that is already gone.
        # Falls back to the source group when the membership group is unknown or ambiguous,
        # where the steps really are distinct because each names a different parent.
        "dedupe_key": (
            f"{plane}:{str(row.get('effectivePrincipalId') or '')}:"
            f"{str(row.get('membershipGroupId') or row.get('sourceGroupId') or '')}"
            if plane == PLANE_GROUP_MEMBERSHIP
            else ""
        ),
    }


def _bicep_assignment(principal: str, role: str, scope: str) -> str:
    return (
        "resource restore 'Microsoft.Authorization/roleAssignments@2022-04-01' = {\n"
        f"  name: guid({_q(scope)}, {_q(principal)}, {_q(role)})\n"
        f"  scope: tenantResourceId('Microsoft.Resources/resourceGroups', {_q(scope)})\n"
        "  properties: {\n"
        f"    principalId: {_q(principal)}\n"
        f"    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', {_q(role)})\n"
        "  }\n"
        "}"
    )


def _terraform_assignment(principal: str, role: str, scope: str, assignment_id: str) -> str:
    return (
        f'resource "azurerm_role_assignment" "{_tf_name(assignment_id or principal)}" {{\n'
        f"  scope                = {_dq(scope)}\n"
        f"  role_definition_name = {_dq(role)}\n"
        f"  principal_id         = {_dq(principal)}\n"
        "}"
    )


def reduce_assignment(row: dict[str, Any], target_role: str, fmt: str) -> dict[str, Any]:
    """Swap a role for a narrower one. Grant first, then revoke — never the other way round.

    Revoking before granting leaves a window with no access at all. On a break-glass account or
    a deployment identity that window is an incident, so the order is fixed here rather than
    left to whoever runs the script."""
    principal = str(row.get("effectivePrincipalId") or row.get("principalId") or "")
    scope = str(row.get("scope", ""))
    current = str(row.get("roleName", ""))

    plane = plane_of(row)
    if plane != PLANE_AZURE_RBAC:
        # "Reduce" is an Azure-RBAC-shaped idea. There is no narrower version of a directory
        # role held through a group: you would have to take the person out of the group and
        # grant them something else directly, which is a different decision that a human has to
        # make. Emitting the RBAC pair here would create an ARM assignment that does not replace
        # what it claims to replace, leaving BOTH in place.
        comment = "//" if fmt == BICEP else "#"
        who = str(row.get("effectivePrincipalName") or principal)
        step = _manual(
            f"{current} here is {plane.replace('_', ' ')}, which has no narrower equivalent to swap to.",
            f"Revoke it on its own plane (see the revoke step for this row), then grant {who}\n"
            f"the narrower access directly if they still need it. Doing only the grant would\n"
            f"ADD access rather than reduce it.",
            comment,
        )
        return {
            "action": "reduce_assignment",
            "plane": plane,
            "label": f"{who} — {current} @ {scope} cannot be reduced in place",
            "format": fmt,
            "dry_run": f"{comment} {current} for {who} at {scope} ({plane}).",
            "command": step,
            "rollback": _manual("Nothing was changed.", "No action was taken.", comment),
            "breaks_if": "nothing — this step makes no change on its own",
            "order_hint": _order_hint(row),
            "dedupe_key": "",
        }

    if fmt == AZ_CLI:
        cmd = (
            f"# 1. Grant the narrower role FIRST so access is never interrupted.\n"
            f"az role assignment create --assignee {_q(principal)} --role {_q(target_role)} --scope {_q(scope)}\n"
            f"# 2. Only then remove the wider one.\n"
            f"az role assignment delete --assignee {_q(principal)} --role {_q(current)} --scope {_q(scope)}"
        )
        back = (
            f"az role assignment create --assignee {_q(principal)} --role {_q(current)} --scope {_q(scope)}\n"
            f"az role assignment delete --assignee {_q(principal)} --role {_q(target_role)} --scope {_q(scope)}"
        )
        dry = f"az role assignment list --assignee {_q(principal)} --scope {_q(scope)} -o table"
    elif fmt == POWERSHELL:
        cmd = (
            f"# 1. Grant the narrower role FIRST so access is never interrupted.\n"
            f"New-AzRoleAssignment -ObjectId {_dq(principal)} -RoleDefinitionName {_dq(target_role)} -Scope {_dq(scope)}\n"
            f"# 2. Only then remove the wider one.\n"
            f"Remove-AzRoleAssignment -ObjectId {_dq(principal)} -RoleDefinitionName {_dq(current)} -Scope {_dq(scope)}"
        )
        back = (
            f"New-AzRoleAssignment -ObjectId {_dq(principal)} -RoleDefinitionName {_dq(current)} -Scope {_dq(scope)}\n"
            f"Remove-AzRoleAssignment -ObjectId {_dq(principal)} -RoleDefinitionName {_dq(target_role)} -Scope {_dq(scope)}"
        )
        dry = f"Get-AzRoleAssignment -ObjectId {_dq(principal)} -Scope {_dq(scope)} | Format-Table"
    elif fmt == BICEP:
        cmd = _bicep_assignment(principal, target_role, scope) + (
            f"\n// Then DELETE the existing block granting {current} and redeploy."
        )
        back = _bicep_assignment(principal, current, scope)
        dry = f"// Current: {current} at {scope} for {principal}"
    elif fmt == TERRAFORM:
        cmd = _terraform_assignment(principal, target_role, scope, "") + (
            f"\n# Then remove the resource granting {current} and apply."
        )
        back = _terraform_assignment(principal, current, scope, "")
        dry = f"# Current: {current} at {scope} for {principal}"
    else:
        raise ValueError(f"unknown format {fmt!r}")

    return {
        "action": "reduce_assignment",
        "label": f"{row.get('effectivePrincipalName') or principal}: {current} → {target_role} @ {scope}",
        "format": fmt,
        "dry_run": dry,
        "command": cmd,
        "rollback": back,
        "breaks_if": (
            f"anything {row.get('effectivePrincipalName') or principal} does that {target_role} does not permit "
            f"but {current} did — verify against recent activity before scheduling"
        ),
        "order_hint": _order_hint(row),
    }


def _order_hint(row: dict[str, Any]) -> int:
    """Lower runs first.

    Group-derived access is removed BEFORE direct assignments: revoking a direct grant while the
    principal still inherits the same access through a group looks successful and changes
    nothing, which is how "we revoked it" and "they still have it" end up both being true.
    Broader scopes are handled before narrower ones for the same reason."""
    path = str(row.get("accessPath", ""))
    from app.iam import diff as diff_mod

    depth = diff_mod.scope_depth(str(row.get("scope", "")))
    group_first = 0 if path and path != "Direct" else 1
    return group_first * 10 + depth


# --------------------------------------------------------------------------- bundle
def _fold_duplicates(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse actions that are literally the same operation.

    One group membership grants every role that group holds, so a person inheriting four roles
    through one group produced FOUR identical `group member remove` commands. The first succeeds
    and the other three fail — and an operator watching three failures scroll past has no way to
    know they were expected. One membership, one step, listing what it takes away."""
    out: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for a in actions:
        key = str(a.get("dedupe_key") or "")
        if not key:
            out.append(a)
            continue
        first = seen.get(key)
        if first is None:
            folded = dict(a)
            folded["covers"] = [str(a.get("grant") or a["label"])]
            seen[key] = folded
            out.append(folded)
            continue
        first["covers"].append(str(a.get("grant") or a["label"]))
        # Keep the broadest scope's ordering so the folded step still runs early.
        first["order_hint"] = min(first.get("order_hint", 99), a.get("order_hint", 99))
    for folded in seen.values():
        if len(folded["covers"]) > 1:
            # Counted here, listed line by line by `build_bundle`. Every one of them is named:
            # a truncated blast radius is the thing this whole module exists to prevent.
            folded["breaks_if"] = (
                f"{folded['breaks_if']}. This one step removes {len(folded['covers'])} grants, "
                f"each listed under the command"
            )
    return out


def _graph_signin_preamble(fmt: str, comment: str, count: int, tenant_id: str) -> list[str]:
    """The Microsoft Graph PowerShell sign-in, emitted ONCE for the steps that need it.

    Two ways in, because they fail differently and people reach for the wrong one:

    * **Interactive** consents to DELEGATED scopes as the signed-in human.
    * **App-only** (a service principal) ignores `-Scopes` entirely — it carries APPLICATION
      permissions, which an administrator has to grant and consent to in advance. Handing
      someone `-Scopes` and an app registration produces the same 403 they started with, which
      is exactly the confusion this whole plane exists to remove.

    THE SECRET IS NEVER WRITTEN HERE. It is read from the environment at run time; a generated
    artifact that carries a credential is a leak with a filename.
    """
    tenant = tenant_id or "<tenant-id>"
    out = [
        f"{comment} " + "=" * 70,
        f"{comment} SIGN IN FIRST — {count} step(s) below need Microsoft Graph PowerShell.",
        f"{comment} Those groups are role-assignable, so Entra requires",
        f"{comment} RoleManagement.ReadWrite.Directory on the APPLICATION you sign in with.",
        f"{comment} The Azure CLI does not have it, which is why those steps are not az commands.",
        f"{comment}",
        f"{comment} Option A — interactive, as yourself (delegated):",
    ]
    connect_interactive = (
        "Connect-MgGraph -Scopes 'RoleManagement.ReadWrite.Directory','GroupMember.ReadWrite.All'"
    )
    app_only = [
        "$secure = ConvertTo-SecureString $env:GRAPH_CLIENT_SECRET -AsPlainText -Force",
        "$cred   = [System.Management.Automation.PSCredential]::new('<app-id>', $secure)",
        f"Connect-MgGraph -TenantId '{tenant}' -ClientSecretCredential $cred",
    ]
    # In the PowerShell bundle the interactive line is runnable, because it is the one that
    # needs no preparation. App-only stays commented: picking it is a deliberate choice, and
    # running both in one session just replaces the first context with the second.
    out.append(connect_interactive if fmt == POWERSHELL else f"{comment}   {connect_interactive}")
    out += [
        f"{comment}",
        f"{comment} Option B — unattended, as a service principal (app-only). Uncomment to use,",
        f"{comment} and comment out Option A above:",
        f"{comment}   The app registration needs the APPLICATION permissions",
        f"{comment}   RoleManagement.ReadWrite.Directory and GroupMember.ReadWrite.All, with",
        f"{comment}   admin consent granted. `-Scopes` does NOT apply to app-only sign-in, and",
        f"{comment}   without consent you get the same 403 you started with.",
        *[f"{comment}   {ln}" for ln in app_only],
        f"{comment}",
        f"{comment} The secret is read from the environment on purpose. Set it in the shell you",
        f"{comment} run this in, e.g. $env:GRAPH_CLIENT_SECRET = (Read-Host -AsSecureString ...),",
        f"{comment} or pull it from a vault. It is never written into a generated script.",
        f"{comment} " + "=" * 70,
        "",
    ]
    return out


def build_bundle(
    actions: list[dict[str, Any]],
    fmt: str,
    *,
    title: str = "",
    run_id: str = "",
    campaign_id: str = "",
) -> dict[str, Any]:
    """One ordered script for a set of decisions, with a header naming its provenance.

    The header is not decoration: an operator handed a script six weeks later needs to know which
    campaign and which scan produced it, and the generator version tells them whether it predates
    a fix.

    The revoke half and the undo half are also returned SEPARATELY. They are run at different
    times, by different people, under different pressure — and a single blob means the person
    reaching for the rollback at 3am has to select the right half of it by hand."""
    ordered = sorted(_fold_duplicates(actions), key=lambda a: (a.get("order_hint", 99), a.get("label", "")))
    comment = "//" if fmt in (BICEP,) else "#"

    planes = sorted({str(a.get("plane") or PLANE_AZURE_RBAC) for a in ordered})

    def _header(kind: str, note: str) -> list[str]:
        # Repeated on BOTH halves. Each one gets copied on its own, and a script pasted into a
        # change record with no provenance is a script nobody can audit later.
        out = [
            f"{comment} {title or 'IAM remediation'} — {kind}",
            f"{comment} generator: {GENERATOR_VERSION}   format: {fmt}",
            f"{comment} campaign: {campaign_id or '(none)'}   baseline run: {run_id or '(none)'}",
            f"{comment} {len(ordered)} action(s), ordered: group-derived access first, then broadest scope first.",
            f"{comment} THIS SCRIPT IS NOT RUN BY THE PRODUCT. Read it, then run it yourself.",
            f"{comment} {note}",
        ]
        if len(planes) > 1 or planes != [PLANE_AZURE_RBAC]:
            # Different planes need different tools and different consent. Saying so up front
            # stops the run being abandoned at the first step that needs Graph rather than ARM.
            out.append(
                f"{comment} This script spans more than one API: {', '.join(planes)}."
                f" Steps are NOT interchangeable — a directory role is not an ARM role assignment,"
                f" and group-derived access is removed by changing membership, not the assignment."
            )
        out.append("")
        return out

    # Emitted once, on BOTH halves: the rollback is run later, in a fresh session, by someone
    # who did not necessarily run the revoke.
    graph_steps = [a for a in ordered if a.get("needs_graph_ps")]
    signin = (
        _graph_signin_preamble(
            fmt, comment, len(graph_steps), next((str(a.get("tenant_id") or "") for a in graph_steps), "")
        )
        if graph_steps
        else []
    )

    revoke_lines = _header("REVOKE", "Every step here has an undo in the rollback script.") + signin
    for i, a in enumerate(ordered, start=1):
        # Every line of the dry run is commented individually. A multi-line dry run behind a
        # single prefix leaves its later lines bare, and a bare line in a script is one an
        # operator will run — the same class of mistake this module exists to avoid.
        dry_lines = [f"{comment}   {ln}" for ln in str(a["dry_run"]).splitlines() or [""]]
        covers = [str(c) for c in (a.get("covers") or [])]
        cover_lines = [f"{comment}   removes: {c}" for c in covers] if len(covers) > 1 else []
        revoke_lines += [
            f"{comment} --- {i}. {a['label']}",
            f"{comment} breaks if: {a['breaks_if']}",
            *cover_lines,
            f"{comment} dry run first:",
            *dry_lines,
            a["command"],
            "",
        ]

    rollback_lines = _header(
        "ROLLBACK (undo)",
        "Run these to restore the access the revoke script removed. Reverse order of removal.",
    ) + signin
    # Undone in reverse: the revoke took group membership away first, so putting it back last
    # keeps the intermediate state from briefly granting more than the person started with.
    for i, a in enumerate(reversed(ordered), start=1):
        rollback_lines += [f"{comment} --- undo {i}. {a['label']}", a["rollback"], ""]

    revoke_script = "\n".join(revoke_lines)
    rollback_script = "\n".join(rollback_lines)
    # Kept whole as well: the campaign artifact, the agent tool and anything that has already
    # stored a `script` field still expect one document.
    script = f"{revoke_script}\n{comment} ===== ROLLBACK =====\n{rollback_script}"
    for text in (revoke_script, rollback_script, script):
        assert_no_secrets(text)
    return {
        "format": fmt,
        "generator": GENERATOR_VERSION,
        "action_count": len(ordered),
        "script": script,
        "revoke_script": revoke_script,
        "rollback_script": rollback_script,
        "actions": ordered,
    }


def build_all_formats(actions_by_format: dict[str, list[dict[str, Any]]], **kw: Any) -> dict[str, Any]:
    return {fmt: build_bundle(acts, fmt, **kw) for fmt, acts in actions_by_format.items()}


def for_decision(row: dict[str, Any], decision: str, fmt: str, *, target_role: str = "Reader") -> dict[str, Any] | None:
    """Map one review decision to its artifact. `approve` produces nothing, by design."""
    if decision == "revoke":
        return revoke_assignment(row, fmt)
    if decision == "reduce":
        return reduce_assignment(row, target_role, fmt)
    return None
