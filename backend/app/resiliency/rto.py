"""RTO duration bands — the estimation half, and the riskiest code in the module.

The failure mode here is not imprecision. It is that a plausible number gets copied into a
DR plan, signed off, and discovered to be wrong during an incident, at the exact moment
nobody can absorb a surprise. So six rules are enforced in code rather than left to the UI:

1. a band never exists without the assumptions that produced it;
2. unknown size widens the band AND drops confidence — never a default size;
3. the rates that produced a band are named in its assumptions;
4. the rates live in a registry the operator can see and change;
5. a band is always a range, never a midpoint;
6. no band at all for ``none`` or ``unknown`` — there is nothing to time.

:func:`band_for` returns ``None`` rather than a guess whenever it cannot honour those.
"""
from __future__ import annotations

import math
from typing import Any

from app.resiliency import model, reference

# How much slack to allow either side of the point estimate. Restores are not
# deterministic: contention, throttling and cold storage all move the number, and a band
# that pretends otherwise invites the false precision this module exists to avoid.
_SPREAD_KNOWN = 0.5     # +/- 50% when the data volume is known
_SPREAD_UNKNOWN = 3.0   # much wider when it is not

# Used ONLY to widen a band when the size is unknown, and always reported as an assumption.
# Never used to produce a confident number.
_ASSUMED_GB_WHEN_UNKNOWN = 100

_RATE_BY_TYPE: dict[str, str] = {
    "microsoft.compute/virtualmachines": "vm_restore_mbps",
    "microsoft.compute/disks": "disk_restore_mbps",
    "microsoft.storage/storageaccounts": "blob_restore_mbps",
}


def _minutes_for_gb(gb: float, mbps: int) -> int:
    """Transfer time in minutes. ``mbps`` is megabytes per second, as the registry states."""
    if mbps <= 0:
        return 0
    seconds = (gb * 1024) / mbps
    return int(math.ceil(seconds / 60))


def band_for(
    resource_type: str,
    rto_class: str,
    *,
    size_gb: int | None,
    mechanism: str = "",
    doc: dict[str, Any] | None = None,
) -> tuple[tuple[int, int], tuple[str, ...], str] | None:
    """``((low, high) minutes, assumptions, confidence)`` — or ``None`` when a band would lie.

    Returns ``None`` for classes with nothing to time (``automatic``, ``none``, ``unknown``)
    and whenever the inputs cannot support a range.
    """
    if rto_class in (model.RTO_AUTOMATIC, model.RTO_NONE, model.RTO_UNKNOWN):
        return None

    doc = doc or reference.load()
    rates = doc.get("restore_rates") or {}
    mechanisms = doc.get("mechanism_minutes") or {}

    overhead = int(mechanisms.get("detect_and_decide", 30))
    assumptions: list[str] = [
        f"{overhead} min to detect and decide (mechanism_minutes.detect_and_decide)"]

    if mechanism and mechanism in mechanisms:
        overhead += int(mechanisms[mechanism])
        assumptions.append(f"{mechanisms[mechanism]} min for {mechanism} "
                           f"(mechanism_minutes.{mechanism})")

    if rto_class == model.RTO_MINUTES:
        return (max(1, overhead // 2), overhead + 15), tuple(assumptions), model.CONFIDENCE_MEDIUM

    rate_key = _RATE_BY_TYPE.get((resource_type or "").lower(), "generic_restore_mbps")
    rate = int(rates.get(rate_key, 50))

    if size_gb and size_gb > 0:
        transfer = _minutes_for_gb(float(size_gb), rate)
        assumptions.append(f"{size_gb} GB at {rate} MB/s (restore_rates.{rate_key})")
        spread, confidence = _SPREAD_KNOWN, model.CONFIDENCE_MEDIUM
    else:
        transfer = _minutes_for_gb(float(_ASSUMED_GB_WHEN_UNKNOWN), rate)
        assumptions.append(
            f"data volume is unknown, so this range is deliberately wide — it assumes "
            f"roughly {_ASSUMED_GB_WHEN_UNKNOWN} GB at {rate} MB/s (restore_rates.{rate_key})")
        spread, confidence = _SPREAD_UNKNOWN, model.CONFIDENCE_LOW

    centre = overhead + transfer
    low = max(1, int(centre * (1 - min(spread, 0.9))))
    high = int(math.ceil(centre * (1 + spread)))
    if high <= low:
        high = low + 1
    assumptions.append("unverified — no recovery drill has confirmed this")
    return (low, high), tuple(assumptions), confidence


def apply_bands(
    verdicts: dict[str, model.Verdict],
    *,
    resource_type: str,
    size_gb: int | None,
    doc: dict[str, Any] | None = None,
) -> dict[str, model.Verdict]:
    """Attach a duration band to every verdict that can carry one honestly."""
    doc = doc or reference.load()
    out: dict[str, model.Verdict] = {}
    for scenario, v in verdicts.items():
        if not v.applicable:
            out[scenario] = v
            continue
        mechanism = _mechanism_for(scenario, v)
        result = band_for(resource_type, v.rto_class, size_gb=size_gb,
                          mechanism=mechanism, doc=doc)
        if result is None:
            out[scenario] = v
            continue
        band, assumptions, confidence = result
        out[scenario] = model.Verdict(
            scenario=v.scenario, rpo_minutes=v.rpo_minutes, rpo_state=v.rpo_state,
            rto_class=v.rto_class, basis=v.basis,
            # A band is only as good as the weaker of the verdict and the estimate.
            confidence=model.weakest_confidence([v.confidence, confidence]),
            applicable=v.applicable, rto_band_minutes=band, rto_assumptions=assumptions,
        )
    return out


def _mechanism_for(scenario: str, v: model.Verdict) -> str:
    kinds = {e.kind for e in v.basis}
    if model.EV_REPLICATION in kinds and scenario == model.SCENARIO_REGION_LOSS:
        return "asr_failover"
    if model.EV_NATIVE_BACKUP in kinds:
        return "native_pitr_overhead"
    if model.EV_BACKUP_POLICY in kinds or model.EV_OBSERVED_RECOVERY_POINT in kinds:
        return "vault_restore_overhead"
    return ""


__all__ = ["band_for", "apply_bands"]
