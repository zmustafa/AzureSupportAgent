---
layout: default
title: Investigate with Change Explorer
parent: Estate intelligence operations
grand_parent: How-to guides
nav_order: 13
description: Analyze every Change Explorer view, preserve evidence, compare runs, and export investigation reports.
permalink: /how-to/estate-intelligence/change-explorer/
feature_ids: [CHANGEEXPLORER_NAV:summary, CHANGEEXPLORER_NAV:operations, CHANGEEXPLORER_NAV:narrative, CHANGEEXPLORER_NAV:timeline, CHANGEEXPLORER_NAV:changes, CHANGEEXPLORER_NAV:security, CHANGEEXPLORER_NAV:risk, CHANGEEXPLORER_NAV:resources, CHANGEEXPLORER_NAV:actors, CHANGEEXPLORER_NAV:diff, CHANGEEXPLORER_NAV:impact, CHANGEEXPLORER_NAV:compare, CHANGEEXPLORER_NAV:export]
---

# Investigate with Change Explorer

![Change Explorer forensic workspace]({{ site.baseurl }}/assets/change-explorer.png)

## Prerequisites

- Product permission `changeexplorer.read`.
- ARM access to Activity Log/change evidence across the selected scope.
- A workload for workload and dependency modes; broad visibility for tenant-wide mode.
- Optional Microsoft Graph capability for actor names and an AI provider for optional enrichment.

## Route

Open `/change-explorer` or a tab route from **Summary**, **Operations**, **Narrative**, **Timeline**, **All Changes**, **Security**, **Risk Insights**, **Resources**, **Actors**, **Technical Diff**, **Dependency Impact**, **Compare**, and **Export / Reports**. The surrounding modes are Explorer, Fleet, and Cleanup.

## How to run a correctly scoped analysis

1. Choose **Explorer** and select workload or subscription plus the connection.
2. Set a UTC preset/custom start and end.
3. Choose **Workload**, **Workload + dependencies**, or **Tenant-wide**. Confirm broad tenant analysis when prompted.
4. Leave **Perform AI analysis** off for fast deterministic analysis, or enable it when approved.
5. Select **Analyze** and monitor collection, classification, and optional AI phases. The run is persisted before completion is returned.
6. Confirm the displayed analyzed window and scope. If the cached-window banner differs from current selectors, re-analyze.

**Expected result:** A fixed, saved forensic run represents the recorded scope and time window.

**Verification:** Match workload/subscription, mode, start/end, run time, event count, and collection notes. Activity Log can be eventually consistent, so repeat later when necessary.

## How to use Summary, Risk Insights, and Timeline for first-pass triage

1. Open **Summary** and record headline, window, scope, severity counts, top actor/type, and insights.
2. Open **Risk Insights** and inspect highest-risk events before lower-severity volume.
3. Open **Timeline** to place candidate events in chronological context.
4. Open each important event rather than accepting aggregate labels as proof.

**Expected result:** A prioritized timeline of candidate causes and effects is established.

**Verification:** Cite event timestamps, IDs, resources, operations, and underlying evidence. Risk is prioritization, not proof of impact or malice.

## How to inspect All Changes and deep-link exact evidence

1. Open **All Changes** and search/filter the virtualized event grid.
2. Use the plain-English question flow where available; verify the parsed time window/facets and suggested window.
3. Open a row's drawer and inspect Summary and Diff.
4. Open **Raw** only when needed; raw JSON is lazy-loaded to keep large runs responsive.
5. Copy the event deep link, pin the event, and add an investigator note when relevant.

**Expected result:** The exact change can be reopened by its run/event context and included in the case file.

**Verification:** Reload the deep link and confirm the same event opens. Handle raw payloads and identifiers as sensitive evidence.

## How to interpret Operations and Narrative

1. Open **Operations** to review groups formed by correlation ID or, when absent, actor/time bursts.
2. Expand a group and compare verb, actor, resources, risk, security flags, and child events.
3. Open **Narrative** for ordered story beats.
4. If the run is deterministic and narrative is incomplete, start on-demand AI enrichment; it updates the saved run without recollecting Azure.
5. Validate every narrative assertion against child events.

**Expected result:** Related events are summarized into an investigation sequence with optional AI context.

**Verification:** Treat time-burst grouping as heuristic and AI output as fallible; correlation and raw events remain primary evidence.

## How to investigate Security signals

1. Open **Security** and filter/search flagged events.
2. Review public exposure, grants, secret access/change, disabled logging, removed locks, exemptions, off-hours, first-time actors, mass deletion, and escalation signals.
3. Open the underlying event and technical diff.
4. Correlate with approved change records, identity/RBAC evidence, and organizational working hours.
5. Pin substantiated events and note disposition.

**Expected result:** Security flags become validated leads or documented false positives.

**Verification:** Confirm resource, actor, operation, timestamp, before/after, and business context. Flags are not verdicts.

## How to investigate Resources and Dependency Impact

1. Open **Resources** and select a changed resource.
2. Review its event history across the analyzed window.
3. Open **Dependency Impact** to inspect direct/transitive known dependencies and blast radius.
4. Validate important edges against architecture, runtime telemetry, and service ownership.

