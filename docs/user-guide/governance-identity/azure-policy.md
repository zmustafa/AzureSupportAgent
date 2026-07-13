---
layout: default
title: Azure Policy
parent: Governance & Identity
grand_parent: User guide
nav_order: 1
description: Inventory and analyze policy, compliance, exemptions, effective rules, advisors, safe rollout simulations, AI tools, and IaC drift.
permalink: /user-guide/governance-identity/azure-policy/
feature_ids: [PROACTIVE_NAV:policy, POLICY_NAV:advisors, POLICY_NAV:ai, POLICY_NAV:assignments, POLICY_NAV:byperson, POLICY_NAV:bysubscription, POLICY_NAV:drift, POLICY_NAV:effective, POLICY_NAV:exemptions, POLICY_NAV:governance, POLICY_NAV:history, POLICY_NAV:inventory, POLICY_NAV:overview, POLICY_NAV:pivot, POLICY_NAV:rollout, POLICY_NAV:timeline]
---

# Azure Policy

**Product permissions:** `policy.read`; saving drafts, simulations, snapshots, enforcement links, and IaC source requires `policy.write`. The same write permission gates exemption create, update, and delete operations against Azure.

## Purpose

**App routes:** `/policy` and `/policy/:tab`
Azure Policy provides governance inventory and analysis. It can author proposals, resolve effective policy, estimate blast radius, and build staged rollout plans, but it does not assign or deploy policy definitions or assignments to Azure. The Exemptions tab is the exception to the otherwise analytical workflow: with `policy.write` and a write-enabled connection, it can create, update, or delete Azure policy exemptions.

## Prerequisites and data sources

### Prerequisites

- An ARM/Resource Graph-capable connection with Reader access to selected scopes.
- Policy Insights read access for compliance summaries.
- A workload definition when filtering policy inventory to workload scopes.
- A configured AI provider for AI author/explain/triage and AI-assisted simulation phases.
- `policy.write` for local persistence actions and exemption mutations. Local saves do not deploy Azure policy; exemption apply/remove uses ARM against Azure and also requires a write-enabled connection and Azure rights at the target scope.

## Tabs and actions

### Tabs

- **Overview**: governance KPIs and current inventory summary.
- **Inventory**: definitions, initiatives, assignments, exemptions, scope tree, and available compliance.
- **Assignments**: detailed register with scope, definition, effect, enforcement mode, exclusions, and identity.
- **By person**: author/identity-oriented pivot available from inventory metadata.
- **By subscription**: scope-oriented policy view.
- **Timeline**: assignment/compliance history from captured data.
- **Pivot builder**: custom analysis across policy dimensions.
- **Governance**: promotion and governance insights.
- **Exemptions**: expiry and hygiene analysis.
- **Effective policy**: resolves inheritance minus excluded scopes and applicable exemptions for a supplied scope.
- **Advisors**: promote-to-deny candidates, remediation gaps, conflicts, exemption hygiene, and baseline coverage.
- **Rollout Planner**: streaming simulation for deploy, promote, or finding-driven scenarios.
- **AI tools**: author JSON, explain a rule, triage a deny, and propose tag governance.
- **Drift & IaC**: compares stored source-of-truth material with observed policy and proposes reconciliation.
- **History**: saved simulations and coverage runs.

Within **Exemptions**, the **Table** and **Pivot** nested views support scope/group/column filters, saved perspectives, CSV/Excel export, and drill-down. **Pivot builder** also supports reorderable row dimensions, presets, saved local perspectives, date granularity, expand/collapse, CSV, and Excel.

## Freshness and scope behavior

### Collection and freshness

Policy inventory is cached persistently by tenant, connection, workload, and whether compliance was requested. A normal load and the basic cached refresh do not scan Azure on a cache miss. Use **Scan Compliance**/the explicit force action to collect live definitions, initiatives, assignments, exemptions, subscriptions, and—when requested—Policy Insights summaries.

Because the cache has no automatic expiry, always inspect `fetched_at`/age. Resource Graph result-size limits can truncate large policy sets. Compliance is slower and permission-dependent; unavailable compliance does not mean compliant.

## Workflow overview

### Implementation-grounded usage scenarios

