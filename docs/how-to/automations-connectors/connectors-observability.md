---
layout: default
title: Configure observability connectors
parent: Automations and connectors
grand_parent: How-to guides
nav_order: 68
description: Configure and verify Splunk, Grafana, Security Hub, XSOAR, Sumo Logic, and CrowdStrike connectors.
permalink: /how-to/automations-connectors/connectors-observability/
feature_ids: [CONNECTOR:splunk, CONNECTOR:grafana, CONNECTOR:securityhub, CONNECTOR:xsoar, CONNECTOR:sumologic, CONNECTOR:crowdstrike_ngsiem]
---

# Configure observability and SIEM connectors

The screenshots show native **unsaved, disabled** setup or review screens, not working integrations. Credential secrets are empty; entered domains are non-resolving and account, role, and destination values are fictional. No save, authentication, test, event ingestion, finding import, or annotation creation is demonstrated.

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

{% include screenshot.html file="fconn-splunk-hec.png" title="Splunk HEC — unsaved endpoint and routing defaults" caption="The native UNSAVED form shows a non-resolving HEC URL, empty token, and fictional default index and sourcetype. Enabled is off. No collector request, permission validation, or event ingestion occurred; these are setup inputs rather than a successful Test or delivery result." %}

1. Add **Splunk**, enter the HEC endpoint and secret token, set approved index/sourcetype, and save disabled.
2. Select **Test**; it only confirms both values are stored.
3. Enable and select **Send test** to ingest a real event.
4. Search the intended index around the test time and verify source/sourcetype.

**Expected result:** Test reports configured; Send test ingests one event.

**Verification:** Find the event in Splunk and confirm index, sourcetype, title, and time.

## How to configure Grafana

{% include screenshot.html file="fconn-grafana-token.png" title="Grafana — unsaved token and datasource fields" caption="Native UNSAVED setup shows a non-resolving base URL, empty API/service-account token, and fictional default datasource UID with Enabled off. The wizard has no dashboard selector or PromQL editor. No health probe, datasource request, or annotation creation occurred." %}

1. Add **Grafana**, enter the URL/token and optional datasource UID, and save disabled.
2. Select **Test**; it calls the read-only `/api/health` endpoint.
3. Enable and select **Send test**; notification delivery creates a real Grafana annotation.
4. Locate the annotation and confirm its tags and time.

**Expected result:** Test reports reachable; Send test creates an annotation.

**Verification:** Check Grafana annotations on the relevant time range.

## How to configure AWS Security Hub

{% include screenshot.html file="fconn-securityhub-role.png" title="Security Hub role mode — unsaved identity review" caption="Native Review + add shows region, fictional role ARN, external ID, and account ID with a Disabled badge. Em dashes are empty base credentials, not saved secrets; Add connector was not clicked. Real Role mode may use the host credential chain when base keys are blank. No STS call, Security Hub enablement check, permission verification, or finding import occurred." %}

Compare the [keys and role screenshots in the SIEM reference]({{ site.baseurl }}/connectors/siem-security/) when choosing the identity fields to configure.

1. Add **AWS Security Hub**; choose **Keys** or **Role**, enter region/account ID and credentials or role details, then save disabled.
2. Select **Test**; it calls STS `GetCallerIdentity` and creates no finding.
3. Compare the returned ARN with the intended account and role, then enable.
4. There is no Send test UI/API support. If import must be proven, use an explicitly approved, uniquely identifiable low-severity test finding through a controlled workflow.
5. Archive/suppress the test finding according to Security Hub procedures.

**Expected result:** Test identifies the AWS principal without writing a finding.

**Verification:** Confirm ARN, account, region, Security Hub enablement, and—only for an approved import—the expected ASFF finding.

## How to configure Cortex XSOAR

{% include screenshot.html file="fconn-xsoar-api-key.png" title="Cortex XSOAR — unsaved server and key fields" caption="Native UNSAVED setup shows a non-resolving server URL, empty API key, fictional optional key ID, and incident-type default. Enabled is off. The key ID is non-secret metadata, not a working credential; no authenticated-user probe, incident, or entry was created." %}

1. Add **Cortex XSOAR**, enter URL/key, optional key ID and default incident type, then save disabled.
2. Select **Test**; it performs a read-only `GET /user` authentication probe.
3. Confirm authentication and enable.
4. There is no Send test UI/API support. Verify with read access first; use a controlled non-production incident workflow only when explicitly approved.
5. Close/delete the controlled incident according to XSOAR policy.

**Expected result:** Test authenticates without creating an incident.

**Verification:** Confirm the expected XSOAR identity/version; verify any approved test incident in the intended tenant/type.

## How to configure Sumo Logic

{% include screenshot.html file="fconn-sumologic-http-source.png" title="Sumo Logic — unsaved source URL and category" caption="The HTTP source URL is an empty secret; example placeholder text is not a stored endpoint. The native form exposes source category, but no source-host or source-name controls. Enabled is off and the draft is UNSAVED. No collector request, metadata verification, or event ingestion occurred." %}

1. Add **Sumo Logic**, enter the secret source URL and optional metadata, and save disabled.
2. Select **Test**; it only confirms that the source URL is stored.
3. Enable and select **Send test** to ingest a real event.
4. Search the configured source category around the test time.

**Expected result:** Test reports configured; Send test ingests an event.

**Verification:** Confirm event content and `_sourceCategory`, `_sourceHost`, or `_sourceName` metadata when configured.

## How to configure CrowdStrike Next-Gen SIEM

{% include screenshot.html file="fconn-crowdstrike-hec.png" title="CrowdStrike Next-Gen SIEM — unsaved HEC fields" caption="The native UNSAVED setup has an empty HEC API key and Enabled off. Its fictional .invalid hostname is non-resolving and not an accepted CrowdStrike or Humio destination. No host validation, repository or parser selection, or event ingestion is demonstrated." %}

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
| Splunk reports success but the event is not searchable | Confirm HEC index routing, token index permission, event timestamp, and ingestion delay in Splunk before resending. |
| Check HEC enablement, endpoint path/port, token, TLS trust, index permission, and ingestion delay | Configuration success does not contact Splunk. |
| [Connector lifecycle]({{ site.baseurl }}/how-to/automations-connectors/connector-lifecycle/) | Review connector configuration and retry. |

## Related docs

- [Notifications]({{ site.baseurl }}/how-to/automations-connectors/notifications/)
- [Connector lifecycle]({{ site.baseurl }}/how-to/automations-connectors/connector-lifecycle/)
