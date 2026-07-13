---
layout: default
title: Build architectures, Memory, and Know-Me
parent: Design and assessment operations
grand_parent: How-to guides
nav_order: 2
description: Draw or reverse-engineer diagrams, check drift, manage revisions, generate Memory and Know-Me, and publish reviewed runbooks.
permalink: /how-to/design-assessment/architectures-know-me/
---

# Build architectures, Memory, and Know-Me

![Architecture designer]({{ site.baseurl }}/assets/architecture-designer.png)

## Prerequisites

- `architectures.read`; `architectures.write` for create, edit, generate, lifecycle, restore, and delete.
- A workload and readable inventory for reverse-engineering/drift; an active AI provider for generated diagram context, Memory, and Know-Me.

## Route

Architectures: `/architectures`, `/architectures/{id}`, `/architectures/{id}/memory`, `/architectures/memory`. Know-Me: `/knowme`, `/knowme/{id}`.

## How to create and maintain a manual architecture

1. Open `/architectures` and create a blank architecture.
2. Name it, link the workload, and optionally place it in a collection.
3. Drag components from the catalog, set meaningful metadata, and connect relationships with correct direction/type.
4. Group and arrange nodes by tier, region, environment, subscription, resource group, vNet, or trust boundary.
5. Run lint/tidy tools, add dependencies Azure cannot discover, and save a deliberate milestone.
6. Use **⬆ Import** only for a supported Mermaid flowchart and review appended nodes/edges.

**Expected result:** A saved, workload-linked diagram reflects reviewed intent rather than only deployable resources.

**Verification:** Compare resource nodes and edges with inventory, deployment definitions, network design, and service-owner knowledge.

## How to reverse-engineer or rebuild with AI

1. Ensure workload membership and connection are current.
2. From `/architectures`, select **✨ Reverse-engineer architecture**, choose workloads, and start jobs.
3. Monitor phases; cancel only when the run should stop and dismiss completed job records when no longer useful.
4. Open each generated diagram and correct nodes, edges, labels, layout, and external dependencies.
5. For an existing linked diagram, use the rebuild/re-reverse-engineer control only after preserving important manual context through save/revision/export.

**Expected result:** A generated draft is saved for review; it is not automatically authoritative.

**Verification:** Account for all intended workload resources and explicitly document undiscoverable SaaS, on-premises, traffic, and business dependencies.

## How to check drift

1. Open `/architectures/{id}` and select **⟳ Drift**.
2. Wait for comparison with live Azure Resource Graph.
3. Review matched diagram resources, **Gone from Azure**, and **New in Azure**.
4. Decide whether each difference is intended, inventory delay, or diagram debt.
5. Update the diagram manually or rebuild after preserving reviewed annotations.
6. Run drift again.

**Expected result:** The report shows in-sync status or explicit added/removed differences.

**Verification:** Independently confirm changed resources and rerun after save; drift does not prove runtime connectivity.

## How to manage architecture revisions and exports

1. Open History and preview a prior version read-only.
2. Compare it with current content before choosing **Restore this version**.
3. Confirm restore; current content is saved to history first.
4. From **⬇ Export**, choose PNG, SVG, Mermaid, JSON, or the offered Bicep/Terraform skeleton.
5. Treat IaC skeletons as starting artifacts, not deployable representations.

**Expected result:** Restores are non-destructive and exports represent the current saved diagram.

**Verification:** Reopen current/history after restore and inspect the downloaded file for scope and sensitive topology.

## How to generate and maintain architecture Memory

1. Open **🧠 Memory** from the reviewed diagram.
2. Select sections/templates and add approved grounding notes.
3. Generate with AI or author manually; review live preview and evidence.
4. Correct commands, thresholds, dependencies, known issues, ownership, and qualifiers.
5. Regenerate only weak sections when possible.
6. Enable Memory for investigations only after approval.
7. Use history to preview/restore and Markdown/PDF controls offered by the page.

**Expected result:** Architecture Memory contains reviewed operational context suitable for investigations and downstream documents.

**Verification:** Compare generated time with architecture update time and test the most important runbook steps safely.

## How to generate, guided-fill, edit, and publish Know-Me

1. Open `/knowme` and choose **✨ Build from workload** for the workload→architecture→Memory→Know-Me pipeline, or create **+ New** from a buildable architecture.
2. Open the document and review source, completion, evidence (**How built**), and stale-Memory warning.
3. Click an open or filled field chip to pick a suggested value or type an approved value; use **Save**, **Clear**, or **Cancel**.
4. Use **✍️ Fill** for the whole document or one section; add assignee/note where useful.
5. Use section **✏️** editing for visual/Markdown content and Preview for tables, Mermaid, and images.
6. Use the per-section **✨** control to regenerate only weak content; cancel if grounding is wrong.
7. Move through Draft → In review → Published/Archived as shown. Set the exclusive Reference document for the workload when appropriate.
8. Export Markdown or PDF only after auto-save completes.

**Expected result:** A human-completed, architecture-grounded support reference is published without replacing reviewed sections unnecessarily.

**Verification:** Required completion passes, field values remain editable, stale-source warning is resolved, and exported content matches the current revision.

## Safety and rollback

- Generated diagrams, Memory, and Know-Me are drafts; never invent people, IDs, SLAs, RTOs, or RPOs.
- Save milestones before rebuild/regeneration. Revision restore creates a new current state.
- Diagram/Memory/Know-Me exports can reveal topology and operational procedures.
- Soft-deleted diagrams/Know-Me can be restored; purge/empty Trash are permanent. Architecture dependencies should be reviewed before purge.
- Memory deletion may be immediate; export/revision safeguards apply only where the UI provides them.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Reverse-engineering finds no resources | Refresh workload inventory and verify connection/scope. |
| Drift is unexpected | Check live inventory timing, workload link, and manually modeled components. |
| Memory/Know-Me is generic | Improve diagram and grounding notes; regenerate only affected sections. |
| Know-Me build is unavailable | Ensure an active workload, linked architecture, Memory prerequisite, provider, and write permission. |
| Export misses typing | Wait for save/auto-save, then export persisted current content. |
| Concurrent edits conflict | Reload latest and reapply changes; avoid editing the same document in multiple tabs. |

## Related docs

- [Architectures reference]({{ site.baseurl }}/user-guide/design-ownership/architectures/)
- [Know-Me reference]({{ site.baseurl }}/user-guide/design-ownership/know-me/)
- [FMEA recipes]({{ site.baseurl }}/how-to/design-assessment/fmea/)
