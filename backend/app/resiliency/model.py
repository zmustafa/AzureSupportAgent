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

from dataclasses import dataclass, replace
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


# --------------------------------------------------------------------------- caveats
#: A recovery path is only worth what it survives. These name WHY one might not survive.
CAVEAT_BLAST_RADIUS = "blast_radius"          # dies with a larger delete than the one modelled
CAVEAT_NARROW_WINDOW = "narrow_window"        # survives, but briefly or awkwardly
CAVEAT_NOT_SELF_SERVICE = "not_self_service"  # needs a support ticket or an off-portal API
CAVEAT_MITIGATION = "mitigation"              # something reduces the risk; positive tone

CAVEAT_CRITICAL = "critical"
CAVEAT_WARNING = "warning"
CAVEAT_INFO = "info"

CAVEAT_SEVERITIES: tuple[str, ...] = (CAVEAT_CRITICAL, CAVEAT_WARNING, CAVEAT_INFO)
_CAVEAT_SEVERITY_RANK: dict[str, int] = {CAVEAT_CRITICAL: 0, CAVEAT_WARNING: 1, CAVEAT_INFO: 2}


@dataclass(frozen=True)
class Caveat:
    """A condition under which the verdict above it stops being true.

    Deliberately NOT an :class:`Evidence`. Evidence explains why the answer is what it is; a
    caveat explains when the answer is worthless. Rendering both in one list — which is what
    reusing ``basis`` would do — makes a warning read as though it *supports* the green cell,
    which is the exact inversion this module exists to prevent."""

    kind: str
    severity: str
    detail: str
    doc_url: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "severity": self.severity,
                "detail": self.detail, "doc_url": self.doc_url}


def worst_caveat_severity(caveats: tuple[Caveat, ...]) -> str:
    """Worst severity present, or ``""`` for none. For sorting and for export columns."""
    ranked = [c.severity for c in caveats if c.severity in _CAVEAT_SEVERITY_RANK]
    if not ranked:
        return ""
    return min(ranked, key=lambda s: _CAVEAT_SEVERITY_RANK[s])


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
    # Only ever set for accidental deletion: the radii this answer does not cover.
    caveats: tuple[Caveat, ...] = ()

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
            "caveats": [c.as_dict() for c in self.caveats],
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


def with_caveats(verdict_in: Verdict, caveats: tuple[Caveat, ...]) -> Verdict:
    """Attach caveats without touching the computed answer.

    A caveat is never a basis: it cannot license confidence, and it cannot turn an unmeasured
    resource into a measured one. Attaching to a not-applicable verdict is a no-op, because a
    scenario the resource cannot experience has no radius to escape."""
    if not caveats or not verdict_in.applicable:
        return verdict_in
    return replace(verdict_in, caveats=verdict_in.caveats + caveats)


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


# --------------------------------------------------------------------------- blast radius
# Deletion has a RADIUS — object, container, resource, parent, resource group, subscription —
# and a recovery path is only valid for the radius it survives. Corruption has none: an
# overwrite leaves the resource standing, which is why this table is read for deletion only.
#
# Every entry below was read from Microsoft Learn, not recalled. The verdict is NOT rewritten
# from here: deleting an Azure SQL *database* really is a routine restore, and saying otherwise
# would trade one wrong answer for another. What the table adds is the radius the answer stops
# covering.
_LEARN = "https://learn.microsoft.com/en-us/azure"

_SQL_SERVER_CAVEAT = Caveat(
    kind=CAVEAT_BLAST_RADIUS,
    severity=CAVEAT_CRITICAL,
    detail=(
        "Point-in-time restore recovers this database if the database alone is deleted. It does "
        "not survive deletion of the parent logical server: the server, every database on it and "
        "all of their point-in-time backups are removed together and cannot be recovered. Only "
        "long-term retention backups survive, and they restore only to another server in the "
        "same subscription."
    ),
    doc_url=f"{_LEARN}/azure-sql/database/long-term-retention-overview",
)

_MI_CAVEAT = replace(
    _SQL_SERVER_CAVEAT,
    detail=(
        "Point-in-time restore recovers a database if the database alone is deleted. It does not "
        "survive deletion of the managed instance: the instance, every database on it and all of "
        "their point-in-time backups are removed together and cannot be recovered. Only long-term "
        "retention backups survive, and they restore only to another instance in the same "
        "subscription."
    ),
)

