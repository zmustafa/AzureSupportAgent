"""Recovery Readiness — the scenario model.

Pure: no I/O, no Azure, no imports from collectors. Everything else in the module is a
projection of the types declared here, which is why they are defined once, in one place,
and exhaustively tested. Same reason :mod:`app.fmea.compute` keeps its scoring pure — the
grid, the export and the summary cannot disagree if they all read one function.

**Why scenarios at all.** "RTO 4 hours" is not a fact; it is an answer to an unstated
question. *Recover from what?* A zone-redundant VM survives a zone loss untouched and is
completely unrecoverable once someone deletes its resource group. A GRS storage account has
an excellent region story and, for ransomware, a fifteen-minute head start on replicating
the encryption to the paired region. The same resource has different answers per failure,
and collapsing them into one number produces a figure that is wrong four times out of five —
reassuring in exactly the cases where it should not be.

**The rule that carries the product.** Redundancy is not a control for logical loss.
ZRS, GRS and multi-region writes replicate corruption and deletion, usually within seconds.
:func:`redundancy_helps` encodes that here rather than leaving it to each caller, because a
caller that forgets it produces a confident, wrong, green verdict.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- scenarios
SCENARIO_INSTANCE_LOSS = "instance_loss"
SCENARIO_ZONE_LOSS = "zone_loss"
SCENARIO_REGION_LOSS = "region_loss"
SCENARIO_DATA_CORRUPTION = "data_corruption"
SCENARIO_ACCIDENTAL_DELETE = "accidental_delete"

#: Ordered for display: infrastructure loss first, then logical loss.
SCENARIOS: tuple[str, ...] = (
    SCENARIO_INSTANCE_LOSS,
    SCENARIO_ZONE_LOSS,
    SCENARIO_REGION_LOSS,
    SCENARIO_DATA_CORRUPTION,
    SCENARIO_ACCIDENTAL_DELETE,
)

#: The two scenarios where redundancy is worthless. Kept as a set rather than a check on
#: each site so the rule is stated once and cannot be half-applied.
LOGICAL_SCENARIOS: frozenset[str] = frozenset({SCENARIO_DATA_CORRUPTION, SCENARIO_ACCIDENTAL_DELETE})

SCENARIO_LABEL: dict[str, str] = {
    SCENARIO_INSTANCE_LOSS: "Instance loss",
    SCENARIO_ZONE_LOSS: "Zone loss",
    SCENARIO_REGION_LOSS: "Region loss",
    SCENARIO_DATA_CORRUPTION: "Data corruption",
    SCENARIO_ACCIDENTAL_DELETE: "Accidental deletion",
}

SCENARIO_DESCRIPTION: dict[str, str] = {
    SCENARIO_INSTANCE_LOSS: "A single instance, node or replica fails.",
    SCENARIO_ZONE_LOSS: "One availability zone becomes unavailable.",
    SCENARIO_REGION_LOSS: "A whole Azure region becomes unavailable.",
    SCENARIO_DATA_CORRUPTION: (
        "Data is corrupted in place — a bad deployment, a bad migration, or ransomware. "
        "Redundancy replicates the damage; only point-in-time recovery helps."
    ),
    SCENARIO_ACCIDENTAL_DELETE: (
        "A resource or its data is deleted. Redundancy replicates the deletion; only "
        "backup, soft delete or a resource lock helps."
    ),
}

# --------------------------------------------------------------------------- RTO classes
RTO_AUTOMATIC = "automatic"
RTO_MINUTES = "minutes"
RTO_HOURS = "hours"
RTO_DAY_PLUS = "day_plus"
RTO_NONE = "none"
RTO_UNKNOWN = "unknown"

#: Worst-first rank, for aggregation. ``unknown`` is DELIBERATELY ABSENT: it is not a point
#: on this scale, and putting it on one would make a single unmeasured component either win
#: or lose a max() silently. Callers must handle it explicitly — see ``worst_rto``.
_RTO_RANK: dict[str, int] = {
    RTO_NONE: 0,
    RTO_DAY_PLUS: 1,
    RTO_HOURS: 2,
    RTO_MINUTES: 3,
    RTO_AUTOMATIC: 4,
}

RTO_CLASSES: tuple[str, ...] = (
    RTO_AUTOMATIC, RTO_MINUTES, RTO_HOURS, RTO_DAY_PLUS, RTO_NONE, RTO_UNKNOWN,
)

RTO_LABEL: dict[str, str] = {
    RTO_AUTOMATIC: "Automatic",
    RTO_MINUTES: "Minutes",
    RTO_HOURS: "Hours",
    RTO_DAY_PLUS: "A day or more",
    RTO_NONE: "No recovery path",
    RTO_UNKNOWN: "Unknown",
}


def rto_rank(rto_class: str) -> int | None:
    """Position on the worst-first scale, or ``None`` for ``unknown``.

    ``None`` is not a sort key. It is a refusal to place an unmeasured value on a scale, and
    the caller has to decide what that means for its aggregate."""
    return _RTO_RANK.get(rto_class)


def worst_rto(classes: list[str]) -> tuple[str, int]:
    """Worst class among those that are *determined*, plus the count that were not.

    Returns ``(class, undetermined_count)``. An all-unknown input yields
    ``(RTO_UNKNOWN, len)`` rather than a fabricated verdict — the aggregate of nothing
    measured is not "automatic"."""
    determined = [c for c in classes if c in _RTO_RANK]
    undetermined = len(classes) - len(determined)
    if not determined:
        return RTO_UNKNOWN, undetermined
    return min(determined, key=lambda c: _RTO_RANK[c]), undetermined


# --------------------------------------------------------------------------- RPO states
RPO_KNOWN = "known"
RPO_NONE = "none"
RPO_UNKNOWN = "unknown"

RPO_STATES: tuple[str, ...] = (RPO_KNOWN, RPO_NONE, RPO_UNKNOWN)

# --------------------------------------------------------------------------- confidence
CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"

CONFIDENCE_LEVELS: tuple[str, ...] = (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW)

_CONFIDENCE_RANK = {CONFIDENCE_HIGH: 2, CONFIDENCE_MEDIUM: 1, CONFIDENCE_LOW: 0}


def weakest_confidence(levels: list[str]) -> str:
    """A composed verdict is only as trustworthy as its least trustworthy input."""
    known = [c for c in levels if c in _CONFIDENCE_RANK]
    if not known:
        return CONFIDENCE_LOW
    return min(known, key=lambda c: _CONFIDENCE_RANK[c])


# --------------------------------------------------------------------------- evidence
EV_BACKUP_POLICY = "backup_policy"
EV_NATIVE_BACKUP = "native_backup"
EV_REPLICATION = "replication"
EV_ZONE_CONFIG = "zone_config"
EV_SKU = "sku"
EV_SOFT_DELETE = "soft_delete"
EV_OBSERVED_RECOVERY_POINT = "observed_recovery_point"
EV_VAULT_REDUNDANCY = "vault_redundancy"
EV_NO_PROTECTION = "no_protection"


@dataclass(frozen=True)
class Evidence:
    """One configuration fact that contributed to a verdict.

    A verdict without evidence is an opinion. :func:`verdict` refuses to be confident
    without one, which is what keeps a derived number reviewable."""

    kind: str
    detail: str
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail, "source": self.source}


@dataclass(frozen=True)
class Provenance:
    """Where a section came from, and whether to trust its emptiness."""

    source: str
    collected_at: str = ""
    unreadable: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "collected_at": self.collected_at,
            "unreadable": self.unreadable,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Verdict:
    """What happens to one resource in one failure scenario."""

    scenario: str
    rpo_minutes: int | None
    rpo_state: str
    rto_class: str
    basis: tuple[Evidence, ...] = ()
    confidence: str = CONFIDENCE_LOW
    applicable: bool = True
    # Only ever set by the RTO engine (P3); absent means "no duration was estimated".
    rto_band_minutes: tuple[int, int] | None = None
    rto_assumptions: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "rpo_minutes": self.rpo_minutes,
            "rpo_state": self.rpo_state,
            "rto_class": self.rto_class,
            "rto_band_minutes": list(self.rto_band_minutes) if self.rto_band_minutes else None,
            "rto_assumptions": list(self.rto_assumptions),
            "basis": [e.as_dict() for e in self.basis],
            "confidence": self.confidence,
            "applicable": self.applicable,
        }


def verdict(
    scenario: str,
    *,
    rpo_minutes: int | None = None,
    rpo_state: str = RPO_UNKNOWN,
    rto_class: str = RTO_UNKNOWN,
    basis: tuple[Evidence, ...] = (),
    confidence: str = CONFIDENCE_LOW,
    applicable: bool = True,
    rto_band_minutes: tuple[int, int] | None = None,
    rto_assumptions: tuple[str, ...] = (),
) -> Verdict:
    """Construct a verdict, forcing the invariants rather than trusting the caller.

    Two are enforced here because both have a plausible-looking wrong answer:

    * an RPO in minutes is only meaningful when the state says it is ``known`` — a bare
      ``None`` cannot distinguish "no recovery point exists" from "we could not tell", and
      those are opposite facts;
    * a verdict with no evidence cannot be confident. If we cannot say why, we say unknown.
    """
    if rpo_state != RPO_KNOWN:
        rpo_minutes = None
    elif rpo_minutes is None:
        rpo_state = RPO_UNKNOWN
    if not basis and rto_class != RTO_UNKNOWN:
        rto_class = RTO_UNKNOWN
        rpo_state, rpo_minutes = RPO_UNKNOWN, None
    if not basis:
        confidence = CONFIDENCE_LOW
    return Verdict(
        scenario=scenario,
        rpo_minutes=rpo_minutes,
        rpo_state=rpo_state,
        rto_class=rto_class,
        basis=basis,
        confidence=confidence,
        applicable=applicable,
        rto_band_minutes=rto_band_minutes,
        rto_assumptions=rto_assumptions,
    )


def not_applicable(scenario: str, reason: str) -> Verdict:
    """A scenario this resource cannot experience.

    Rendered as absent, never as a pass. A stateless front end showing green for data
    corruption implies a protection it does not have."""
    return Verdict(
        scenario=scenario,
        rpo_minutes=None,
        rpo_state=RPO_UNKNOWN,
        rto_class=RTO_UNKNOWN,
        basis=(Evidence(kind="not_applicable", detail=reason),),
        confidence=CONFIDENCE_HIGH,
        applicable=False,
    )


# --------------------------------------------------------------------------- applicability
# Resource types that hold no durable state of their own. Their data lives elsewhere, so a
# logical-loss verdict against them would describe a risk they do not carry — and, worse,
# would render green.
#
# This is a DEFAULT, not a fact about the type: `applies()` lets a resource override it when
# its own configuration says otherwise. A Redis with persistence enabled is a data store,
# and the type alone cannot tell you which one you are looking at.
STATELESS_TYPES: frozenset[str] = frozenset({
    "microsoft.web/sites",
    "microsoft.web/serverfarms",
    "microsoft.web/staticsites",
    "microsoft.network/applicationgateways",
    "microsoft.network/loadbalancers",
    "microsoft.network/publicipaddresses",
    "microsoft.network/trafficmanagerprofiles",
    "microsoft.network/azurefirewalls",
    "microsoft.network/natgateways",
    "microsoft.network/virtualnetworkgateways",
    "microsoft.network/bastionhosts",
    "microsoft.cdn/profiles",
    "microsoft.logic/workflows",
    "microsoft.search/searchservices",
    "microsoft.cache/redis",
    "microsoft.app/containerapps",
    "microsoft.app/managedenvironments",
    "microsoft.desktopvirtualization/hostpools",
    "microsoft.datafactory/factories",
    "microsoft.apimanagement/service",
    # Messages in flight are transient and the namespace itself is redeployed. The queues
    # and topics are configuration, not a data store to restore.
    "microsoft.eventhub/namespaces",
    "microsoft.servicebus/namespaces",
    # Usually a stateless tier redeployed from an image. `shape()` sets `holds_data` when
    # data disks say otherwise.
    "microsoft.compute/virtualmachinescalesets",
})

# Types for which corruption is not a risk they carry, and why. Distinct from statelessness:
# these hold durable state, so DELETION still matters — only the overwrite question is moot.
CORRUPTION_NOT_APPLICABLE: dict[str, str] = {
    "microsoft.keyvault/vaults": (
        "Every write keeps the previous version of the object, so an overwrite is "
        "recoverable without a backup."),
    "microsoft.recoveryservices/vaults": (
        "A vault holds recovery points rather than application data; the risk it carries is "
        "losing the vault, not corruption inside it."),
    "microsoft.dataprotection/backupvaults": (
        "A vault holds recovery points rather than application data; the risk it carries is "
        "losing the vault, not corruption inside it."),
}

# Global services have no single region or zone to lose.
GLOBAL_TYPES: frozenset[str] = frozenset({
    "microsoft.cdn/profiles",
    "microsoft.network/trafficmanagerprofiles",
    "microsoft.web/staticsites",
})


def applies(resource_type: str, scenario: str,
            config: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Whether ``scenario`` is a real risk for ``resource_type``, and why not if it is not.

    ``config`` is the shaped resource, when one is available. It can only ever make a
    scenario apply that the type-level default would have excluded — never the reverse."""
    rtype = (resource_type or "").strip().lower()
    if scenario in (SCENARIO_ZONE_LOSS, SCENARIO_INSTANCE_LOSS) and rtype in GLOBAL_TYPES:
        return False, "This is a global service with no zonal footprint."
    if scenario == SCENARIO_DATA_CORRUPTION and rtype in CORRUPTION_NOT_APPLICABLE:
        return False, CORRUPTION_NOT_APPLICABLE[rtype]
    if scenario in LOGICAL_SCENARIOS and rtype in STATELESS_TYPES:
        if (config or {}).get("holds_data"):
            return True, ""
        if scenario == SCENARIO_DATA_CORRUPTION:
            return False, ("This resource holds no durable data of its own; corruption "
                           "applies to the data stores it reads.")
        return False, ("Deleting this resource loses configuration, not data; it is "
                       "redeployed rather than restored.")
    return True, ""


