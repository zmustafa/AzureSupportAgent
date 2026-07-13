---
layout: default
title: Mission Control
parent: User guide
nav_order: 3
description: Run coordinated multi-system workload sweeps and interpret the go, warning, or no-go posture board.
permalink: /user-guide/mission-control/
---

# Mission Control

**Routes:** `/mission-control` and mission detail/history within that view

## Purpose

Mission Control runs a coordinated set of workload analyses and presents their latest states on one posture board. Use it for release readiness, operational reviews, incident handoff, or a repeatable check across several systems without opening every feature manually.
![Mission Control readiness board with workload rings and system status]({{ site.baseurl }}/assets/mission-control.png)

### When to use it

- Before a production release or change window.
- During a service review when coverage, architecture, risk, ownership, and performance must be considered together.
- To refresh stale analyses in a controlled sweep.
- To compare current readiness with prior mission history.
- For fleet operations when several workloads need the same review.

Mission Control orchestrates existing product systems; it does not replace their detailed reports.

## Prerequisites and data sources

### Prerequisites and permissions

- `missions.read` to view the board and mission history.
- `missions.run` to launch, cancel, rerun, or delete missions.
- `workloads.read` and at least one defined workload.
- A valid Azure connection for systems that perform live scans.
- Feature and data access required by each selected system.
- An AI provider for systems that use model reasoning.

A user may be allowed to run missions while an individual system still lacks Azure, Graph, model, or application access. Such failures remain visible per system.

## Tabs and actions



## Freshness and scope behavior

### Queue and execution behavior

Mission Control uses a central FIFO admission queue with per-tenant/connection lanes. By default, only one system runs at a time inside a mission, global mission concurrency is limited, and AI-heavy operations use an additional throttle. This prevents one fleet sweep from overwhelming Azure or the model provider.

If a mission remains queued, another mission for the same connection may be active. Check queue position rather than repeatedly submitting duplicates.

## Workflow overview

### Run a mission

1. Open `/mission-control`.
2. Select the target workload and verify its Azure connection.
3. Review the cached board and freshness labels before choosing what to rerun.
4. Select **Run mission**.
5. Choose all systems or a focused subset. Available systems can include Architecture, Assessment, Monitoring Coverage, Telemetry Coverage, Backup/DR, Retirement Radar, Performance, Identity, Estate Graph, Ownership, Tag Intelligence, Change Explorer, and Mission Memory.
6. Leave force refresh off when recent cached results are acceptable. Enable it only when current evidence justifies the extra Azure/model work.
7. Start the mission. The central queue may hold it until capacity is available.
8. Watch streamed progress, per-system status, and logs. Systems execute serially within a mission to reduce Azure throttling.
9. Open any attention tile to inspect the owning feature's findings and evidence.
10. Re-run an individual system after addressing a transient error or stale result.

Fleet missions use the same principles but can queue multiple workloads. Keep the fleet small enough to remain operationally useful and provider-budget aware.

## Interpretation of results

### Interpret the board

### Mission readiness

| State | Meaning |
| --- | --- |
| Go | Completed contributing systems did not report attention conditions |
| Warning | The sweep completed with findings or partial concerns that require review |
| No-go | One or more hard failures or high-impact conditions prevent a clean readiness verdict |
| Unknown / not assessed | Insufficient systems have produced a usable state |
| Cancelled | Execution was stopped before a complete verdict |

### System status

- **Queued/running**: execution has not produced a final state.
- **Done**: the system completed; inspect its headline and attention flag.
- **Skipped**: a sufficiently fresh result was reused, or a dependency/condition prevented execution. Read the detail.
- **Fail/error**: the system could not produce a normal result or found a hard failure. Open the system and logs.
- **Freshness age**: how old the underlying result is. A completed mission can still include reused data unless forced.
- **Informational systems**: displayed on the board but do not contribute to the needs-attention count.

A readiness ring measures completed systems, not an Azure SLA or probability of success. A green board is bounded by the selected systems, data access, scope, and collection time.

## Exports, history, scheduling, and integrations

No dedicated export, history, scheduling, or integration controls are documented for this feature page.

## Safety and limitations

### Safety

- The mission is primarily an orchestrated analysis, but selected systems may expose downstream remediation workflows. Mission Control does not authorize bypassing their approvals.
- Confirm workload membership before launch; every system inherits that boundary.
- Force refresh can increase Azure requests, model usage, runtime, and rate-limit exposure.
- Cancel stops remaining orchestration but cannot undo completed external reads or generated artifacts.
- Deleting mission history is permanent and does not delete source feature records.
- Keep mission evidence and logs free of secrets when sharing or exporting them.

## Troubleshooting


| Symptom | Resolution |
| --- | --- |
| Mission stays queued | Check queue depth/position and active missions for the same connection; do not submit duplicates |
| One system fails while others succeed | Open that system's detail and verify its feature permission, Azure/Graph access, provider, and dependency state |
| Board shows old data | Review age labels and run a focused or forced refresh when justified |
| Readiness remains unknown | Select contributing systems and resolve skipped/error states that left no usable result |
| Streaming disconnects | Reopen the mission; durable mission state persists even when the browser stream drops |
| Azure returns 429 | Let the queue/backoff complete, reduce fleet size, and avoid forced reruns |
| Mission Memory is skipped | Ensure Architecture completed and a linked architecture exists |
| Cancel appears slow | The current system must reach a cancellation point; already completed work remains recorded |

## Related pages

- [Workload fleet]({{ site.baseurl }}/user-guide/workloads/fleet/)
- [Workload detail]({{ site.baseurl }}/user-guide/workloads/workload-detail/)
- [Dashboard]({{ site.baseurl }}/user-guide/core/dashboard/)
- [Chat and Deep Investigation]({{ site.baseurl }}/user-guide/core/chat-deep-investigation/)
