---
layout: default
title: Dashboard
parent: Core Experience
grand_parent: User guide
nav_order: 1
description: Read setup progress, workload coverage, posture, risks, and activity from the home dashboard.
permalink: /user-guide/core/dashboard/
---

# Dashboard

**Route:** `/dashboard` (the root route also opens the Dashboard)

## Purpose

The Dashboard is the application's home base. Use it after sign-in to finish setup, choose a primary workload, review cached estate signals, and jump to the feature that owns the underlying detail.
![Azure Support Agent dashboard with guided onboarding cards]({{ site.baseurl }}/assets/proactive-support.png)

### When to use it

- Immediately after deployment to complete the setup guide.
- At the start of an operational review to scan posture and recent activity.
- To compare monitoring, telemetry, backup/DR, and performance trends for one primary workload.
- To find the lowest assessment score, near-term retirement, reservation, identity, RBAC, or optimization signals available to your role.

The Dashboard is a summary, not a replacement for the source feature. Open the linked tool before making a decision.

## Prerequisites and data sources

### Prerequisites and permissions

All signed-in users can see the shell, but each data source has its own permission. Administrative setup data and several posture panels are shown only to administrators. Standard users may see provider/connection summaries and self-service data allowed by their role.

For a useful Dashboard, configure:

- An active AI provider.
- At least one Azure connection.
- A workload for scope-specific trends.
- Feature scans whose cached results feed the posture cards.

## Tabs and actions



## Freshness and scope behavior



## Workflow overview

### Workflow

1. Open `/dashboard`.
2. Expand **Setup guide** and complete outstanding items. A check indicates the configuration was detected; it does not certify every downstream permission.
3. Select a **primary workload** when the coverage controls are available. The selection is retained in the browser.
4. Review coverage trend cards. Follow their links to Monitoring Coverage, Telemetry Coverage, Backup/DR, or Performance for current evidence.
5. Review **Posture & risks** and the KPI strip. Prioritize severity, deadline, and affected workload rather than card order alone.
6. Check recent assessments, deep investigations, insight runs, scheduled activity, and notifications.
7. Open the owning feature, refresh stale data if appropriate, and record the investigation or remediation there.

## Interpretation of results

### Interpret the Dashboard

- **Setup complete** means required configuration objects exist. Always test provider and connection health separately.
- **Trend values** are cache/history reads. They do not trigger a new Azure scan on Dashboard load.
- **Missing or hidden cards** may indicate no permission, a failed optional query, no primary workload, or no prior scan. Missing is not equivalent to healthy or zero.
- **Assessment averages** summarize completed runs and can hide a low-scoring workload; inspect the lowest run.
- **Freshness** matters. Follow a tile into its feature to see scan age and scope.
- **Activity entries** are navigation cues, not a complete audit log.

## Exports, history, scheduling, and integrations

No dedicated export, history, scheduling, or integration controls are documented for this feature page.

## Safety and limitations

### Safety

The Dashboard itself is read-oriented. Its links can lead to scan, generation, or mutation workflows with different permissions. Before acting:

- Confirm the selected workload and Azure connection.
- Refresh stale evidence in the owning feature.
- Distinguish **unknown/not analyzed** from zero coverage.
- Review generated remediation and approval prompts.
- Use the Audit Log or durable feature history when a compliance record is required.

## Troubleshooting


| Symptom | Resolution |
| --- | --- |
| Setup item remains incomplete | Test and activate the provider/connection, then reload; non-admin views may expose only a summary |
| Coverage cards are empty | Create/select a workload and run the corresponding scans |
| A panel disappeared | Check application role, endpoint access, and whether the optional data source failed |
| Values look old | Open the owning feature, check freshness, and run a scoped refresh if authorized |
| Dashboard is slow | Expensive panels are deferred and cached; check failing network requests rather than repeatedly refreshing |
| Primary workload is wrong | Change the workload selector; the choice is stored per browser |

## Related pages

- [First-run setup]({{ site.baseurl }}/getting-started/first-run/)
- [Chat and Deep Investigation]({{ site.baseurl }}/user-guide/core/chat-deep-investigation/)
- [Workload fleet]({{ site.baseurl }}/user-guide/workloads/fleet/)
- [Mission Control]({{ site.baseurl }}/user-guide/mission-control/)
