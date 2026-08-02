"""RBAC (access review) feature: port of the standalone all-azure-access scanner.

Discovers and normalizes WHO can access WHAT across Azure RBAC (control + data plane), Entra
directory roles, group-derived (transitive) access, service-principal ownership and PIM, into
one comparable 46-column grid (:mod:`schema`). Per-scope server-side cache (:mod:`cache`) lets
a single subscription/management-group be refreshed independently while the rest stay served
from the last-good snapshot."""
