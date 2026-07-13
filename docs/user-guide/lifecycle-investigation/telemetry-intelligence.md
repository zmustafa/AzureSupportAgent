---
layout: default
title: Telemetry Intelligence
parent: Lifecycle & Investigation
grand_parent: User guide
nav_order: 4
description: Ask bounded telemetry questions, inspect generated KQL, correlate failures, and reconstruct transactions.
permalink: /user-guide/lifecycle-investigation/telemetry-intelligence/
---

# Telemetry Intelligence

**Permission:** `teleintel.read`

## Purpose

**App route:** `/telemetry-intel`
Telemetry Intelligence queries Application Insights components for a workload or subscription. It exposes the generated KQL and source rows alongside AI triage, timelines, Smart Detection aggregation, transaction reconstruction, and code-optimization suggestions.

## Prerequisites and data sources

### Prerequisites

The selected connection must read the scoped Application Insights resources and queries. An AI provider is required for natural-language translation and narrative; direct validated KQL remains bounded by configured timespan and row limits.

## Tabs and actions



## Freshness and scope behavior



## Workflow overview

### Workflow

1. Select workload or subscription, connection, component, and timespan.
2. Ask a precise question. The stream shows start, generated KQL, rows, and answer.
3. Read the KQL before trusting the narrative; edit and rerun if scope or predicates are wrong.
4. Use **Triage** for failure rate, top dependency/exception, probable trigger, hypothesis, confidence, and supporting queries.
5. Use **Timeline** to compare request, dependency, and exception signals with deployment/configuration events.
6. Use **Smart Detection** for aggregated anomalies.
7. Supply an `operation_Id` to reconstruct spans and narration for one transaction.
8. Register a finding, pin investigation context, or create a supported ticket only after validating evidence.

## Interpretation of results

### Interpret

The query path identifies where execution occurred. Row count is bounded by `teleintel_max_rows`; absence beyond that boundary is unknown. Correlation and probable-trigger text are hypotheses, not causal proof. Transaction data expires according to telemetry retention.

## Exports, history, scheduling, and integrations

No dedicated export, history, scheduling, or integration controls are documented for this feature page.

## Safety and limitations

### Safety

Only read-only KQL is accepted. Validation rejects mutation/control commands and enforces limits, but administrators should still use least-privilege workspace access. Queries and results may contain customer or user data; apply organizational handling and redaction rules before export or ticketing.

## Troubleshooting


| Symptom | Resolution |
| --- | --- |
| No components | Verify workload scope, subscription, component existence, and connection access. |
| Generated query rejected | Read the validation error; remove unsupported statements and reduce limits. |
| Query times out | Narrow timespan, component, operation, and predicates. |
| Transaction not found | Confirm component and `operation_Id`; the telemetry may have aged out. |
| Smart Detection empty | No matching anomaly was returned; try a justified longer window or another component. |

## Related pages

### Related docs

- [Evidence Locker]({{ site.baseurl }}/user-guide/lifecycle-investigation/evidence-locker/)
- [Case Files]({{ site.baseurl }}/user-guide/lifecycle-investigation/case-files/)
- [Credential handling]({{ site.baseurl }}/security/credential-handling/)
