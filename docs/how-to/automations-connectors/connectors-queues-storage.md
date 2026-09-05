---
layout: default
title: Configure queue and storage connectors
parent: Automations and connectors
grand_parent: How-to guides
nav_order: 69
description: Configure and safely verify Azure Service Bus, Amazon SQS, and Amazon S3 connectors.
permalink: /how-to/automations-connectors/connectors-queues-storage/
feature_ids: [CONNECTOR:sqs, CONNECTOR:s3, CONNECTOR:servicebus]
---

# Configure queue and storage connectors

These types do not expose **Send test** in the UI/API because it would write a queue message or storage object.

The screenshots show native **unsaved, disabled** setup and review screens. Credential secrets are empty; account, role, and destination values are fictional, and entered domains are deliberately non-resolving. No save, authentication, test, queue message, or object write is demonstrated. In real Role mode, blank base keys may use the host credential chain; no STS call was made for these examples.

## Prerequisites

- `connectors.manage`.
- A queue and either a namespace connection string with Send rights or namespace FQDN, SAS policy name, and SAS key.
- AWS region, queue URL, and static keys or an assumable role.
- `sqs:SendMessage` on the target queue for real use.
- AWS region, default bucket/key prefix, and static keys or an assumable role.
- `s3:PutObject` on the intended prefix for real use.

## Route

- Open `/automations/connectors`.

## How to configure Azure Service Bus Queue

{% include screenshot.html file="fconn-servicebus-connection-string.png" title="Service Bus connection-string mode — unsaved setup" caption="The entire connection-string secret is blank and only a fictional default queue is entered. Visible connection-string text is a placeholder, not a stored credential. Enabled is off and the native form is UNSAVED. This mode has no separate namespace or SAS-key inputs; no connection or message send occurred." %}

{% include screenshot.html file="fconn-servicebus-sas.png" title="Service Bus SAS mode — unsaved namespace and policy fields" caption="SAS mode exposes namespace FQDN, policy name, an empty SAS key, and default queue. The namespace is deliberately non-resolving and not a valid Azure namespace; the policy is fictional and its Send rights are unverified. Enabled is off and the draft is UNSAVED. No test or message send occurred." %}

1. Add **Azure Service Bus Queue** and choose **Connection string** or **SAS**.
2. Enter the credentials and a default queue when calls should not supply one, then save disabled.
3. Select **Test**; it only checks that mode-required credential fields are present.
4. Enable after independently confirming queue existence and Send scope.
5. There is no Send test UI/API support. If end-to-end proof is required, send a controlled message through an approved workflow to a disposable/test queue and consume or remove it through normal queue processing.

**Expected result:** Test reports configured without connecting or sending a message.

**Verification:** Confirm namespace, queue, policy Send permission, and—only for a controlled workflow—the message in queue metrics/receiver logs.

## How to configure Amazon SQS

{% include screenshot.html file="fconn-sqs-keys.png" title="Amazon SQS keys mode — unsaved queue setup" caption="Native UNSAVED setup shows region, empty access key ID, secret access key, and session token fields, plus a fictional queue URL. Its .invalid domain is not a valid AWS destination and Enabled is off. FIFO message-group and deduplication IDs are call-time arguments, not form controls; no STS call or queue access occurred." %}

{% include screenshot.html file="fconn-sqs-role.png" title="Amazon SQS role mode — unsaved review" caption="Native Review + add shows fictional role ARN, external ID, and FIFO queue URL with Disabled selected. Em dashes represent empty base credentials, not stored secrets. The draft is UNSAVED and Add connector was not clicked; no role assumption, queue discovery, message send, or deduplication verification occurred." %}

1. Add **Amazon SQS**, choose **Keys** or **Role**, enter AWS identity fields and queue URL, then save disabled.
2. Select **Test**; it calls STS `GetCallerIdentity` and sends no message.
3. Confirm the returned ARN/account and enable.
4. There is no Send test UI/API support. Use an approved workflow against a test queue if delivery proof is required.
5. For FIFO queues, ensure the workflow supplies an appropriate message group; deduplication behavior may hide apparent duplicates.

**Expected result:** Test identifies the AWS principal without writing to SQS.

**Verification:** Confirm ARN, queue region/account, IAM resource scope, and controlled-message receipt/consumption when performed.

## How to configure Amazon S3

{% include screenshot.html file="fconn-s3-keys.png" title="Amazon S3 keys mode — unsaved bucket and prefix setup" caption="Native UNSAVED setup shows region and fictional bucket and key-prefix defaults; access key ID, secret access key, and session token are empty. Enabled is off. There is no bucket-policy or encryption selector in this form, and no STS call, permission check, or object write occurred." %}

{% include screenshot.html file="fconn-s3-role.png" title="Amazon S3 role mode — unsaved destination review" caption="Native Review + add shows all seven fields, including fictional role, external ID, bucket, and prefix values. Empty base credentials appear as em dashes and the draft is Disabled and UNSAVED. Add connector was not clicked; this is not evidence of role assumption, PutObject authorization, or a written object." %}

1. Add **Amazon S3**, choose **Keys** or **Role**, enter AWS identity fields, bucket, and optional prefix, then save disabled.
2. Select **Test**; it calls STS `GetCallerIdentity` and writes no object.
3. Confirm the returned ARN/account and enable.
4. There is no Send test UI/API support. If required, write a uniquely named harmless object through an approved workflow to a test prefix.
5. Verify metadata/content, then delete the controlled object under bucket policy.

**Expected result:** Test identifies the AWS principal without writing to S3.

**Verification:** Confirm ARN, bucket region/account, intended prefix, encryption requirements, and controlled object only when deliberately created.

## Safety and rollback

Prefer role mode and a test queue. Disable the connector, revoke access, and consume/purge only controlled messages under queue policy.

Scope `s3:PutObject` to a dedicated prefix. Disable the connector, revoke access, and remove controlled objects or versions according to retention policy.

Avoid namespace-wide manage keys where a Send-only policy suffices. Disable the connector, revoke the SAS key/policy, and drain controlled test messages safely.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| Message or object lands in the wrong queue, topic, bucket, or prefix | Disable the connector, verify the destination identifier and credential scope, then remove only the controlled test artifact under its retention policy. |
| A successful Test proves only field presence | Check namespace FQDN, queue name, policy scope, key, firewall/private networking, and consumer dead-letter behavior. |
| [Connector lifecycle]({{ site.baseurl }}/how-to/automations-connectors/connector-lifecycle/) | Review connector configuration and retry. |

## Related docs

- [Connector lifecycle]({{ site.baseurl }}/how-to/automations-connectors/connector-lifecycle/)
