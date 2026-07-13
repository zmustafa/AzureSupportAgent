---
layout: default
title: Workload Fleet
parent: Workloads
grand_parent: User guide
nav_order: 1
description: Use the fleet cockpit to compare workload health, composition, criticality, and risk.
permalink: /user-guide/workloads/fleet/
---

# Workload fleet

**Route:** `/workloads`

## Purpose

The fleet cockpit provides a portfolio view of active workloads. Use it to identify unknown or low-health workloads, see estate composition, compare environment and criticality, and open a workload for deeper analysis.
![Workload fleet cockpit showing health and resource composition]({{ site.baseurl }}/assets/workloads-fleet.png)

### When to use it

- Daily or weekly fleet triage.
- Finding workloads that have never been analyzed.
- Prioritizing production/critical workloads with coverage or retirement risks.
- Starting Autopilot, manual workload creation, merge, grouping, or overlap analysis.

## Prerequisites and data sources

### Prerequisites and permissions

- `workloads.read` to view the fleet and profiles.
- `workloads.write` to create, edit, merge, delete, or group workloads.
- Existing workloads, or a readable Azure connection for discovery.
- Prior feature scans for meaningful health and risk values.

## Tabs and actions



## Freshness and scope behavior



## Workflow overview

### Workflow

1. Open `/workloads`.
2. Review fleet composition, environment-by-criticality distribution, health bands, and risk indicators.
3. Filter or sort to place critical, production, unknown, or poor-health workloads first.
4. Open a workload card or row to inspect its detail.
5. For **Not analyzed**, run analysis from workload detail rather than assuming failure.
6. Use **Autopilot** to discover missing application boundaries.
7. Open **Overlaps** when resource ownership is ambiguous, or **Groups** to compare related environments.
8. Launch [Mission Control]({{ site.baseurl }}/user-guide/mission-control/) for a coordinated multi-system sweep.

### Safety and lifecycle

- Editing membership changes the scope used by downstream analyses.
- **Merge** moves source workloads to Trash and has no dedicated undo; review members and downstream links first.
- Normal delete is soft-delete and can be restored. Purge and empty-trash are permanent.
- Trashed workloads are detached from groups and excluded by downstream active-workload consumers.
- Fleet pages read cached profiles and should not be mistaken for a live Azure scan.

## Interpretation of results

### Interpret fleet health

A workload profile combines available monitoring, telemetry, backup/DR, performance, ownership, policy, and tag signals. The overall score reweights only the signals that are present.

| Band | Score | Meaning |
| --- | ---: | --- |
| Good | 80–100 | Available signals are broadly healthy; inspect freshness and any remaining risks |
| Warning | 50–79 | Material gaps exist and should be planned for remediation |
| Poor | 0–49 | Available signals indicate significant gaps or failures |
| Not analyzed | No score | No usable signals have been computed; this is unknown, not zero |

Because the score is normalized over present signals, two workloads with different signal coverage are not always directly comparable. Open detail and check freshness and component scores.

## Exports, history, scheduling, and integrations

No dedicated export, history, scheduling, or integration controls are documented for this feature page.

## Safety and limitations



## Troubleshooting


| Symptom | Resolution |
| --- | --- |
| Fleet is empty | Run Autopilot or create a workload; verify `workloads.read` |
| Workload says **Not analyzed** | Open it and run Analyze |
| Score changed sharply | Compare component freshness and determine which newly available signal changed normalization |
| Resource count is stale | Refresh an Autopilot-origin workload or edit its nodes |
| A deleted workload vanished from a group | Restore it; trashed workloads are intentionally detached |
| Merge result is unexpected | Inspect the merged workload and source entries in Trash before any permanent purge |

## Related pages

- [Discovery and Autopilot]({{ site.baseurl }}/user-guide/workloads/discovery-autopilot/)
- [Workload detail]({{ site.baseurl }}/user-guide/workloads/workload-detail/)
- [Groups and overlaps]({{ site.baseurl }}/user-guide/workloads/groups-overlaps/)
- [Mission Control]({{ site.baseurl }}/user-guide/mission-control/)
