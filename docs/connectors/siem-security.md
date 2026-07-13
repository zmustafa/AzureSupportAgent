---
layout: default
title: SIEM & Security Destinations
parent: Connectors
nav_order: 3
description: Send governed events to Splunk, Sumo Logic, CrowdStrike NG-SIEM, Cortex XSOAR, or AWS Security Hub.
permalink: /connectors/siem-security/
---

# SIEM and security destinations

The connector registry implements the following security destinations. Azure Sentinel, Datadog, and Elasticsearch are **not explicit connector types** in this registry; use a reviewed generic webhook or Logic App only when the destination exposes a compatible HTTPS contract.

## Splunk (`splunk`, HEC)
Configure HEC URL/token and optional default index/sourcetype. The tool sends an event envelope or explicit object to the HTTP Event Collector. Restrict the token to the intended index and enforce trusted TLS.

## Sumo Logic (`sumologic`, HTTP source)
Configure the secret hosted-collector source URL and optional source category. Events can be single JSON or newline-delimited batches. The URL embeds authority to ingest and must be treated as a credential.

## CrowdStrike Falcon NG-SIEM (`crowdstrike_ngsiem`, HEC)
Configure allowed ingest URL and token. Host validation restricts accepted CrowdStrike/Humio/LogScale domains. Events can include HEC fields metadata.

## Cortex XSOAR (`xsoar`)
XSOAR creates incidents and entries; see [Ticketing & On-call]({{ site.baseurl }}/connectors/ticketing-on-call/).

## AWS Security Hub (`securityhub`)
Use static-key or assume-role mode with region and account ID. The tool imports an ASFF finding via `BatchImportFindings`. Prefer assume-role with external ID and the minimum `securityhub:BatchImportFindings` permission. Health testing calls STS identity only.

## Operations
Normalize severity and identifiers before routing, avoid sending secrets or full raw telemetry, and monitor destination rejection/throttling. A connector event is not equivalent to a durable audit-export guarantee; reconcile with destination records.

## Related pages

- [Auditing]({{ site.baseurl }}/security/auditing/)
- [Credential handling]({{ site.baseurl }}/security/credential-handling/)
