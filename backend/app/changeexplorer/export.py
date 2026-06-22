"""Export + report generation for a Change Explorer run. Read-only text/CSV/JSON producers and
deterministic report writers (executive / technical / RCA / ServiceNow). Validation-only Azure
queries are emitted for the user to run — never executed here.
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any

_CSV_COLUMNS = [
    "eventTime", "riskLabel", "riskScore", "category", "resourceName", "resourceType",
    "resourceGroup", "subscriptionId", "operation", "actor", "actorType", "source",
    "confidence", "plainEnglishSummary", "possibleImpact", "correlationId",
]


def to_csv(events: list[dict[str, Any]], *, high_risk_only: bool = False) -> str:
    rows = [e for e in events if not high_risk_only or e.get("riskLabel") in ("Critical", "High")]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_CSV_COLUMNS)
    for e in rows:
        w.writerow([e.get(c, "") for c in _CSV_COLUMNS])
    return buf.getvalue()


def to_json(run: dict[str, Any]) -> str:
    return json.dumps(run, indent=2, default=str)


def _counts_line(run: dict[str, Any]) -> str:
    return (f"{run.get('totalChanges', 0)} changes — "
            f"{run.get('criticalCount', 0)} critical, {run.get('highCount', 0)} high, "
            f"{run.get('mediumCount', 0)} medium, {run.get('lowCount', 0)} low, "
            f"{run.get('informationalCount', 0)} informational.")


def executive_summary(run: dict[str, Any]) -> str:
    lines = [
        f"# Change report — {run.get('workloadName', '')}",
        f"Window: {run.get('startTime', '')} to {run.get('endTime', '')}  ·  Scope: {run.get('scopeMode', '')}",
        "",
        run.get("summary", ""),
        "",
        _counts_line(run),
    ]
    insights = run.get("insights") or []
    if insights:
        lines += ["", "## Key insights"]
        for i in insights[:6]:
            lines.append(f"- [{i.get('severity','')}] {i.get('title','')} — {i.get('summary','')}")
    return "\n".join(lines)


def technical_summary(run: dict[str, Any]) -> str:
    lines = [f"# Technical change summary — {run.get('workloadName', '')}", _counts_line(run), ""]
    for e in sorted(run.get("events", []), key=lambda x: -int(x.get("riskScore", 0))):
        lines.append(f"## {e.get('riskLabel','')} ({e.get('riskScore',0)}) — {e.get('resourceName','')} [{e.get('category','')}]")
        lines.append(f"- {e.get('eventTime','')} · {e.get('operation','')} · by {e.get('actor','')} ({e.get('actorType','')}) · source {e.get('source','')}")
        lines.append(f"- {e.get('plainEnglishSummary','')}")
        lines.append(f"- Impact: {e.get('possibleImpact','')}")
        lines.append(f"- Why risky: {e.get('whyRisk','')}")
        for d in e.get("details", []):
            lines.append(f"    · {d.get('propertyPath','')}: {d.get('beforeValue')} -> {d.get('afterValue')}")
        if e.get("correlationId"):
            lines.append(f"- correlationId: {e.get('correlationId')}")
        lines.append("")
    return "\n".join(lines)


def rca_summary(run: dict[str, Any]) -> str:
    high = [e for e in run.get("events", []) if e.get("riskLabel") in ("Critical", "High")]
    high.sort(key=lambda e: e.get("eventTime", ""))
    lines = [
        f"# RCA-style change timeline — {run.get('workloadName', '')}",
        f"Window: {run.get('startTime', '')} to {run.get('endTime', '')}",
        "",
        "## Highest-risk changes in chronological order (candidate root causes)",
    ]
    for e in high:
        lines.append(f"- {e.get('eventTime','')} — {e.get('resourceName','')}: {e.get('plainEnglishSummary','')} "
                     f"({e.get('riskLabel','')}; could impact: {e.get('possibleImpact','')})")
    if not high:
        lines.append("- No critical/high-risk changes in this window.")
    lines += ["", "Note: impact statements are inferred from resource role, not confirmed incidents."]
    return "\n".join(lines)


def servicenow_text(run: dict[str, Any]) -> str:
    high = [e for e in run.get("events", []) if e.get("riskLabel") in ("Critical", "High")]
    lines = [
        f"Change review — {run.get('workloadName', '')} ({run.get('startTime','')} to {run.get('endTime','')})",
        _counts_line(run),
        "",
        "High-risk changes for review:",
    ]
    for e in high:
        lines.append(f"* {e.get('resourceName','')} [{e.get('category','')}] — {e.get('plainEnglishSummary','')} "
                     f"(risk {e.get('riskScore',0)}/100, actor {e.get('actor','')})")
    if not high:
        lines.append("* None")
    return "\n".join(lines)


def validation_queries(run: dict[str, Any]) -> dict[str, str]:
    """Read-only validation queries the operator can run to confirm current state."""
    ids = [e.get("resourceId", "") for e in run.get("events", []) if e.get("resourceId")][:50]
    arg_ids = ", ".join(f"'{i}'" for i in ids) if ids else ""
    arg = ("Resources\n| where id in~ (" + arg_ids + ")\n| project id, name, type, resourceGroup, tags, properties"
           if arg_ids else "// no resource ids in this run")
    subs = sorted({e.get("subscriptionId", "") for e in run.get("events", []) if e.get("subscriptionId")})
    az = "\n".join(
        f"az monitor activity-log list --subscription {s} --start-time {run.get('startTime','')} "
        f"--end-time {run.get('endTime','')} -o table" for s in subs
    ) or "# no subscriptions in this run"
    ps = "\n".join(
        f"Get-AzActivityLog -StartTime '{run.get('startTime','')}' -EndTime '{run.get('endTime','')}'"
        for _ in [0]
    )
    kql = ("AzureActivity\n| where TimeGenerated between (datetime('" + str(run.get("startTime", "")) +
           "') .. datetime('" + str(run.get("endTime", "")) + "'))\n| project TimeGenerated, OperationNameValue, "
           "Caller, ResourceId, ActivityStatusValue\n| order by TimeGenerated desc")
    return {"arg": arg, "azcli": az, "powershell": ps, "kql": kql}
