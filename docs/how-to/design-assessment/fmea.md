---
layout: default
title: Build and maintain FMEA
parent: Design and assessment operations
grand_parent: How-to guides
nav_order: 7
description: Generate and edit FMEA tables, calculate RPN, assign actions, manage lifecycle and revisions, export worksheets, and handle deleted workloads.
permalink: /how-to/design-assessment/fmea/
feature_ids: [PROACTIVE_NAV:fmea, ROUTE:fmea]
---

# Build and maintain FMEA

{: .note }
**Screenshot note:** These native views contain synthetic support context and FMEA results. Saves, ownership entries, and post-action scores update browser-only examples; no AI generation, DNS/SSH probe, or real mitigation occurred. The source-document menu shown below belongs to Know-Me, not the FMEA editor.

## Prerequisites

- `architectures.read`; `architectures.write` for create, generate, edit, lifecycle, restore, and delete.
- A reviewed architecture and current Memory for grounded AI generation; an active provider for generation.

## Route

Open `/fmea` or `/fmea/{fmeaId}`.

If a review starts from Know-Me, use its **Source** menu to open the linked Architecture Memory and diagram before drafting the FMEA. The support reference and source Memory are separate documents; a completed Know-Me field does not verify Memory or a risk control.

{% include screenshot.html file="fdesign-knowme-source-links.png" title="Trace support context to the source Memory and diagram" caption="The native Know-Me Source menu distinguishes Architecture Memory, the architecture diagram, and related workload evidence. This synthetic navigation example supports the FMEA grounding check; it is neither an FMEA menu nor proof that AI generation or a diagnostic ran." %}

## How to create or generate an FMEA

1. Review the workload architecture and refresh stale Memory.
2. Open `/fmea` and choose a buildable architecture/workload suggestion, create a blank document, or select **Generate from Memory**.
3. Monitor draft, verify/refine, and save phases; a parse failure in the refinement pass can fall back to the first draft.
4. Open the document and validate every table, scope reference, failure mode, effect, cause, current control, and recommendation.
5. Add/remove tables or rows as needed; use per-table regeneration only when that table can be safely replaced.

**Expected result:** A saved draft contains one or more reviewable failure-mode tables grounded in architecture Memory.

**Verification:** Trace each modeled component/failure to the architecture, Memory, evidence, or explicit workshop input.

## How to edit factors and interpret RPN

1. In a cross-functional workshop, define consistent 1–10 criteria for Severity, Occurrence, and Detection.
2. Edit factor cells; the UI updates RPN immediately and the server recomputes authoritatively on save.
3. Use $\mathrm{RPN}=S\times O\times D$; a zero/blank factor leaves RPN blank.
4. Interpret bands: Critical $\ge 200$, High $120$–$199$, Medium $40$–$119$, Low $<40$.
5. Sort/review high RPN, but separately review catastrophic low-occurrence and common-cause failures.
6. Wait for the short auto-save before navigating or exporting.

**Expected result:** Every complete row has normalized factors, derived RPN, and risk band.

**Verification:** Reopen the document and spot-check multiplication and band; out-of-range values are normalized server-side.

{% include screenshot.html file="fdesign-fmea-factors-controls.png" title="Edit factors and retain the control rationale" caption="Native factor and Controls cells show synthetic scores of 9, 3, and 5 with RPN 135 after browser-local autosave. No separate factor dialog is implied, and the revised score is not evidence that a real control changed." %}

## How to assign ownership, actions, status, and lifecycle

{: .note }
**Current worksheet limits:** Use **Action results**, **Date completed**, and post-action factors to record follow-up. There is no separate per-row status selector or open/in-progress/completed summary; the status steps below are review guidance, not additional UI controls. Document lifecycle remains separate from row completion.

1. Replace blank/generated owner and due-date placeholders with human-approved values.
2. Enter recommended action and row status; record post-mitigation factors only after controls are implemented and verified.
3. Review open/in-progress/completed summaries.
4. Move the document from Draft to In review and Published only after approval; use Archived for retained inactive records.
5. Do not treat lifecycle status as validation by itself.

**Expected result:** Risks have accountable action, due date/status, and reviewed document lifecycle.

**Verification:** Confirm owners accepted responsibility and completed controls have evidence before lowering post-mitigation scores.

{% include screenshot.html file="fdesign-fmea-saved-actions.png" title="Inspect saved ownership and modeled post-action scores" caption="Scroll right in the native worksheet to see owner, dates, Action results, and synthetic post-action RPN 36. Saved refers only to this browser example, not verified mitigation. Export controls are visible but no export or revision action was performed." %}

## How to use revisions and exports

{: .important }
**Current FMEA history limit:** The editor has no History button, revision drawer, or revision-preview control. Revision listing and restore are API operations with no dedicated preview endpoint; the history steps below are not a clickable worksheet workflow. Architecture Memory has its own native preview/diff, but that history does not restore an FMEA. Wait for **Saved** before using the worksheet's CSV or Excel controls.

1. Open revision history and preview the desired snapshot.
2. Restore only after comparing it with current content; restore creates a new current revision.
3. Select **⬇ CSV** for flat tabular data or **⬇ Excel** for Summary plus per-table sheets, conditional formatting, dates, and live RPN formulas.
4. Open the workbook and recalculate formulas in the target spreadsheet application.

**Expected result:** History remains intact and the export reflects current saved tables without unresolved token syntax.

**Verification:** Compare table/row counts, factors, RPN, status, owner, and dates between UI and export.

## How to handle a deleted workload

1. On `/fmea`, identify the amber **workload deleted** badge.
2. Open the retained FMEA and decide whether it must be exported, archived, relinked through a supported workflow, or deleted.
3. Do not expect new buildable suggestions for deleted workloads.
4. Move obsolete documents to Trash; restore if needed, or purge after retention review.

**Expected result:** Orphaned risk records remain visible for deliberate disposition rather than silent loss.

**Verification:** The deleted workload does not reappear as buildable and the FMEA's chosen lifecycle/export/delete outcome is recorded.

## Safety and rollback

- AI can omit modes, invent controls, or score inconsistently; human facilitation is mandatory.
- Regeneration can replace manual content. Preserve a revision/export first and prefer per-table regeneration.
- RPN can under-rank catastrophic or systemic failures.
- Owner/due date must never be inferred as fact.
- Soft deletion is recoverable; purge/empty Trash is permanent. Deleted workloads do not cascade-delete FMEA.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| No buildable suggestion | Confirm active workload, linked architecture, Memory, and permission. |
| Generation is partial | Check provider and Memory; review the first-pass fallback rather than assuming completeness. |
| RPN is blank | Enter non-zero S, O, and D values from 1 through 10. |
| Score changes after save | Inspect out-of-range/non-numeric values; server normalization is authoritative. |
| Edits are missing | Wait for auto-save, avoid concurrent tabs, and inspect revisions. |
| Excel differs | Recalculate formulas and verify spreadsheet support for formatting/formulas. |

## Related docs

- [FMEA reference]({{ site.baseurl }}/user-guide/assessment-performance/fmea/)
- [Architectures and Know-Me recipes]({{ site.baseurl }}/how-to/design-assessment/architectures-know-me/)
- [Mission Control recipes]({{ site.baseurl }}/how-to/core-workloads/mission-control/)
