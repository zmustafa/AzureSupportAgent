"""Azure Policy governance toolkit.

A comprehensive, read-first toolkit for Azure Policy: inventory (definitions,
initiatives, assignments, exemptions), an effective-policy resolver across the scope
hierarchy, a compliance dashboard, and a set of AI + deterministic advisors
(promote-to-deny, what-if impact, exemption hygiene, remediation-gap, conflicts,
coverage gaps, deny-event triage, natural-language authoring, safe-rollout planning,
policy-as-code drift, explain-this-policy, and tag governance).

All Azure access is read-only via the existing command runner (Resource Graph +
``az policy state``). Nothing here mutates the tenant.
"""
