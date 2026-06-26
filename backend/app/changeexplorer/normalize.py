"""Normalize raw change rows (from collectors or the demo) into ChangeEvent dicts.

A *raw change* is the common intermediate shape every collector emits:
    {
      source, resourceId, resourceName, resourceType, resourceGroup, subscriptionId, location,
      eventTime, operation, changeType, actor, actorType, correlationId,
      changes: [{propertyPath, before, after}],
      raw: {...}            # the original event JSON
    }
The normalizer fills identity/derived fields and builds ChangeEventDetail rows; classification,
risk scoring and explanation are applied afterward by the service.
"""
from __future__ import annotations

import re
from typing import Any

from app.changeexplorer.models import make_detail, new_id

_RT_FROM_ID = re.compile(r"/providers/([^/]+/[^/]+(?:/[^/]+)*?)/[^/]+$", re.IGNORECASE)
_RG_FROM_ID = re.compile(r"/resourceGroups/([^/]+)", re.IGNORECASE)
_SUB_FROM_ID = re.compile(r"/subscriptions/([^/]+)", re.IGNORECASE)
_NAME_FROM_ID = re.compile(r"/([^/]+)$")


def _derive(raw: dict[str, Any], field: str, pattern: re.Pattern[str]) -> str:
    if raw.get(field):
        return str(raw[field])
    m = pattern.search(raw.get("resourceId", "") or "")
    return m.group(1) if m else ""


def normalize(raw: dict[str, Any], *, run_id: str, tenant_id: str, workload_id: str) -> dict[str, Any]:
    rid = raw.get("resourceId", "") or ""
    change_id = raw.get("changeId") or new_id()
    details = []
    for ch in raw.get("changes", []) or []:
        details.append(make_detail(
            change_id,
            ch.get("propertyPath", ch.get("path", "")),
            ch.get("before", ch.get("oldValue")),
            ch.get("after", ch.get("newValue")),
            ch.get("changeType", raw.get("changeType", "Update")),
            ch.get("technicalSummary", ""),
        ))
    return {
        "changeId": change_id,
        "runId": run_id,
        "tenantId": tenant_id,
        "subscriptionId": _derive(raw, "subscriptionId", _SUB_FROM_ID),
        "workloadId": workload_id,
        "resourceId": rid,
        "resourceName": raw.get("resourceName") or (_NAME_FROM_ID.search(rid).group(1) if _NAME_FROM_ID.search(rid) else rid),
        "resourceType": (raw.get("resourceType") or _derive(raw, "resourceType", _RT_FROM_ID)).lower(),
        "resourceGroup": _derive(raw, "resourceGroup", _RG_FROM_ID),
        "location": raw.get("location", ""),
        "eventTime": raw.get("eventTime", ""),
        "operation": raw.get("operation", raw.get("changeType", "")),
        "category": "",          # filled by classifier
        "riskScore": 0,           # filled by risk engine
        "riskLabel": "",
        "actor": raw.get("actor", "") or "unknown",
        "actorType": raw.get("actorType", "Unknown"),
        "source": raw.get("source", "Unknown"),
        "correlationId": raw.get("correlationId", ""),
        "plainEnglishSummary": "",
        "possibleImpact": "",
        "confidence": "",
        "rawEventJson": raw.get("raw", raw),
        "riskFactors": [],
        "dependencyRole": "",
        "blastRadius": "",
        "whyRisk": "",
        # Identity attribution — carried from the collector/backfill (filled later by resolve step).
        "actorDisplay": raw.get("actorDisplay", ""),
        "actorObjectId": raw.get("actorObjectId", ""),
        "actorKind": raw.get("actorKind", "") or raw.get("actorType", "Unknown"),
        "actorIp": raw.get("actorIp", ""),
        "actorOnBehalfOf": raw.get("actorOnBehalfOf", ""),
        "actorResolved": bool(raw.get("actorResolved", False)),
        "categoryHint": raw.get("category_hint", ""),
        "details": details,
    }
