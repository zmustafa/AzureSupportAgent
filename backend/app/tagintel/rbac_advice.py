"""Least-privilege RBAC guidance for tag operations (F11).

A static map from each Tag Intelligence action to the minimum Azure built-in role and the
scope it should be granted at. Advisory only — Tag Intelligence never assigns roles; it shows
the exact ``az role assignment`` command the operator can run.
"""
from __future__ import annotations

from typing import Any

# Built-in role definition ids (stable GUIDs).
_ROLES = {
    "Reader": "acdd72a7-3385-48ef-bd42-f606fba81ae7",
    "Tag Contributor": "4a9ae827-6dc8-4573-8ac7-8239d42aa03f",
    "Resource Policy Contributor": "36243c78-bf99-498c-9df9-86d9f8d28608",
    "Cost Management Reader": "72fafb9e-0641-4937-9268-a91bfd8191a3",
}

_ADVICE: list[dict[str, Any]] = [
    {"action": "Tag discovery, census, hygiene, coverage", "role": "Reader",
     "scope": "Management group or subscription (read-only)",
     "why": "Resource Graph reads every resource's tags. Reader is sufficient — no write access."},
    {"action": "Cost allocation & billing map", "role": "Cost Management Reader",
     "scope": "Billing account / management group / subscription",
     "why": "Reads Cost Management actuals. Pair with Reader for the resource join."},
    {"action": "Apply tag remediation (add / rename / normalize)", "role": "Tag Contributor",
     "scope": "Resource group or subscription containing the targets",
     "why": "Grants ONLY Microsoft.Resources/tags write — cannot modify the resources themselves."},
    {"action": "Create / assign tag policies", "role": "Resource Policy Contributor",
     "scope": "Management group or subscription for the assignment",
     "why": "Creates policy definitions/initiatives and assignments without broad Contributor rights."},
]


def advice() -> dict[str, Any]:
    rows = []
    for a in _ADVICE:
        role = a["role"]
        rid = _ROLES.get(role, "")
        rows.append({
            **a,
            "role_definition_id": rid,
            "assignment_example": (
                f"az role assignment create --role \"{role}\" "
                f"--assignee <principalId> --scope <scope>"
            ),
        })
    return {
        "rows": rows,
        "principle": "Start read-only (Reader). Grant Tag Contributor only for the scope you are remediating, "
                     "and only for the duration you need it. Never use Owner for tagging work.",
    }
