"""Precomputed pivot summaries for the RBAC Insights tab.

Server-side equivalents of the scanner workbook's Pivots sheet: count access rows along the
axes a reviewer actually asks about (by role, by principal, by subscription, …). Each pivot is
a sorted list of ``{label, count}`` so the UI can render compact bar lists without shipping the
full row set."""
from __future__ import annotations

from collections import Counter
from typing import Any, Callable

from app.rbac import schema


def _top(rows: list[dict[str, Any]], key: Callable[[dict[str, Any]], str], *, limit: int = 15) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for r in rows:
        label = key(r)
        if label:
            counter[label] += 1
    return [{"label": label, "count": count} for label, count in counter.most_common(limit)]


def compute_pivots(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """The 13 pivot sections, mirroring the workbook's Pivots sheet."""
    privileged = [r for r in rows if r.get("roleIsPrivileged")]
    data_plane = [r for r in rows if r.get("roleHasDataActions")]
    group_rows = [r for r in rows if r.get("accessPath") == schema.PATH_GROUP]
    eligible = [r for r in rows if r.get("assignmentState") == schema.STATE_ELIGIBLE]
    active = [r for r in rows if r.get("assignmentState") == schema.STATE_ACTIVE]

    return {
        "by_surface": _top(rows, lambda r: r.get("surface", "")),
        "by_role": _top(rows, lambda r: r.get("roleName", "")),
        "by_principal_type": _top(rows, lambda r: r.get("effectivePrincipalType", "") or r.get("principalType", "")),
        "by_principal": _top(rows, lambda r: r.get("effectivePrincipalName", "") or r.get("principalDisplayName", "")),
        "by_subscription": _top(rows, lambda r: r.get("subscriptionName", "") or r.get("subscriptionId", "")),
        "by_scope_type": _top(rows, lambda r: r.get("scopeType", "")),
        "privileged_by_principal": _top(privileged, lambda r: r.get("effectivePrincipalName", "") or r.get("principalDisplayName", "")),
        "data_plane_by_resource_type": _top(data_plane, lambda r: r.get("resourceType", "") or "(subscription/RG)"),
        "group_derived_by_group": _top(group_rows, lambda r: r.get("sourceGroupName", "")),
        "access_by_role_category": _top(rows, lambda r: r.get("roleCategory", "")),
        "pim_eligible_vs_active": [
            {"label": "Eligible", "count": len(eligible)},
            {"label": "Active", "count": len(active)},
        ],
        "by_access_path": _top(rows, lambda r: r.get("accessPath", "")),
        "privileged_by_subscription": _top(privileged, lambda r: r.get("subscriptionName", "") or r.get("subscriptionId", "") or "(directory)"),
    }


PIVOT_LABELS = {
    "by_surface": "Access by surface",
    "by_role": "Access by role",
    "by_principal_type": "Access by principal type",
    "by_principal": "Access by principal",
    "by_subscription": "Access by subscription",
    "by_scope_type": "Access by scope type",
    "privileged_by_principal": "Privileged roles by principal",
    "data_plane_by_resource_type": "Data-plane roles by resource type",
    "group_derived_by_group": "Group-derived access by group",
    "access_by_role_category": "Access by role category",
    "pim_eligible_vs_active": "PIM eligible vs active",
    "by_access_path": "Access by path",
    "privileged_by_subscription": "Privileged access by subscription",
}
