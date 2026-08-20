"""Backup cost: live-priced estimation, actual-spend allocation, variance, and waste.

Three sources answer three different questions, and the module carries all three with honest
labeling rather than pretending one is the others:

* **Cost Management** — what backup *actually cost*. Authoritative, lags 8-24h, and attributes
  every charge to the **vault**, never to the protected item.
* **Log Analytics Backup Reports** — how much each item actually *consumes*. The only source
  of per-item truth, and therefore the weighting used to allocate vault spend down to items.
* **Retail list prices** — what protection *would* cost. The only source that can price a
  resource that is not protected yet, which the gap remediation planner depends on.

Combining the first two is what makes per-item cost meaningful: actual totals apportioned by
actual consumption reconcile exactly to the invoice, which neither source manages alone.

Azure Backup prices a protected instance at a **flat monthly rate per datasource type**
(Azure VM, Azure Files, SQL-in-VM, SAP HANA...), not on a tier over source size. Azure Disk
Backup has no protected-instance meter at all — it is snapshot-billed outside the Backup
service — and is reported as such rather than silently costed at zero.
"""
from __future__ import annotations

from typing import Any

from app.backup_manager import pricing, reference

# Datasource families whose vault storage is priced on the Azure Files meters.
_FILES_DATASOURCES = ("azurefileshare", "microsoft.storage/storageaccounts/fileservices")


def reference_rate_card() -> dict[str, Any]:
    """Fallback rate card from the editable reference, used when retail prices are unreachable.

    Deliberately marked ``source: "reference"`` so the UI can say the numbers are seeded list
    prices in the reference's currency rather than live ones in the billing currency."""
    rates = reference.cost_rates()
    instance = rates.get("protected_instance") or {}
    return {
        "source": "reference",
        "currency": rates.get("currency", "USD"),
        "region": "",
        "as_of": rates.get("as_of", ""),
        # One flat rate for every datasource: the seeded table cannot distinguish them.
        "instance_meters": {},
        "fallback_instance_rate": float(instance.get("50_to_500gb", 10.0)),
        "storage_gb_month": dict(rates.get("storage_gb_month") or {}),
        "files_storage_gb_month": {},
        "site_recovery_instance_month": float(rates.get("site_recovery_instance_month", 25.0)),
        "meter_count": 0,
        "error": "",
    }


def _is_files(instance: dict[str, Any]) -> bool:
    token = str(instance.get("datasource_type") or "").strip().lower()
    return token in _FILES_DATASOURCES or token.endswith("fileshare")


def _instance_rate(card: dict[str, Any], instance: dict[str, Any]) -> tuple[float | None, str, str]:
    """Return ``(monthly rate, meter key, note)`` for one protected item."""
    rate, key = pricing.instance_rate(
        card, instance.get("datasource_type", ""), instance.get("backup_management_type", ""),
    )
    if key == "no_instance_meter":
        return 0.0, key, "Snapshot-billed: Azure Disk Backup has no protected-instance charge."
    if rate is None:
        fallback = card.get("fallback_instance_rate")
        if fallback is not None:
            return float(fallback), "fallback", "Seeded flat rate; no live meter matched."
        return None, "", "No retail meter matched this datasource type."
    return rate, key, ""


