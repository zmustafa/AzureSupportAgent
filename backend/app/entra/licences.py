"""Entra license detection from ``/subscribedSkus``.

Half the product needs Entra ID P1 or P2. Without detection, an unlicensed tenant gets
silent empty screens and a score that looks catastrophic for the wrong reason.

License flags are **advisory, not gating**: we always attempt the call and let a real 403
be authoritative, because real tenants have odd license mixes (some users P2, some P1) and
a tenant-level flag is only ever an approximation. The flag exists to *explain* an empty
pillar, not to prevent the attempt.
"""
from __future__ import annotations

import logging
from typing import Any

from app.entra.graphclient import GraphClient, GraphError, GraphPermissionError

log = logging.getLogger("app.entra.licences")

# Service plan names that imply each tier. Bundles (EMS, M365 E3/E5, ...) all surface the
# underlying AAD_PREMIUM / AAD_PREMIUM_P2 service plan, so matching on service plans rather
# than SKU part numbers covers every bundle without an exhaustive SKU table.
_P1_PLANS = {"AAD_PREMIUM", "AAD_PREMIUM_P2"}
_P2_PLANS = {"AAD_PREMIUM_P2"}
_GOVERNANCE_PLANS = {"Microsoft_Entra_ID_Governance", "ENTRA_IDENTITY_GOVERNANCE"}
_WORKLOAD_PLANS = {"Entra_Workload_IdentityPremium", "WORKLOAD_IDENTITY_PREMIUM", "Entra_Workload_Identities"}

# Fallback SKU part numbers, for tenants whose servicePlans list is unexpectedly sparse.
_P1_SKUS = {"AAD_PREMIUM", "AAD_PREMIUM_P2", "EMS", "EMSPREMIUM", "SPE_E3", "SPE_E5", "SPE_F1"}
_P2_SKUS = {"AAD_PREMIUM_P2", "EMSPREMIUM", "SPE_E5"}

# What each tier unlocks — rendered on the setup screen and on unlicensed empty states.
TIER_VALUE: dict[str, str] = {
    "p1": "Conditional Access, sign-in log retention, last-sign-in activity and the MFA registration report.",
    "p2": "Privileged Identity Management, Identity Protection risk signals, and access reviews.",
    "governance": "Lifecycle workflows and advanced entitlement management.",
    "workload_id_premium": "Risky workload identities and Conditional Access for service principals.",
}


def empty_flags(reason: str = "") -> dict[str, Any]:
    """Unknown licensing — every flag False with the reason recorded."""
    return {
        "p1": False,
        "p2": False,
        "governance": False,
        "workload_id_premium": False,
        "detected": False,
        "reason": reason,
        "skus": [],
    }


def flags_from_skus(skus: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive tier flags from a ``/subscribedSkus`` payload (pure — unit-testable)."""
    plans: set[str] = set()
    parts: set[str] = set()
    summary: list[dict[str, Any]] = []
    for sku in skus or []:
        if not isinstance(sku, dict):
            continue
        part = str(sku.get("skuPartNumber") or "")
        parts.add(part)
        enabled = 0
        for plan in sku.get("servicePlans") or []:
            if not isinstance(plan, dict):
                continue
            # Only count plans that are actually provisioned for the tenant.
            if str(plan.get("provisioningStatus") or "").lower() in ("success", "pendinginput", "pendingactivation", ""):
                name = str(plan.get("servicePlanName") or "")
                if name:
                    plans.add(name)
                    enabled += 1
        units = sku.get("prepaidUnits") or {}
        summary.append({
            "sku": part,
            "enabled_units": int(units.get("enabled") or 0) if isinstance(units, dict) else 0,
            "consumed_units": int(sku.get("consumedUnits") or 0),
            "service_plans": enabled,
        })

    p2 = bool(plans & _P2_PLANS) or bool(parts & _P2_SKUS)
    p1 = p2 or bool(plans & _P1_PLANS) or bool(parts & _P1_SKUS)
    return {
        "p1": p1,
        "p2": p2,
        "governance": bool(plans & _GOVERNANCE_PLANS),
        "workload_id_premium": bool(plans & _WORKLOAD_PLANS),
        "detected": True,
        "reason": "",
        "skus": sorted(summary, key=lambda s: -s["enabled_units"])[:40],
    }


async def detect(client: GraphClient) -> dict[str, Any]:
    """Read ``/subscribedSkus`` and derive the tier flags. Never raises."""
    try:
        skus, _ = await client.get_all(
            "/subscribedSkus",
            select=["skuId", "skuPartNumber", "servicePlans", "prepaidUnits", "consumedUnits"],
            top=0,
        )
    except GraphPermissionError as exc:
        return empty_flags(f"Organization.Read.All not granted — licence tier unknown ({exc.message[:120]}).")
    except GraphError as exc:
        return empty_flags(f"Could not read subscribed SKUs: {exc}")
    except Exception as exc:  # noqa: BLE001 - license detection is never fatal
        log.warning("entra licence detection failed", exc_info=True)
        return empty_flags(f"Could not read subscribed SKUs: {exc}")
    return flags_from_skus(skus)


def licence_ok(flags: dict[str, Any], licence: str) -> bool:
    """True when the tenant satisfies a ``SignalSpec.licence`` requirement.

    ``detected == False`` is treated as *satisfied* on purpose — if we could not read the
    SKUs we must still attempt the check and let a real 403 tell the truth, rather than
    silently hiding half the product behind a guess."""
    if licence in ("", "free"):
        return True
    if not flags.get("detected"):
        return True
    return bool(flags.get(licence))


def licence_label(licence: str) -> str:
    return {
        "p1": "Entra ID P1",
        "p2": "Entra ID P2",
        "governance": "Entra ID Governance",
        "workload_id_premium": "Entra Workload Identities Premium",
    }.get(licence, "")
