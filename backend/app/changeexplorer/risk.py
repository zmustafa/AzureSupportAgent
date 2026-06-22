"""Transparent change risk scoring (0-100) with a factor breakdown the UI can show.

Every change is scored deterministically: a category base plus additive factors (operation
type, production, dependency role, actor type, shared infra, diff severity). The function
returns the score, the label, and the list of factors that produced it, so the Risk Insights
tab can explain *why* a change is risky instead of presenting an opaque number.

Calibration note: the scoring is tuned so the demo scenario lands on the spec's expected
labels (Tags=Low, AppSetting=Medium, NSG=High, KV cert=High, AppGw=Critical, Private DNS=High,
Diag=Low).
"""
from __future__ import annotations

from typing import Any

from app.changeexplorer.classify import op_kind
from app.changeexplorer import deps

# Category base scores.
_BASE: dict[str, int] = {
    "TagsMetadata": 5,
    "Monitoring": 25,
    "CostScale": 30,
    "Unknown": 30,
    "Compute": 35,
    "Storage": 35,
    "Deployment": 40,
    "Policy": 45,
    "Security": 50,
    "AppConfiguration": 50,
    "Database": 55,
    "Network": 55,
    "KeyVault": 60,
    "DNS": 65,
    "RBAC": 65,
    "PIM": 65,
    "AppRegistration": 65,
    "ServicePrincipal": 65,
    "ManagedIdentity": 65,
    "Secret": 70,
    "Certificate": 70,
}

# High-blast network resource types get a higher base than generic networking.
_HIGH_NETWORK = ("applicationgateways", "frontdoor", "azurefirewalls", "apimanagement", "cdn/profiles")


def score(event: dict[str, Any], *, production: bool = False, shared: bool = False,
          dependency_role: str = "") -> dict[str, Any]:
    """Return {score, label, factors:[{label, delta}]} for a (mostly-built) ChangeEvent dict.

    ``event`` must carry at least ``category``, ``operation`` and ``resourceType``. ``details``
    (before/after) influence the diff-severity factor when present."""
    from app.changeexplorer.models import label_for_score

    category = event.get("category", "Unknown")
    operation = event.get("operation", "")
    rtype = (event.get("resourceType", "") or "").lower()
    role = dependency_role or event.get("dependencyRole", "")
    factors: list[dict[str, Any]] = []

    base = _BASE.get(category, 30)
    if category == "Network" and any(h in rtype for h in _HIGH_NETWORK):
        base = 70
    if category == "Policy" and "deny" in (operation or "").lower():
        base += 20
        factors.append({"label": "Policy with deny effect", "delta": 20})
    factors.append({"label": f"Base for {category}", "delta": base})
    total = base

    kind = op_kind(operation)
    op_delta = {"delete": 20, "action": 5, "write": 0, "create": 0, "read": -30}.get(kind, 0)
    if op_delta:
        factors.append({"label": f"{kind.capitalize()} operation", "delta": op_delta})
        total += op_delta

    if production:
        factors.append({"label": "Production workload", "delta": 10})
        total += 10
    if shared:
        factors.append({"label": "Shared infrastructure (larger blast radius)", "delta": 10})
        total += 10

    role_delta = {
        deps.ROLE_PUBLIC_INGRESS: 10,
        deps.ROLE_SECRET: 8,
        deps.ROLE_IDENTITY: 8,
        deps.ROLE_DATABASE: 5,
        deps.ROLE_PRIVATE_NET: 5,
    }.get(role, 0)
    if role_delta:
        factors.append({"label": f"{role}", "delta": role_delta})
        total += role_delta

    actor_type = event.get("actorType", "")
    actor_delta = {"Unknown": 10, "User": 5, "System": -5, "AzurePolicy": -5}.get(actor_type, 0)
    if actor_delta:
        lab = "Unknown actor" if actor_type == "Unknown" else ("Manual (user) change" if actor_type == "User" else "Automated platform actor")
        factors.append({"label": lab, "delta": actor_delta})
        total += actor_delta

    # Diff severity: more changed properties -> a little more risk (capped).
    n_changes = len(event.get("details") or [])
    if n_changes >= 3:
        d = min(10, (n_changes - 2) * 3)
        factors.append({"label": f"{n_changes} properties changed", "delta": d})
        total += d

    # Metadata-only changes are forced low regardless of context.
    if category == "TagsMetadata":
        total = min(total, 15)
        factors.append({"label": "Metadata-only change (capped low)", "delta": 0})

    total = max(0, min(100, total))
    return {"score": total, "label": label_for_score(total), "factors": factors}
