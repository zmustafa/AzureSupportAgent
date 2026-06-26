"""Common models + enums for the Change Explorer.

The runtime works with plain dicts (easy to persist as JSON and unit-test, matching the rest
of the app), but the dataclasses here document the canonical shape of each model and provide
factory helpers so every ChangeEvent / detail / run / insight is built consistently.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

# --------------------------------------------------------------------------- enums

CATEGORIES = [
    "Network", "DNS", "Identity", "RBAC", "PIM", "AppRegistration", "ServicePrincipal",
    "ManagedIdentity", "Secret", "Certificate", "KeyVault", "AppConfiguration", "Deployment",
    "Policy", "Security", "Compute", "Storage", "Database", "Monitoring", "CostScale",
    "TagsMetadata", "Unknown",
]

RISK_LABELS = ["Critical", "High", "Medium", "Low", "Informational"]

SCOPE_MODES = ["workload", "workload_dependencies", "tenant"]

ACTOR_TYPES = ["User", "ServicePrincipal", "ManagedIdentity", "AzurePolicy", "AzurePlatform", "System", "Unknown"]


def label_for_score(score: int) -> str:
    """Map a 0-100 risk score to a label per the spec's bands."""
    if score >= 90:
        return "Critical"
    if score >= 70:
        return "High"
    if score >= 40:
        return "Medium"
    if score >= 10:
        return "Low"
    return "Informational"


def new_id() -> str:
    return uuid.uuid4().hex


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- dataclasses


@dataclass
class ChangeEventDetail:
    detailId: str
    changeId: str
    propertyPath: str
    beforeValue: Any
    afterValue: Any
    changeType: str          # Create | Update | Delete
    technicalSummary: str


@dataclass
class ChangeEvent:
    changeId: str
    runId: str
    tenantId: str
    subscriptionId: str
    workloadId: str
    resourceId: str
    resourceName: str
    resourceType: str
    resourceGroup: str
    location: str
    eventTime: str
    operation: str
    category: str
    riskScore: int
    riskLabel: str
    actor: str
    actorType: str
    source: str
    correlationId: str
    plainEnglishSummary: str
    possibleImpact: str
    confidence: str          # High | Medium | Low (confidence in the *interpretation*)
    rawEventJson: dict[str, Any]
    # Extensions beyond the base spec (kept for the richer tabs):
    riskFactors: list[dict[str, Any]] = field(default_factory=list)   # transparent scoring
    dependencyRole: str = ""                                          # for Dependency Impact
    blastRadius: str = ""
    whyRisk: str = ""
    details: list[dict[str, Any]] = field(default_factory=list)       # ChangeEventDetail[]
    # Identity attribution (resolved post-collect; empty on older cached runs).
    actorDisplay: str = ""        # human-friendly name (Graph) or "" when only an id is known
    actorObjectId: str = ""       # directory object-id of the actor, when known
    actorKind: str = ""           # refined kind: User|ServicePrincipal|ManagedIdentity|AzurePlatform|Unknown
    actorIp: str = ""             # originating client IP (from the ipaddr claim)
    actorOnBehalfOf: str = ""     # originating user when an app/SPN acted on their behalf
    actorResolved: bool = False   # True when a friendly name was resolved from the directory
    # Security intelligence (C1/C3) — computed deterministically each run.
    securityFlags: list[dict[str, str]] = field(default_factory=list)  # [{code,label,severity}]
    securitySeverity: str = ""    # highest flag severity (critical|high|medium|low) or ""
    rollbackHint: str = ""        # read-only az command to inspect/revert (copy-only)


@dataclass
class ChangeInsight:
    insightId: str
    runId: str
    insightType: str
    title: str
    summary: str
    severity: str
    relatedChangeIds: list[str] = field(default_factory=list)


@dataclass
class ChangeAnalysisRun:
    runId: str
    tenantId: str
    workloadId: str
    workloadName: str
    startTime: str
    endTime: str
    scopeMode: str
    requestedBy: str
    createdAt: str
    completedAt: str
    status: str               # running | succeeded | failed
    totalChanges: int
    criticalCount: int
    highCount: int
    mediumCount: int
    lowCount: int
    informationalCount: int
    summary: str
    demo: bool = False
    truncated: bool = False
    changeLimit: int = 0      # per-scan source cap (e.g. 1000) when the change list was capped; 0 = not capped
    aiAnalyzed: bool = False   # whether the AI enrichment pass has run for this run's events
    notes: list[str] = field(default_factory=list)
    scopeInfo: dict[str, Any] = field(default_factory=dict)
    facets: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    insights: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------- factories


def make_detail(change_id: str, property_path: str, before: Any, after: Any,
                change_type: str = "Update", technical_summary: str = "") -> dict[str, Any]:
    return asdict(ChangeEventDetail(
        detailId=new_id(), changeId=change_id, propertyPath=property_path,
        beforeValue=before, afterValue=after, changeType=change_type,
        technicalSummary=technical_summary or _detail_summary(property_path, before, after, change_type),
    ))


def _detail_summary(path: str, before: Any, after: Any, change_type: str) -> str:
    if change_type == "Create":
        return f"{path} set to {after!r}."
    if change_type == "Delete":
        return f"{path} removed (was {before!r})."
    return f"{path} changed from {before!r} to {after!r}."


def make_insight(run_id: str, insight_type: str, title: str, summary: str, severity: str,
                 related: list[str] | None = None) -> dict[str, Any]:
    return asdict(ChangeInsight(
        insightId=new_id(), runId=run_id, insightType=insight_type, title=title,
        summary=summary, severity=severity, relatedChangeIds=related or [],
    ))