**Expected result:** Resource-local history and modeled downstream impact guide the investigation scope.

**Verification:** The graph reflects known dependencies only; absence of an edge does not prove no runtime or data-plane dependency.

## How to resolve actors without mislabeling unknown identities

1. Open **Actors** and inspect display name, stable ID, actor kind, source IP, on-behalf-of context, and activity count where available.
2. Distinguish User, Service Principal, Managed Identity, Azure Policy/platform/system, and Unknown badges.
3. Treat an unresolved GUID as an identity-resolution limitation, not anonymous activity.
4. Check Connection Capability and Graph consent when names remain unresolved.
5. Correlate actor events with approved change records and source IP context.

**Expected result:** Activity is attributed as precisely as available evidence permits, with graceful degradation.

**Verification:** Compare object/app IDs and claims with Graph/Activity Log. Rerun after capability correction if display-name resolution is required.

## How to inspect Technical Diff and rollback hints safely

1. Open **Technical Diff** or an event drawer's Diff section.
2. Compare available before/after properties and security-sensitive fields.
3. Review any rollback hint as read-only guidance only.
4. Confirm the current Azure state and use the owning service's approved change process for remediation.

**Expected result:** Property-level evidence supports a remediation plan without Change Explorer mutating Azure.

**Verification:** Missing before-data means evidence was unavailable, not that no change occurred. Re-query current state before any external rollback.

## How to compare two runs

1. Open **Compare**; the most recent other run can be selected as a baseline automatically.
2. Choose runs with comparable scopes and windows.
3. Review added/removed/changed resources, risk deltas, and count deltas.
4. Open underlying events in each run when a delta matters.

**Expected result:** The later run is contrasted with a meaningful baseline.

**Verification:** Confirm run IDs, scopes, windows, collection notes, and reference direction before citing deltas.

## How to operate Fleet and background analysis

1. Choose **Fleet** to see the latest run per active workload, with never-analyzed workloads last and higher risk prioritized.
2. Review run age, scope mode, total changes, and severity counts.
3. Open a workload for detailed analysis or start the appropriate scoped run.
4. Navigate away if necessary; the background registry can surface completion when you return.

**Expected result:** Fleet identifies stale, never-analyzed, or high-risk workloads from saved runs.

**Verification:** Fleet is not a substitute for checking the run's exact window and scope after drill-down.

## How to build investigation evidence and export the right format

1. Pin relevant changes, add per-change notes, and maintain the run's case summary.
2. Open **Export / Reports** and choose the minimum necessary artifact:
   - CSV for event filtering.
   - High-risk CSV for critical/high events.
   - JSON for the full run and raw operational payloads.
   - Executive Markdown for a concise briefing.
   - Technical Markdown for engineering handoff.
   - RCA Markdown as a reviewed starting template.
   - ServiceNow text for ticket transfer.
   - Validation queries as KQL starting points.
   - PDF for a board-oriented incident report.
3. Open the download and verify scope, window, event count, and redaction/handling requirements.
4. Store and share according to evidence policy.

**Expected result:** A fixed investigation artifact is downloaded from the saved run.

**Verification:** Compare export metadata with Summary and sample events. Never treat generated RCA or AI narrative as an approved conclusion without review.

## How to trash, restore, or purge runs

1. Open run history or **Cleanup**.
2. Trash obsolete runs; this is recoverable.
3. Restore a mistakenly trashed run.
4. Purge only after confirming incident, legal, audit, and retention requirements; bulk cleanup supports selected run IDs.

**Expected result:** Trashed runs remain restorable; purged runs are permanently deleted.

**Verification:** Confirm the restored run reappears or the purged run is absent. Preserve required exports before purge.

## Safety and rollback

- Change Explorer is read-only with respect to Azure and never performs rollback.
- AI enrichment and risk/security classification can be wrong.
- Actor resolution and technical diff are best-effort.
- JSON/raw exports can contain sensitive operational identifiers and payloads.
- Perform remediation through a separate approved service/IaC workflow, then run a new Change Explorer analysis for verification.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| No events | Check UTC window, scope mode, workload, Activity Log access, subscription visibility, and eventual consistency. |
| Cached-window banner | Re-analyze current selectors; do not relabel the old run. |
| Actor unresolved | Verify Graph token/consent; unresolved does not mean anonymous. |
| Narrative empty | Run optional AI enrichment if allowed, or use deterministic tabs. |
| Raw JSON absent | Open the drawer's Raw section; source evidence may still lack it. |
| Diff incomplete | Validate in Azure/source logs; absence of before-data is not no-change proof. |
| Compare misleading | Use comparable scopes/windows and confirm baseline direction. |
| Export too sensitive | Use a narrower/high-risk/executive format and evidence-handling controls. |

## Related docs

- [Change Explorer reference]({{ site.baseurl }}/user-guide/estate-intelligence/change-explorer/)
- [Inventory recipes]({{ site.baseurl }}/how-to/estate-intelligence/inventory/)
- [Alerts Manager recipes]({{ site.baseurl }}/how-to/coverage/alerts-manager/)
- [Evidence Locker]({{ site.baseurl }}/user-guide/lifecycle-investigation/evidence-locker/)
