---
layout: default
title: Design and assessment operations
parent: How-to guides
nav_order: 5
description: Task recipes for insight packs, architecture knowledge, ownership, graph analysis, assessments, performance, and FMEA.
permalink: /how-to/design-assessment/
has_children: true
---

# Design and assessment operations

Use these recipes to turn workload inventory into reviewable design, operational knowledge, accountability, posture evidence, performance findings, and failure-mode analysis.

## Prerequisites

- Confirm the workload and Azure connection before generation or analysis.
- Product permissions vary: `insights.*`, `architectures.*`, `ownership.*`, `graph.read`, `assessments.*`, and `perfprofile.read`.
- AI-assisted tasks require an active provider; source collection requires the corresponding Azure/Graph access.

## Route

Open the route listed by the selected recipe.

## How to choose a workflow

1. Use [AI Insight Packs]({{ site.baseurl }}/how-to/design-assessment/insight-packs/) for repeatable on-demand or scheduled evidence digests.
2. Use [Architectures and Know-Me]({{ site.baseurl }}/how-to/design-assessment/architectures-know-me/) for diagrams, drift, memory, runbooks, revisions, and exports.
3. Use [Ownership]({{ site.baseurl }}/how-to/design-assessment/ownership/) for directory records, assignments, suggestions, attestations, and previewed tag application.
4. Use [Estate Graph]({{ site.baseurl }}/how-to/design-assessment/estate-graph/) for search, lenses, overlays, paths, blast radius, and visual export.
5. Use [Assessments]({{ site.baseurl }}/how-to/design-assessment/assessments/) for control evaluation, lifecycle, findings, waivers, tickets, reports, and fleet/scheduled runs.
6. Use [Performance Profiler]({{ site.baseurl }}/how-to/design-assessment/performance-profiler/) for windowed metrics, bottleneck heatmaps, findings, evidence, history, and cleanup.
7. Use [FMEA]({{ site.baseurl }}/how-to/design-assessment/fmea/) for architecture-grounded failure modes, RPN, action ownership, revisions, and worksheet exports.

**Expected result:** The workflow starts from current scope/evidence and produces a reviewable record rather than an unverified AI conclusion.

**Verification:** Confirm timestamps, source completeness, human approval state, and exported artifact contents.

## Safety and rollback

Most collection and generation is read-only against Azure, but saved records, external tickets, ownership tags, waivers, lifecycle states, and purge actions are mutations. Use previews and revisions; restore soft-deleted records before purge; roll back Azure tag changes through the available tag revision/revert flow or reviewed organizational process.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Generation is unavailable | Check provider health, write permission, workload/architecture prerequisites, and source data. |
| Result is empty or stale | Refresh the owning source module and verify connection capability and scope. |
| Export omits edits | Wait for save/auto-save completion and export the persisted current record. |

## Related docs

- [Design & Ownership reference]({{ site.baseurl }}/user-guide/design-ownership/)
- [Assessment & Performance reference]({{ site.baseurl }}/user-guide/assessment-performance/)