def estimate(
    estate: dict[str, Any],
    *,
    rate_card: dict[str, Any] | None = None,
    storage_by_instance: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Forward-looking monthly cost for the in-scope estate at list prices.

    ``storage_by_instance`` (consumed GB, from Log Analytics) replaces the size assumption
    where available; every row records whether its inputs were real.
    """
    card = rate_card or reference_rate_card()
    legacy = reference.cost_rates()
    assumed_gb = float(legacy.get("assumed_instance_gb", 200.0))
    storage_map = {k.lower(): v for k, v in (storage_by_instance or {}).items()}
    vault_index = {v["id"].lower(): v for v in estate.get("vaults", [])}

    rows: list[dict[str, Any]] = []
    instance_total = 0.0
    storage_total = 0.0
    measured_instances = 0
    unpriced = 0
    for instance in estate.get("instances", []):
        vault = vault_index.get(str(instance.get("vault_id") or "").lower(), {})
        stored_gb = storage_map.get(str(instance.get("id") or "").lower())
        measured = stored_gb is not None
        if measured:
            measured_instances += 1
        effective_stored = float(stored_gb if stored_gb is not None else assumed_gb)

        rate, meter_key, note = _instance_rate(card, instance)
        if rate is None:
            unpriced += 1
            instance_cost = 0.0
        else:
            instance_cost = float(rate)
        per_gb = pricing.storage_rate(card, vault.get("redundancy", ""), files=_is_files(instance))
        if per_gb is None:
            per_gb = 0.0
            note = note or "No storage meter matched this vault's redundancy."
        storage_cost = effective_stored * per_gb

        instance_total += instance_cost
        storage_total += storage_cost
        rows.append({
            "instance_id": instance.get("id", ""),
            "name": instance.get("friendly_name", ""),
            "datasource_id": instance.get("datasource_id", ""),
            "datasource_type": instance.get("datasource_type", ""),
            "vault_id": instance.get("vault_id", ""),
            "vault_name": instance.get("vault_name", ""),
            "redundancy": vault.get("redundancy", ""),
            "stored_gb": round(effective_stored, 1),
            "instance_cost": round(instance_cost, 2),
            "storage_cost": round(storage_cost, 2),
            "monthly_cost": round(instance_cost + storage_cost, 2),
            "meter": meter_key,
            "measured": measured,
            "priced": rate is not None,
            "note": note,
        })

    replication_count = len(estate.get("replication", []))
    asr_rate = card.get("site_recovery_instance_month")
    asr_total = replication_count * float(asr_rate) if asr_rate is not None else 0.0
    rows.sort(key=lambda r: -r["monthly_cost"])
    total = instance_total + storage_total + asr_total
    return {
        "currency": card.get("currency", "USD"),
        "region": card.get("region", ""),
        "as_of": card.get("as_of", ""),
        "rate_source": card.get("source", "reference"),
        "rate_error": card.get("error", ""),
        "estimate_only": True,
        "source": (
            "Azure Retail Prices (live list prices)"
            if card.get("source") == "azure_retail_prices"
            else legacy.get("source", "")
        ),
        "protected_instance_cost": round(instance_total, 2),
        "storage_cost": round(storage_total, 2),
        "site_recovery_cost": round(asr_total, 2),
        "monthly_total": round(total, 2),
        "annual_total": round(total * 12, 2),
        "instance_count": len(rows),
        "replicated_item_count": replication_count,
        "measured_instances": measured_instances,
        "unpriced_instances": unpriced,
        "assumed_instance_gb": assumed_gb,
        "confidence": (
            "measured" if rows and measured_instances == len(rows)
            else ("partial" if measured_instances else "assumed")
        ),
        "top_rows": rows[:200],
    }


# --------------------------------------------------------------------------- allocation
def allocate(
    estate: dict[str, Any],
    actuals: dict[str, Any],
    *,
    estimate_rows: list[dict[str, Any]] | None = None,
    storage_by_instance: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Split each vault's *actual* spend across the items it protects.

    Cost Management stops at the vault, so per-item cost has to be apportioned. Weights come
    from the best available signal, in order: measured consumed GB, then the item's estimated
    cost, then an equal share. Every vault's allocated rows sum back to its actual total (the
    rounding remainder lands on the largest row), so the view reconciles to the invoice
    instead of drifting from it.
    """
    by_vault = {k.lower(): float(v) for k, v in (actuals.get("by_vault") or {}).items()}
    storage_map = {k.lower(): float(v) for k, v in (storage_by_instance or {}).items()}
    estimate_map = {
        str(r.get("instance_id") or "").lower(): float(r.get("monthly_cost") or 0.0)
        for r in (estimate_rows or [])
    }

    instances_by_vault: dict[str, list[dict[str, Any]]] = {}
    for instance in estate.get("instances", []):
        instances_by_vault.setdefault(str(instance.get("vault_id") or "").lower(), []).append(instance)

    rows: list[dict[str, Any]] = []
    allocated_vaults = 0
    unattributed = 0.0
    for vault_id, vault_cost in by_vault.items():
        members = instances_by_vault.get(vault_id, [])
        if not members:
            # Spend on a vault with no protected items in scope (replication-only vault, a
            # vault outside the selected scope, or soft-deleted data). Never silently dropped.
            unattributed += vault_cost
            continue
        allocated_vaults += 1

        weights: list[float] = []
        basis = "equal"
        if all(storage_map.get(str(m.get("id") or "").lower()) for m in members):
            weights = [storage_map[str(m["id"]).lower()] for m in members]
            basis = "consumed_gb"
        elif any(estimate_map.get(str(m.get("id") or "").lower()) for m in members):
            weights = [estimate_map.get(str(m.get("id") or "").lower(), 0.0) for m in members]
            basis = "estimated_cost"
        if not weights or sum(weights) <= 0:
            weights = [1.0] * len(members)
            basis = "equal"

        total_weight = sum(weights)
        rounded = [round(vault_cost * (w / total_weight), 2) for w in weights]
        # Push the rounding remainder onto the largest row so the vault reconciles exactly.
        drift = round(vault_cost - sum(rounded), 2)
        if rounded and abs(drift) >= 0.01:
            largest = max(range(len(rounded)), key=lambda i: rounded[i])
            rounded[largest] = round(rounded[largest] + drift, 2)

        for member, value, weight in zip(members, rounded, weights):
            rows.append({
                "instance_id": member.get("id", ""),
                "name": member.get("friendly_name", ""),
                "datasource_type": member.get("datasource_type", ""),
                "vault_id": member.get("vault_id", ""),
                "vault_name": member.get("vault_name", ""),
                "allocated_cost": value,
                "vault_total": round(vault_cost, 2),
                "weight": round(weight, 3),
                "weight_basis": basis,
            })

    rows.sort(key=lambda r: -r["allocated_cost"])
    return {
        "rows": rows,
        "currency": actuals.get("currency", ""),
        "allocated_total": round(sum(r["allocated_cost"] for r in rows), 2),
        "unattributed_total": round(unattributed, 2),
        "vaults_allocated": allocated_vaults,
        "vaults_unattributed": len(by_vault) - allocated_vaults,
        "basis_counts": {
            basis: sum(1 for r in rows if r["weight_basis"] == basis)
            for basis in ("consumed_gb", "estimated_cost", "equal")
        },
        "note": (
            "Azure Cost Management attributes backup charges to the vault, not to individual "
            "protected items. These per-item figures are the vault's actual spend apportioned "
            "by measured consumption, and sum back to it exactly."
        ),
    }


def variance(estimate_result: dict[str, Any], actuals: dict[str, Any]) -> dict[str, Any]:
    """Compare the list-price estimate against actual spend, and say whether it is comparable."""
    estimated = float(estimate_result.get("monthly_total") or 0.0)
    actual = float(actuals.get("total") or 0.0)
    same_currency = (
        str(estimate_result.get("currency") or "").upper() == str(actuals.get("currency") or "").upper()
    )
    partial = bool(actuals.get("partial_period"))
    available = bool(actuals.get("available")) and bool(actuals.get("currency"))
    comparable = available and same_currency and not partial
    delta = round(actual - estimated, 2)
    if comparable:
        reason = ""
    elif not available:
        reason = actuals.get("reason") or "Actual spend is unavailable."
    elif partial:
        reason = "Month-to-date actuals cannot be compared with a full-month estimate."
    elif not same_currency:
        reason = (
            f"Estimate is in {estimate_result.get('currency')} but the bill is in "
            f"{actuals.get('currency')}."
        )
    else:
        reason = "Not comparable."
    return {
        "comparable": comparable,
        "estimated": round(estimated, 2),
        "actual": round(actual, 2),
        "delta": delta,
        "delta_pct": round(100 * delta / estimated, 1) if estimated else None,
        "estimate_currency": estimate_result.get("currency", ""),
        "actual_currency": actuals.get("currency", ""),
        "period": actuals.get("period", {}),
        "by_meter": actuals.get("by_meter", {}),
        "reason": reason,
    }


# --------------------------------------------------------------------------- waste
def waste(
    estate: dict[str, Any],
    *,
    policies: list[dict[str, Any]] | None = None,
    rate_card: dict[str, Any] | None = None,
    cost_by_instance: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Recoverable spend — every item here is money leaving with no protection value.

    ``cost_by_instance`` (allocated actuals) is used where available so the figures are real
    money rather than list-price arithmetic."""
    card = rate_card or reference_rate_card()
    legacy = reference.cost_rates()
    assumed_gb = float(legacy.get("assumed_instance_gb", 200.0))
    actual_map = {k.lower(): float(v) for k, v in (cost_by_instance or {}).items()}
    vault_index = {v["id"].lower(): v for v in estate.get("vaults", [])}
    findings: list[dict[str, Any]] = []

    def monthly_for(instance: dict[str, Any]) -> float:
        actual = actual_map.get(str(instance.get("id") or "").lower())
        if actual is not None:
            return round(actual, 2)
        vault = vault_index.get(str(instance.get("vault_id") or "").lower(), {})
        rate, _key, _note = _instance_rate(card, instance)
        per_gb = pricing.storage_rate(card, vault.get("redundancy", ""), files=_is_files(instance)) or 0.0
        return round(float(rate or 0.0) + assumed_gb * per_gb, 2)

    for instance in estate.get("instances", []):
        if instance.get("orphaned"):
            findings.append({
                "kind": "orphaned_protection",
                "severity": "error",
                "title": "Source resource no longer exists",
                "detail": "The protected datasource has been deleted but the backup instance still bills.",
                "instance_id": instance.get("id", ""),
                "datasource_id": instance.get("datasource_id", ""),
                "vault_id": instance.get("vault_id", ""),
                "name": instance.get("friendly_name", ""),
                "vault_name": instance.get("vault_name", ""),
                "monthly_cost": monthly_for(instance),
                "action": "Review in the Azure portal — removing backup data is a portal-only operation.",
            })
        elif instance.get("retain_data_only"):
            findings.append({
                "kind": "stopped_with_data",
                "severity": "warning",
                "title": "Protection stopped, data retained",
                "detail": "No new recovery points are created, but retained data continues to bill.",
                "instance_id": instance.get("id", ""),
                "datasource_id": instance.get("datasource_id", ""),
                "vault_id": instance.get("vault_id", ""),
                "name": instance.get("friendly_name", ""),
                "vault_name": instance.get("vault_name", ""),
                "monthly_cost": monthly_for(instance),
                "action": "Resume protection, or retire the item in the Azure portal.",
            })

    seen: dict[str, list[dict[str, Any]]] = {}
    for instance in estate.get("instances", []):
        key = str(instance.get("datasource_id") or "")
        if key:
            seen.setdefault(key, []).append(instance)
    for datasource, members in seen.items():
        if len(members) > 1:
            findings.append({
                "kind": "duplicate_protection",
                "severity": "warning",
                "title": "Datasource protected more than once",
                "detail": f"{len(members)} backup instances protect the same datasource.",
                "instance_id": members[0].get("id", ""),
                "datasource_id": datasource,
                "vault_id": members[0].get("vault_id", ""),
                "name": members[0].get("friendly_name", ""),
                "vault_name": ", ".join(sorted({m.get("vault_name", "") for m in members})),
                "monthly_cost": round(sum(monthly_for(m) for m in members[1:]), 2),
                "action": "Retire the redundant protection so only one vault holds this datasource.",
            })

    for vault in estate.get("vaults", []):
        if vault.get("empty"):
            findings.append({
                "kind": "empty_vault",
                "severity": "info",
                "title": "Vault holds nothing",
                "detail": "No protected items and no replicated items.",
                "instance_id": vault.get("id", ""),
                "datasource_id": "",
                "vault_id": vault.get("id", ""),
                "name": vault.get("name", ""),
                "vault_name": vault.get("name", ""),
                "monthly_cost": 0.0,
                "action": "Delete the vault if it is no longer needed; it adds management surface and limit pressure.",
            })

    floor = int(reference.tier_for(None).get("retention_days") or 30)
    for policy in policies or estate.get("policies", []):
        retention = policy.get("retention_days")
        in_use = int(policy.get("in_use_count") or 0)
        if retention and retention > floor * 4 and in_use:
            findings.append({
                "kind": "over_retention",
                "severity": "info",
                "title": "Retention far above the baseline",
                "detail": f"{policy.get('name')} retains {retention} days against a {floor}-day baseline across {in_use} item(s).",
                "instance_id": policy.get("arm_id", policy.get("id", "")),
                "datasource_id": "",
                "vault_id": policy.get("vault_id", ""),
                "name": policy.get("name", ""),
                "vault_name": policy.get("vault_name", ""),
                "monthly_cost": 0.0,
                "action": "Confirm the retention is a deliberate compliance requirement; model the change first.",
            })

    severity_rank = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (severity_rank.get(f["severity"], 3), -f["monthly_cost"]))
    return {
        "findings": findings,
        "recoverable_monthly": round(sum(f["monthly_cost"] for f in findings), 2),
        "counts": {
            kind: sum(1 for f in findings if f["kind"] == kind)
            for kind in ("orphaned_protection", "stopped_with_data", "duplicate_protection", "empty_vault", "over_retention")
        },
        "currency": (actual_map and str(card.get("currency") or "")) or card.get("currency", "USD"),
        "basis": "actual" if actual_map else "estimated",
    }
