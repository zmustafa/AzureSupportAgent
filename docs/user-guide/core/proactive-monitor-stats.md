---
layout: default
title: Proactive Support, Monitor, and Stats
parent: Core Experience
grand_parent: User guide
nav_order: 3
description: Understand the proactive feature catalog, operational monitor, and read-only statistics views.
permalink: /user-guide/core/proactive-monitor-stats/
feature_ids: [SHELL_NAV:proactive, ROUTE:proactive, ROUTE:monitor, ROUTE:stats]
---

# Proactive Support, Monitor, and Stats

**Routes:** `/proactive`, `/monitor`, and `/stats`

**Product permissions:** `/proactive` appears when at least one destination read permission is present and shows only permitted cards. `/monitor` and `/stats` require `monitor.view`; Monitor dashboard authoring additionally requires `settings.write`.

## Purpose


**Proactive Support** groups current design, assessment, coverage, estate-intelligence, governance, identity, lifecycle, and investigation features. It is a catalog and navigation surface; opening it does not run a scan.

**Monitor** presents application health and operational activity. **Stats** provides a compact, read-only metrics summary. Neither page substitutes for feature history or the audit log.

{% include screenshot.html file="core-monitor-activity.png" title="Monitor application counters and live-operation status" caption="The built-in overview separates application activity from currently running work. This synthetic fixture shows no active operations; its counters are not measured operator activity or billed provider usage." %}

## Prerequisites and data sources

Sign in with a role that exposes the route. Monitor and Stats require `monitor.view`; a custom
role that must author Monitor dashboards also needs `settings.write`. Monitor and Stats use
tenant-scoped application telemetry, usage, tool-call, automation, assessment, connector, and
activity records. A custom widget can additionally resolve application telemetry, Azure Resource
Graph, metrics, Log Analytics/KQL, workbooks, availability checks, or static content according to
its selected data source and connection.

Proactive destination cards can depend on Azure Resource Manager, Resource Graph, Microsoft
Graph, Log Analytics, or feature caches after navigation. The landing page itself does not query
those destinations.

## Tabs and actions

- On `/proactive`, select a grouped feature card to open its owning route. Only cards allowed by
	the active role are rendered.
- On `/monitor`, select a saved dashboard or the built-in overview; pause/resume live refresh,
	choose a 5/15/30/60-second cadence, refresh now, export the current overview snapshot as JSON,
	or use fullscreen NOC mode.
- With `settings.write`, use **Customize**, **New**, **Build from workload**, **Set default**,
	**Delete**, **Add widget**, **Save**, **Save as**, and **Cancel**. Drag and resize widgets while
	editing. The widget drawer separates **Data**, **Visualize**, and **Settings**, including data
	binding, chart/type options, live/manual refresh, and thresholds.
- AI authoring can propose one widget from a prompt or suggest a workload dashboard. Review the
	preview and selected suggestions before adding or saving; AI output is not source evidence.
- On `/stats`, select a 1/3/7/14/30-day range, optionally scope Azure posture to one assessed
	workload, pause/resume the 30-second refresh, refresh now, and inspect the fixed read-only view.
- Follow links to the owning feature before interpreting stale, partial, failed, or unavailable data.

## Freshness and scope behavior

The landing page does not collect Azure data. Monitor defaults to a 15-second live refresh and
lets the viewer select 5, 15, 30, or 60 seconds; Stats defaults to 30 seconds. Pausing stops the
periodic refetch but does not freeze upstream systems. The generated timestamp describes the
current aggregate response, not the collection time of every source record.

The Monitor widget resolver caches normalized tabular results according to the data source and
widget configuration. A manual refresh requests current aggregate/widget data; it does not rerun
every source feature. Missing values can mean no run, no permission, unavailable telemetry, an
empty selected range, or a failed optional source; they do not prove a healthy zero.

## Workflow overview

1. Open the required route.
2. Confirm the active role and intended tenant context.
3. Inspect freshness, status, and error indicators.
4. For dashboard authoring, enter Customize, change a working copy, preview data/viz behavior,
   then save or cancel. Saving changes application dashboard configuration, not Azure resources.
5. Navigate to the owning feature for evidence or remediation.
6. Use dashboard revisions, feature history, or Audit Log when a durable record is required.

## Interpretation of results

Treat summaries as navigation and triage signals. The fixed Stats range applies to activity-based
aggregations; current-state values such as pending approvals, schedules, connectors, live turns,
and posture can use their own lifetime/latest semantics. A workload selection scopes the Azure
posture section, not every tenant-wide metric. Validate feature health against its detailed page
and source evidence. Validate AI-generated widget/dashboard choices and destination conclusions
against collected evidence.

## Exports, history, scheduling, and integrations

Monitor can download its current aggregate response as a local JSON artifact. Saved dashboard
changes create application-side dashboard state and revisions; they do not create schedules or
apply Azure changes. The current toolbar exposes create/update/copy/default/delete authoring but
does not expose a revision browser. Stats and Proactive Support have no dedicated export.

Widget data sources can integrate with workbooks, Azure queries, Log Analytics, metrics, and
availability checks. Export, notification, evidence, and remediation history for an owning
feature remain on that feature.

## Safety and limitations

Viewing, filtering, refreshing, fullscreen mode, and JSON export do not change Azure. Monitor
dashboard authoring with `settings.write` changes application configuration. A widget data source
can issue live reads and consume provider/Azure capacity; review connection scope, KQL/Resource
Graph text, refresh cadence, and AI-generated configuration before saving. Deleting a dashboard
removes it from the active registry; preserve or copy important configuration first.

A Proactive destination can expose scans, application-state writes, generated artifacts,
external deliveries, or approval-gated Azure mutations. Review that destination's permission and
safety model before acting.

## Troubleshooting


| Symptom | Resolution |
| --- | --- |
| Route is absent | Confirm the active role has the required feature permission. |
| A metric is empty | Check whether the source feature has run and whether its API returned an error or partial result. |
| Values appear stale | Refresh the owning feature and verify its recorded collection time. |
| A shared/deep URL is denied | Request the exact permission listed on the destination feature page or switch to an assigned role that has it. |
| Monitor shows **Read-only dashboard access** | The active role has `monitor.view` but not `settings.write`. Add the write key to an approved authoring role; do not remove the view key because the route still requires it. |
| A widget preview is empty or unauthorized | Verify its selected data source, connection, parameters, workbook/KQL, and upstream Azure or Log Analytics access. |
| Stats changes only part of the page after a range/workload selection | Date range drives activity aggregations; the workload picker scopes only Azure posture. Current-state metrics remain tenant-wide. |

## Related pages

- [Dashboard]({{ site.baseurl }}/user-guide/core/dashboard/)
- [Permissions]({{ site.baseurl }}/reference/permissions/)
- [How to use Proactive Support, Monitor, and Stats]({{ site.baseurl }}/how-to/core-workloads/proactive-monitor-stats/)
