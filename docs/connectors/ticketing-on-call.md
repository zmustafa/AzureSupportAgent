---
layout: default
title: Ticketing & On-call
parent: Connectors
nav_order: 2
description: Configure Jira, ServiceNow, Cortex XSOAR, and PagerDuty integrations.
permalink: /connectors/ticketing-on-call/
feature_ids: [CONNECTOR:jira, CONNECTOR:servicenow, CONNECTOR:pagerduty, CONNECTOR:xsoar]
---

# Jira, ServiceNow, XSOAR, and PagerDuty

The screenshots show native **unsaved, disabled** forms or review steps with empty credential secrets. Entered domains are non-resolving examples and other values are fictional. No connector was saved, authenticated, or tested; no issue, incident, entry, or alert was created.

## Jira (`jira`, token mode)
Configure base URL, account email, API token, optional default project, and default issue type. Implemented operations create an issue, add an ADF comment, and search with JQL. The health test reads the authenticated user and does not create an issue. Custom field authoring is not implemented.

{% include screenshot.html file="fconn-jira-token.png" title="Jira token mode — unsaved project defaults" caption="The native UNSAVED setup contains fictional site, account, project, and issue-type values; the API token is empty and Enabled is off. The example domains do not resolve. This is not a custom-field editor, authenticated-user result, search result, or created issue." %}

## ServiceNow (`servicenow`, basic mode)
Configure instance URL, integration username/password, optional default urgency/impact, assignment group, and caller ID. Implemented operations create, read, search, and update incidents and add work notes/comments. Use a dedicated least-privilege integration user for the Incident Table API. Change-request tables are not implemented.

{% include screenshot.html file="fconn-servicenow-basic.png" title="ServiceNow basic mode — unsaved review" caption="Native Review + add shows all seven fields, including fictional caller, assignment group, urgency, and impact defaults. The password em dash means empty, not a stored credential. The draft is Disabled and UNSAVED; Add connector was not clicked, and no Incident Table query or write occurred." %}

## Cortex XSOAR (`xsoar`, API-key mode)
Configure server URL, API key, optional API key ID, and default incident type. Implemented operations create incidents and add entries. The health test authenticates with a read probe.

{% include screenshot.html file="fconn-xsoar-api-key.png" title="Cortex XSOAR — unsaved API-key setup" caption="The native UNSAVED form shows a non-resolving server URL, an empty API key, a fictional optional key ID, and a default incident type. Enabled is off. The key ID is not a credential or authentication result; no user probe, incident, or entry was created." %}

## PagerDuty (`pagerduty`, Events API v2)
Configure routing key and optional default source. Implemented operations trigger, acknowledge, and resolve by deduplication key. It does not manage escalation policies or schedules.

{% include screenshot.html file="fconn-pagerduty-events-v2.png" title="PagerDuty Events API v2 — unsaved routing fields" caption="The routing key is blank, the default source is fictional, and Enabled is off in this UNSAVED native form. No escalation-policy or schedule controls are shown. Send test was not used and no alert was triggered; a real send could notify responders." %}

## Safe workflow
Use connector health tests first. Ticket/incident creation is a real external write, so the generic Send test is intentionally unavailable for ticketing/storage-like destinations. Create a controlled low-severity test only through an approved workflow, then close it at the destination.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Authentication or authorization fails | Verify base/instance URL, dedicated account state, token/password, and project/role permissions. |
| Calls succeed inconsistently | Confirm defaults and destination API version match the connector configuration. |
| PagerDuty updates do not correlate | Use the same stable deduplication key across trigger, acknowledge, and resolve operations. |

## Related pages

- [Case Files]({{ site.baseurl }}/user-guide/lifecycle-investigation/case-files/)
- [Approvals]({{ site.baseurl }}/security/approvals/)
