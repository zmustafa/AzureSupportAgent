"""Permission catalog and built-in system roles.

Permissions are coarse capability strings checked by ``require_permission`` in the API.
Roles bundle permissions; groups bundle roles. A user's effective permissions are the
union across their directly-assigned roles and the roles of every group they belong to.

The catalog is organized into ordered groups (mirroring the product's navigation) so the
role editor can render readable sections. ``PERMISSIONS`` (flat key -> label) and
``ALL_PERMISSIONS`` are derived from the groups and remain the canonical lookups used by
the API guard layer.
"""
from __future__ import annotations

# --- Permission catalog ----------------------------------------------------------
# Ordered groups of (capability key -> human label). Adding a feature? Add its
# permission here and gate its router with ``require_permission`` so the new capability
# shows up in the role editor and is actually enforced.
PERMISSION_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Agent", [
        ("chat.use", "Use the chat / run the agent"),
    ]),
    ("Automation", [
        ("agents.read", "View sub agents"),
        ("agents.write", "Create, edit, enable/disable, import/export agents"),
        ("tasks.read", "View scheduled tasks"),
        ("tasks.write", "Create / edit / delete scheduled tasks"),
        ("tasks.run", "Run scheduled tasks on demand"),
        ("workbooks.read", "View workbooks"),
        ("workbooks.write", "Create, edit, and run workbooks"),
        ("playbooks.read", "View playbooks"),
        ("playbooks.write", "Create, edit, and run playbooks"),
        ("notifications.read", "View the in-app notification center"),
        ("notifications.manage", "Manage notification rules and routing"),
    ]),
    ("Workloads & design", [
        ("workloads.read", "View workloads"),
        ("workloads.write", "Create / edit / delete workloads"),
        ("architectures.read", "View architectures"),
        ("architectures.write", "Create, edit, and AI-generate architectures"),
        ("missions.read", "View Mission Control missions"),
        ("missions.run", "Launch and manage mission sweeps"),
    ]),
    ("Ownership", [
        ("ownership.read", "View ownership (owners, assignments, coverage, my estate)"),
        ("ownership.write", "Assign / transfer ownership, manage owners and teams"),
    ]),
    ("Estate insight", [
        ("inventory.read", "View the resource inventory"),
        ("graph.read", "View the knowledge graph"),
        ("changeexplorer.read", "View the Azure Workload Change Explorer"),
        ("reservations.read", "View reservation expiry tracking"),
        ("perfprofile.read", "View the workload performance heatmap"),
        ("radar.read", "View the Retirement Radar"),
        ("quota.read", "View the Quota Monitor (usage, limits, risk)"),
        ("quota.run", "Run quota scans"),
    ]),
    ("Tagging", [
        ("tagintel.read", "View Tag Intelligence (census, hygiene, coverage, cost)"),
        ("tagintel.write", "Generate tag remediation, policies, and IaC"),
    ]),
    ("Governance & compliance", [
        ("assessments.read", "View assessments, runs, and compliance reports"),
        ("assessments.run", "Run assessments; manage waivers, custom checks, and schedules"),
        ("policy.read", "View Azure Policy inventory and compliance"),
        ("policy.write", "Generate policy rollouts and IaC"),
        ("rbac.read", "View the Azure RBAC access review"),
        ("identity.read", "View identity security findings and app registrations"),
        ("evidence.read", "View the Evidence Locker (investigation snapshots) and diffs"),
        ("evidence.write", "Create, attach, share, and export evidence snapshots"),
    ]),
    ("Observability", [
        ("monitor.view", "View the Monitor dashboard"),
        ("coverage.read", "View monitoring, telemetry, and backup/DR coverage"),
        ("coverage.manage", "Curate coverage reference sets and approve change requests"),
        ("teleintel.read", "View App Insights correlation and KQL tools"),
    ]),
    ("Live diagnostics", [
        ("sandbox.exec", "Run diagnostic commands on sandbox troubleshooting VMs (vm_exec)"),
        ("netdiag.run", "Run private network and DNS reachability probes"),
    ]),
    ("Integrations", [
        ("connections.manage", "Manage Azure tenant connections"),
        ("connectors.manage", "Manage connectors (Teams, Slack, Email, Jira, Grafana)"),
    ]),
    ("Administration", [
        ("settings.read", "View application settings"),
        ("settings.write", "Change application settings (general, tuning, providers, prompts, scoring)"),
        ("users.manage", "Manage users, groups, roles, identity providers, sessions"),
        ("audit.read", "View the audit log"),
        ("backup.manage", "Export / import the whole-tenant configuration"),
        ("demo.manage", "Load or remove demo data"),
    ]),
]

# Flat capability -> label lookup (canonical; used by the API guard + role validation).
PERMISSIONS: dict[str, str] = {
    key: label for _group, items in PERMISSION_GROUPS for key, label in items
}

ALL_PERMISSIONS: list[str] = list(PERMISSIONS.keys())

# Every read-only capability — the backbone of the auditor role.
READ_PERMISSIONS: list[str] = [p for p in ALL_PERMISSIONS if p.endswith(".read")]

# Capabilities reserved for full administrators (operator is denied exactly these).
_ADMIN_ONLY: set[str] = {
    "settings.write",
    "users.manage",
    "audit.read",
    "backup.manage",
    "demo.manage",
}

# Operator = everything an admin can do EXCEPT the security/config/admin-only surface.
_OPERATOR_PERMISSIONS: list[str] = [p for p in ALL_PERMISSIONS if p not in _ADMIN_ONLY]

# Auditor = read-only oversight across the whole product (+ audit log + monitor + chat).
_AUDITOR_PERMISSIONS: list[str] = list(dict.fromkeys(
    ["chat.use", "monitor.view", "audit.read", *READ_PERMISSIONS]
))

# Standard user = chat plus the self-service reads.
_USER_PERMISSIONS: list[str] = [
    "chat.use",
    "ownership.read",
    "workloads.read",
    "architectures.read",
]

# --- Built-in system roles --------------------------------------------------------
# (name, description, permissions). Seeded on startup; cannot be deleted.
SYSTEM_ROLES: list[tuple[str, str, list[str]]] = [
    ("admin", "Full administrator — all permissions.", list(ALL_PERMISSIONS)),
    (
        "operator",
        "Run the agent and operate every feature, but not security, settings, or admin config.",
        _OPERATOR_PERMISSIONS,
    ),
    (
        "auditor",
        "Read-only oversight across the product, plus the audit log and monitor dashboard.",
        _AUDITOR_PERMISSIONS,
    ),
    (
        "user",
        "Standard user — chat plus read access to their own workloads, architectures, and ownership.",
        _USER_PERMISSIONS,
    ),
    (
        "noaccess",
        "No access — blocked from the entire application. Used as the safe default for "
        "newly auto-provisioned SSO users until an admin grants them a real role.",
        [],
    ),
]

SYSTEM_ROLE_NAMES = {name for name, _, _ in SYSTEM_ROLES}

# A user whose ONLY role is this (or who has no roles at all) gets zero permissions and is
# blocked from every API path except the minimal self/logout allowlist (see core.security).
NO_ACCESS_ROLE = "noaccess"


def role_rank(name: str) -> int:
    """Ordering used to pick a user's primary display role (highest wins)."""
    order = {"noaccess": -1, "user": 0, "auditor": 1, "operator": 2, "admin": 3}
    return order.get(name, 0)
