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
        ("insights.read", "View AI Insight Packs and their runs"),
        ("insights.write", "Create, edit, and AI-generate insight packs"),
        ("insights.run", "Run / test insight packs on demand"),
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
        ("radar.manage", "Curate Retirement Radar reference data"),
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
        ("iam.read", "View the Azure IAM access review"),
        ("iam.write", "Import scanner runs into the IAM access review and purge imported data"),
        ("iam.review", "Run IAM access review campaigns and record certification decisions"),
        ("iam.simulate", "Model IAM access changes before making them (read-only what-if)"),
        ("identity.read", "View identity security findings and app registrations"),
        ("entra.read", "View the Entra ID tenant posture, policies and identities"),
        ("entra.admin", "Refresh Entra collection, manage findings state and confirm break-glass accounts"),
        ("evidence.read", "View the Evidence Locker (investigation snapshots) and diffs"),
        ("evidence.write", "Create, attach, share, and export evidence snapshots"),
    ]),
    ("Incident response", [
        ("cases.read", "View durable case files and their timelines"),
        ("cases.write", "Open, update, attach to, and resolve case files"),
        ("investigate.read", "Investigate one identity: who it is, what it can reach, and how that changed"),
        ("investigate.activity", "Read a named identity's sign-in, audit and activity history (behavioural data)"),
    ]),
    ("Observability", [
        ("monitor.view", "View the Monitor dashboard"),
        ("coverage.read", "View monitoring, telemetry, and backup/DR coverage"),
        ("coverage.manage", "Curate coverage reference sets and approve change requests"),
        ("alert_analysis.read", "Analyze Azure Monitor alert overlaps, notification proliferation, and gaps"),
        ("alert_analysis.manage", "Record alert decisions and approve non-executing remediation plans"),
        ("alerts_manager.read", "View fired alerts, Action Groups, dependencies, and managed changes"),
        ("alerts_manager.alert_state_write", "Acknowledge, close, or reopen fired Azure alerts"),
        ("alerts_manager.action_group_write", "Draft Action Group creates and updates"),
        ("alerts_manager.rule_write", "Draft metric, log-query, and Activity Log alert-rule changes"),
        ("alerts_manager.advanced_rule_write", "Draft Smart Detector and Prometheus rule-group changes"),
        ("alerts_manager.bulk_write", "Draft bounded bulk alert-rule changes"),
        ("alerts_manager.amba_blueprint_write", "Manage AMBA blueprints and assignments"),
        ("alerts_manager.query_preview", "Run bounded metric and Log Analytics previews while authoring alerts"),
        ("alerts_manager.test_notifications", "Send real Action Group test notifications"),
        ("alerts_manager.delete", "Request Action Group deletion and rollback applied changes"),
        ("alerts_manager.approve", "Approve and apply Alerts Manager changes to Azure"),
        ("backup_manager.read", "View backup inventory, jobs, policies, vaults, DR readiness, and managed changes"),
        ("backup_manager.protect_write", "Draft enable / resume / stop-with-data-retained protection changes"),
        ("backup_manager.policy_write", "Draft backup-policy changes and auto-protect policy assignments"),
        ("backup_manager.vault_write", "Draft vault creation and vault security hardening changes"),
        ("backup_manager.ondemand", "Trigger on-demand backups and cancel running backup jobs"),
        ("backup_manager.drill_write", "Manage recovery drills and request Site Recovery test failovers"),
        ("backup_manager.reference_write", "Curate the backup failure knowledge base and protection baselines"),
        ("backup_manager.approve", "Approve and apply Backup Manager changes to Azure"),
        ("teleintel.read", "View App Insights correlation and KQL tools"),
    ]),
    ("Live diagnostics", [
        ("sandbox.exec", "Run diagnostic commands on sandbox troubleshooting VMs (vm_exec)"),
        ("netdiag.run", "Run private network and DNS reachability probes"),
    ]),
    ("Integrations", [
        ("connections.read", "View Azure connections and the capability / blind-spot matrix"),
        ("connections.manage", "Manage Azure tenant connections"),
        ("connectors.manage", "Manage connectors (Teams, Slack, Email, Jira, Grafana)"),
    ]),
    ("Administration", [
        ("settings.read", "View application settings"),
        ("settings.write", "Change application settings (general, tuning, providers, prompts, scoring)"),
        ("users.manage", "Manage users, groups, roles, identity providers, sessions"),
        ("audit.read", "View the audit log"),
        # Deliberately NOT folded into settings.write: restricting which networks may reach the
        # application has a completely different blast radius from tuning prompts or scoring,
        # and a misconfiguration here takes the whole deployment offline. Split read/manage so
        # an auditor can evidence the network policy without being able to change it.
        ("firewall.read", "View network access control (IP allowlist)"),
        ("firewall.manage", "Change network access control (IP allowlist)"),
        ("backup.manage", "Export / import the whole-tenant configuration"),
        ("demo.manage", "Load or remove demo data"),
    ]),
]

