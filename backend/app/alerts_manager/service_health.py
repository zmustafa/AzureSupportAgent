"""Canonical Activity Log filter taxonomies and condition projection."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

SERVICE_HEALTH_EVENT_TYPE_INCIDENT_TYPES: Final[dict[str, tuple[str, ...]]] = {
    "service_issue": ("Incident",),
    "planned_maintenance": ("Maintenance",),
    "health_advisory": ("Informational", "ActionRequired"),
    "security_advisory": ("Security",),
}
SERVICE_HEALTH_EVENT_TYPES: Final[tuple[str, ...]] = tuple(SERVICE_HEALTH_EVENT_TYPE_INCIDENT_TYPES)
SERVICE_HEALTH_INCIDENT_TYPES: Final[tuple[str, ...]] = tuple(
    incident_type
    for event_type in SERVICE_HEALTH_EVENT_TYPES
    for incident_type in SERVICE_HEALTH_EVENT_TYPE_INCIDENT_TYPES[event_type]
)
RESOURCE_HEALTH_EVENT_STATUS_VALUES: Final[dict[str, tuple[str, ...]]] = {
    "active": ("Active",),
    "in_progress": ("InProgress", "In Progress"),
    "resolved": ("Resolved",),
    "updated": ("Updated",),
}
RESOURCE_HEALTH_CURRENT_STATUS_VALUES: Final[dict[str, tuple[str, ...]]] = {
    "available": ("Available",),
    "degraded": ("Degraded",),
    "unavailable": ("Unavailable",),
    "unknown": ("Unknown",),
}
RESOURCE_HEALTH_PREVIOUS_STATUS_VALUES: Final[dict[str, tuple[str, ...]]] = dict(
    RESOURCE_HEALTH_CURRENT_STATUS_VALUES
)
RESOURCE_HEALTH_REASON_TYPE_VALUES: Final[dict[str, tuple[str, ...]]] = {
    "platform_initiated": ("PlatformInitiated", "Platform Initiated"),
    "unknown": ("Unknown",),
    "user_initiated": ("UserInitiated", "User Initiated"),
}
RECOMMENDATION_CATEGORY_VALUES: Final[dict[str, tuple[str, ...]]] = {
    "cost": ("Cost",),
    "performance": ("Performance",),
    "high_availability": ("HighAvailability", "High Availability"),
    "operational_excellence": ("OperationalExcellence", "Operational Excellence"),
    "security": ("Security",),
}
RECOMMENDATION_IMPACT_VALUES: Final[dict[str, tuple[str, ...]]] = {
    "high": ("High",),
    "medium": ("Medium",),
    "low": ("Low",),
}
ACTIVITY_CATEGORIES: Final[tuple[str, ...]] = (
    "ServiceHealth", "ResourceHealth", "Security", "Recommendation",
)
ACTIVITY_CATEGORY_FILTERS: Final[tuple[str, ...]] = (*ACTIVITY_CATEGORIES, "Other")

_CATEGORY_BY_CASEFOLD = {category.casefold(): category for category in ACTIVITY_CATEGORIES}


def _value_lookup(values: dict[str, tuple[str, ...]]) -> dict[str, str]:
    return {
        raw_value.casefold(): canonical
        for canonical, raw_values in values.items()
        for raw_value in raw_values
    }


@dataclass(frozen=True)
class ServiceHealthConditionProjection:
    """Known event-type coverage projected from a rule's Activity Log conditions."""

    event_types: tuple[str, ...]
    unrestricted: bool
    unmapped: bool
    unmapped_values: tuple[str, ...]


@dataclass(frozen=True)
class ActivityConditionProjection:
    """Canonical values for one Activity Log condition field."""

    values: tuple[str, ...]
    unrestricted: bool
    unmapped_values: tuple[str, ...]


def activity_category(rule: dict[str, Any]) -> str:
    """Return a canonical Activity Log category or ``Other`` for an activity rule."""
    if str(rule.get("family") or "").casefold() != "activity":
        return ""
    supplied = str(rule.get("category") or "").strip()
    if supplied:
        return _CATEGORY_BY_CASEFOLD.get(supplied.casefold(), "Other")
    for condition in rule.get("activity_conditions") or []:
        if not isinstance(condition, dict):
            continue
        if str(condition.get("field") or "").casefold() != "category":
            continue
        value = str(condition.get("equals") or "").strip()
        return _CATEGORY_BY_CASEFOLD.get(value.casefold(), "Other")
    return "Other"


def _field_projection(
    condition: Any, *, field: str, universe: tuple[str, ...], value_lookup: dict[str, str],
) -> tuple[set[str], set[str]] | None:
    """Project one condition onto a field; ``None`` means it does not constrain it."""
    if not isinstance(condition, dict):
        return None
    any_of = condition.get("anyOf")
    if any_of is not None:
        if not isinstance(any_of, list) or not any_of:
            return set(universe), {"<invalid anyOf>"}
        projections = [
            _field_projection(item, field=field, universe=universe, value_lookup=value_lookup)
            for item in any_of
        ]
        if all(item is None for item in projections):
            return None
        known: set[str] = set()
        unmapped: set[str] = set()
        for projection in projections:
            # An unrelated OR branch can match independently of incident type, so this
            # anyOf cannot narrow this field's projection.
            if projection is None:
                known.update(universe)
                continue
            known.update(projection[0])
            unmapped.update(projection[1])
        return known, unmapped
    if str(condition.get("field") or "").casefold() != field.casefold():
        return None
    raw_values: list[Any] = []
    if "equals" in condition:
        raw_values.append(condition.get("equals"))
    if "containsAny" in condition:
        contains_any = condition.get("containsAny")
        if isinstance(contains_any, list):
            raw_values.extend(contains_any)
        else:
            raw_values.append("<invalid containsAny>")
    if not raw_values:
        return set(universe), {"<missing operator>"}
    known: set[str] = set()
    unmapped: set[str] = set()
    for raw_value in raw_values:
        value = str(raw_value or "").strip()
        canonical = value_lookup.get(value.casefold())
        if canonical:
            known.add(canonical)
        else:
            unmapped.add(value or "<empty>")
    return known, unmapped


