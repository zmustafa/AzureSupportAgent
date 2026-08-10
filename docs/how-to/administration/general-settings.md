---
layout: default
title: Change General settings safely
parent: Administration tasks
grand_parent: How-to guides
nav_order: 55
description: Adjust runtime, safety, command, Azure pacing, and Performance Profiler capacity settings with bounded verification.
permalink: /how-to/administration/general-settings/
---

# Change General settings safely

## Prerequisites

- Product permission `settings.write`.
- An approved reason, current value, expected value, and representative verification workflow.
- A current backup for broad or high-impact changes.

## Route

- Open `/admin/audit`.
- Open `/admin/settings`.

## How to change an application setting

1. Find the setting by its visible label. Configure only controls present in the current build.
2. Record the current value and the affected workflows.
3. Change the smallest related set. High-impact controls on this page include MCP read-only mode, automatic writes, command execution, deep parallelism, Resource Graph pacing, profiler concurrency, retries, and timeouts.
4. Save and review the values returned by the page; numeric bounds, lists, schedules, time zones, durations, thresholds, and colors are normalized or validated by the backend.
5. Reopen `/admin/settings` and confirm the effective value.
6. Run one bounded workflow affected by the change.
7. Review `/admin/audit` for `settings.update`.

**Expected result:** The validated value persists and the representative workflow changes only as intended.

**Verification:** Confirm the saved value after reload, inspect the workflow result for truncation/timeouts or newly exposed actions, and check the audit timestamp and actor.

## How to tune Performance Profiler capacity

1. In `/admin/settings`, record the five values under **Performance Profiler capacity**.
2. Leave **Fleet workloads in parallel** at its default `1` unless a bounded measurement supports `2` or `3`.
3. Use **Delay between Fleet starts** to spread authentication and discovery; the default is 1,000 ms.
4. Keep **Azure Monitor calls in parallel** at the process-wide default `2` until throttle and host-load evidence supports a change.
5. Set **Metric request attempts** as total attempts including the first. The default is `3`; the accepted range is 1–6.
6. Set **Workload timeout** as the collection ceiling. The default is 1,200 seconds; the accepted range is 60–7,200.
7. Save and reload the page. Start delay, metric concurrency, attempts, and timeout apply to subsequent work.
8. If Fleet workload concurrency changed, restart the application because worker count is created at startup. The SQL-backed Fleet batch resumes after restart.
9. Run the same small Fleet selection and time range used for the baseline.
10. Compare duration, succeeded/partial/failed counts, throttle/retry/timeout counters, Azure CLI process count, and host capacity. Review `/admin/audit` for `settings.update`.

**Expected result:** The bounded batch uses the intended limits without an unacceptable increase in throttling, partial attempts, Azure CLI processes, or host load.

**Verification:** Confirm effective values after reload, restart when worker width changed, and verify that the durable batch retains its ID and reaches a terminal state.

## Safety and rollback

Validate changes in a non-production scope first, and preserve a known-good configuration for rollback. Restore profiler capacity one value at a time and restart when Fleet worker width changed.

Keep MCP read-only and automatic-write protections aligned with organizational approvals. Command and Sandbox toggles expose execution surfaces; network allow/deny lists affect egress. To roll back, restore the recorded value, save, rerun the same verification, or restore the prior configuration backup when many settings changed.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Value changes after save | Use the returned value; backend validation may clamp or normalize the input. |
| A tool disappears | Check MCP read-only, Entra enablement, built-in disabled tools, and egress controls. |
| Fleet still uses old workload concurrency | Restart the application; Fleet worker count is created at startup and durable SQL work resumes afterward. |
| Profiler partial/failed count rises after tuning | Lower metric concurrency, increase start delay, and inspect throttled, retried, timed-out, and failed-check counters. |
| Profiler failures take much longer | Restore the previous metric-attempt count or timeout; both extend the worst-case failure window. |
| An existing chat ignores a behavior change | Start a new operation because in-flight AI context is not rebuilt. |

## Related docs

- [How-to guides]({{ site.baseurl }}/how-to/)
- [General settings reference]({{ site.baseurl }}/admin/general-settings/)
- [Run Performance Profiler]({{ site.baseurl }}/how-to/design-assessment/performance-profiler/)
- [Azure and Entra MCP tools]({{ site.baseurl }}/how-to/administration/mcp-tools/)
- [Backup and Restore]({{ site.baseurl }}/how-to/administration/backup-demo/)