1. **Explain an unexpected deny:** open `/policy/effective` at the failing resource scope, trace inherited assignments, `notScopes`, and exemptions, then use `/policy/ai` **Triage** only as a hypothesis and verify the blocking assignment in Azure.
2. **Review an expiring waiver:** open `/policy/exemptions`, filter expiring records, inspect assignment and scope, run **Preview & validate**, and either copy the generated CLI on a read-only connection or apply the approved update with `policy.write` on a write-enabled connection.
3. **Plan audit-to-deny promotion:** refresh compliance, open `/policy/rollout`, choose **Promote an existing policy**, stream the bounded impact simulation, save the local plan if required, and deploy externally only after representative audit testing.

### Simulate a rollout

1. Open **Rollout Planner** and choose **deploy**, **promote**, or **finding**.
2. Supply intent/policy JSON, target scope, and target effect as applicable.
3. Start simulation. The stream reports authoring/resolution, what-if translation, blast-radius analysis, and staged-plan generation.
4. Inspect whether what-if is supported, matching count, sample, exclusions, identity requirements, and exemption guidance.
5. Begin externally with audit at a test scope, validate telemetry and false positives, then use staged expansion. A sample deny and full deny require separate organizational approval and external deployment.

A 100% compliant audit assignment is not automatically safe to deny: sample limitations, stale compliance, exemptions, and unobserved deployment paths still matter.

## Interpretation of results

An effective-policy result is calculated from the loaded assignment/exemption snapshot; it is not a live Azure evaluation trace. Advisor labels such as **safe to promote** are leads derived from available compliance and can be unsafe when the collection is stale, partial, or unrepresentative. Missing compliance is unknown, and a saved simulation is a local point-in-time analysis rather than deployment evidence.

## Exports, history, scheduling, and integrations

### AI, IaC, remediation, and export

AI output is proposal text/JSON; validate aliases, modes, effects, parameters, and resource-provider behavior. Drift compares a locally stored source of truth and returns analysis/reconciliation proposals. It does not synchronize Azure or provide a built-in deployment/export pipeline.

DeployIfNotExists and Modify remediation require assignment identity, location where applicable, suitable role-definition IDs, and Azure remediation tasks. The view highlights gaps but does not execute remediation.

## Safety and limitations

### Review governance posture

1. Select connection and workload/scope.
2. Run an explicit scan if the cache is absent or old.
3. Review Inventory and Assignments for scope, `notScopes`, enforcement mode, definition/initiative, parameters, and identity.
4. Review Exemptions for expired, expiring, never-expiring, or weakly justified records.
5. Resolve Effective policy at a representative resource scope; confirm inherited assignments and exclusions.
6. Use Advisors as leads:
   - promotion candidates are audit assignments whose observed compliance suggests further evaluation;
   - remediation gaps identify modify/DeployIfNotExists designs missing required identity/RBAC;
   - conflicts identify duplicate/redundant patterns;
   - baseline coverage identifies missing governance areas.
7. Capture a snapshot or save analysis only when the record is needed.

- Policy analysis, simulation, and local saves are read-only with respect to Azure. Exemption apply/remove is not: it performs audited ARM create, update, or delete operations after preview and connection/write checks.
- Removing an exemption can immediately restore enforcement. Recreate the previously approved values to recover an accidental update or removal; remove an accidentally created exemption only after checking impact.
- What-if translates only supported policy-rule patterns into Resource Graph predicates; unsupported results require external testing.
- Match samples are limited and Resource Graph itself is eventually consistent.
- Compliance can be absent due to permission/API failure.
- Deny can break deployments; Append/Modify can alter resources; DINE can create resources and cost.
- Exemptions and `notScopes` can make top-level compliance percentages misleading.
- Saved drafts/simulations are local records, not Azure definitions or assignments.

## Troubleshooting


| Symptom | Check |
| --- | --- |
| Inventory says never loaded | Use the explicit live scan, not a cache-only refresh. |
| Compliance is unavailable | Verify Policy Insights access, subscription coverage, and connection token/scope. |
| Assignments appear missing | Check workload filtering, Resource Graph truncation, scope visibility, and cache age. |
| What-if is unsupported | The rule cannot be translated safely; validate through an external test assignment in audit. |
| Remediation gap is reported | Add an assignment identity and least-privilege role at the correct scope before external deployment. |
| Drift does not update Azure | Expected: Drift is analysis only; reconcile through reviewed IaC. |

## Related pages

- [Telemetry Coverage]({{ site.baseurl }}/user-guide/coverage/telemetry-coverage/)
- [Tag Intelligence]({{ site.baseurl }}/user-guide/estate-intelligence/tag-intelligence/)
- [RBAC]({{ site.baseurl }}/user-guide/governance-identity/rbac/)
