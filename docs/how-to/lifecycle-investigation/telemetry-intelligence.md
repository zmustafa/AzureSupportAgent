---
layout: default
title: Investigate telemetry with Telemetry Intelligence
parent: Lifecycle and investigation
grand_parent: How-to guides
nav_order: 4
description: Generate and validate KQL, triage failures, correlate timelines, reconstruct transactions, and preserve findings.
permalink: /how-to/lifecycle-investigation/telemetry-intelligence/
---

# Investigate telemetry with Telemetry Intelligence

**Exact route:** `/telemetry-intel`.

![Telemetry coverage and query context]({{ site.baseurl }}/assets/telemetry-coverage.png)

## Prerequisites

- Product permission `teleintel.read`.
- A connection that can discover and query scoped Application Insights/Log Analytics data.
- An enabled AI provider for natural-language KQL, narrative, triage, and optimization proposals.
- A justified time window and organizational approval to handle telemetry data.

## Route

**Exact route:** `/telemetry-intel`.

## How to ask a telemetry question and validate KQL

1. Open `/telemetry-intel` and select workload or subscription, connection, component, and timespan.

2. Ask a precise, sanitized question.
3. Follow streamed start, generated KQL, rows, and answer events.
4. Read the KQL, component, timespan, predicates, joins, and limits before accepting the narrative.
5. Edit and rerun a narrower query when scope or assumptions are wrong.
6. Compare the narrative with raw rows.

**Expected result:** Bounded read-only KQL, source rows, and an AI summary.

**Verification:** Manually confirm key counts/timestamps in the rows. Results are capped; absence beyond the row/time boundary is unknown.

## How to triage and correlate an incident

1. Run **Triage** for failure rate, top dependencies/exceptions, probable trigger, hypothesis, confidence, and supporting queries.

2. Open **Timeline** to compare request, dependency, and exception series with available change events.
3. Use **Smart Detection** to inspect aggregated anomalies for the selected component/window.
4. Treat correlation and probable trigger as hypotheses.
5. Validate against deployment records, Change Explorer, configuration history, and raw telemetry.

**Expected result:** A ranked incident hypothesis with supporting evidence and known gaps.

**Verification:** Re-run supporting queries and confirm event ordering, affected operations, and component boundaries.

## How to reconstruct a transaction

1. Obtain a sanitized `operation_Id` from a trusted row.

2. Select the correct component and a window that includes the event.
3. Run transaction reconstruction.
4. Review span order, parent/child relationships, duration, result codes, dependencies, exceptions, and narration.
5. If absent, check component, sampling, retention, and ID before broadening the window.

**Expected result:** A bounded trace reconstruction for one operation where retained telemetry exists.

**Verification:** Compare reconstructed spans with raw requests/dependencies/traces and service logs.

## How to preserve a finding or optimization proposal

1. Validate the KQL and raw evidence.

2. Review code-optimization suggestions as proposals, not patches.
3. Register a finding, pin investigation context, attach an evidence snapshot, or create a supported ticket.
4. Redact user/customer content, tokens, URLs with credentials, and unnecessary IDs.
5. Re-query after remediation and add verification to the case.

**Expected result:** A traceable, minimally disclosed investigation record.

**Verification:** Destination scope and links are correct, and fresh telemetry demonstrates the intended outcome.

## Safety and rollback

The query path accepts read-only KQL and enforces limits, but telemetry may contain personal or customer data. Do not paste secrets or raw payloads into AI prompts/tickets. Queries do not need rollback. Code/config changes based on suggestions require normal review, tests, deployment, and rollback outside the feature.

### Freshness and partial results

Queries are live rather than cache-backed, but Azure ingestion delay, sampling, retention, component boundaries, permissions, row caps, and timeouts can produce partial results. AI can misinterpret rows. Smart Detection may be empty even when another anomaly method would find an issue.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| No components | Verify scope, component existence, connection, and read permission. |
| KQL is rejected | Remove unsupported/control statements and reduce scope/limits. |
| Query times out | Narrow component, timespan, operation, and predicates. |
| Transaction is absent | Check component, `operation_Id`, sampling, ingestion, and retention. |
| Narrative contradicts rows | Trust validated source rows, revise query, and disregard unsupported narrative. |

## Related docs

- [Telemetry Intelligence reference]({{ site.baseurl }}/user-guide/lifecycle-investigation/telemetry-intelligence/)
- [Evidence Locker]({{ site.baseurl }}/how-to/lifecycle-investigation/evidence-locker/)
- [Case Files]({{ site.baseurl }}/how-to/lifecycle-investigation/case-files/)
