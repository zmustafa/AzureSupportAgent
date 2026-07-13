---
layout: default
title: Plan policy rollouts and use AI tools
parent: Governance and identity
grand_parent: How-to guides
nav_order: 4
description: Simulate staged policy rollout and validate AI-authored, explained, triaged, and tag-governance proposals.
permalink: /how-to/governance-identity/policy-rollout-ai/
---

# Plan policy rollouts and use AI tools

## Prerequisites

- Product permission `policy.read`; `policy.write` to save a local simulation or draft.
- A current inventory, exact test scope, policy intent, and an enabled AI provider for AI phases.
- External approval and deployment tooling for any Azure change.

## Route

`/policy/rollout`, `/policy/ai`, and `/policy/history`.

## How to simulate a staged rollout

1. Open `/policy/rollout`.

2. Choose **deploy**, **promote**, or **finding** mode.
3. Provide non-sensitive intent or policy JSON, exact scope, and target effect.
4. Select **Simulate** and follow streamed author, what-if, blast-radius, and plan events.
5. Inspect whether translation is supported, match count, sample, exclusions, remediation identity, and exemption guidance.
6. Review the proposed audit, limited/sample enforcement, and full-enforcement stages.
7. Save the simulation only when a local record is required.
8. Implement externally only after peer review, approval, audit telemetry, and rollback preparation.

**Expected result:** A read-only staged plan and impact estimate; no Azure definition or assignment is deployed.

**Verification:** Test the policy in audit at a non-production scope, inspect fresh compliance, and exercise representative create/update operations before enforcement.

## How to author or explain a policy with AI

1. Open `/policy/ai` and choose **Author** or **Explain**.

2. For Author, describe the resource type, condition, effect, exclusions, and parameters without real identifiers.
3. For Explain, paste sanitized policy JSON.
4. Validate generated JSON syntax, aliases, mode, effect, parameter types, and provider behavior against Microsoft documentation.
5. Pass the reviewed proposal into Rollout Planner; do not deploy raw model output.

**Expected result:** A draft policy or plain-language explanation suitable for expert review.

**Verification:** Run policy validation/linting, compare aliases with Azure, and perform a bounded audit test.

## How to triage a deny or propose tag governance

1. In `/policy/ai`, choose **Triage** and paste a redacted deployment error, or choose **Tag governance** and select sanitized inventory context.

2. Review the suggested blocking assignment, rationale, and fix or proposed tag rules.
3. Resolve effective policy at the failing scope and confirm the assignment ID in Azure.
4. Prefer fixing the deployment or approved policy design over creating a broad exemption.
5. Simulate any proposed tag policy before external rollout.

**Expected result:** A hypothesis and proposal, not a confirmed root cause or applied change.

**Verification:** Re-run the failed operation in an approved test after the external correction and confirm the expected policy evaluation.

## Safety and rollback

Never paste secrets, tokens, full customer payloads, real object IDs, or personal data into AI inputs. Simulation and AI tools are Azure-read-only, but saving creates local records. Azure rollback must be designed in the deployment mechanism: remove or revert the assignment/definition, restore prior IaC, and account for resources modified or deployed by Modify/DINE.

### Freshness and partial results

What-if translates only supported rule patterns to Resource Graph and uses bounded samples. Resource Graph is eventually consistent. AI output can hallucinate aliases or capabilities. A compliant audit sample does not guarantee deny safety across unobserved deployment paths.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Stream stops with an error | Preserve the redacted input, check AI/provider status, and retry with narrower intent. |
| What-if is unsupported | Use an external audit assignment and representative deployment tests. |
| Match count looks too small | Check scope, inventory age, rule translation, resource type, and ARG limits. |
| AI JSON is invalid | Correct syntax and validate aliases/effects before simulation. |
| Saved plan is not in Azure | Expected: saved simulations are local analysis records only. |

## Related docs

- [Effective policy and advisors]({{ site.baseurl }}/how-to/governance-identity/policy-effective-advisors/)
- [Policy pivots and history]({{ site.baseurl }}/how-to/governance-identity/policy-pivots-history/)
- [Azure Policy reference]({{ site.baseurl }}/user-guide/governance-identity/azure-policy/)