# Flat capability -> label lookup (canonical; used by the API guard + role validation).
PERMISSIONS: dict[str, str] = {
    key: label for _group, items in PERMISSION_GROUPS for key, label in items
}

ALL_PERMISSIONS: list[str] = list(PERMISSIONS.keys())

# --- Legacy permission keys -------------------------------------------------------
# Renamed capabilities, old key -> current key. ``seed_system_roles`` rewrites the built-in
# roles from code on every startup, but CUSTOM roles keep whatever was stored when they were
# created — so renaming a key silently strips access from every custom role that held it, and
# the symptom is an unexplained 403. ``require_permission`` accepts either key; the startup
# migration rewrites stored custom-role grants to the new one.
PERMISSION_ALIASES: dict[str, str] = {
    "rbac.read": "iam.read",  # /rbac screen renamed to /iam
}

# Reverse lookup used by the API guard: current key -> every legacy key that still satisfies it.
LEGACY_KEYS_FOR: dict[str, tuple[str, ...]] = {}
for _old, _new in PERMISSION_ALIASES.items():
    LEGACY_KEYS_FOR[_new] = (*LEGACY_KEYS_FOR.get(_new, ()), _old)


def canonical_permission(key: str) -> str:
    """Map a possibly-legacy permission key to its current name."""
    return PERMISSION_ALIASES.get(key, key)


def accepted_permission_keys(key: str) -> tuple[str, ...]:
    """Every key that satisfies ``key`` — the key itself plus any legacy spelling of it."""
    return (key, *LEGACY_KEYS_FOR.get(key, ()))

# Every read-only capability — the backbone of the auditor role.
READ_PERMISSIONS: list[str] = [p for p in ALL_PERMISSIONS if p.endswith(".read")]

# Capabilities reserved for full administrators (operator is denied exactly these).
_ADMIN_ONLY: set[str] = {
    "settings.write",
    "users.manage",
    "audit.read",
    "firewall.manage",
    "backup.manage",
    "demo.manage",
}

# Operator = everything an admin can do EXCEPT the security/config/admin-only surface.
_OPERATOR_PERMISSIONS: list[str] = [p for p in ALL_PERMISSIONS if p not in _ADMIN_ONLY]

# Auditor = read-only oversight across the whole product (+ audit log + monitor + chat).
# ``investigate.activity`` is named explicitly because it does NOT end in ``.read``: reading a
# named person's sign-in and audit history is behavioural data, deliberately held apart from the
# structural reads. The auditor is nevertheless the persona that feature exists for — proving who
# held privileged access in a period and what they did with it is the job.
_AUDITOR_PERMISSIONS: list[str] = list(dict.fromkeys(
    ["chat.use", "monitor.view", "audit.read", "investigate.activity", *READ_PERMISSIONS]
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
