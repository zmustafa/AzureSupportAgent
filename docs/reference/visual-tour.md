---
layout: default
title: Visual Tour
parent: Reference
nav_order: 5
description: Explore critical Azure Support Agent workflows through nine screenshot highlights and twenty grouped routes into the synthetic screenshot collection.
permalink: /reference/visual-tour/
---

# Visual tour

Follow a critical operational question from workload scope to findings, evidence, and a reviewed next step. This tour groups **more than 100 captured UI states** into **20 workflow groups**, with **nine representative screenshots** here. Open the linked reference and how-to pages for the detailed views rather than loading the entire collection on one page. Select any screenshot to inspect it full-size. The [screenshot manifest]({{ site.baseurl }}/assets/screenshots/manifest.json) records the exact collection, route and synthetic-source notes.

{% include screenshot.html file="core-dashboard.png" title="Dashboard readiness, source coverage, and saved activity" caption="Use summaries to choose where to investigate, then inspect the owning feature. The displayed readiness and activity are synthetic browser fixtures, not live health evidence." %}

## Read the examples safely

- **Synthetic, not live:** the screenshots use an isolated documentation instance with seeded demo records, synthetic browser responses, or built-in catalogs and defaults. Names, identifiers, measurements, costs, and conclusions do not describe a customer tenant. A populated result is not proof that a live Azure or Microsoft Graph collection, assessment, or AI investigation ran.
- **Presentation is not evidence:** scores, completion labels, framework mappings, and investigation conclusions illustrate how to read the UI. They are not certification, proof of root cause, or a test of backend persistence. Evidence Locker hashes and verified badges in the fixtures do not demonstrate a cryptographic integrity check.
- **Default settings and unsaved forms are different states:** configuration pages may show shipped defaults or explicitly unsaved drafts. Connection, provider, network, and instruction drafts were not saved, validated, or applied; credential fields are intentionally empty. Do not copy example values into a production connection or infer that a displayed provider works.
- **A preview is not an executed action:** a tool directory or built-in agent definition does not prove a workflow ran. Case drafts, remediation previews, and backup selection screens do not demonstrate case creation, access removal, export, or restore. The illustrated disabled-access removal and rollback previews contain comments, not executable remediation.
- **Verify your own scope:** before acting, check the selected connection and workload, permissions, source freshness, errors, and missing data in the owning feature. Unknown, not assessed, and unavailable are not healthy results. Review proposed changes, approvals, and rollback separately.

## Establish scope and understand the estate

### 1. Triage readiness and choose the next investigation

Start with [Dashboard]({{ site.baseurl }}/user-guide/core/dashboard/) for the primary workload and saved activity. Use [Mission Control]({{ site.baseurl }}/user-guide/mission-control/) to move from fleet readiness to a workload board, attention tiles, and saved history. Follow the [Dashboard and Chat workflow]({{ site.baseurl }}/how-to/core-workloads/dashboard-chat/) or [Mission Control recipes]({{ site.baseurl }}/how-to/core-workloads/mission-control/) when ready to work in your own scope.

### 2. Confirm workload membership and compare environments

Move from the [Workload Fleet]({{ site.baseurl }}/user-guide/workloads/fleet/) to [Workload Detail]({{ site.baseurl }}/user-guide/workloads/workload-detail/) to inspect the actual member resources. [Groups and Overlaps]({{ site.baseurl }}/user-guide/workloads/groups-overlaps/) covers group membership and production-versus-development comparison; the [workload and group recipes]({{ site.baseurl }}/how-to/core-workloads/workload-detail-groups/) explain the review sequence.

### 3. Inspect architecture and relationship context

[Architectures]({{ site.baseurl }}/user-guide/design-ownership/architectures/) covers the gallery, connected canvas, resource inspector, and retained versions. Use [Estate Graph]({{ site.baseurl }}/user-guide/design-ownership/estate-graph/) and its [exploration recipes]({{ site.baseurl }}/how-to/design-assessment/estate-graph/) to navigate typed relationships across workloads, architectures, and resources. A modeled edge does not establish live traffic or causality.

{% include screenshot.html file="estate-architecture-canvas.png" title="Connected checkout architecture and resource palette" caption="Inspect component relationships before changing the design or interpreting impact. This canvas uses synthetic fixtures; displayed retail amounts are illustrative, not price quotes or proof of deployed resources." %}

