"""Permission catalog and built-in system roles.

Permissions are coarse capability strings checked by ``require_permission`` in the API.
Roles bundle permissions; groups bundle roles. A user's effective permissions are the
union across their directly-assigned roles and the roles of every group they belong to.
"""
from __future__ import annotations

# --- Permission catalog (capability -> human label) -------------------------------
PERMISSIONS: dict[str, str] = {
    "chat.use": "Use the chat / run the agent",
    "agents.read": "View sub agents",
    "agents.write": "Create, edit, enable/disable, import/export agents",
    "tasks.read": "View scheduled tasks",
    "tasks.write": "Create / edit / delete scheduled tasks",
    "tasks.run": "Run scheduled tasks on demand",
    "connectors.manage": "Manage connectors (Teams, Email, Jira, Grafana)",
    "connections.manage": "Manage Azure tenant connections",
    "sandbox.exec": "Run diagnostic commands on sandbox troubleshooting VMs (vm_exec)",
    "monitor.view": "View the Monitor dashboard",
    "assessments.read": "View assessments, runs, and compliance reports",
    "assessments.run": "Run assessments; manage waivers, custom checks, and schedules",
    "evidence.read": "View the Evidence Locker (investigation snapshots) and diffs",
    "evidence.write": "Create, attach, share, and export evidence snapshots",
    "settings.read": "View application settings",
    "settings.write": "Change application settings (general, tuning, providers)",
    "users.manage": "Manage users, groups, roles, identity providers, sessions",
    "audit.read": "View the audit log",
}

ALL_PERMISSIONS: list[str] = list(PERMISSIONS.keys())

# --- Built-in system roles --------------------------------------------------------
# (name, description, permissions). Seeded on startup; cannot be deleted.
SYSTEM_ROLES: list[tuple[str, str, list[str]]] = [
    ("admin", "Full administrator — all permissions.", list(ALL_PERMISSIONS)),
    (
        "operator",
        "Run the agent and manage automations, but not security settings.",
        [
            "chat.use",
            "agents.read",
            "agents.write",
            "tasks.read",
            "tasks.write",
            "tasks.run",
            "connectors.manage",
            "connections.manage",
            "sandbox.exec",
            "monitor.view",
            "assessments.read",
            "assessments.run",
            "evidence.read",
            "evidence.write",
            "settings.read",
        ],
    ),
    (
        "auditor",
        "Read-only oversight: audit log, monitor, settings, and chat.",
        [
            "chat.use",
            "monitor.view",
            "settings.read",
            "audit.read",
            "agents.read",
            "tasks.read",
            "assessments.read",
            "evidence.read",
        ],
    ),
    ("user", "Standard user — chat access only.", ["chat.use"]),
]

SYSTEM_ROLE_NAMES = {name for name, _, _ in SYSTEM_ROLES}


def role_rank(name: str) -> int:
    """Ordering used to pick a user's primary display role (highest wins)."""
    order = {"user": 0, "auditor": 1, "operator": 2, "admin": 3}
    return order.get(name, 0)
