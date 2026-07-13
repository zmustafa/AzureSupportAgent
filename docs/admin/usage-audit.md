---
layout: default
title: Usage & Audit Log
parent: Administration
nav_order: 8
description: Review model token/cost estimates and searchable privileged-action history.
permalink: /admin/usage-audit/
---

# Usage and Audit Log

**Permissions:** `settings.write` for Usage; `audit.read` for Audit Log

## Purpose

**App routes:** `/admin/usage`, `/admin/audit`

## Prerequisites and data sources



## Tabs and actions



## Freshness and scope behavior



## Workflow overview

### Investigation workflow

1. Define a UTC window and actor/target.
2. Find the initiating action and associated approval or configuration change.
3. Correlate IDs and timestamps with feature, Azure, and connector records.
4. Export only through an approved process and redact sensitive metadata.
5. Preserve decision-grade records in Evidence Locker or the organization's SIEM.

## Interpretation of results



## Exports, history, scheduling, and integrations

### Usage

Usage is grouped by provider and model with request count, prompt tokens, completion tokens, and estimated USD cost plus totals. A tilde marks a fallback rate for a model absent from the price table. These numbers support governance only; provider invoices, Azure agreements, caching, and delayed billing remain authoritative.

### Audit Log

Audit entries include timestamp, actor, action, target, optional provider/model, and action-specific metadata. Use pagination and available filters/search to investigate configuration, provider OAuth, approvals, connections, connectors, users, sessions, backups, demo data, and feature writes.

An audit event proves the application recorded an action; it does not alone prove an external Azure or connector operation completed. Correlate with managed-change status, destination delivery logs, Azure Activity Log, or external system records.

## Safety and limitations



## Troubleshooting


Use the checks below when results differ from expectations.

## Related pages

- [Auditing]({{ site.baseurl }}/security/auditing/)
- [SIEM and security destinations]({{ site.baseurl }}/connectors/siem-security/)