### 4. Explain inventory, cost, tags, and accountability

Use [Inventory]({{ site.baseurl }}/user-guide/estate-intelligence/inventory/) for estate composition, cost views, location grouping, and resource drawers. Continue into [Tag Intelligence]({{ site.baseurl }}/user-guide/estate-intelligence/tag-intelligence/) for census, normalization, key/value drill-down, and required-tag gaps, then [Ownership]({{ site.baseurl }}/user-guide/design-ownership/ownership/) for accountable teams, assignment sources, and coverage.

{% include screenshot.html file="estate-inventory-overview.png" title="Estate composition and actionable inventory insights" caption="Start with the overview, then narrow the grid and inspect individual resources. The inventory and insights are synthetic examples, not a fresh estate scan or verified billing data." %}

## Prioritize findings and retain the investigation

### 5. Review assessment findings and performance constraints

[Assessments]({{ site.baseurl }}/user-guide/assessment-performance/assessments/) connects pillar scores to expanded findings and compliance mappings. Follow [assessment governance recipes]({{ site.baseurl }}/how-to/design-assessment/assessments/) to review completeness, ownership, and follow-up. [Performance Profiler]({{ site.baseurl }}/user-guide/assessment-performance/performance-profiler/) adds resource metrics, inventory, heatmaps, and bottleneck views; neither a score nor an AI summary replaces the underlying observations.

{% include screenshot.html file="core-assessment-finding.png" title="Assessment finding with affected storage, ownership, and remediation" caption="Read the affected resource and evidence before accepting a proposed fix. This synthetic finding is not an executed control result, certification, or evidence that an assignment or remediation was saved." %}

### 6. Investigate a symptom, correlate changes, and preserve context

Use [Chat and Deep Investigation]({{ site.baseurl }}/user-guide/core/chat-deep-investigation/) to review a question, competing hypotheses, evidence, and proposed actions. Correlate the result with [Change Explorer]({{ site.baseurl }}/user-guide/estate-intelligence/change-explorer/) summaries, event details, and technical diffs. [Evidence Locker]({{ site.baseurl }}/user-guide/lifecycle-investigation/evidence-locker/) provides snapshot content and comparisons; [Case Files]({{ site.baseurl }}/user-guide/lifecycle-investigation/case-files/) connects the case queue, draft, and incident timeline. Verify real source records before preserving or sharing a conclusion.

{% include screenshot.html file="core-investigation-result.png" title="Deep Investigation hypotheses, evidence, and recommended actions" caption="The completed-result layout shows where to review supporting evidence and uncertainty. Its conclusions are synthetic browser responses, not an executed LLM investigation or proof of root cause; no action was applied." %}

## Check operational coverage and recovery

### 7. Find missing monitoring and diagnostic signals

[Monitoring Coverage]({{ site.baseurl }}/user-guide/coverage/monitoring-coverage/) shows the baseline matrix, all-resource view, and alert evidence. [Telemetry Coverage]({{ site.baseurl }}/user-guide/coverage/telemetry-coverage/) adds diagnostic categories and resource-level settings. Use the [monitoring]({{ site.baseurl }}/how-to/coverage/monitoring-coverage/) and [telemetry]({{ site.baseurl }}/how-to/coverage/telemetry-coverage/) recipes to separate missing configuration from unsupported or unreadable evidence.

### 8. Review alert noise and notification routing

[Alerts Manager]({{ site.baseurl }}/user-guide/coverage/alerts-manager/) covers rationalization, individual rule analysis, action-group destinations, routing gaps, and overlapping notifications. The [operations guide]({{ site.baseurl }}/how-to/coverage/alerts-manager/) explains how to inspect a proposed change before applying it; a displayed destination does not prove delivery.

### 9. Trace backup protection from coverage to operations

Start with [Backup & DR Coverage]({{ site.baseurl }}/user-guide/coverage/backup-dr-coverage/) for protection checks and resource-state detail. [Backup Manager]({{ site.baseurl }}/user-guide/coverage/backup-manager/) adds protected items, policies, failed-job remediation, vault controls, protection flows, cost and waste, and Site Recovery. The [coverage recipes]({{ site.baseurl }}/how-to/coverage/backup-dr-coverage/) and [manager recipes]({{ site.baseurl }}/how-to/coverage/backup-manager/) keep gap review separate from operational changes.

