---
layout: default
title: Change Explorer
parent: Estate Intelligence
grand_parent: User guide
nav_order: 3
description: Investigate Azure changes by time, operation, actor, risk, resource, technical diff, dependency impact, and run comparison.
permalink: /user-guide/estate-intelligence/change-explorer/
feature_ids: [CHANGEEXPLORER_NAV:summary, CHANGEEXPLORER_NAV:operations, CHANGEEXPLORER_NAV:narrative, CHANGEEXPLORER_NAV:timeline, CHANGEEXPLORER_NAV:changes, CHANGEEXPLORER_NAV:security, CHANGEEXPLORER_NAV:risk, CHANGEEXPLORER_NAV:resources, CHANGEEXPLORER_NAV:actors, CHANGEEXPLORER_NAV:diff, CHANGEEXPLORER_NAV:impact, CHANGEEXPLORER_NAV:compare, CHANGEEXPLORER_NAV:export]
---

# Change Explorer

**Product permission:** `changeexplorer.read` (the current API is admin-gated).

## Purpose

**App routes:** `/change-explorer` and `/change-explorer/:tab`
Change Explorer collects a bounded Azure change window, classifies activity, resolves actors where possible, and saves a repeatable forensic run. AI enrichment is optional and off by default.
![Change Explorer showing a forensic change timeline, actors, risk, and impact]({{ site.baseurl }}/assets/change-explorer.png)

## Prerequisites and data sources

### Prerequisites

- An ARM-capable connection with access to Activity Log/change evidence across the selected scope.
- A registered workload for workload and workload-plus-dependencies modes; tenant-wide mode requires broad subscription visibility.
- Microsoft Graph capability is optional but improves user/service-principal display-name resolution.
- A configured AI provider only for optional narrative/enrichment; deterministic analysis does not require AI.

## Tabs and actions

### Tabs

- **Summary**: headline, analyzed window, scope, severity counts, top actor/type, and insights.
- **Operations**: correlated operations or actor/time bursts with verb, resources, highest risk, and flags.
- **Narrative**: ordered story beats; if the run was deterministic, this tab offers on-demand AI enrichment.
- **Timeline**: chronological interactive event view.
- **All Changes**: virtualized searchable event grid and detail drawer.
- **Security**: flagged events and suspicious patterns such as public exposure, grants, secret access/change, disabled logging, removed locks, exemptions, off-hours activity, mass deletion, or potential escalation.
- **Risk Insights**: severity distribution and highest-risk events.
- **Resources**: per-resource history and available blast-radius context.
- **Actors**: resolved identity kind, source information where available, and activity counts.
- **Technical Diff**: before/after property differences for available events.
- **Dependency Impact**: direct/transitive dependency and blast-radius analysis available to the model.
- **Compare**: deltas between two saved runs.
- **Export / Reports**: CSV, high-risk CSV, JSON, executive/technical Markdown, RCA, ServiceNow text, validation queries, and PDF.

The surrounding Fleet view ranks workloads by latest run/risk. Cleanup supports trash, restore, and permanent purge.

## Freshness and scope behavior

### Freshness and retention

Each run is a fixed analysis of its recorded start/end window. Changing selectors does not rewrite the run. Runs persist until trashed and purged; trash is recoverable, purge is permanent. Raw JSON is omitted from lightweight reads and fetched when needed to keep large runs responsive.

Activity Log and Resource Graph are eventually consistent. A run performed immediately after a change may need to be repeated later. Actor resolution is best-effort and can degrade without Graph permissions.

## Workflow overview

### Configure an analysis

Choose workload, connection, start/end time, and scope mode:

- **Workload** limits analysis to direct workload resources.
- **Workload + dependencies** expands through the dependency model available to the app.
- **Tenant-wide** scans all subscriptions visible to the connection.

Enable AI only when contextual narrative/risk enrichment is valuable and approved. Start the streaming analysis and monitor collection, classification, and AI phases. The run is persisted before completion is returned.

### Investigate a run

1. Confirm the displayed **analyzed window** and scope. A stale-window banner means the saved run does not match current selectors; re-analyze instead of assuming it does.
2. Start with Summary and Risk Insights, then validate high-risk events in All Changes.
3. Open an event drawer. Inspect summary and technical diff; raw event JSON is loaded only on demand.
4. Use Security flags as leads, not verdicts. Confirm context and expected change records.
5. Review Actors. An unresolved identifier means Graph resolution was unavailable, not that the actor was anonymous.
6. Inspect resource history and dependency impact before declaring blast radius.
7. Pin relevant events, add notes, or hand off to investigation where the UI offers it.
8. Compare against a suitable prior run and export the minimum evidence needed.

## Interpretation of results

### Interpretation

- Risk is triage prioritization, not proof of impact or malicious intent.
- Operations can be grouped by correlation ID or actor/time burst; grouped events are related heuristically when correlation is absent.
- Security patterns such as off-hours or first-time actor require organizational context.
- Technical diff availability depends on source evidence; absence of a before value is not proof that nothing changed.
- Dependency impact reflects the app's known graph and cannot discover every runtime/data-plane dependency.
- AI narrative and re-scoring can be wrong. Cite underlying events and timestamps in an incident conclusion.

## Exports, history, scheduling, and integrations

### Exports and safety

Exports are read-only and Change Explorer never reverts Azure changes. JSON can include raw operational payloads and identifiers; handle it as sensitive evidence. CSV/Markdown summaries may omit detail, while PDF is board-oriented. Validation queries are starting points and must be reviewed before use.

Do not purge runs needed for incident, legal, or audit retention. Do not mistake a generated RCA for an approved final RCA.

## Safety and limitations



## Troubleshooting


| Symptom | Check |
| --- | --- |
| No events | Verify UTC window, selected mode/workload, Activity Log permissions, subscription visibility, and eventual consistency. |
| Banner says cached window differs | Re-analyze the current selection; do not use the cached run as if it matched. |
| Actors show unresolved IDs | Verify Graph token/consent and rerun or refresh identity context. |
| Narrative is empty | The run likely used deterministic mode; start optional AI enrichment if allowed. |
| Raw JSON is absent | Open the event's Raw JSON section to lazy-load it; confirm the source retained it. |
| Export is too large/sensitive | Filter or use high-risk/executive output and follow evidence-handling policy. |
| Compare looks misleading | Ensure both runs use comparable scope and windows. |

## Related pages

- [Inventory]({{ site.baseurl }}/user-guide/estate-intelligence/inventory/)
- [RBAC]({{ site.baseurl }}/user-guide/governance-identity/rbac/)
- [Alerts Manager]({{ site.baseurl }}/user-guide/coverage/alerts-manager/)
