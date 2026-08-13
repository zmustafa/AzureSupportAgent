---
layout: default
title: Operate Backup Manager
parent: Coverage operations
grand_parent: How-to guides
nav_order: 5
description: Analyze a backup estate, sweep the whole fleet, triage failed jobs, close protection gaps, harden vaults, and reclaim stored analyses.
permalink: /how-to/coverage/backup-manager/
feature_ids: [PROACTIVE_NAV:backup-manager, ROUTE:backup-manager, BACKUP_MANAGER_NAV:overview, BACKUP_MANAGER_NAV:flow, BACKUP_MANAGER_NAV:inventory, BACKUP_MANAGER_NAV:jobs, BACKUP_MANAGER_NAV:policies, BACKUP_MANAGER_NAV:vaults, BACKUP_MANAGER_NAV:gaps, BACKUP_MANAGER_NAV:dr, BACKUP_MANAGER_NAV:cost, BACKUP_MANAGER_NAV:changes, BACKUP_MANAGER_NAV:fleet, BACKUP_MANAGER_NAV:cleanup]
---

# Operate Backup Manager

## Prerequisites

- Product permission `backup_manager.read`. Drafting a change additionally requires the permission for that action (`protect_write`, `policy_write`, `vault_write`, `ondemand`, `drill_write`, `reference_write`), and `backup_manager.approve` decides, applies, and permanently purges.
- An Azure connection that can read the Resource Graph `recoveryservicesresources` table across the selected scope, plus Reader on the vaults themselves.
- `Microsoft.CostManagement/query/action` on the in-scope subscriptions for invoiced spend. Without it the Cost tab still reports list-price estimates and states why actuals are missing.
- A service-principal or managed-identity connection for Backup Reports; a pasted token cannot query Log Analytics.

## Route

Open `/backup-manager`. The view strip at the top selects **Manager** (the selected scope), **Fleet** (every workload), or **Cleanup** (what the module is storing).

## How to analyze one scope

1. In **Manager**, choose the connection and the workload or subscription.
2. Click **Analyze backups**. Nothing is read from Azure until you do — not on page load, not on a tab switch, not when the scope changes.
3. Watch the phase log: resolving scope, reading Resource Graph, checking for orphans, reading vault configuration, analyzing, pricing, saving. Each Resource Graph source reports the row count it returned.
4. Navigate away if you want. The analysis runs on the server and the module reconnects to it when you return.
5. When it completes, every tab is served from that one analysis.

**Expected result:** One analysis populates Overview, Protection flow, Protection, Job inbox, Policies, Vaults, Gaps, DR & drills, Cost & waste.

**Verification:** The header states when the current analysis was taken. If every Resource Graph source failed, the run is reported as failed and the previous analysis is deliberately kept — refresh the credential and analyze again.

## How to sweep the whole fleet

1. Open **Fleet**. Every workload is listed with the headline of its last analysis, served from cache; drawing the grid reads nothing from Azure.
2. Sort worst-first (the default): never-analyzed workloads lead, then the most gaps, then the lowest protection percentage, then the most failing jobs.
3. Select the workloads to measure and choose **Analyze selected**.
4. Expect **two analyses at a time**. A backup analysis is nine Resource Graph queries per subscription plus per-vault reads, Cost Management and retail pricing calls; the client queues the rest and the server enforces the same cap independently.
5. Leave the tab if you want — each row shows its live phase, `queued`, or `failed` with the error on hover. **Retry failed** re-queues only the failures.
6. Click a workload name to open it in the Manager view with its connection already selected.

**Expected result:** Every selected workload ends with a fresh analysis and a fleet row; the header counts how many are analyzed and how many are outstanding.

**Verification:** Compare the **Last analysis** column against the change you are investigating. A fleet row is only as fresh as the analysis behind it.

Demo workloads are listed but cannot be launched — they are synthetic and composed on read.

## How to triage failing backups

1. Analyze the scope, then open **Job inbox**.
2. Work the failure clusters, not individual jobs: each carries the Azure error code, its cause, and the documented remediation.
3. Check **chronic failures** — an item with no recent recovery point is a protection outage even when today's job succeeded.
4. Draft an on-demand backup where a retry is appropriate. It enters the ledger as a change rather than running immediately.