def redundancy_helps(scenario: str) -> bool:
    """Whether replication is a control for this failure at all.

    False for logical loss. ZRS, GRS and multi-region writes copy a corrupted or deleted
    object to every replica, usually within seconds — which is why a resource can be
    flawlessly redundant and still have no recovery path."""
    return scenario not in LOGICAL_SCENARIOS


__all__ = [
    "SCENARIOS", "SCENARIO_LABEL", "SCENARIO_DESCRIPTION", "LOGICAL_SCENARIOS",
    "SCENARIO_INSTANCE_LOSS", "SCENARIO_ZONE_LOSS", "SCENARIO_REGION_LOSS",
    "SCENARIO_DATA_CORRUPTION", "SCENARIO_ACCIDENTAL_DELETE",
    "RTO_AUTOMATIC", "RTO_MINUTES", "RTO_HOURS", "RTO_DAY_PLUS", "RTO_NONE", "RTO_UNKNOWN",
    "RTO_CLASSES", "RTO_LABEL", "rto_rank", "worst_rto",
    "RPO_KNOWN", "RPO_NONE", "RPO_UNKNOWN", "RPO_STATES",
    "CONFIDENCE_HIGH", "CONFIDENCE_MEDIUM", "CONFIDENCE_LOW", "weakest_confidence",
    "Evidence", "Provenance", "Verdict", "verdict", "not_applicable",
    "applies", "redundancy_helps", "STATELESS_TYPES", "GLOBAL_TYPES",
    "CORRUPTION_NOT_APPLICABLE",
    "EV_BACKUP_POLICY", "EV_NATIVE_BACKUP", "EV_REPLICATION", "EV_ZONE_CONFIG", "EV_SKU",
    "EV_SOFT_DELETE", "EV_OBSERVED_RECOVERY_POINT", "EV_VAULT_REDUNDANCY", "EV_NO_PROTECTION",
]
