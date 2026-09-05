---
layout: default
title: Discover workloads with Autopilot
parent: Core and workload operations
grand_parent: How-to guides
nav_order: 3
description: Survey an Azure scope, sculpt discovery inputs, review candidates, and save approved workloads.
permalink: /how-to/core-workloads/autopilot/
---

# Discover workloads with Autopilot

{: .note }
**Screenshot note:** The native Autopilot screens use synthetic browser-only survey and candidate results. No Azure Resource Graph enumeration, AI inference, or probe occurred. Save was deliberately not clicked; no workload records or follow-on jobs were created, and the displayed estimates are not measured execution costs.

## Prerequisites

- `workloads.read` to survey/discover and `workloads.write` to save candidates.
- Reader and Resource Graph access on the chosen connection and scope.
- An active AI provider only for the AI grouping strategy.

## Route

Open `/workloads` and select **✨ Autopilot**.

## How to survey the estate without model calls

1. Select the connection and a management-group or subscription scope.
2. Select full or delta discovery as offered; delta is appropriate when existing workloads should be considered.
3. Run **Survey**. This Resource Graph phase does not call the LLM.
4. Review effective resource count and facets for subscriptions, resource groups, types, regions, environments, and tags.
5. Review estimated model calls and duration; treat them as planning estimates, not invoices.

**Expected result:** A cached, filterable survey describes the candidate resource population before AI grouping.

**Verification:** Compare survey scope and counts with Resource Graph/inventory and confirm no unintended subscription is included.

{% include screenshot.html file="fdesign-autopilot-survey.png" title="Inspect the survey before grouping resources" caption="Five supplied resources populate native facets and a zero-AI-call estimate. The survey response is synthetic rather than collected from Azure; use the controls to understand scope and sculpting without reading the counts as estate evidence." %}

## How to sculpt discovery inputs

1. Choose **Fast**, **Balanced**, or **Thorough** as a starting preset.
2. Select resource, resource-group, or sampled granularity.
3. Keep noise/system-resource-group exclusion enabled when those resources should be reattached by context rather than grouped independently.
4. Apply hard filters for type, environment, region, subscription, or name only when omitted resources must not enter any candidate.
5. Optionally seed groups with a reliable tag key or detected naming convention.
6. Set a confidence floor and maximum AI-call budget.
7. Under Advanced, set **Minimum resources per proposed workload** when recurring discovery
   should exclude undersized candidates during review. A value of `5` excludes candidates with
   1–4 resources; a five-resource candidate remains eligible. The default `1` excludes nothing.
8. Recheck the estimate after every material change; save a discovery profile when the same configuration will be reused. The minimum is stored in the profile.

**Expected result:** The effective input is small enough to review and broad enough to preserve intended application members.

**Verification:** Inspect filtered, noise, tag-seeded, and effective counts. Hard-filtered resources are not reattached.

## How to discover and review candidates

1. Select AI, resource-group, subscription, or tag grouping.
2. Start discovery and monitor enumeration, filtering, grouping, naming, and completion progress.
3. Review each candidate's name, description, type, environment, criticality, members, confidence, evidence, and reasoning.
4. Adjust **Minimum resources per workload** in Review to hide fragmented candidates without
   rerunning Azure discovery or AI. Select **Show excluded** to inspect them, and **Include
   anyway** for a legitimate undersized workload.
5. Correct editable metadata/membership and reject weak or duplicate candidates. Automatic
   minimum-size exclusions are review policy and are not learned as AI grouping rejections.
6. Compare **Grouped by discovery** with **Will be saved**. The latter reflects the current
   minimum, manual selections, overrides, and unique resource IDs.
7. Pay special attention to shared services, resources reattached after noise filtering, and candidates produced after the AI-call cap.
8. Save only accepted candidates.

**Expected result:** Approved candidates become active workload records; discovery itself does not change Azure.

**Verification:** Open each saved workload, inspect membership, then run Groups suggestions and Overlaps.

{% include screenshot.html file="fdesign-autopilot-candidate-evidence.png" title="Inspect a candidate's membership and reasoning" caption="Native confidence badges, member counts, tags, type chips, and expandable reasoning display synthetic candidate evidence. Follow-on Mission Control and diagram options are unchecked. No AI grouping or workload save is demonstrated." %}

{% include screenshot.html file="fdesign-autopilot-review-needed.png" title="Hold uncertain shared services out of the selection" caption="The synthetic shared-service candidate is left unselected for human review using native selection controls. Review-needed is example evidence, not an added UI status. Save was not clicked, so this view is a review decision rather than a saved workload result." %}

## How to tune poor results

1. For broad candidates, use finer granularity, smaller scope, hard filters, or a stronger tag/naming seed.
2. For fragmented candidates, use resource-group/tag seeding or merge reviewed workloads afterward.
3. For high cost, use deterministic grouping, RG granularity, a lower AI cap, or a narrower scope.
4. For missing shared resources, remove inappropriate hard filters and survey again.

**Expected result:** A new candidate set improves boundary quality without overwriting previously saved workloads automatically.

**Verification:** Compare candidate membership and confidence/evidence, not just candidate count.

## Safety and rollback

- Survey and discovery are read-only against Azure.
- AI grouping can expose resource metadata to the configured provider; use approved providers and narrow filters.
- Confidence describes grouping certainty, not health.
- The minimum-size policy is applied after grouping and child-resource reattachment. It reduces
	review and save volume, not the AI calls already used to discover candidate boundaries.
- Single-resource Seed mode ignores the minimum because the operator deliberately requested one workload.
- Saving writes only workload records. Incorrect records can be edited or soft-deleted.
- Never put secrets or personal data in naming hints or saved profiles.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Survey returns zero | Check selected scope, Reader assignment, Resource Graph access, and connection state. |
| Survey required message | The cache expired or controls target a different scope; survey again. |
| Discovery stream fails | Check provider health/rate limits; rerun survey before retrying. |
| Too many fallback groups | Raise the AI-call budget only after scope reduction, or use deterministic grouping deliberately. |
| Candidates overlap | Save only reviewed candidates, then run deep overlap analysis. |
| Small candidates are absent from the visible review list | Select **Show excluded**, lower the minimum, or choose **Include anyway** for an intentional exception. |
| Save remains disabled | Select at least one eligible candidate, or override an undersized candidate. Search and sort do not change selection, but manual unselection does. |

## Related docs

- [Autopilot reference]({{ site.baseurl }}/user-guide/workloads/discovery-autopilot/)
- [Fleet and manual creation recipes]({{ site.baseurl }}/how-to/core-workloads/workload-fleet/)
- [Groups and overlaps recipes]({{ site.baseurl }}/how-to/core-workloads/workload-detail-groups/)
