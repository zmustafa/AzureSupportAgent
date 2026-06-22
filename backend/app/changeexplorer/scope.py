"""WorkloadScopeBuilder — turn a (workload, scope mode) into the query scope the collectors use.

Reuses the assessments scope resolver (workload nodes -> KQL predicate + subscriptions).
Scope modes:
  * workload              — exactly the workload's resources.
  * workload_dependencies — broaden to the resource groups the workload spans (one-hop sibling
                            dependencies). Inferred — flagged so the UI shows it as such.
  * tenant                — every subscription the connection/workload touches.
"""
from __future__ import annotations

import re
from typing import Any

_SUB_RE = re.compile(r"/subscriptions/([^/]+)", re.IGNORECASE)
_RG_RE = re.compile(r"/resourceGroups/([^/]+)", re.IGNORECASE)


def _esc(v: str) -> str:
    return (v or "").replace("'", "''")


async def build_scope(workload: dict[str, Any], connection: dict[str, Any] | None, mode: str) -> dict[str, Any]:
    """Return {predicate, mode, subscriptions, resource_ids, error}."""
    from app.assessments.runner import _resolve_scope

    resolved = await _resolve_scope(workload, connection)
    subs = resolved.get("effective_subscriptions") or resolved.get("subscriptions") or []
    resource_ids = resolved.get("resource_ids") or []

    if mode == "tenant":
        predicate = resolved.get("sub_predicate", "")
        return {"predicate": predicate, "mode": mode, "subscriptions": subs,
                "resource_ids": resource_ids, "error": "" if predicate else "No subscriptions in scope."}

    if mode == "workload_dependencies":
        # Broaden id-scoped resources to their resource groups (sibling dependencies).
        rg_pairs: set[tuple[str, str]] = set(tuple(p) for p in (resolved.get("rg_pairs") or []))
        for rid in resource_ids:
            ms, mr = _SUB_RE.search(rid), _RG_RE.search(rid)
            if ms and mr:
                rg_pairs.add((ms.group(1), mr.group(1)))
        clauses = [f"(subscriptionId =~ '{_esc(s)}' and resourceGroup =~ '{_esc(rg)}')" for s, rg in sorted(rg_pairs)]
        if resolved.get("subscriptions"):
            joined = ", ".join(f"'{_esc(s)}'" for s in sorted(resolved["subscriptions"]))
            clauses.append(f"subscriptionId in~ ({joined})")
        predicate = " or ".join(clauses) if clauses else resolved.get("predicate", "")
        return {"predicate": predicate, "mode": mode, "subscriptions": subs,
                "resource_ids": resource_ids, "error": "" if predicate else resolved.get("error", "")}

    # workload (default)
    return {"predicate": resolved.get("predicate", ""), "mode": "workload", "subscriptions": subs,
            "resource_ids": resource_ids, "error": resolved.get("error", "")}
