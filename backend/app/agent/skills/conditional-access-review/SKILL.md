---
id: conditional-access-review
name: Conditional Access review
description: Determine which policies apply to a user/application scenario and distinguish block, challenge, session restriction, report-only, and exclusions.
bundles: entra.conditional_access, entra.users, entra.applications, entra.authentication
---
# Conditional Access review

1. Resolve the user, application, device, location, client type, authentication flow, and risk assumptions.
2. Prefer `ca_evaluate` for the product's enriched policy model and explicit verdict.
3. Include policy state: enabled, report-only, or disabled. Never describe report-only as enforcement.
4. Distinguish grant controls from session controls; session controls do not create a hard block.
5. Account for include/exclude users, groups, roles, apps, platforms, locations, client apps, and authentication flows.
6. Name coverage gaps instead of converting unreadable conditions into an allow verdict.
7. Present applicable policies and decisive conditions as evidence, then suggest a safe simulator change before any write.
