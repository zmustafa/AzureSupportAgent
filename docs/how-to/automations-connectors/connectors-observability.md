---
layout: default
title: Configure observability connectors
parent: Automations and connectors
grand_parent: How-to guides
nav_order: 68
description: Configure and verify Splunk, Grafana, Security Hub, XSOAR, Sumo Logic, and CrowdStrike connectors.
permalink: /how-to/automations-connectors/connectors-observability/
---

# Configure observability and SIEM connectors

## Prerequisites

- `connectors.manage`.
- Enabled Splunk HTTP Event Collector, HEC URL/token, and optional index/sourcetype.
- Grafana base URL, service-account token, and optional datasource UID.
- Permission to read health/alerts and create annotations when used for delivery.
- Security Hub enabled in the target account/region.
- AWS account ID and either static keys or an assumable role; the execution identity needs `securityhub:BatchImportFindings` for real use.
- XSOAR URL/API key; XSOAR 8 or XSIAM also requires the API key ID.
- Permission to read the user endpoint and create incidents only when required.
- Hosted Collector HTTP Logs & Metrics Source URL and optional source category/host/name.
- Falcon/LogScale HEC ingest URL and bearer ingest token from the same connector.

## Route

- Open `/api/health`.
- Open `/automations/connectors`.

## How to configure Splunk HEC

1. Add **Splunk**, enter the HEC endpoint and secret token, set approved index/sourcetype, and save disabled.
2. Select **Test**; it only confirms both values are stored.
3. Enable and select **Send test** to ingest a real event.
4. Search the intended index around the test time and verify source/sourcetype.

**Expected result:** Test reports configured; Send test ingests one event.

**Verification:** Find the event in Splunk and confirm index, sourcetype, title, and time.

## How to configure Grafana

1. Add **Grafana**, enter the URL/token and optional datasource UID, and save disabled.
2. Select **Test**; it calls the read-only `/api/health` endpoint.
3. Enable and select **Send test**; notification delivery creates a real Grafana annotation.
4. Locate the annotation and confirm its tags and time.

**Expected result:** Test reports reachable; Send test creates an annotation.

**Verification:** Check Grafana annotations on the relevant time range.

## How to configure AWS Security Hub

1. Add **AWS Security Hub**; choose **Keys** or **Role**, enter region/account ID and credentials or role details, then save disabled.
2. Select **Test**; it calls STS `GetCallerIdentity` and creates no finding.
3. Compare the returned ARN with the intended account and role, then enable.
4. There is no Send test UI/API support. If import must be proven, use an explicitly approved, uniquely identifiable low-severity test finding through a controlled workflow.
5. Archive/suppress the test finding according to Security Hub procedures.

**Expected result:** Test identifies the AWS principal without writing a finding.

**Verification:** Confirm ARN, account, region, Security Hub enablement, and—only for an approved import—the expected ASFF finding.

## How to configure Cortex XSOAR

1. Add **Cortex XSOAR**, enter URL/key, optional key ID and default incident type, then save disabled.
2. Select **Test**; it performs a read-only `GET /user` authentication probe.
3. Confirm authentication and enable.
4. There is no Send test UI/API support. Verify with read access first; use a controlled non-production incident workflow only when explicitly approved.
5. Close/delete the controlled incident according to XSOAR policy.

**Expected result:** Test authenticates without creating an incident.

**Verification:** Confirm the expected XSOAR identity/version; verify any approved test incident in the intended tenant/type.

## How to configure Sumo Logic

1. Add **Sumo Logic**, enter the secret source URL and optional metadata, and save disabled.
2. Select **Test**; it only confirms that the source URL is stored.
3. Enable and select **Send test** to ingest a real event.
4. Search the configured source category around the test time.

**Expected result:** Test reports configured; Send test ingests an event.

**Verification:** Confirm event content and `_sourceCategory`, `_sourceHost`, or `_sourceName` metadata when configured.

## How to configure CrowdStrike Next-Gen SIEM

1. Add **CrowdStrike Next-Gen SIEM**, enter the approved HEC URL/token, and save disabled.
2. Select **Test**; it only confirms both values are stored.
3. Enable and select **Send test** to ingest a real HEC-style event.
4. Query the selected repository/parser around the test time.

**Expected result:** Test reports configured; Send test ingests one event.

**Verification:** Confirm the event envelope, parser/repository, timestamp, and fields in Falcon Next-Gen SIEM/LogScale.

## Safety and rollback

Use a non-production dashboard/time range where possible. Remove the annotation if required, disable the connector, and rotate the token.

Prefer role mode and least privilege. Disable the connector, revoke keys/role access, and handle any controlled finding in Security Hub. Absence of Send test is intentional.

Use a dedicated key. Disable the connector, revoke the key, and clean up controlled incidents. Absence of Send test is intentional.

Use a test source/category. Disable the connector, follow retention procedures for test data, and rotate a disclosed source URL.

Use a test repository/parser. Disable the connector, follow data-retention procedures, and rotate an exposed token.

Use a test index first. Disable the connector, delete/expire the event according to retention policy, and rotate an exposed HEC token.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Unexpected result | Re-check route, permissions, and latest refresh state before retrying. |
| Check HEC enablement, endpoint path/port, token, TLS trust, index permission, and ingestion delay | Configuration success does not contact Splunk. |
| [Connector lifecycle]({{ site.baseurl }}/how-to/automations-connectors/connector-lifecycle/) | Review connector configuration and retry. |

## Related docs

- [Notifications]({{ site.baseurl }}/how-to/automations-connectors/notifications/)
- [Connector lifecycle]({{ site.baseurl }}/how-to/automations-connectors/connector-lifecycle/)
