---
layout: default
title: Discovery and Autopilot
parent: Workloads
grand_parent: User guide
nav_order: 2
description: Survey Azure resources, shape discovery inputs, and review AI-proposed workloads before saving.
permalink: /user-guide/workloads/discovery-autopilot/
---

# Discovery and Autopilot

**Route:** `/workloads` → **Autopilot**

## Purpose

Autopilot discovers candidate application boundaries from an Azure estate. It separates inexpensive Resource Graph survey from AI grouping, allowing you to filter, estimate, and control the operation before model calls begin.

### When to use it

- First onboarding of a subscription or management group.
- After major estate reorganization.
- When fleet coverage shows many orphaned resources.
- To replace manual resource-by-resource selection with reviewable candidates.

Use manual workload creation when the intended boundary is already known and small.

## Prerequisites and data sources

### Prerequisites and permissions

- `workloads.read` to survey and run discovery.
- `workloads.write` to save candidates.
- A valid Azure connection with Reader over the selected scope.
- An AI provider for the AI grouping strategy. Deterministic strategies can reduce or avoid model calls.

## Tabs and actions



## Freshness and scope behavior



## Workflow overview

### Three-phase workflow

### 1. Survey

1. Select a connection and management-group or subscription scope.
2. Run **Survey**. It enumerates resources through Azure Resource Graph without calling the LLM.
3. Review counts and facets by type, resource group, region, subscription, environment, and tags.
4. Check the estimated model calls, time, and effective resource count.

The survey is cached for a limited period. Re-run it if controls report that a survey is needed.

### 2. Sculpt

1. Choose **Fast**, **Balanced**, or **Thorough** based on desired granularity and cost.
2. Apply hard filters only to resources that must be excluded from candidate workloads.
3. Use soft/noise filters for resources that may be reattached after grouping.
4. Select resource, resource-group, or sampled granularity.
5. Optionally seed grouping from a reliable tag key or detected naming pattern.
6. Set a confidence floor and maximum AI-call budget.
7. Review the updated estimate after every significant control change.

Hard-filtered resources are not reattached. Tag and naming conventions should be inspected for drift before they are used as authoritative grouping signals.

### 3. Group and review

1. Select AI, resource-group, subscription, or tag grouping.
2. Start discovery and follow streamed progress.
3. Review each candidate's name, type, environment, criticality, members, confidence, evidence, and reasoning.
4. Correct membership and classification where the UI permits; discard weak candidates.
5. Save only accepted candidates. Saving creates active workload records; discovery itself is non-destructive.

## Interpretation of results

### Interpret output

- **Confidence** measures the grouping strategy's certainty, not operational health.
- **Evidence** can include co-location, network, dependency, RBAC, tags, names, and provenance. Correlated evidence is stronger than one naming token.
- **Filtered** is the count excluded by sculpt controls.
- **Tag-seeded** groups are deterministic starting points and still require review.
- **Reattached** resources were initially treated as noise but found a plausible group.
- **Below floor** candidates were omitted because confidence did not meet the selected threshold.
- **Capped** means the AI-call budget was exhausted and fallback grouping handled remaining resources.

Cost and token values are estimates, not provider invoices or execution guarantees.

## Exports, history, scheduling, and integrations

No dedicated export, history, scheduling, or integration controls are documented for this feature page.

## Safety and limitations

### Safety

- Survey and discovery do not modify Azure resources.
- Saving modifies only the application's workload registry; candidates can later be edited or soft-deleted.
- Broad scopes can expose extensive resource metadata to the selected model during AI grouping. Use filtering and a provider approved for that data.
- Do not encode secrets or personal data in naming hints.
- Review shared services and overlaps after saving; an application boundary is not necessarily exclusive.

## Troubleshooting


| Symptom | Resolution |
| --- | --- |
| Survey returns zero resources | Check connection, selected scope, Reader assignment, and Resource Graph access |
| Estimate says a survey is needed | Survey cache expired or controls target another scope; run Survey again |
| Discovery is expensive | Use RG granularity, tag seeding, filters, a lower AI-call cap, or deterministic grouping |
| Candidates are too broad | Use resource granularity, stronger filters, or split the input scope |
| Candidates are too fragmented | Use RG/tag seeding, lower the confidence floor carefully, or merge manually after review |
| Valid shared resources are missing | Inspect hard filters; hard-filtered resources are not reattached |
| Stream fails midway | Check provider availability/rate limits; rerun the survey before retrying discovery |

## Related pages

- [Workload fleet]({{ site.baseurl }}/user-guide/workloads/fleet/)
- [Workload detail]({{ site.baseurl }}/user-guide/workloads/workload-detail/)
- [Groups and overlaps]({{ site.baseurl }}/user-guide/workloads/groups-overlaps/)
- [First-run setup]({{ site.baseurl }}/getting-started/first-run/)
