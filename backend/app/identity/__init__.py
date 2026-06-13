"""Identity posture aggregation: expiring credentials, ownerless app registrations,
conditional-access gaps, users without MFA, and Key Vault secret/certificate expiry.

The slow Microsoft Graph / Azure Resource Graph work in :mod:`collector` is wrapped by a
persistent server-side cache (:mod:`cache`) so the dashboard renders instantly and only
recomputes on a TTL miss or an explicit refresh."""
