---
layout: default
title: Queues & Storage
parent: Connectors
nav_order: 6
description: Configure Amazon S3/SQS and Azure Service Bus destinations for durable downstream processing.
permalink: /connectors/queues-storage/
---

# Queues and storage

## Amazon S3 (`s3`)
Static-key and assume-role modes support region, credentials/role, external ID, optional bucket, and key prefix. The tool writes one object, generating a timestamped key when omitted. Grant only `s3:PutObject` to the intended prefix.

## Amazon SQS (`sqs`)
Static-key and assume-role modes support optional default queue URL. The tool sends a JSON/text message. FIFO queues require a group ID and can use a deduplication ID. Grant only `sqs:SendMessage` on the target queue.

## Azure Service Bus (`servicebus`)
Use a connection string or namespace plus SAS policy name/key, with optional default queue. The tool sends one message and optional subject. Create a SAS policy limited to **Send** on the intended queue rather than namespace management authority.

## AWS Security Hub
Security Hub is documented under [SIEM & Security Destinations]({{ site.baseurl }}/connectors/siem-security/).

## Safety and limitations

These connectors create durable external data and are not offered a generic Send test. Health probes verify configuration or identity without writing. Validate with a dedicated test queue/bucket through an approved run, configure lifecycle/dead-letter/retention policies at the destination, and avoid secrets in message bodies.

## Related pages

- [Approvals]({{ site.baseurl }}/security/approvals/)
- [Auditing]({{ site.baseurl }}/security/auditing/)
