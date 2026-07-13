---
layout: default
title: Run Mission Control
parent: Core and workload operations
grand_parent: How-to guides
nav_order: 5
description: Launch focused or fleet workload missions, interpret readiness, and investigate per-system results.
permalink: /how-to/core-workloads/mission-control/
---

# Run Mission Control

![Mission Control readiness board]({{ site.baseurl }}/assets/mission-control.png)

## Prerequisites

- `missions.read` to view and `missions.run` to launch, cancel, rerun, or delete mission history.
- `workloads.read`, a defined workload, and system-specific application/Azure/Graph/AI access.

## Route

Landing: `/mission-control`. Workload board: `/mission-control/{workloadId}`.

## How to run a workload mission

1. Open `/mission-control` and select the workload.
2. Confirm its connection and inspect cached tile freshness.
3. Select **Run mission** and choose all systems or a focused subset.
4. Leave force refresh off when current cached evidence is acceptable; enable it only when freshness justifies additional Azure/model work.
5. Start and monitor queue position, streamed log, system tiles, and readiness ring.
6. Allow dependencies to run; architecture/memory-dependent systems may skip when prerequisites are absent.
7. Open every attention, skipped, or failed tile in its source feature.
8. Rerun only the affected system after correcting transient access/provider issues.

**Expected result:** A durable mission records terminal per-system states and a Go, Warning, No-go, Unknown, or Cancelled rollup.

**Verification:** Open source records and confirm scope/timestamp; the readiness ring measures completed selected systems, not release probability.

## How to run fleet missions

1. From the workload fleet, select the target workloads and choose **Launch missions**, or use available fleet controls in Mission Control.
2. Select a consistent system subset and force policy.
3. Launch the batch and let the central queue control concurrency.
4. Review each workload independently; do not infer fleet success from one completed mission.
5. Retry failed workloads only after checking their connection and system log.

**Expected result:** One durable mission is queued/run per selected workload without overwhelming the connection or provider.

**Verification:** Every selected workload reaches a terminal state and links to its own system evidence.

## How to interpret and revisit a mission

1. Read tile status: queued/running, done, reused/skipped, failed/error, or not run.
2. Check age badges for reused results and whether informational systems contribute to attention.
3. Treat **Warning** as actionable findings/partial concerns and **No-go** as hard/high-impact conditions requiring source review.
4. Use mission history to reopen prior runs and compare selected systems, force state, duration, and result.
5. Delete per-workload mission history only when durable mission records are no longer required; source-feature records are separate.

**Expected result:** The rollup is interpreted in the context of selected systems, freshness, access, and source details.

**Verification:** Confirm attention counts against tiles and follow **Open** links to the actual report or generated artifact.

## How to cancel or recover a disconnected run

1. Select **Cancel** for a running mission when remaining work should stop.
2. Wait for the current system to reach a cancellation point.
3. If the browser disconnects, reopen `/mission-control/{workloadId}`; durable state and live reconnect recover progress.
4. After a server restart, interrupted runs appear failed rather than remaining permanently running; start a new mission when appropriate.

**Expected result:** Remaining orchestration stops or reconnects without losing persisted terminal work.

**Verification:** Mission status becomes cancelled/failed/succeeded/partial and no tile remains falsely running.

## Safety and rollback

- Confirm workload membership before launch; every system inherits it.
- Force refresh increases requests, model usage, runtime, and throttling exposure.
- Cancel does not undo completed reads or generated architecture, memory, assessment, or FMEA records.
- Mission Control does not bypass downstream remediation approvals.
- Mission-history deletion is permanent and does not delete source records.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Mission stays queued | Check queue position and active work for the same connection; do not submit duplicates. |
| One tile fails | Open its source feature and verify permission, Azure/Graph capability, provider, and dependency. |
| Board is old | Review ages, then run a focused or forced refresh. |
| Readiness is unknown | Run contributing systems and resolve skips/errors that left no usable state. |
| Azure returns 429 | Let queue/backoff complete, reduce fleet size, and avoid force. |
| Memory or FMEA skips | Ensure a linked architecture and generated Memory exist. |

## Related docs

- [Mission Control reference]({{ site.baseurl }}/user-guide/mission-control/)
- [Workload detail recipes]({{ site.baseurl }}/how-to/core-workloads/workload-detail-groups/)
- [Architectures and Know-Me recipes]({{ site.baseurl }}/how-to/design-assessment/architectures-know-me/)
