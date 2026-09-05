---
layout: default
title: Configure messaging connectors
parent: Automations and connectors
grand_parent: How-to guides
nav_order: 66
description: Configure and verify Teams, Slack, Outlook, and SMTP email connectors.
permalink: /how-to/automations-connectors/connectors-messaging/
feature_ids: [CONNECTOR:teams, CONNECTOR:slack, CONNECTOR:outlook, CONNECTOR:email]
---

# Configure messaging and ChatOps connectors

All four types support real **Send test** delivery. Follow [connector lifecycle]({{ site.baseurl }}/how-to/automations-connectors/connector-lifecycle/) for enable, disable, edit, delete, and common troubleshooting.

The screenshots below show native **unsaved, disabled** forms. Credential secrets are empty, Azure connection selectors are unselected, and entered domains are non-resolving examples. No save, authentication, consent, test, or delivery is demonstrated; placeholder text is not a stored credential.

## Prerequisites

- `connectors.manage`.
- Webhook mode: a Teams Workflows/incoming webhook URL.
- Graph mode: an Azure connection permitted to post to the target Team and channel, plus Team ID and Channel ID.
- Incoming Webhook URL, or an installed Slack app bot token with `chat:write` and an optional default channel.
- An Azure connection whose Entra application has admin-consented Microsoft Graph `Mail.Send`; additional read/reply operations need their corresponding mail permissions.
- A licensed sender mailbox address.
- SMTP host/port, valid From address, and optional username/password.

## Route

- Open `/automations/connectors`.

## How to configure Microsoft Teams

{% include screenshot.html file="fconn-teams-graph.png" title="Teams Graph mode — unsaved connection and channel fields" caption="The native Graph form exposes Azure connection, Team ID, and Channel ID rather than a webhook URL. The connection list is empty and the selector is unselected; identifiers are fictional. Enabled is off and the draft is UNSAVED. No identity, consent, token acquisition, channel access, or message delivery is demonstrated." %}

1. Add **Microsoft Teams** and choose **Webhook** or **Microsoft Graph**.
2. For Webhook, paste the secret webhook URL. For Graph, select the Azure connection and enter Team ID and Channel ID.
3. Save disabled and select **Test**. Webhook mode only confirms a URL is stored; Graph mode requests a Graph token without posting.
4. Enable the connector and select **Send test**; this posts a real card to the channel.
5. Confirm the card in the intended Team/channel before selecting the connector in a task or notification rule.

**Expected result:** **Test** reports configured/token acquired; **Send test** creates a visible Teams message.

**Verification:** Check channel identity, card title/body, and test time in Teams.

## How to configure Slack

{% include screenshot.html file="fconn-slack-webhook.png" title="Slack webhook mode — unsaved fixed-destination setup" caption="The incoming webhook URL is an empty secret and Enabled is off. This UNSAVED native mode has no default-channel field: a real webhook determines its destination outside this application. No webhook was saved or contacted and no message was posted." %}

{% include screenshot.html file="fconn-slack-token.png" title="Slack token mode — unsaved channel selection fields" caption="Token mode adds an optional default channel alongside the empty Bot/User OAuth token field. The channel is fictional and Enabled is off. This is an UNSAVED native form, not an OAuth sign-in or app installation; no auth.test call or Slack message occurred." %}

1. Add **Slack** and choose **Incoming Webhook** or **Bot token**.
2. Enter the secret webhook URL, or bot token and default channel.
3. Save disabled and select **Test**. Webhook mode checks only that the URL is stored; token mode calls Slack `auth.test`.
4. Enable and select **Send test** to post a real Block Kit message.
5. Verify the workspace and channel, then use the connector in routing.

**Expected result:** Token Test identifies the authenticated Slack user; Send test posts a message.

**Verification:** Confirm the message in the intended channel and that the bot/app identity is expected.

## How to configure Microsoft Outlook