def project_activity_field(
    conditions: Any, *, field: str, values: dict[str, tuple[str, ...]],
) -> ActivityConditionProjection:
    """Apply Activity Log ``allOf``/``anyOf`` semantics to one canonical dimension."""
    universe = tuple(values)
    lookup = _value_lookup(values)
    possible = set(universe)
    unmapped: set[str] = set()
    restricted = False
    for condition in conditions if isinstance(conditions, list) else []:
        projection = _field_projection(
            condition, field=field, universe=universe, value_lookup=lookup,
        )
        if projection is None:
            continue
        restricted = True
        possible.intersection_update(projection[0])
        unmapped.update(projection[1])
    return ActivityConditionProjection(
        values=tuple(value for value in universe if value in possible),
        unrestricted=not restricted,
        unmapped_values=tuple(sorted(unmapped, key=str.casefold)),
    )


def project_service_health_conditions(conditions: Any) -> ServiceHealthConditionProjection:
    """Preserve the Service Health-specific projection contract used by existing callers."""
    projection = project_activity_field(
        conditions, field="properties.incidentType",
        values=SERVICE_HEALTH_EVENT_TYPE_INCIDENT_TYPES,
    )
    return ServiceHealthConditionProjection(
        event_types=projection.values,
        unrestricted=projection.unrestricted,
        unmapped=bool(projection.unmapped_values),
        unmapped_values=projection.unmapped_values,
    )


def rule_activity_metadata(rule: dict[str, Any]) -> dict[str, Any]:
    """Return normalized category-specific filter metadata for one Activity Log rule."""
    category = activity_category(rule)
    conditions = rule.get("activity_conditions") or []
    dimensions: dict[str, ActivityConditionProjection] = {}
    if category == "ServiceHealth":
        dimensions["service_health_event_types"] = project_activity_field(
            conditions, field="properties.incidentType",
            values=SERVICE_HEALTH_EVENT_TYPE_INCIDENT_TYPES,
        )
    elif category == "ResourceHealth":
        dimensions = {
            "resource_health_event_statuses": project_activity_field(
                conditions, field="status", values=RESOURCE_HEALTH_EVENT_STATUS_VALUES,
            ),
            "resource_health_current_statuses": project_activity_field(
                conditions, field="properties.currentHealthStatus",
                values=RESOURCE_HEALTH_CURRENT_STATUS_VALUES,
            ),
            "resource_health_previous_statuses": project_activity_field(
                conditions, field="properties.previousHealthStatus",
                values=RESOURCE_HEALTH_PREVIOUS_STATUS_VALUES,
            ),
            "resource_health_reason_types": project_activity_field(
                conditions, field="properties.cause", values=RESOURCE_HEALTH_REASON_TYPE_VALUES,
            ),
        }
    elif category == "Recommendation":
        dimensions = {
            "recommendation_categories": project_activity_field(
                conditions, field="properties.recommendationCategory",
                values=RECOMMENDATION_CATEGORY_VALUES,
            ),
            "recommendation_impacts": project_activity_field(
                conditions, field="properties.recommendationImpact",
                values=RECOMMENDATION_IMPACT_VALUES,
            ),
        }
    metadata: dict[str, Any] = {
        "activity_category": category,
        "service_health_event_types": [],
        "service_health_unrestricted": False,
        "service_health_unmapped": False,
        "service_health_unmapped_values": [],
        "resource_health_event_statuses": [],
        "resource_health_current_statuses": [],
        "resource_health_previous_statuses": [],
        "resource_health_reason_types": [],
        "recommendation_categories": [],
        "recommendation_impacts": [],
        "activity_unrestricted_fields": [],
        "activity_unmapped_values": {},
    }
    for name, projection in dimensions.items():
        metadata[name] = list(projection.values)
        if projection.unrestricted:
            metadata["activity_unrestricted_fields"].append(name)
        if projection.unmapped_values:
            metadata["activity_unmapped_values"][name] = list(projection.unmapped_values)
    service_projection = dimensions.get("service_health_event_types")
    if service_projection:
        metadata["service_health_unrestricted"] = service_projection.unrestricted
        metadata["service_health_unmapped"] = bool(service_projection.unmapped_values)
        metadata["service_health_unmapped_values"] = list(service_projection.unmapped_values)
    return metadata


def matches_activity_filters(
    rule: dict[str, Any], *, activity_categories: set[str] | None = None,
    service_health_event_types: set[str] | None = None,
) -> bool:
    """Apply category filters and an optional Service Health event-type selection."""
    categories = activity_categories or set()
    if not categories and service_health_event_types is None:
        return True
    if str(rule.get("family") or "").casefold() != "activity":
        return False
    metadata = rule_activity_metadata(rule)
    category = str(metadata["activity_category"])
    if categories and category not in categories:
        return False
    if category != "ServiceHealth" or service_health_event_types is None:
        return True
    return bool(set(metadata["service_health_event_types"]) & service_health_event_types)
