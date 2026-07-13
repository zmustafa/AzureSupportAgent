---
layout: default
title: Proactive Support, Monitor, and Stats
parent: Core Experience
grand_parent: User guide
nav_order: 3
description: Understand the proactive feature catalog, operational monitor, and read-only statistics views.
permalink: /user-guide/core/proactive-monitor-stats/
---

# Proactive Support, Monitor, and Stats

**Routes:** `/proactive`, `/monitor`, and `/stats`

**Product permissions:** `/proactive` is an admin-only navigation landing page and each destination enforces its own permission. `/monitor` requires `monitor.view`. `/stats` is admin-only and read-only.

## Purpose


**Proactive Support** groups current design, assessment, coverage, estate-intelligence, governance, identity, lifecycle, and investigation features. It is a catalog and navigation surface; opening it does not run a scan.

**Monitor** presents application health and operational activity. **Stats** provides a compact, read-only metrics summary. Neither page substitutes for feature history or the audit log.

## Prerequisites and data sources

Sign in with a role that exposes the route. Monitor and Stats use application telemetry and stored operational records. Proactive destination cards can depend on Azure Resource Manager, Resource Graph, Microsoft Graph, Log Analytics, or feature caches after navigation.

## Tabs and actions

- On `/proactive`, select a grouped feature card to open its owning route.
- On `/monitor`, inspect runtime status and available operational metrics; use refresh controls when present.
- On `/stats`, inspect the at-a-glance read-only counters.
- Follow links to the owning feature before interpreting stale, partial, failed, or unavailable data.

## Freshness and scope behavior

The landing page does not collect Azure data. Monitor and Stats reflect the latest application records available to their APIs. Missing values can mean no run, no permission, unavailable telemetry, or a failed optional source; they do not prove a healthy zero.

## Workflow overview

1. Open the required route.
2. Confirm the active role and intended tenant context.
3. Inspect freshness, status, and error indicators.
4. Navigate to the owning feature for evidence or remediation.
5. Use feature history or Audit Log when a durable record is required.

## Interpretation of results

Treat summaries as navigation and triage signals. Validate feature health against its detailed page and source evidence. Validate AI-generated conclusions in destination features against collected evidence.

## Exports, history, scheduling, and integrations

These three views do not create schedules or apply Azure changes. Export, history, notification, and evidence behavior belongs to each destination feature.

## Safety and limitations

All three views are read-oriented. A destination can expose scans, application-state writes, generated artifacts, external deliveries, or approval-gated Azure mutations. Review that destination's permission and safety model before acting.

## Troubleshooting


| Symptom | Resolution |
| --- | --- |
| Route is absent | Confirm the user is an administrator or has the required feature permission. |
| A metric is empty | Check whether the source feature has run and whether its API returned an error or partial result. |
| Values appear stale | Refresh the owning feature and verify its recorded collection time. |
| A card opens a denied page | Request the exact permission listed on the destination feature page. |

## Related pages

- [Dashboard]({{ site.baseurl }}/user-guide/core/dashboard/)
- [Permissions]({{ site.baseurl }}/reference/permissions/)
- [How to use Proactive Support, Monitor, and Stats]({{ site.baseurl }}/how-to/core-workloads/proactive-monitor-stats/)
