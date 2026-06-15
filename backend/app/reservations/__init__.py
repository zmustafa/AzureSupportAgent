"""Azure Reservations Monitor.

Tenant-scoped reservation-expiry tracking + a weekly digest, reimplementing the original
"Weekly Digest of Azure Reservations" Logic App inside the app (scope picker, server-side
cache, in-app + email delivery, configurable schedule)."""
