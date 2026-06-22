"""Azure Policy generation from real tag usage (F8).

Produces self-contained policy *definitions* (audit / append / inherit-from-RG / deny) and an
*initiative* that groups them, plus the safe staged-rollout ladder. Everything is generated
read-only — definitions are returned as JSON the user exports or drops into the existing
Policy drafts registry; nothing is assigned here.
"""
from __future__ import annotations

import re
from typing import Any

_CONTRIB_ROLE = "/providers/Microsoft.Authorization/roleDefinitions/b24988ac-6180-42a0-ab88-20f7382dd24c"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "tag").lower()).strip("-")[:60] or "tag"


def _tag_field(tag: str) -> str:
    return f"tags['{tag}']"


def audit_policy(tag: str) -> dict[str, Any]:
    return {
        "name": f"audit-tag-{_slug(tag)}",
        "properties": {
            "displayName": f"Audit missing tag '{tag}'",
            "policyType": "Custom",
            "mode": "Indexed",
            "description": f"Audits resources that do not have the '{tag}' tag.",
            "metadata": {"category": "Tags", "generatedBy": "TagIntelligence"},
            "policyRule": {
                "if": {"field": _tag_field(tag), "exists": "false"},
                "then": {"effect": "audit"},
            },
        },
        "_effect": "audit",
        "_tag": tag,
    }


def deny_policy(tag: str) -> dict[str, Any]:
    return {
        "name": f"deny-missing-tag-{_slug(tag)}",
        "properties": {
            "displayName": f"Deny resources missing tag '{tag}'",
            "policyType": "Custom",
            "mode": "Indexed",
            "description": f"Denies creation of resources that do not have the '{tag}' tag.",
            "metadata": {"category": "Tags", "generatedBy": "TagIntelligence"},
            "policyRule": {
                "if": {"field": _tag_field(tag), "exists": "false"},
                "then": {"effect": "deny"},
            },
        },
        "_effect": "deny",
        "_tag": tag,
    }


def append_policy(tag: str, default_value: str = "") -> dict[str, Any]:
    return {
        "name": f"append-tag-{_slug(tag)}",
        "properties": {
            "displayName": f"Add tag '{tag}' when missing",
            "policyType": "Custom",
            "mode": "Indexed",
            "description": f"Adds the '{tag}' tag with a default value when it is missing (Modify).",
            "metadata": {"category": "Tags", "generatedBy": "TagIntelligence"},
            "parameters": {
                "tagValue": {"type": "String", "metadata": {"displayName": f"{tag} value"},
                             "defaultValue": default_value or "REPLACE_ME"},
            },
            "policyRule": {
                "if": {"field": _tag_field(tag), "exists": "false"},
                "then": {
                    "effect": "modify",
                    "details": {
                        "roleDefinitionIds": [_CONTRIB_ROLE],
                        "operations": [{"operation": "add", "field": _tag_field(tag), "value": "[parameters('tagValue')]"}],
                    },
                },
            },
        },
        "_effect": "modify",
        "_tag": tag,
    }


def inherit_policy(tag: str) -> dict[str, Any]:
    return {
        "name": f"inherit-tag-{_slug(tag)}",
        "properties": {
            "displayName": f"Inherit tag '{tag}' from resource group",
            "policyType": "Custom",
            "mode": "Indexed",
            "description": f"Adds the '{tag}' tag from the containing resource group when missing (Modify).",
            "metadata": {"category": "Tags", "generatedBy": "TagIntelligence"},
            "policyRule": {
                "if": {
                    "allOf": [
                        {"field": _tag_field(tag), "exists": "false"},
                        {"value": f"[resourceGroup().tags['{tag}']]", "notEquals": ""},
                    ],
                },
                "then": {
                    "effect": "modify",
                    "details": {
                        "roleDefinitionIds": [_CONTRIB_ROLE],
                        "operations": [{"operation": "add", "field": _tag_field(tag),
                                        "value": f"[resourceGroup().tags['{tag}']]"}],
                    },
                },
            },
        },
        "_effect": "modify",
        "_tag": tag,
    }


_BUILDERS = {"audit": audit_policy, "deny": deny_policy, "append": append_policy, "inherit": inherit_policy}


def generate(selections: list[dict[str, Any]]) -> dict[str, Any]:
    """Generate definitions for a list of ``{tag, effect, default_value?}`` selections, plus an
    initiative grouping them. ``effect`` is one of audit|deny|append|inherit."""
    definitions: list[dict[str, Any]] = []
    for sel in selections:
        tag = str(sel.get("tag", "")).strip()
        effect = str(sel.get("effect", "audit")).lower()
        if not tag or effect not in _BUILDERS:
            continue
        if effect == "append":
            definitions.append(append_policy(tag, str(sel.get("default_value", ""))))
        else:
            definitions.append(_BUILDERS[effect](tag))

    initiative = {
        "name": "tag-governance-initiative",
        "properties": {
            "displayName": "Tag governance (generated)",
            "policyType": "Custom",
            "description": "Initiative grouping the generated tag policies.",
            "metadata": {"category": "Tags", "generatedBy": "TagIntelligence"},
            "policyDefinitions": [
                {"policyDefinitionReferenceId": d["name"],
                 "policyDefinitionId": f"/subscriptions/<sub>/providers/Microsoft.Authorization/policyDefinitions/{d['name']}"}
                for d in definitions
            ],
        },
    }
    has_deny = any(d["_effect"] == "deny" for d in definitions)
    return {
        "definitions": definitions,
        "initiative": initiative,
        "warnings": (["This set includes a DENY policy. Assign it only after a staged rollout (audit → append → deny) "
                      "and confirming deployment impact — deny blocks non-compliant deployments."] if has_deny else []),
    }


def rollout_ladder() -> list[dict[str, Any]]:
    """The safe 5-phase enforcement ladder Tag Intelligence recommends."""
    return [
        {"phase": 1, "name": "Discover", "effect": None,
         "description": "Census only. Understand which tags exist and where. No policy.", "risk": "none"},
        {"phase": 2, "name": "Report", "effect": None,
         "description": "Coverage + missing-tag reporting. Share gaps with owners. No policy.", "risk": "none"},
        {"phase": 3, "name": "Audit", "effect": "audit",
         "description": "Assign Audit policies. Non-compliant resources are flagged, never blocked.", "risk": "low"},
        {"phase": 4, "name": "Append / Inherit", "effect": "modify",
         "description": "Auto-fill missing tags via Modify (append default / inherit from RG). Remediate existing.", "risk": "medium"},
        {"phase": 5, "name": "Deny", "effect": "deny",
         "description": "Block non-compliant new deployments. Only after approval and impact review.", "risk": "high"},
    ]
