"""Evidence Locker — forensic investigation snapshots.

Captures hash-stamped, immutable point-in-time bundles (inventory, properties, recent
changes, metrics, active findings, architecture + memory revisions) scoped to a workload /
subscription / selected resources. Snapshots are write-once: the full content blob is
written to ``.data/evidence/<id>.json`` and a SHA-256 over the canonicalized content is
recorded in the index AND the audit log. Diffs are field-level; snapshots attach to RCA
drafts and Jira/ServiceNow tickets (SHA carried into the body); read-only share links are
RBAC-gated. All storage is on the existing Azure Files JSON-registry volume — no new
dependency."""