{% include screenshot.html file="ops-backup-coverage-matrix.png" title="Backup coverage across protection, offsite copies, and job SLA" caption="Inspect individual protection checks rather than inferring recoverability from a single summary. This is an isolated seeded demo, not customer backup evidence or proof of a successful restore." %}

### 10. Test the reasoning behind recovery readiness

[Recovery Readiness]({{ site.baseurl }}/user-guide/coverage/recovery-readiness/) connects the overview and scenario matrix to the resource register, per-resource reasoning, workload weakest link, objective breaches, and analysis detail. Follow the [recovery review workflow]({{ site.baseurl }}/how-to/coverage/recovery-readiness/) to distinguish an unknown outcome from a known lack of a recovery path.

{% include screenshot.html file="ops-recovery-scenario-matrix.png" title="Recovery matrix with distinct unknown and no-path outcomes" caption="Read each failure scenario and its evidence before accepting an RTO or RPO conclusion. The seeded demonstration is not a live recovery assessment or an executed failover drill." %}

### 11. Anticipate capacity limits and service retirements

[Quota Monitor]({{ site.baseurl }}/user-guide/lifecycle-investigation/quota-monitor/) links capacity posture to individual limits. [Retirement Radar]({{ site.baseurl }}/user-guide/lifecycle-investigation/retirement-radar/) connects deadlines to migration-impact detail. Use the [quota]({{ site.baseurl }}/how-to/lifecycle-investigation/quota-monitor/) and [retirement]({{ site.baseurl }}/how-to/lifecycle-investigation/retirement-radar/) recipes to verify scope, source dates, and the proposed next step before planning a change.

## Review identity and access exposure

### 12. Follow grant paths and review disabled-account exposure

[IAM]({{ site.baseurl }}/user-guide/governance-identity/iam/) covers the grant grid, Access Map, standing versus eligible privilege, grant paths, and groups whose membership could not be expanded. Use the [disabled-access workflow]({{ site.baseurl }}/how-to/governance-identity/iam-disabled-access/) to review direct grants, group-derived access, owned applications, recoverable identities, and separate removal/rollback previews. Missing membership is a coverage limit, not proof that a group grants no access.

{% include screenshot.html file="identity-accessmap-grant-paths.png" title="Access Map paths through principals, groups, roles, and subscriptions" caption="Trace why access is present rather than relying on a role count alone. This offline synthetic example excludes eligible grants from standing access and reports a deny separately; it is not a live authorization check." %}

### 13. Investigate principals and scanner findings

[Entra principal investigation]({{ site.baseurl }}/user-guide/governance-identity/entra-investigate/) includes search, Azure access, cached and transitive memberships, group members, cycle handling, and change history. [Findings and scanners]({{ site.baseurl }}/user-guide/governance-identity/entra-findings-scanners/) explains saved baselines and deltas; the [investigation recipe]({{ site.baseurl }}/how-to/governance-identity/investigate-entra-finding/) connects them. Zero new findings does not mean there are no existing findings.

### 14. Examine authentication, Conditional Access, and blast radius

Use [Entra setup and coverage]({{ site.baseurl }}/user-guide/governance-identity/entra-setup-coverage/) for authentication perimeter and federation trust context. [Conditional Access]({{ site.baseurl }}/user-guide/governance-identity/entra-conditional-access/) covers user/application coverage, exposure, and impact; [Blast Radius]({{ site.baseurl }}/user-guide/governance-identity/entra-blast-radius/) adds privileged, escalation, federation, and primitive-specific paths. Follow the [coverage-gap workflow]({{ site.baseurl }}/how-to/governance-identity/close-ca-coverage-gaps/) without treating unavailable controls as protected.

### 15. Review guests, privileged activity, and PIM hygiene

[Guest access]({{ site.baseurl }}/user-guide/governance-identity/entra-guests/) separates lifecycle states, partner organizations, and pending invitations. [Privileged access]({{ site.baseurl }}/user-guide/governance-identity/entra-privileged/) connects activation sessions and actions with stale eligibility and standing-access drift. The [guest review]({{ site.baseurl }}/how-to/governance-identity/review-guest-access/) and [privileged-activity review]({{ site.baseurl }}/how-to/governance-identity/review-privileged-activity/) guides explain how to interpret unknown activity and elevation context before taking action.

