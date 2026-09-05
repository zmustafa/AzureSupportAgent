---
layout: default
title: Teams, Slack & Email
parent: Connectors
nav_order: 1
description: Configure implemented Teams, Slack, SMTP email, and Outlook/Microsoft Graph delivery modes.
permalink: /connectors/messaging/
feature_ids: [CONNECTOR:teams, CONNECTOR:slack, CONNECTOR:outlook, CONNECTOR:email]
---

# Teams, Slack, and email

The screenshots show native **unsaved, disabled** forms, not working integrations. Credential secrets are empty, entered domains are non-resolving examples, and other values are fictional. No save, authentication, test, or delivery is demonstrated; placeholder text in a secret field is not a stored credential.

## Microsoft Teams (`teams`)
- **webhook mode:** Teams webhook URL (secret).
- **graph mode:** existing Azure connection, Team ID, and Channel ID.
- **Tool:** post a severity-styled message/card.

Webhook URLs require HTTPS and are subject to outbound URL checks. Graph mode acquires a Microsoft Graph application token through the selected Azure connection; grant only channel-message permissions required by your deployment model.

{% include screenshot.html file="fconn-teams-webhook.png" title="Teams webhook mode — unsaved setup" caption="The incoming webhook URL is blank and Enabled is off in this UNSAVED native form. Webhook mode has no Team ID or Channel ID fields; those belong to Graph mode. No endpoint was contacted or message posted." %}

## Slack (`slack`)
- **webhook mode:** incoming webhook URL (secret), fixed destination behavior.
- **token mode:** bot/user OAuth token (secret) and optional default channel.
- **Tool:** post a Block Kit message with severity styling.

Token test calls Slack `auth.test`; webhook test checks configuration presence. Send a real test only to a dedicated channel.

{% include screenshot.html file="fconn-slack-token.png" title="Slack token mode — unsaved channel defaults" caption="Token mode exposes the Bot/User OAuth token and optional default channel. The token is empty, the channel is fictional, and Enabled is off. This UNSAVED form is not an OAuth sign-in or app installation, and no auth.test call or message occurred." %}

## SMTP Email (`email`)
Fields are SMTP host/port, From address, optional username, and optional password. The connector uses SMTP with TLS behavior appropriate to the configured port/server and sends HTML body. Recipient and header validation guards against malformed addresses and CR/LF injection. Attachments are not implemented.

{% include screenshot.html file="fconn-email-smtp.png" title="SMTP — unsaved relay fields" caption="The native form shows host, port, From address, optional username, and an empty password. Example domains do not resolve and Enabled is off. This UNSAVED setup has no recipient field or TLS toggle; port 587 is not evidence of a negotiated TLS connection or email delivery." %}

## Outlook (`outlook`)
Office 365/Graph modes use an existing Azure connection and mailbox/from address. Implemented tools send, reply, and read email through Microsoft Graph. Configure application permissions and mailbox access policy narrowly.

{% include screenshot.html file="fconn-outlook-office365.png" title="Outlook Office 365 — unsaved identity and mailbox fields" caption="Office 365 mode shows an unselected Azure connection, the native Managed identity type control, and a fictional mailbox on a non-resolving domain. Enabled is off and the draft is UNSAVED. The selected identity type does not establish that an identity exists; no consent, token acquisition, mailbox access, or mail delivery is demonstrated." %}

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Webhook configured but no message | use Send test, inspect endpoint policy, channel ownership, and status detail. |
| Slack auth fails | reissue token and verify workspace/app scopes. |
| SMTP fails | verify DNS, port, STARTTLS/SSL expectation, relay policy, sender, and credentials. |
| Graph mail/channel fails | test the Azure connection and verify admin consent plus target identifiers. |

## Related pages

- [Notifications]({{ site.baseurl }}/user-guide/automations/notifications/)
- [Credential handling]({{ site.baseurl }}/security/credential-handling/)