{% include screenshot.html file="fconn-outlook-office365.png" title="Outlook Office 365 — unsaved managed-identity fields" caption="Native Office 365 setup shows the Managed identity type control and a fictional connected mailbox. The Azure connection list is empty and its selector is unselected. Enabled is off and the draft is UNSAVED; choosing an identity type does not prove that identity exists. No token, mailbox access, or mail delivery is demonstrated." %}

{% include screenshot.html file="fconn-outlook-graph.png" title="Outlook Graph — unsaved sender fields" caption="Graph mode shows an unselected Azure connection and a fictional From mailbox on a non-resolving domain. Unlike Office 365 mode, this form has no managed-identity-type selector; neither form has a recipient field. Enabled is off and the draft is UNSAVED. No authentication, mailbox access, or email send occurred." %}

1. Add **Microsoft Outlook**, choose Office 365 or Graph mode, select the Azure connection, and enter the sender mailbox.
2. Scope app-only mailbox access according to organizational policy.
3. Save disabled and select **Test**; it acquires a Graph token but sends no email.
4. Enable and select **Send test**. The allowlisted action invokes the real send tool, but the connector form has no recipient field; if no recipient is supplied by the calling context, the status correctly reports that a recipient is required.
5. Verify real delivery through an approved workflow that supplies a safe recipient before enabling automated routing.

**Expected result:** Test reports a Graph token. Send test attempts the real send path and reports success or the missing-recipient/provider error.

**Verification:** For an approved workflow with a recipient, check the recipient mailbox and mail trace/audit facilities available to the administrator.

## How to configure Email (SMTP)

{% include screenshot.html file="fconn-email-smtp.png" title="SMTP — unsaved host, sender, and credential fields" caption="Native UNSAVED setup shows a non-resolving host and sender domain, port 587, a fictional username, and an empty password with Enabled off. There is no recipient field or TLS toggle. No SMTP connection, negotiated TLS session, authentication, or message delivery is demonstrated." %}

1. Add **Email (SMTP)** and enter host, port, From address, and optional credentials.
2. Match transport to the server: port 587 commonly uses STARTTLS; 465 uses SSL; only use unencrypted transport when explicitly approved.
3. Save disabled and select **Test**; it only confirms that an SMTP host is stored.
4. Enable and select **Send test**. The allowlisted action invokes the real SMTP tool, but the connector form has no recipient field; if no recipient is supplied by the calling context, the status correctly reports that a recipient is required.
5. Verify real delivery through an approved workflow that supplies a safe recipient before selecting it for automation.

**Expected result:** Test reports SMTP host configured. Send test attempts the real SMTP path and reports success or the missing-recipient/provider error.

**Verification:** Check recipient inbox, spam/quarantine, sender, and server delivery logs.

## Safety and rollback

Use a dedicated app and narrow channel access. Delete the test message if allowed, disable the connector, and revoke/rotate exposed tokens or webhooks.

App-only mail permission can be broad. Disable the connector, remove unwanted mail, and revoke or narrow application access when needed.

Prefer TLS and a least-privilege relay identity. Remove the test message, disable the connector, and rotate exposed credentials.

Treat the webhook URL as a secret. Delete the test post if policy requires, disable the connector, and rotate a disclosed webhook or Graph credential.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| A test message reaches the wrong channel or mailbox | Recheck the configured Team/channel, Slack channel, or recipient and use a dedicated non-production destination before sending again. |
| A webhook Test success does not check reachability | For Graph errors, verify the Azure connection, Team/channel IDs, and application permissions. For delivery errors, confirm the app/workflow may post to that channel. |
| [Notifications]({{ site.baseurl }}/how-to/automations-connectors/notifications/) | Review connector configuration and retry. |
| [Connector lifecycle]({{ site.baseurl }}/how-to/automations-connectors/connector-lifecycle/) | Review connector configuration and retry. |

## Related docs

- [Connector lifecycle]({{ site.baseurl }}/how-to/automations-connectors/connector-lifecycle/)