## Configure a controlled operating environment

### 16. Complete onboarding and select connection methods

Use [First-Run Setup]({{ site.baseurl }}/getting-started/first-run/) and [Overview and Prerequisites]({{ site.baseurl }}/getting-started/overview/) to choose the right tool; the Proactive Support screenshot is a directory, not an operational dashboard. [Azure Tenants]({{ site.baseurl }}/admin/azure-tenants/) covers [host identity]({{ site.baseurl }}/admin/azure-tenants-host-identity/), [pasted tokens]({{ site.baseurl }}/admin/azure-tenants-pasted-token/), [service-principal secrets]({{ site.baseurl }}/admin/azure-tenants-service-principal-secret/), and [certificates]({{ site.baseurl }}/admin/azure-tenants-service-principal-certificate/). [AI Providers]({{ site.baseurl }}/admin/ai-providers/) includes Azure OpenAI and OpenAI configuration examples. These draft forms do not establish that credentials were supplied or a connection was tested.

### 17. Separate application roles, sign-in policy, and network restrictions

[Access Control]({{ site.baseurl }}/admin/access-control/) shows local users and built-in roles; [Permissions]({{ site.baseurl }}/reference/permissions/) explains the exact product capabilities. [Security Policy & Active Sessions]({{ site.baseurl }}/admin/security-policy-sessions/) and [Network Access]({{ site.baseurl }}/admin/network-access/) cover different control layers. Review the defaults and preserve recovery access before making changes; the network example is an unsaved draft, not an applied firewall rule.

{% include screenshot.html file="admin-access-built-in-roles.png" title="Built-in application roles and exact permissions" caption="Inspect the shipped role catalog before designing a narrower assignment. This default catalog is application authorization, not Azure RBAC or Graph consent; no role or user assignment was changed during capture." %}

### 18. Inspect instructions, scoring, and reference baselines

[General Settings]({{ site.baseurl }}/admin/general-settings/) shows an unsaved instruction draft. [System Prompts & Assessments]({{ site.baseurl }}/admin/prompts-scoring/) covers the built-in Chat Agent prompt, assessment defaults, and workload-health weights. [Reference Sets & Change Requests]({{ site.baseurl }}/admin/reference-sets-change-requests/) shows AMBA VM baselines, backup/DR checks, and Key Vault telemetry categories. Distinguish inspecting shipped definitions from saving a customization or running its checks.

### 19. Understand agents and automation before enabling execution

[Sub Agents]({{ site.baseurl }}/user-guide/automations/sub-agents/) and the [agent-management workflow]({{ site.baseurl }}/how-to/automations-connectors/sub-agents/) describe the built-in library. The [Automations and connectors directory]({{ site.baseurl }}/how-to/automations-connectors/) leads to [Scheduled Tasks]({{ site.baseurl }}/user-guide/automations/scheduled-tasks/), [Workbooks]({{ site.baseurl }}/user-guide/automations/workbooks/), [Playbooks]({{ site.baseurl }}/user-guide/automations/playbooks/), and [Notifications]({{ site.baseurl }}/user-guide/automations/notifications/). The directory and defaults are not evidence of agent invocation, scheduled execution, or notification delivery.

### 20. Review application backup scope and demo-data boundaries

[Backup & Restore and Demo Data]({{ site.baseurl }}/admin/backup-demo/) covers the application export-section catalog and the loaded synthetic workload scope; it is separate from Azure resource backup protection. Follow the [backup and demo-data workflow]({{ site.baseurl }}/how-to/administration/backup-demo/) before exporting, restoring, or removing data. The captured screens do not demonstrate a download, upload, restore, or deletion.

## Continue with a task

Choose the relevant [how-to guide]({{ site.baseurl }}/how-to/) for prerequisites, steps, expected results, and verification. Use the [full feature reference]({{ site.baseurl }}/user-guide/) for behavior and limitations, or the [troubleshooting index]({{ site.baseurl }}/reference/troubleshooting/) when your own instance differs from an example.