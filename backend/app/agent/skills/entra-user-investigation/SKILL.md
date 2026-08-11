---
id: entra-user-investigation
name: Entra user investigation
description: Build an evidence-backed identity dossier covering account state, roles, MFA, Conditional Access, and permitted activity.
bundles: entra.users, entra.authentication, entra.roles, entra.conditional_access, entra.audit
---
# Entra user investigation

1. Resolve the person unambiguously by UPN or object id; never infer identity from a display-name collision.
2. Prefer `identity_investigate` for the cached, audited dossier before issuing low-level Graph calls.
3. Separate active role assignments, PIM eligibility, and live activations. Eligibility is not standing access.
4. Report MFA registration coverage separately from a confirmed absence of methods.
5. Evaluate applicable Conditional Access policy state, exclusions, and grant/session controls.
6. Read behavioral history only when the caller is authorized for `investigate.activity`.
7. State collection freshness, missing permissions, licensing gaps, and sampling/caps.
8. End with evidence, risk, and proportionate next actions; do not make identity changes unless explicitly requested and approved.
