---
layout: default
title: SIEM & Security Destinations
parent: Connectors
nav_order: 3
description: Send governed events to Splunk, Sumo Logic, CrowdStrike NG-SIEM, Cortex XSOAR, or AWS Security Hub.
permalink: /connectors/siem-security/
feature_ids: [CONNECTOR:splunk, CONNECTOR:sumologic, CONNECTOR:crowdstrike_ngsiem, CONNECTOR:securityhub]
---

# SIEM and security destinations

The connector registry implements the following security destinations. Azure Sentinel, Datadog, and Elasticsearch are **not explicit connector types** in this registry; use a reviewed generic webhook or Logic App only when the destination exposes a compatible HTTPS contract.

The screenshots show native **unsaved, disabled** setup or review screens. Credential secrets are empty, entered domains are non-resolving examples, and account or role details are fictional. No connector was saved, authenticated, or tested, and no event or finding was delivered.

## Splunk (`splunk`, HEC)
Configure HEC URL/token and optional default index/sourcetype. The tool sends an event envelope or explicit object to the HTTP Event Collector. Restrict the token to the intended index and enforce trusted TLS.

{% include screenshot.html file="fconn-splunk-hec.png" title="Splunk HEC — unsaved endpoint and event defaults" caption="Native UNSAVED setup shows a non-resolving example HEC URL, an empty token, and fictional index and sourcetype defaults. Enabled is off. These inputs do not show a collector response, token permissions, or an ingested event." %}

## Sumo Logic (`sumologic`, HTTP source)
Configure the secret hosted-collector source URL and optional source category. Events can be single JSON or newline-delimited batches. The URL embeds authority to ingest and must be treated as a credential.

{% include screenshot.html file="fconn-sumologic-http-source.png" title="Sumo Logic — unsaved HTTP source fields" caption="The credential-bearing HTTP source URL is blank; its example placeholder is not a configured endpoint. Source category is the only optional metadata field in this native form. The draft is UNSAVED with Enabled off; no source-host or source-name controls, collector contact, or ingestion result are shown." %}

## CrowdStrike Falcon NG-SIEM (`crowdstrike_ngsiem`, HEC)
Configure allowed ingest URL and token. Host validation restricts accepted CrowdStrike/Humio/LogScale domains. Events can include HEC fields metadata.

{% include screenshot.html file="fconn-crowdstrike-hec.png" title="CrowdStrike HEC — unsaved ingest fields" caption="The HEC API key is empty and Enabled is off. The UNSAVED .invalid URL is deliberately non-resolving and is not an accepted CrowdStrike or Humio host. This native setup screen demonstrates neither host validation nor repository selection, parser selection, or successful ingestion." %}

## Cortex XSOAR (`xsoar`)
XSOAR creates incidents and entries; see [Ticketing & On-call]({{ site.baseurl }}/connectors/ticketing-on-call/).

## AWS Security Hub (`securityhub`)
Use static-key or assume-role mode with region and account ID. The tool imports an ASFF finding via `BatchImportFindings`. Prefer assume-role with external ID and the minimum `securityhub:BatchImportFindings` permission. Health testing calls STS identity only.

{% include screenshot.html file="fconn-securityhub-keys.png" title="Security Hub keys mode — unsaved identity fields" caption="Native UNSAVED setup shows region and a fictional account ID with access key ID, secret access key, and optional session token all empty. Enabled is off. This is not an STS identity result, evidence of Security Hub enablement, or an imported ASFF finding." %}

{% include screenshot.html file="fconn-securityhub-role.png" title="Security Hub role mode — unsaved review" caption="The native Review + add step shows region plus fictional role ARN, external ID, and account details with Disabled selected. Em dashes represent empty base credentials, not stored secrets. Add connector was not clicked. Real Role mode may use the host credential chain when base keys are blank; no STS call, permission check, or finding import occurred here." %}

## Operations
Normalize severity and identifiers before routing, avoid sending secrets or full raw telemetry, and monitor destination rejection/throttling. A connector event is not equivalent to a durable audit-export guarantee; reconcile with destination records.

## Related pages

- [Auditing]({{ site.baseurl }}/security/auditing/)
- [Credential handling]({{ site.baseurl }}/security/credential-handling/)
