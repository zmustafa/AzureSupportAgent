---
layout: default
title: Use Proactive Support, Monitor, and Stats
parent: Core and workload operations
grand_parent: How-to guides
nav_order: 6
description: Navigate proactive tools and verify application health and summary statistics without triggering Azure writes.
permalink: /how-to/core-workloads/proactive-monitor-stats/
feature_ids: [SHELL_NAV:proactive, ROUTE:proactive, ROUTE:monitor, ROUTE:stats]
---

# Use Proactive Support, Monitor, and Stats

## Prerequisites

- At least one Proactive destination read permission for `/proactive`.
- `monitor.view` for `/monitor` and `/stats`.
- Both `monitor.view` and `settings.write` when creating, customizing, copying, deleting,
  defaulting, AI-authoring, or restoring a Monitor dashboard. A write key alone does not expose
  the read-gated route.
- The destination feature's exact product permission before opening a Proactive Support card.

## Route

Use `/proactive`, `/monitor`, or `/stats`.

## How to choose a proactive workflow

1. Open `/proactive`.
2. Select the relevant group: daily intelligence, design and ownership, assessment and performance, coverage, estate intelligence, governance and identity, or lifecycle and investigation.
3. Read the card description and open the owning feature.
4. On the destination page, confirm connection, scope, freshness, and partial-result indicators before running or changing anything.

**Expected result:** The selected feature opens without a scan being started by the landing page.

**Verification:** Confirm the browser route and the destination title, then check its permission and freshness statement.

## How to inspect operational health

{% include screenshot.html file="core-monitor-activity.png" title="Read the built-in Monitor overview" caption="Confirm the selected dashboard and freshness before interpreting activity counters. Empty live-operation and investigation panels are distinct from system-health results; this example uses synthetic fixture responses." %}

1. Open `/monitor`.
2. Select the built-in overview or a saved dashboard.
3. Choose the live refresh cadence, pause/resume refresh, or select **Refresh now**.
4. Review available runtime, activity, usage, tool, automation, connector, and posture indicators.
5. Note loading, failed, unavailable, or stale states rather than treating them as zero.
6. Use fullscreen for a temporary NOC display or export the current aggregate snapshot as JSON.
7. Follow a feature link or inspect the Audit Log when a durable explanation is required.

**Expected result:** Current application telemetry available to the signed-in role is displayed.

**Verification:** Compare the generated timestamp and displayed activity with the owning
feature's latest run or history record. Open the JSON locally and confirm it reflects the same
tenant-scoped aggregate; do not treat it as a live feed after download.

## How to author a Monitor dashboard

1. Open `/monitor` with an active role containing both `monitor.view` and `settings.write`.
2. Select **New** for the built-in layout, **Customize** for the selected dashboard, or **Build
	from workload** for AI-assisted suggestions.
3. When building from a workload, choose the workload and dashboard style, select **Suggest
	widgets**, review the design brief and each proposed widget, clear unsuitable suggestions,
	then build only the approved set.
4. In Customize mode, drag widget headers to move them and edges/corners to resize them.
5. Select **Add widget** to add a built-in tile or a stat, chart, table, list, gauge,
	availability, map, Markdown, or clock widget. **Build a widget with AI** produces a preview;
	it does not become part of the dashboard until selected.
6. For a data widget, use **Data** to choose and scope the data source, **Visualize** to choose
	rendering and columns, and **Settings** to select live/manual refresh and thresholds.
7. Inspect the preview and the actual grid. Select **Save** to update the selected dashboard or
	**Save as** to preserve it and create a copy.
8. Optionally select **Set default** after verifying the saved dashboard from another view-only
	session.

**Expected result:** A tenant-scoped application dashboard is saved with the approved layout and
widgets. No Azure resource is created or changed by saving the dashboard.

**Verification:** Leave and reopen `/monitor`, select the dashboard, and confirm its layout,
data source scopes, refresh modes, thresholds, and default marker. Test with a `monitor.view`-only
role and confirm it can read/run widget data but sees **Read-only dashboard access**.

## How to review read-only statistics

1. Open `/stats` with `monitor.view`.
2. Select a 1-day, 3-day, 7-day, 2-week, or 30-day activity range.
3. Optionally select one assessed workload for the Azure posture section.
4. Pause/resume the 30-second refresh or refresh now.
5. Review the at-a-glance counters, health/usage, trends, breakdowns, posture, and activity.
6. Open the owning feature for scope, collection time, and evidence before drawing an operational conclusion.

**Expected result:** A read-only summary appears; no Azure operation is submitted.

**Verification:** Confirm no approval, apply, or mutation control is present on Stats and verify the source in the owning feature.

## Safety and rollback

These pages do not themselves mutate Azure. No rollback is needed for viewing, filtering,
refreshing, fullscreen mode, or JSON export. Dashboard save/default/delete operations change the
application dashboard registry. Use **Cancel** before save to discard the working copy; use
**Save as** before risky edits to preserve the prior dashboard. The backend retains dashboard
revisions and protects restore with `settings.write`, but the current Monitor toolbar does not
expose a revision browser or restore control. Deletion cannot be undone from the toolbar and is
not a substitute for removing the upstream workbook, query, or Azure data source.

AI-proposed widgets and dashboards can be invalid, incomplete, expensive, or scoped too broadly.
Validate their data source, connection, query, refresh cadence, and thresholds. Destination
features can create local records, artifacts, deliveries, or approval-gated Azure changes;
follow their preview, approval, apply, verification, retry, and rollback instructions exactly.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Proactive Support is hidden | Assign at least one destination read capability to the active role. |
| Stats is hidden | Assign `monitor.view` to the active role. |
| Monitor returns forbidden | Assign a role containing `monitor.view`. |
| Monitor opens but authoring is read-only | Switch to an approved role containing both `monitor.view` and `settings.write`. |
| A custom role has `settings.write` but Monitor is hidden | Add `monitor.view`; the route is independently read-gated. |
| Saved widget has no data | Reopen Customize and verify data source, connection, parameters, query/workbook, and upstream permissions. |
| AI suggestion is irrelevant or ungrounded | Discard it, narrow workload/style/prompt, and validate every retained widget against source data. |
| A shared destination URL is forbidden | Assign its exact product permission or switch to an assigned role that carries it. |
| A summary is blank | Run or refresh the owning feature if authorized, then check errors and partial-result warnings. |

## Related docs

- [Proactive Support, Monitor, and Stats reference]({{ site.baseurl }}/user-guide/core/proactive-monitor-stats/)
- [Dashboard]({{ site.baseurl }}/user-guide/core/dashboard/)
- [Permissions]({{ site.baseurl }}/reference/permissions/)
