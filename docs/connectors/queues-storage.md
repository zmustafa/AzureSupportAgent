---
layout: default
title: Queues & Storage
parent: Connectors
nav_order: 6
description: Configure Amazon S3/SQS and Azure Service Bus destinations for durable downstream processing.
permalink: /connectors/queues-storage/
feature_ids: [CONNECTOR:sqs, CONNECTOR:s3, CONNECTOR:servicebus]
---

# Queues and storage

The screenshots show native **unsaved, disabled** setup forms. Credential secrets are empty; destinations are fictional and entered domains are deliberately non-resolving. No connector was saved or tested, no AWS identity was verified, and no message or object was written.

## Amazon S3 (`s3`)
Static-key and assume-role modes support region, credentials/role, external ID, optional bucket, and key prefix. The tool writes one object, generating a timestamped key when omitted. Grant only `s3:PutObject` to the intended prefix.

{% include screenshot.html file="fconn-s3-keys.png" title="Amazon S3 keys mode — unsaved bucket and prefix" caption="Native UNSAVED setup shows region, empty access-key and session-token fields, and fictional bucket and key-prefix defaults. Enabled is off. These are destination inputs, not a bucket-policy or encryption editor; no STS call, PutObject authorization check, or object write occurred." %}

## Amazon SQS (`sqs`)
Static-key and assume-role modes support optional default queue URL. The tool sends a JSON/text message. FIFO queues require a group ID and can use a deduplication ID. Grant only `sqs:SendMessage` on the target queue.

{% include screenshot.html file="fconn-sqs-keys.png" title="Amazon SQS keys mode — unsaved queue fields" caption="Access key ID, secret access key, and optional session token are empty; Enabled is off. The UNSAVED default queue URL uses a non-resolving .invalid domain, not a valid AWS destination. FIFO group and deduplication IDs are call-time arguments, not controls in this form; no queue was contacted." %}

## Azure Service Bus (`servicebus`)
Use a connection string or namespace plus SAS policy name/key, with optional default queue. The tool sends one message and optional subject. Create a SAS policy limited to **Send** on the intended queue rather than namespace management authority.

{% include screenshot.html file="fconn-servicebus-connection-string.png" title="Service Bus connection-string mode — unsaved setup" caption="The entire connection-string credential remains blank; the visible example text is a placeholder. Only a fictional default queue is entered and Enabled is off. This UNSAVED mode has no separate namespace or SAS-key inputs, and no configuration test or message send occurred." %}

## AWS Security Hub
Security Hub is documented under [SIEM & Security Destinations]({{ site.baseurl }}/connectors/siem-security/).

## Safety and limitations

These connectors create durable external data and are not offered a generic Send test. Health probes verify configuration or identity without writing. Validate with a dedicated test queue/bucket through an approved run, configure lifecycle/dead-letter/retention policies at the destination, and avoid secrets in message bodies.

## Related pages

- [Queue and storage setup, including role and SAS modes]({{ site.baseurl }}/how-to/automations-connectors/connectors-queues-storage/)
- [Approvals]({{ site.baseurl }}/security/approvals/)
- [Auditing]({{ site.baseurl }}/security/auditing/)