#: Keyed by resource type. Config-sensitive types are handled in :func:`deletion_caveats`.
DELETION_CAVEATS: dict[str, tuple[Caveat, ...]] = {
    "microsoft.sql/servers/databases": (_SQL_SERVER_CAVEAT,),
    "microsoft.sql/managedinstances": (_MI_CAVEAT,),
    "microsoft.storage/storageaccounts": (
        Caveat(
            kind=CAVEAT_BLAST_RADIUS,
            severity=CAVEAT_WARNING,
            detail=(
                "Blob soft delete does not recover blobs inside a deleted container — that needs "
                "container soft delete, and the container must be restored under its original "
                "name."
            ),
            doc_url=f"{_LEARN}/storage/blobs/soft-delete-container-overview",
        ),
    ),
    "microsoft.containerservice/managedclusters": (
        Caveat(
            kind=CAVEAT_BLAST_RADIUS,
            severity=CAVEAT_WARNING,
            detail=(
                "Operational-tier AKS backups are stored in this subscription, so a resource "
                "group or subscription deletion takes them with the cluster. Only vault-tier "
                "copies sit outside the tenant, at one recovery point per day and for Azure Disk "
                "volumes only. Persistent volumes on Azure Files (NFS) or Blob storage are "
                "skipped by backup entirely."
            ),
            doc_url=f"{_LEARN}/backup/azure-kubernetes-service-backup-overview",
        ),
    ),
    "microsoft.netapp/netappaccounts/capacitypools/volumes": (
        Caveat(
            kind=CAVEAT_BLAST_RADIUS,
            severity=CAVEAT_WARNING,
            detail=(
                "Volume snapshots live on the volume and are deleted with it. Only a backup-vault "
                "policy survives, restoring to a new volume in the same region."
            ),
            doc_url=f"{_LEARN}/azure-netapp-files/backup-introduction",
        ),
    ),
}

# --- caveats that depend on whether a VAULT backup exists ------------------------------
# A vault backup stores data outside the resource, so it changes the deletion answer. Azure
# Backup's own words: vaulted blob backup protects against "any accidental or malicious
# deletion of blobs or storage account", and vaulted PostgreSQL backup is copied to "an
# isolated storage environment outside of customer tenant and subscription".
#
# Asserting "the account is gone" over the top of that is the same false-confidence error this
# module exists to prevent, just pointed the other way. Backup Manager does not report WHICH
# tier a policy uses, so a joined vault instance means the claim cannot be made either way —
# name the distinction and let the reader check, rather than pick one and be wrong half the time.
_STORAGE_ACCOUNT_UNPROTECTED = Caveat(
    kind=CAVEAT_BLAST_RADIUS,
    severity=CAVEAT_CRITICAL,
    detail=(
        "Blob and container soft delete protect objects inside this account. Neither protects "
        "the account itself. A deleted storage account can only be recovered on a best-effort "
        "basis within 14 days, and not at all if the name was reused or the resource group was "
        "deleted too. A resource lock is the documented control."
    ),
    doc_url=f"{_LEARN}/storage/common/storage-account-recover",
)

_STORAGE_ACCOUNT_VAULTED = Caveat(
    kind=CAVEAT_BLAST_RADIUS,
    severity=CAVEAT_WARNING,
    detail=(
        "Whether this survives deletion of the account depends on the backup tier. Vaulted "
        "backup copies data to the Backup vault and does protect against account deletion, but "
        "restores only to a different storage account. Operational backup keeps data inside this "
        "account and does not survive it — though it does apply a delete lock automatically. "
        "Check which tier the policy uses."
    ),
    doc_url=f"{_LEARN}/backup/blob-backup-overview",
)

_PG_WINDOW_UNPROTECTED = Caveat(
    kind=CAVEAT_NARROW_WINDOW,
    severity=CAVEAT_WARNING,
    detail=(
        "If the server itself is deleted, its backups are kept for five days only. Recovery is a "
        "ReviveDropped call to the management API using the delete timestamp from the Activity "
        "Log — there is no portal path, it must run in the original subscription, and Microsoft "
        "does not guarantee it succeeds."
    ),
    doc_url=f"{_LEARN}/postgresql/backup-restore/how-to-restore-deleted-server",
)

_MYSQL_WINDOW_UNPROTECTED = Caveat(
    kind=CAVEAT_NARROW_WINDOW,
    severity=CAVEAT_WARNING,
    detail=(
        "If the server itself is deleted, a five-day window exists to recover it, and nothing "
        "after that. Recovery runs through the management API rather than the portal and must "
        "run in the original subscription."
    ),
    # The same Learn page states five days in the body and "can't be recovered" in its FAQ. The
    # body is newer and links a dedicated how-to, so it is the one quoted here. Do not "correct"
    # this to the FAQ wording without re-reading both.
    doc_url=f"{_LEARN}/mysql/flexible-server/how-to-restore-dropped-server",
)

_FLEX_SERVER_VAULTED = Caveat(
    kind=CAVEAT_MITIGATION,
    severity=CAVEAT_INFO,
    detail=(
        "The server's own automated backups are kept for five days after the server is deleted, "
        "but this server also has a vault backup, which is held outside the subscription and "
        "survives deletion of the server."
    ),
    doc_url=f"{_LEARN}/backup/backup-azure-database-postgresql-flex-overview",
)

_COSMOS_PERIODIC_CAVEAT = Caveat(
    kind=CAVEAT_NOT_SELF_SERVICE,
    severity=CAVEAT_CRITICAL,
    detail=(
        "Periodic backups cannot be reached directly. Restoring requires a support request, lands "
        "in a new single-region account, and does not bring back firewall, virtual network or "
        "private endpoint settings, data-plane role assignments, stored procedures, triggers or "
        "user-defined functions."
    ),
    doc_url=f"{_LEARN}/cosmos-db/periodic-backup-restore-introduction",
)

