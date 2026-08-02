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
from typing import Any

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


# --------------------------------------------------------------------------- one action
def revoke_assignment(row: dict[str, Any], fmt: str) -> dict[str, Any]:
    """Revoke one role assignment, with its rollback.

    The rollback recreates the assignment from the *snapshot* — principal, role and scope — not
    from the assignment id, because a recreated assignment gets a new id. Someone restoring
    access needs the grant back, not the GUID."""
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
    a fix."""
    ordered = sorted(actions, key=lambda a: (a.get("order_hint", 99), a.get("label", "")))
    comment = "//" if fmt in (BICEP,) else "#"

    lines = [
        f"{comment} {title or 'IAM remediation'}",
        f"{comment} generator: {GENERATOR_VERSION}   format: {fmt}",
        f"{comment} campaign: {campaign_id or '(none)'}   baseline run: {run_id or '(none)'}",
        f"{comment} {len(ordered)} action(s), ordered: group-derived access first, then broadest scope first.",
        f"{comment} THIS SCRIPT IS NOT RUN BY THE PRODUCT. Read it, then run it yourself.",
        f"{comment} Every step below has a rollback in the 'rollback' section at the end.",
        "",
    ]
    for i, a in enumerate(ordered, start=1):
        lines += [
            f"{comment} --- {i}. {a['label']}",
            f"{comment} breaks if: {a['breaks_if']}",
            f"{comment} dry run first:",
            f"{comment}   {a['dry_run']}",
            a["command"],
            "",
        ]
    lines += [f"{comment} ===== ROLLBACK =====", f"{comment} Run these to restore the state above.", ""]
    for i, a in enumerate(ordered, start=1):
        lines += [f"{comment} --- undo {i}. {a['label']}", a["rollback"], ""]

    script = "\n".join(lines)
    assert_no_secrets(script)
    return {
        "format": fmt,
        "generator": GENERATOR_VERSION,
        "action_count": len(ordered),
        "script": script,
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
