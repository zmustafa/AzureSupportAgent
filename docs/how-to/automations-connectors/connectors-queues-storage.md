---
layout: default
title: Configure queue and storage connectors
parent: Automations and connectors
grand_parent: How-to guides
nav_order: 69
description: Configure and safely verify Azure Service Bus, Amazon SQS, and Amazon S3 connectors.
permalink: /how-to/automations-connectors/connectors-queues-storage/
---

# Configure queue and storage connectors

These types do not expose **Send test** in the UI/API because it would write a queue message or storage object.

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

1. Add **Azure Service Bus Queue** and choose **Connection string** or **SAS**.
2. Enter the credentials and a default queue when calls should not supply one, then save disabled.
3. Select **Test**; it only checks that mode-required credential fields are present.
4. Enable after independently confirming queue existence and Send scope.
5. There is no Send test UI/API support. If end-to-end proof is required, send a controlled message through an approved workflow to a disposable/test queue and consume or remove it through normal queue processing.

**Expected result:** Test reports configured without connecting or sending a message.

**Verification:** Confirm namespace, queue, policy Send permission, and—only for a controlled workflow—the message in queue metrics/receiver logs.

## How to configure Amazon SQS

1. Add **Amazon SQS**, choose **Keys** or **Role**, enter AWS identity fields and queue URL, then save disabled.
2. Select **Test**; it calls STS `GetCallerIdentity` and sends no message.
3. Confirm the returned ARN/account and enable.
4. There is no Send test UI/API support. Use an approved workflow against a test queue if delivery proof is required.
5. For FIFO queues, ensure the workflow supplies an appropriate message group; deduplication behavior may hide apparent duplicates.

**Expected result:** Test identifies the AWS principal without writing to SQS.

**Verification:** Confirm ARN, queue region/account, IAM resource scope, and controlled-message receipt/consumption when performed.

## How to configure Amazon S3

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
| Unexpected result | Re-check route, permissions, and latest refresh state before retrying. |
| A successful Test proves only field presence | Check namespace FQDN, queue name, policy scope, key, firewall/private networking, and consumer dead-letter behavior. |
| [Connector lifecycle]({{ site.baseurl }}/how-to/automations-connectors/connector-lifecycle/) | Review connector configuration and retry. |

## Related docs

- [Connector lifecycle]({{ site.baseurl }}/how-to/automations-connectors/connector-lifecycle/)