_COSMOS_CONTINUOUS_CAVEAT = Caveat(
    kind=CAVEAT_MITIGATION,
    severity=CAVEAT_INFO,
    detail=(
        "Continuous backup can restore a deleted account within its retention tier, but the "
        "operator needs read access to restorableDatabaseAccounts or the portal's restore list "
        "appears empty."
    ),
    doc_url=f"{_LEARN}/cosmos-db/restore-account-continuous-backup",
)

_KEYVAULT_PURGE_CAVEAT = Caveat(
    kind=CAVEAT_BLAST_RADIUS,
    severity=CAVEAT_WARNING,
    detail=(
        "Purge protection is not enabled. Soft delete only delays permanent deletion: a "
        "subscription owner or Key Vault Purge Operator can purge this vault immediately and "
        "irrecoverably."
    ),
    doc_url=f"{_LEARN}/key-vault/general/soft-delete-overview",
)

_KEYVAULT_LINKS_CAVEAT = Caveat(
    kind=CAVEAT_BLAST_RADIUS,
    severity=CAVEAT_INFO,
    detail=(
        "Recovering a soft-deleted vault does not restore its role assignments or Event Grid "
        "subscriptions. Those must be recreated by hand."
    ),
    doc_url=f"{_LEARN}/key-vault/general/soft-delete-overview",
)


def deletion_caveats(
    resource_type: str, config: dict[str, Any] | None = None, *, vaulted: bool = False,
) -> tuple[Caveat, ...]:
    """Radii this type's recovery path does not cover.

    ``vaulted`` says a Backup vault instance is joined to this resource, which stores data
    outside it and therefore changes the answer for the types whose native backup does not
    survive the resource.

    ``config`` may only ever ADD a caveat the type-level default would have missed, never
    remove one — same constraint as :func:`applies`, and for the same reason: a resource we
    could not read must not come out quieter than one we could."""
    rtype = (resource_type or "").strip().lower()
    out = list(DELETION_CAVEATS.get(rtype, ()))
    cfg = config or {}

    if rtype == "microsoft.storage/storageaccounts":
        out.insert(0, _STORAGE_ACCOUNT_VAULTED if vaulted else _STORAGE_ACCOUNT_UNPROTECTED)

    if rtype == "microsoft.dbforpostgresql/flexibleservers":
        out.append(_FLEX_SERVER_VAULTED if vaulted else _PG_WINDOW_UNPROTECTED)

    if rtype == "microsoft.dbformysql/flexibleservers":
        out.append(_FLEX_SERVER_VAULTED if vaulted else _MYSQL_WINDOW_UNPROTECTED)

    if rtype == "microsoft.documentdb/databaseaccounts":
        kind = str((cfg.get("native_backup") or {}).get("kind") or "")
        # An unread backup policy yields NEITHER caveat: guessing "periodic" would manufacture
        # a critical warning out of missing data.
        if kind == "cosmos_periodic":
            out.append(_COSMOS_PERIODIC_CAVEAT)
        elif kind == "cosmos_continuous":
            out.append(_COSMOS_CONTINUOUS_CAVEAT)

    if rtype == "microsoft.keyvault/vaults":
        native = cfg.get("native_backup") or {}
        if "purge_protection" in native and not native.get("purge_protection"):
            out.append(_KEYVAULT_PURGE_CAVEAT)
        out.append(_KEYVAULT_LINKS_CAVEAT)

    return tuple(out)


def lock_caveat(locks: list[dict[str, Any]] | None) -> tuple[Caveat, ...]:
    """A management lock, described honestly.

    Never upgrades a verdict. A lock stops the ARM delete; it creates no recovery point, does
    not survive subscription cancellation, and does not touch data-plane deletes. Letting one
    turn a red cell green would reproduce, in a new place, the "redundancy looks like
    protection" error this module exists to kill."""
    if not locks:
        return ()
    levels = {str(lock.get("level") or "").strip().lower() for lock in locks}
    scopes = {str(lock.get("scope_kind") or "resource") for lock in locks}
    where = ("the resource group" if "resource_group" in scopes
             else "the subscription" if "subscription" in scopes else "this resource")
    verb = "blocks deletion and configuration change" if "readonly" in levels else "blocks deletion"
    return (
        Caveat(
            kind=CAVEAT_MITIGATION,
            severity=CAVEAT_INFO,
            detail=(
                f"A management lock on {where} {verb} through Azure Resource Manager. It does not "
                "stop data being deleted through the data plane, it does not survive subscription "
                "cancellation, and any Owner or User Access Administrator can remove it."
            ),
            doc_url=f"{_LEARN}/azure-resource-manager/management/lock-resources",
        ),
    )


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
    "Caveat", "with_caveats", "deletion_caveats", "lock_caveat", "DELETION_CAVEATS",
    "worst_caveat_severity", "CAVEAT_SEVERITIES",
    "CAVEAT_BLAST_RADIUS", "CAVEAT_NARROW_WINDOW", "CAVEAT_NOT_SELF_SERVICE",
    "CAVEAT_MITIGATION", "CAVEAT_CRITICAL", "CAVEAT_WARNING", "CAVEAT_INFO",
]
