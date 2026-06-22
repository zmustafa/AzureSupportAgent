"""Plain-English explanation engine. For each ChangeEvent it produces: what happened, why it
matters, possible workload impact, why the risk score was assigned, and a confidence level.

Deterministic + template-driven so it never invents evidence. Inferred impact uses "could
impact" language; only data-backed statements are stated as fact. An optional AI layer (see
service) may narrate the run-level summary on top of these per-event facts.
"""
from __future__ import annotations

from typing import Any

from app.changeexplorer.classify import op_kind
from app.changeexplorer import deps

# category -> (what-it-is phrase, why-it-matters phrase)
_CATEGORY_TEXT: dict[str, tuple[str, str]] = {
    "Network": ("a networking resource", "networking changes can affect connectivity, routing and exposure"),
    "DNS": ("a DNS record or zone", "DNS changes can repoint traffic and affect name resolution for the workload"),
    "Identity": ("an identity object", "identity changes can affect authentication and access"),
    "RBAC": ("a role assignment", "RBAC changes alter who can access or modify resources"),
    "PIM": ("a privileged-access assignment", "PIM changes alter just-in-time privileged access"),
    "AppRegistration": ("an app registration", "app-registration changes can affect authentication and API permissions"),
    "ServicePrincipal": ("a service principal", "service-principal changes can affect automated access"),
    "ManagedIdentity": ("a managed identity", "managed-identity changes can affect how the workload authenticates"),
    "Secret": ("a Key Vault secret", "secret changes can break consumers that reference the value"),
    "Certificate": ("a Key Vault certificate", "certificate changes can cause TLS or auth failures if a consumer uses a rotated value"),
    "KeyVault": ("a Key Vault", "Key Vault changes can affect access to secrets, keys and certificates"),
    "AppConfiguration": ("application configuration", "app-setting or connection-string changes can change runtime behavior"),
    "Deployment": ("a deployment", "deployments can change many resources at once"),
    "Policy": ("an Azure Policy object", "policy changes can allow or block future deployments"),
    "Security": ("a security setting", "security changes can affect posture and protection"),
    "Compute": ("a compute resource", "compute changes can affect availability and performance"),
    "Storage": ("a storage resource", "storage changes can affect data access and exposure"),
    "Database": ("a database resource", "database changes can affect connectivity and data access"),
    "Monitoring": ("a monitoring resource", "monitoring changes affect observability, not the running workload directly"),
    "CostScale": ("a scale/SKU setting", "scale changes can affect capacity and cost"),
    "TagsMetadata": ("resource tags/metadata", "tag changes are usually cosmetic unless automation depends on them"),
    "Unknown": ("a resource", "the change type could not be classified precisely"),
}

_OP_VERB = {"delete": "deleted", "create": "created", "action": "ran an action on", "write": "modified", "read": "read"}


def explain(event: dict[str, Any]) -> dict[str, Any]:
    """Return {plainEnglishSummary, possibleImpact, whyRisk, confidence} for a scored event."""
    category = event.get("category", "Unknown")
    rname = event.get("resourceName", "the resource")
    rtype = event.get("resourceType", "")
    actor = event.get("actor", "an actor")
    actor_type = event.get("actorType", "Unknown")
    label = event.get("riskLabel", "Low")
    role = event.get("dependencyRole") or deps.role_for(rtype, rname)
    kind = op_kind(event.get("operation", ""))
    what_is, why = _CATEGORY_TEXT.get(category, _CATEGORY_TEXT["Unknown"])
    verb = _OP_VERB.get(kind, "changed")

    # What happened (fact, from the event).
    summary = f"{actor} {verb} {rname}, {what_is}."
    if event.get("details"):
        first = event["details"][0]
        summary += f" {first.get('technicalSummary','')}".rstrip()

    # Possible impact (inferred — uses 'could' language unless metadata-only).
    if category == "TagsMetadata":
        impact = "Cosmetic metadata change. No workload impact expected unless automation keys off these tags."
    else:
        impact = f"Because this is {role.lower()}, it could impact the workload — {why}."
        impact = impact[0].upper() + impact[1:]

    # Why this risk score (transparent, from factors).
    factors = event.get("riskFactors") or []
    top = sorted(factors, key=lambda f: -abs(int(f.get("delta", 0))))[:3]
    why_risk = f"Scored {event.get('riskScore', 0)}/100 ({label}). Main factors: " + ", ".join(
        f"{f['label']} ({'+' if f['delta'] >= 0 else ''}{f['delta']})" for f in top
    ) + "." if top else f"Scored {event.get('riskScore', 0)}/100 ({label})."

    # Confidence in the interpretation: high when we have a real diff, medium otherwise.
    confidence = "High" if event.get("details") else ("Medium" if category != "Unknown" else "Low")

    return {"plainEnglishSummary": summary, "possibleImpact": impact, "whyRisk": why_risk, "confidence": confidence}
