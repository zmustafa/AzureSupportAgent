"""Owner-tag write-back + IaC generation.

Pushes the resolved owner of a resource back into Azure as an ``owner`` (and optional
``owner-email``) **tag**, so Azure-native tooling, cost views and policies can use the same
accountability signal the app tracks.

Safety:
* WRITE requires the ``ownership.write`` permission on the route and an explicit per-action
  confirmation in the UI (a write warning, mirroring Tag Intelligence). A write is **never**
  performed implicitly; only the explicit "Apply owner tag" action calls :func:`apply_owner_tag`.
* The merge-PATCH preserves existing tags (ARM ``PATCH …/tags`` with operation ``Merge``),
  so writing the owner tag never clobbers other tags.

IaC (download-only, never applied): :func:`bicep_for` emits a ``Microsoft.Resources/tags``
extension resource; :func:`policy_for` emits a ``Modify`` Azure Policy that adds the owner
tag fleet-wide. These are returned as text for the user to review/commit — the app never
deploys them."""
from __future__ import annotations

from typing import Any

_TAG_KEY = "owner"
_EMAIL_TAG_KEY = "owner-email"
_TAGS_API = "2021-04-01"


async def apply_owner_tag(
    connection: dict[str, Any] | None,
    *,
    resource_id: str,
    owner: str,
    owner_email: str = "",
) -> dict[str, Any]:
    """Merge-write the ``owner`` tag onto a resource via ARM REST. Returns
    ``{ok, error, applied}``. Fail-closed when no resource/owner/ARM token."""
    if not resource_id:
        return {"ok": False, "error": "resource_id is required.", "applied": {}}
    if not owner:
        return {"ok": False, "error": "No owner to write.", "applied": {}}
    if connection is None:
        return {"ok": False, "error": "No Azure connection configured.", "applied": {}}

    from app.azure.arm import arm_rest
    from app.azure.credentials import get_arm_token

    token, err = await get_arm_token(connection)
    if not token:
        return {"ok": False, "error": err or "Could not acquire an ARM token.", "applied": {}}

    tags = {_TAG_KEY: owner}
    if owner_email:
        tags[_EMAIL_TAG_KEY] = owner_email
    # Merge so other tags survive (ARM tags resource, operation=Merge).
    url = f"https://management.azure.com/{resource_id.lstrip('/')}/providers/Microsoft.Resources/tags/default?api-version={_TAGS_API}"
    body = {"operation": "Merge", "properties": {"tags": tags}}
    out, err = await arm_rest(token, "PATCH", url, body)
    if err:
        return {"ok": False, "error": err, "applied": {}}
    return {"ok": True, "error": "", "applied": tags}


def bicep_for(resource_id: str, owner: str, owner_email: str = "") -> str:
    """A Microsoft.Resources/tags extension resource that stamps the owner tag (review-only)."""
    tag_lines = [f"    owner: '{owner}'"]
    if owner_email:
        tag_lines.append(f"    'owner-email': '{owner_email}'")
    tags_block = "\n".join(tag_lines)
    return (
        "// Owner tag write-back (review before deploying). Scope this module at the\n"
        "// resource's resource group, or deploy at subscription scope with the full id.\n"
        f"resource ownerTags 'Microsoft.Resources/tags@{_TAGS_API}' = {{\n"
        "  name: 'default'\n"
        f"  scope: tenantResourceId('', '{resource_id}')\n"
        "  properties: {\n"
        "    tags: {\n"
        f"{tags_block}\n"
        "    }\n"
        "  }\n"
        "}\n"
    )


def policy_for(owner: str = "<owner>") -> str:
    """A 'Modify' Azure Policy that enforces/adds an ``owner`` tag fleet-wide (review-only)."""
    return (
        "{\n"
        '  "properties": {\n'
        '    "displayName": "Require and add an owner tag to resources",\n'
        '    "mode": "Indexed",\n'
        '    "policyRule": {\n'
        '      "if": { "field": "tags[\'owner\']", "exists": "false" },\n'
        '      "then": {\n'
        '        "effect": "modify",\n'
        '        "details": {\n'
        '          "roleDefinitionIds": ["/providers/Microsoft.Authorization/roleDefinitions/b24988ac-6180-42a0-ab88-20f7382dd24c"],\n'
        '          "operations": [{ "operation": "add", "field": "tags[\'owner\']", "value": "[parameters(\'ownerValue\')]" }]\n'
        "        }\n"
        "      }\n"
        "    },\n"
        '    "parameters": {\n'
        f'      "ownerValue": {{ "type": "String", "defaultValue": "{owner}", "metadata": {{ "displayName": "Owner tag value" }} }}\n'
        "    }\n"
        "  }\n"
        "}\n"
    )