**Expected result:** Failures are grouped by root cause with a documented remediation each.

**Verification:** Resource Graph job history covers a rolling window of about seven days. Absence of a job here is not proof it never ran; enable vault diagnostics for a longer horizon.

## How to close a protection gap

1. Open **Gaps**. Live detection and findings ingested from Backup & DR Coverage are listed separately.
2. Select the resources to protect, then choose a target vault and policy.
3. Preview. Blocked items state why — most often that the vault must be in the resource's own subscription.
4. Submit. Each ready item becomes a pending change; nothing has reached Azure yet.
5. Approve and apply from **Managed changes**.

**Expected result:** Protection is drafted as reviewable changes, never applied silently.

**Verification:** Backup writes are long-running. A row parks in `applying` with its operation URL until the poller drives it to applied or failed. Analyze again to prove Azure converged.

## How to reclaim storage

1. Open **Cleanup**.
2. In **Stored analyses**, select **orphaned** scopes — a deleted workload, a removed connection, or an older analysis shape — and purge them. This is the change that frees real space and stops useful scopes being evicted by the store's scope cap.
3. In the history list below, apply a retention preset: retain the last N per scope, older than 7 / 30 / 90 days, demo runs, or empty runs.
4. **Trash** first if you are unsure; trashed entries can be restored. **Purge** is permanent.

**Expected result:** The stored-analysis count and total size fall, and the scopes you actually use survive the cap.

**Verification:** A purged scope shows "No backup analysis yet" until it is analyzed again. Nothing in Azure changes and no backup data is touched.

## Safety and rollback

- Backup Manager never restores data and never performs destructive backup operations — deleting backup data, purging soft-deleted items, locking immutability, disabling soft delete, unregistering a container. `GET /api/backup-manager/refusals` lists them and the capability flags stay `false`. Do not plan a recovery procedure around this module.
- Only **stop protection with data retained** exists. Any other stop mode is refused by the apply path, not merely hidden.
- High-risk targets require two distinct approvers and cannot be approved in bulk.
- Purging a stored analysis or its history is irreversible, and requires `backup_manager.approve`. It deletes cached reports only — never Azure state, audit records, or Evidence Locker snapshots.
- A vault's storage redundancy cannot be changed once it holds a protected item; the action is withdrawn rather than offered and failed.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Every tab asks me to analyze | Intended. The module never reads Azure on its own. Click **Analyze backups**. |
| Fleet says a workload was never analyzed but I analyzed it | The analysis was recorded against a different connection than the workload is registered with, or its stored analysis was purged. Analyze it from the Fleet row. |
| A fleet sweep seems slow | Two analyses run at a time by design. The header shows how many are outstanding; each row shows its current phase. |
| A fleet row sat on "analyzing" then went idle | The job stopped reporting for longer than the grace window, usually a backend restart. Nothing was applied — analyze the row again. |
| A tab went back to "No backup analysis yet" | The stored analysis was evicted by the scope cap or purged. Analyze again, and purge orphaned scopes so the cap protects the ones you use. |
| Cost shows an estimate but no actuals | The connection lacks Cost Management query rights on the in-scope subscriptions, or the period has no billed backup usage. The reported reason states which. |
| Per-item cost looks approximate | It is apportioned. Azure bills backup to the vault, not the item; the rows reconcile to the vault total and each states its basis. |
| A change is stuck in **applying** | Backup writes are long-running; the poller has a 120-minute deadline, after which the row is marked timed out and must be re-drafted. |
| A change is marked **stale** | Azure changed after the request was drafted. Analyze again and create a new request rather than forcing the original payload. |
| Purge is disabled in Cleanup | Permanent deletion requires `backup_manager.approve`. Trash is available with read access and is restorable. |

## Related docs

- [Backup Manager feature reference]({{ site.baseurl }}/user-guide/coverage/backup-manager/)
- [Operate Backup and DR Coverage]({{ site.baseurl }}/how-to/coverage/backup-dr-coverage/)
- [Operate Alerts Manager]({{ site.baseurl }}/how-to/coverage/alerts-manager/)
- [Evidence Locker]({{ site.baseurl }}/user-guide/lifecycle-investigation/evidence-locker/)
