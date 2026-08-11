---
id: application-credential-hygiene
name: Application credential hygiene
description: Review application and service-principal ownership, credentials, permissions, and expiration risk without confusing registrations with enterprise applications.
bundles: entra.applications, entra.users, entra.groups
---
# Application credential hygiene

1. Resolve both the application registration and corresponding service principal when present.
2. Inventory secrets and certificates with expiration, owner coverage, and sign-in audience.
3. Separate delegated permissions from application permissions and flag high-impact Graph application roles.
4. Identify ownerless applications and credentials that are expired or approaching expiry.
5. Treat Microsoft-managed and managed-identity service principals according to platform ownership rules.
6. State whether evidence came from live Graph, cache, or a capped inventory.
7. Recommend rotation, owner assignment, permission reduction, and workload validation; never delete or rotate credentials without an explicit approved write.
