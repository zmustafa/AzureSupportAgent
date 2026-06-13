"""Telemetry Coverage (Diagnostic Settings / Log Coverage Auditor).

Audits each resource's Azure Monitor diagnostic settings against an editable, versioned
reference of recommended log/metric categories per resource type: are any settings
present, are the recommended categories enabled, and do logs ship to an admin-approved
Log Analytics workspace (vs. drift to an unknown destination)? Gaps roll up per workload,
register as Operations-pillar findings, and export as Bicep or an Azure Policy assignment.

Pairs with the AMBA Monitoring Coverage feature (app/amba): alerts without telemetry are
useless; telemetry without alerts is silent. Reuses the same shell, cache, reference-
registry, findings and approval-inbox patterns."""
