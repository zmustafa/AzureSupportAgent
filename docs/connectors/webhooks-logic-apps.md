---
layout: default
title: Webhooks & Azure Logic Apps
parent: Connectors
nav_order: 5
description: Configure generic signed HTTPS POST delivery or invoke an Azure Logic Apps HTTP trigger.
permalink: /connectors/webhooks-logic-apps/
feature_ids: [CONNECTOR:webhook, CONNECTOR:logicapp]
---

# Generic webhook and Azure Logic Apps

The screenshots show native **unsaved, disabled** setup forms. Credential secrets are empty, entered domains are non-resolving examples, and headers contain only fictional routing metadata. No connector was saved or tested, no receiver was contacted, and no workflow was invoked.

## Generic webhook (`webhook`, HTTP)
Configure an HTTPS URL, optional custom headers, optional HMAC signing secret, and optional signature-header name. The tool posts an explicit JSON payload or a standard title/message/severity/facts envelope. When signing is configured, it emits an HMAC-SHA256 signature over the request body.

Use custom headers for non-secret routing metadata where possible; secret headers are part of encrypted connector configuration but can still be exposed at the receiving proxy. Validate signatures at the receiver, reject replays according to your gateway design, and return bounded responses.

{% include screenshot.html file="fconn-webhook-http.png" title="Generic webhook — unsaved routing and signing fields" caption="Native UNSAVED setup shows a non-resolving URL, non-secret example headers, an empty HMAC secret, and a signature-header name. Enabled is off. A header name alone does not enable or verify signing; no JSON POST, receiver response, or signature verification occurred." %}

## Azure Logic Apps (`logicapp`, HTTP)
Create a **When an HTTP request is received** trigger and store its SAS-signed trigger URL as a secret. Optional headers and static key/value payload fields are merged with call-time data; call-time fields take precedence. Host validation restricts the trigger to Azure Logic Apps domains.

The trigger URL grants invocation authority. Rotate it after suspected disclosure and never place it in tickets, screenshots, or source control.

{% include screenshot.html file="fconn-logicapp-http.png" title="Azure Logic Apps — unsaved trigger and payload fields" caption="The secret SAS-signed trigger URL is entirely blank; visible URL text is a placeholder. Native fields show fictional Key: Value header lines and key=value static payload additions, with no authorization header. Enabled is off and the draft is UNSAVED; no trigger or downstream action ran." %}

## Testing and troubleshooting
Presence-only health tests do not call the endpoint. **Send test** makes a real POST. Verify HTTPS, DNS/egress, receiver status code, schema, authentication/signature, and timeout. For Logic Apps, inspect run history and trigger schema. For arbitrary systems without an explicit connector, generic webhook is supported only when their contract accepts the posted JSON securely.

## Related pages

- [Notifications]({{ site.baseurl }}/user-guide/automations/notifications/)
- [Credential handling]({{ site.baseurl }}/security/credential-handling/)
