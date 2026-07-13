---
layout: default
title: Know-Me
parent: Design & Ownership
grand_parent: User guide
nav_order: 3
description: Maintain architecture-grounded support knowledge, runbooks, and revision history.
permalink: /user-guide/design-ownership/know-me/
---

# Know-Me

## Purpose

Know-Me turns architecture memory into a support-facing workload reference. It captures operational context that a diagram alone cannot express: triage steps, dependencies, known issues, thresholds, escalation guidance, and human completion items.

**Application routes:** `/knowme`, `/knowme/:id`; architecture memory is also available at `/architectures/:id/memory` and `/architectures/memory`.

## Common use cases

- Give on-call engineers a workload-specific starting point.
- Record known failure symptoms, safe checks, and escalation contacts.
- Ground deep investigations in reviewed architecture context.
- Preserve operational knowledge across team changes.
- Supply reviewed context for FMEA and other AI-assisted workflows.

## Prerequisites, permissions, and data

- `architectures.read` allows viewing Know-Me and architecture memory.
- `architectures.write` allows creation, edits, AI generation, revision restore, deletion, and lifecycle changes.
- A linked architecture and workload provide the best grounding.
- AI generation requires a configured provider and uses the diagram, accessible live resource context, known weaknesses, and optional imported grounding notes.
- Human-authored notes are treated as operational context; do not paste secrets, tokens, customer data, or unapproved personal information.

## Index and editor

The index shows existing documents, buildable workload/architecture suggestions, source and status badges, last update, and Trash. Open a document to edit it, or create one from a buildable suggestion.

Architecture Memory uses a two-pane editor:

- **Section editor:** structured cards for operational topics, with per-section regeneration.
- **Live preview:** the combined Markdown document as support users will read it.
- **Templates:** quickly select a relevant set of memory sections.
- **Import grounding notes:** add authoritative context before generation.
- **Generate with AI:** draft all selected sections from available evidence.
- **Investigate:** hand the linked workload and memory to a deep investigation.
- **Enabled for investigations:** controls whether this memory is injected into linked investigations.
- **History:** preview a saved revision, compare it with current content, and restore it non-destructively.

## Recommended content

A useful Know-Me document should state:

1. workload purpose, critical user journeys, and service boundaries;
2. primary dependencies and ownership/escalation paths;
3. health signals, expected ranges, and where to query them;
4. known issues and distinguishing symptoms;
5. safe first-response checks and explicit stop conditions;
6. recovery prerequisites, validation steps, and rollback considerations;
7. unresolved questions and dates for review.

Avoid generic advice. A short, verified instruction is safer than a long speculative runbook.

## Workflow

1. Open the reviewed [architecture]({{ site.baseurl }}/user-guide/design-ownership/architectures/) and select **Memory**.
2. Choose a template or add the required sections.
3. Add approved grounding notes and generate a draft, or author manually.
4. Verify commands, links, thresholds, dependencies, and contacts with the owning team.
5. Regenerate only weak sections so reviewed material is not replaced unnecessarily.
6. Enable the memory for investigations after approval.
7. Export or print a reviewed copy when needed, and revisit it after architecture changes.

## Interpret status and freshness

Source badges distinguish generated, edited, and hybrid material. A generated-at timestamp describes when AI last drafted content, not when every source was observed. The editor warns when architecture changes are newer than generated memory. Treat that warning as a review requirement.

If lifecycle states such as **Draft**, **In review**, or **Published** are shown, use them as governance signals. Published content should be changed through a new reviewed revision, not silently assumed current forever.

## Exports, history, and integrations

- Download the current combined memory as Markdown.
- Use print/save-as-PDF for a portable rendered copy.
- Revision history stores snapshots; restoring an older revision first preserves the current version, making restore non-destructive.
- **Investigate** creates a deep-investigation handoff grounded in the linked workload and memory.
- Know-Me supports [FMEA]({{ site.baseurl }}/user-guide/assessment-performance/fmea/), architecture workflows, and operational handoffs.

## Safety and limitations

- AI-generated runbook steps can be unsafe, obsolete, or environment-specific. Test and approve them before use.
- Never include credentials or secret values. Link to an approved secret-management process instead.
- Memory can become stale after topology, deployment, ownership, or operating-model changes.
- Deleting architecture memory is immediate and cannot be undone; revision restore applies only while history exists.
- Enabling memory for investigations increases its influence on AI responses but does not make it authoritative.

## Troubleshooting

| Symptom | Checks |
|---|---|
| Generate is unavailable | Save the memory/architecture first and verify write permission and AI provider health. |
| A section is generic | Add precise grounding notes, verify architecture detail, and regenerate only that section. |
| Stale warning appears | Review recent architecture changes, update content, and regenerate where appropriate. |
| Investigate is disabled | Link the architecture to a workload and ensure the memory is enabled for investigations. |
| History is empty | Save meaningful edits first; revisions are created from persisted changes. |
| Export omits recent typing | Wait for save completion or save explicitly before producing the export. |

## Related docs

- [Architectures]({{ site.baseurl }}/user-guide/design-ownership/architectures/)
- [FMEA]({{ site.baseurl }}/user-guide/assessment-performance/fmea/)
- [Ownership]({{ site.baseurl }}/user-guide/design-ownership/ownership/)
- [Estate Graph]({{ site.baseurl }}/user-guide/design-ownership/estate-graph/)
