"""Backup & DR Coverage Gap Detector.

Audits each resource's backup and disaster-recovery posture against an editable, versioned
per-type protection reference: is backup enabled, is there a policy with adequate retention,
did the last job succeed recently, is there offsite/geo redundancy (incl. whether the backup
destination region differs from the resource region), is a DR pair configured and recently
drilled, is the resource encrypted (CMK/PMK) and soft-delete protected.

Third sibling to the AMBA (app/amba) and Telemetry (app/telemetry) coverage detectors;
reuses the same shell, cache, versioned reference registry, findings, approval-inbox and
ticketing patterns. Gaps roll up into the Reliability assessment pillar."""
