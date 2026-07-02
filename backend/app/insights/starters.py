"""Starter Insight Packs — the curated catalog that ships with the application.

These seed the library and populate the "Templates" gallery. Each is a complete,
scope-agnostic pack: point it at a tenant / subscription / workload and it gathers
deterministic Azure change + telemetry data, an LLM reasons over it, and it notifies the
owner only when something material happened. The instructions bodies use ``{{scope_label}}``
and ``{{lookback_hours}}`` placeholders the runtime fills in per assignment.

Every pack follows the same "yesterday vs today" reporting pattern: what was **added**,
**removed** and **changed**, **who** changed it, which **workload** it belongs to, the
**risk**, and a **recommended action** — and concludes ``nothing_notable`` on a quiet day.

Bump ``SEED_VERSION`` whenever this catalog changes so existing installs re-seed the
built-ins (see ``registry._ensure_seeded``). User-created packs are never touched.
"""
from __future__ import annotations

from typing import Any

# Bump when STARTERS below change so shipped built-ins refresh on existing installs.
SEED_VERSION = 2

# Shared reporting-pattern reminder appended to every pack's instructions.
_PATTERN = (
    "\n\nFor each finding that matters, give: what changed (added / removed / modified), "
    "**who** did it (if known), which **workload** it affects, a one-line **why it matters**, "
    "the **risk level**, and a concrete **recommended action**. Lead with a one-line headline, "
    "then the top findings as bullets, then the detailed table. Group related changes and "
    "de-prioritize routine noise. If nothing in this window is materially notable, set the "
    "verdict to `nothing_notable` and keep the summary to a single reassuring line."
)

_COLS = ["time", "workload", "change", "risk", "owner", "recommended_action"]


def _pack(**kw: Any) -> dict[str, Any]:
    """Build a starter with sensible shared defaults, then apply overrides."""
    p: dict[str, Any] = {
        "supported_scopes": ["workload", "subscription", "tenant"],
        "lookback_hours": 24,
        "filters": {"categories": [], "operations": [], "min_risk": "low"},
        "materiality": {"notify_threshold": "notable", "always_notify_if": []},
        "output": {"format": ["bullets", "table"], "table_columns": _COLS},
        "builtin": True,
    }
    p.update(kw)
    p["instructions"] = (kw.get("instructions", "").rstrip() + _PATTERN)
    return p


STARTERS: list[dict[str, Any]] = [
    # 1 — Daily Change Detection ------------------------------------------------------------
    _pack(
        id="daily-tenant-change-report",
        name="Daily Change Detection",
        icon="📋",
        category="change",
        description="Everything that changed across the scope in the last day — the answer to “what changed since yesterday?”.",
        sources=["change_explorer"],
        filters={"categories": [], "operations": [], "min_risk": "low"},
        materiality={"notify_threshold": "notable",
                     "always_notify_if": ["public_exposure", "public_network_access", "public_ip",
                                          "security_control_deleted", "lock_removed", "logging_disabled",
                                          "policy_exemption"]},
        instructions=(
            "Give a busy platform owner a plain-language digest of **everything that changed** "
            "across **{{scope_label}}** in the last {{lookback_hours}} hours.\n\n"
            "Call out explicitly: **new resources created** (shadow IT, accidental or unknown "
            "deployments), **resources deleted** (potential outages), **resources moved** to "
            "another resource group or subscription (breaks RBAC / policy / alerts / ownership), "
            "**SKU or size changes** (cost / performance impact), **tags changed or removed** "
            "(cost-allocation and ownership breaks), **resource locks removed** from protected "
            "infra, **diagnostic settings removed** (reduced logging visibility), **policy "
            "exemptions created** (governance bypass), and **new private or public endpoints** "
            "(data-path / exposure impact).\n\n"
            "Flag anything that looks unintended or was done by an unusual actor or at an unusual "
            "time. Treat expected, routine changes as low signal."
        ),
    ),
    # 2 — Network Security Daily ------------------------------------------------------------
    _pack(
        id="network-exposure-watch",
        name="Network Security Daily",
        icon="🛡️",
        category="security",
        description="Did my network exposure change in the last 24 hours? NSG, public IP, firewall, route and DNS changes.",
        sources=["change_explorer"],
        filters={"categories": ["Network", "DNS", "Security"], "operations": [], "min_risk": "medium"},
        materiality={"notify_threshold": "notable",
                     "always_notify_if": ["public_exposure", "public_ip", "public_network_access",
                                          "security_control_deleted"]},
        instructions=(
            "Review the last {{lookback_hours}} hours of **network and security** changes for "
            "**{{scope_label}}** and answer one question: **did our exposure or segmentation get "
            "weaker?**\n\n"
            "Prioritize: a **new NSG or NSG rule**, a rule flipped from **deny to allow**, any rule "
            "allowing **`0.0.0.0/0` inbound** or **RDP/SSH from the Internet**, a **new public IP** "
            "(or one attached to a NIC, App Gateway, Load Balancer, Firewall or Bastion), **route "
            "table / UDR changes** that bypass an NVA or firewall, a **subnet losing its NSG or "
            "route-table association**, **Azure Firewall rule-collection changes**, a **new App "
            "Gateway listener**, **WAF disabled or set to Detection mode**, and **private DNS zone "
            "links added or removed**.\n\n"
            "De-prioritize routine, safe changes: added *deny* rules, tag edits, or changes wholly "
            "inside an already-private subnet."
        ),
    ),
    # 3 — VM Operations ---------------------------------------------------------------------
    _pack(
        id="vm-operations-daily",
        name="VM Operations",
        icon="🖥️",
        category="operations",
        description="Which machines changed, restarted, lost monitoring, or became risky in the last day?",
        sources=["change_explorer", "backup"],
        filters={"categories": ["Compute", "Network", "Security"], "operations": [], "min_risk": "low"},
        materiality={"notify_threshold": "notable",
                     "always_notify_if": ["backup_unprotected", "public_ip", "security_control_deleted"]},
        instructions=(
            "Report on **virtual-machine operations and risk** for **{{scope_label}}** over the last "
            "{{lookback_hours}} hours.\n\n"
            "Surface: VMs **rebooted**, **stopped / deallocated**, or **started outside a maintenance "
            "window**; VMs **resized**; **extensions installed or removed**; the **Azure Monitor / "
            "Log Analytics agent missing** or a **heartbeat gap**; **backup or patch jobs that "
            "failed**; **disks added, detached, or an OS disk swapped**; **accelerated networking "
            "disabled**; a **public IP added to a VM NIC** or an **NSG change on a VM subnet/NIC**; "
            "and standing risks such as a VM **running without backup**, **without Defender "
            "coverage**, on an **old OS image**, or with **availability-set / zone placement risk**.\n\n"
            "Use the Backup & DR signal to confirm which machines are actually protected. Treat "
            "expected, scheduled maintenance as low signal."
        ),
    ),
    # 4 — Identity & RBAC -------------------------------------------------------------------
    _pack(
        id="privileged-access-watch",
        name="Identity & RBAC",
        icon="🔐",
        category="identity",
        description="Did privileged access expand yesterday? Role grants, new identities, secrets and MFA posture.",
        sources=["change_explorer", "rbac", "identity"],
        filters={"categories": ["Identity", "RBAC", "PIM", "AppRegistration", "ServicePrincipal",
                                 "ManagedIdentity", "Secret", "Certificate"], "operations": [], "min_risk": "medium"},
        materiality={"notify_threshold": "notable",
                     "always_notify_if": ["owner_grant", "rbac_grant", "eligible_grant",
                                          "cred_expiring", "mfa_gap", "ownerless_app"]},
        instructions=(
            "Review **identity and role-assignment** changes for **{{scope_label}}** over the last "
            "{{lookback_hours}} hours and answer: **did privileged access expand?**\n\n"
            "Prioritize: new **Owner**, **Contributor** or **User Access Administrator** "
            "assignments; roles assigned at **management-group or subscription scope**; roles "
            "granted to an **external guest** or a **service principal**; **privileged assignments "
            "made outside PIM**; a **new managed identity** (especially one granted access to Key "
            "Vault, Storage, SQL or a subscription); an **app registration created** or a **client "
            "secret / federated credential added**; **secrets or certificates expiring soon**; "
            "**Conditional Access policy changes**; **break-glass sign-ins**; **failed privileged "
            "sign-ins**; and **MFA disabled or weakened**. Also fold in standing identity risk from "
            "the Access and Identity signals — ownerless apps and privileged users without MFA.\n\n"
            "Treat routine, expected grants as low signal."
        ),
    ),
    # 5 — Logging & Monitoring Hygiene ------------------------------------------------------
    _pack(
        id="logging-monitoring-hygiene",
        name="Logging & Monitoring Hygiene",
        icon="🔭",
        category="operations",
        description="Where did we lose visibility? Diagnostic settings, agents, DCRs, alerts and telemetry gaps.",
        sources=["change_explorer", "assessments"],
        filters={"categories": ["Monitoring", "Security", "Compute"], "operations": [], "min_risk": "low"},
        materiality={"notify_threshold": "notable", "always_notify_if": ["logging_disabled"]},
        instructions=(
            "Report on **observability hygiene** for **{{scope_label}}** over the last "
            "{{lookback_hours}} hours and answer: **where did we lose visibility?**\n\n"
            "Surface: **diagnostic settings deleted** or resources **with no diagnostic settings**; "
            "logs **not flowing to the central Log Analytics workspace**; **Activity Log export "
            "disabled** for a subscription; the **Log Analytics / Azure Monitor agent or a DCR "
            "association missing**; **alert rules disabled** or an **action group changed**; **alerts "
            "that fired with no action group attached**; a **workbook / reporting resource deleted**; "
            "**log ingestion stopped** for a workload; **VM heartbeats missing**; **App Insights not "
            "receiving telemetry**; and resources that **have metrics but no alerts configured**.\n\n"
            "Fold in any monitoring / diagnostics findings from the latest assessment. A reduction "
            "in logging visibility is always worth surfacing even when nothing else changed."
        ),
    ),
    # 6 — Cost & FinOps Daily ---------------------------------------------------------------
    _pack(
        id="cost-finops-daily",
        name="Cost & FinOps Daily",
        icon="💰",
        category="cost",
        description="What got more expensive since yesterday — and what waste can we reclaim right now?",
        sources=["cost", "change_explorer"],
        filters={"categories": ["CostScale", "Compute", "Storage", "Database"], "operations": [], "min_risk": "low"},
        materiality={"notify_threshold": "notable", "always_notify_if": ["idle_or_orphaned"]},
        instructions=(
            "Give a workload owner (and their exec) a **cost and FinOps** readout for "
            "**{{scope_label}}** over the last {{lookback_hours}} hours: **what got more expensive, "
            "and what money are we leaking?**\n\n"
            "Highlight: **cost increases** vs yesterday and vs the 7-day average; a **new expensive "
            "resource** created; a **VM resized to a larger SKU** or a **disk changed to "
            "Premium/Ultra**; and reclaimable waste — **unattached disks**, **unused NICs**, "
            "**idle public IPs**, **stopped-but-allocated VMs**, **orphaned snapshots**, **idle App "
            "Service Plans**, **underused VMs / App Gateways**, and **Log Analytics ingestion "
            "spikes**. Note **reservation or savings-plan opportunities** and resources **missing a "
            "cost-center tag**. Where useful, rank the **top cost increases by resource group and "
            "by owner tag**.\n\n"
            "Use the Cost-cleanup signal for the idle/orphaned inventory and its estimated monthly "
            "waste. Frame findings by the money at stake."
        ),
    ),
    # 7 — Security Posture Drift ------------------------------------------------------------
    _pack(
        id="security-posture-drift",
        name="Security Posture Drift",
        icon="🚨",
        category="security",
        description="What became less secure yesterday? Defender drift, exposed data planes and new non-compliance.",
        sources=["assessments", "policy", "change_explorer"],
        filters={"categories": ["Security", "Storage", "Database", "KeyVault", "Network", "Compute"],
                 "operations": [], "min_risk": "medium"},
        materiality={"notify_threshold": "notable",
                     "always_notify_if": ["assessment_critical", "non_compliant",
                                          "public_network_access", "public_exposure"]},
        instructions=(
            "Assess **security-posture drift** for **{{scope_label}}** over the last "
            "{{lookback_hours}} hours and answer: **what became less secure?**\n\n"
            "Surface: **new or newly-unhealthy Defender recommendations**; a **Secure Score drop**; "
            "a **Defender plan disabled** (or never enabled on a subscription); a **new high-"
            "severity alert**; a **newly-exposed management port**; **public access enabled** on a "
            "**storage account, Key Vault, SQL server, container registry or Kubernetes API**; "
            "**missing encryption** or a **missing private endpoint**; an **insecure TLS version or "
            "weak cipher**; and resources that **became non-compliant** with Azure Policy.\n\n"
            "Ground the findings in the latest assessment (failing critical/high findings) and the "
            "policy-compliance snapshot. Anything that widens the attack surface is high signal."
        ),
    ),
    # 8 — Data Protection -------------------------------------------------------------------
    _pack(
        id="data-protection-daily",
        name="Data Protection",
        icon="💾",
        category="operations",
        description="Can we recover if something breaks? Backups, DR pairs, Key Vault, storage and secret-expiry risk.",
        sources=["backup", "identity", "change_explorer"],
        filters={"categories": ["KeyVault", "Secret", "Certificate", "Storage", "Database", "Security"],
                 "operations": [], "min_risk": "low"},
        materiality={"notify_threshold": "notable",
                     "always_notify_if": ["backup_unprotected", "dr_unhealthy", "cred_expiring", "secret_change"]},
        instructions=(
            "Assess **data protection and recoverability** for **{{scope_label}}** over the last "
            "{{lookback_hours}} hours and answer: **can we recover if something breaks?**\n\n"
            "Surface: a **backup job failed** or **backup stopped** for a resource; a **Recovery "
            "Services vault deleted or changed**; a **backup policy changed** or **soft delete "
            "disabled**; **Key Vault purge protection disabled**, a **secret deleted** or a **key "
            "disabled**; **certificates expiring in 30/60/90 days**; **storage soft delete or blob "
            "versioning disabled**; **storage public access enabled**; a **SQL backup/restore "
            "issue**, a **Cosmos DB backup-mode change**, or **geo-replication disabled**; and a "
            "**private endpoint removed from a data resource**.\n\n"
            "Use the Backup & DR signal for unprotected resources and unhealthy DR pairs, and the "
            "Identity signal for expiring secrets and certificates. An unprotected production data "
            "store is always worth surfacing."
        ),
    ),
    # 9 — Application Health ----------------------------------------------------------------
    _pack(
        id="application-health-daily",
        name="Application Health",
        icon="📈",
        category="operations",
        description="Did the app change or degrade? App Service, Functions, APIM, App Gateway and AKS activity.",
        sources=["change_explorer", "assessments"],
        filters={"categories": ["Compute", "Deployment", "AppConfiguration", "Network"],
                 "operations": [], "min_risk": "low"},
        materiality={"notify_threshold": "notable", "always_notify_if": ["security_control_deleted"]},
        instructions=(
            "Report on **application health and change** for **{{scope_label}}** over the last "
            "{{lookback_hours}} hours and answer: **did the app change or degrade?**\n\n"
            "Surface, across App Service / Functions / APIM / App Gateway / AKS / Logic Apps: an "
            "**app or function restarted**; a **deployment** or **slot swap**; an **app setting, "
            "connection string or managed identity changed**; an app **scaled up/down** or a "
            "**plan CPU/memory spike**; **increased Function or Logic App failures**; an **APIM 5xx "
            "spike**; an **App Gateway backend unhealthy** or a **WAF blocked-request spike**; an "
            "**AKS node not ready** or **rising pod restarts**; and a **container image changed** or "
            "**pushed** to the registry.\n\n"
            "Correlate configuration changes with signs of degradation — a change immediately "
            "followed by errors is the headline. Fold in relevant reliability findings from the "
            "latest assessment."
        ),
    ),
    # 10 — Availability & Incident Early Warning --------------------------------------------
    _pack(
        id="availability-early-warning",
        name="Availability & Incident Early Warning",
        icon="🚦",
        category="operations",
        description="What might become an incident today? Health events, retirements, saturation and DR gaps.",
        sources=["radar", "backup", "assessments", "change_explorer"],
        filters={"categories": [], "operations": [], "min_risk": "medium"},
        materiality={"notify_threshold": "notable",
                     "always_notify_if": ["retirement_soon", "breaking_change", "dr_unhealthy",
                                          "assessment_critical"]},
        instructions=(
            "Act as an SRE giving an early-warning readout for **{{scope_label}}** over the last "
            "{{lookback_hours}} hours: **what might become an incident today?**\n\n"
            "Weigh: any **resource-health event** or **Azure Service Health** advisory impacting the "
            "subscription/region; **VM heartbeat gaps**; **availability-test failures**; an **App "
            "Gateway backend pool or Load Balancer health probe failing**; **SQL DTU/vCore "
            "saturation**, **storage or Event Hub throttling**, a **Service Bus dead-letter spike** "
            "or a **growing queue**; **failed dependency calls**, **rising API latency** or a **5xx "
            "increase**; **DNS resolution failures**; **certificate-expiration risk**; a **private "
            "endpoint connection pending/rejected**; plus **upcoming service retirements and "
            "breaking changes** (Radar) and **unhealthy DR pairs** (Backup & DR) that would turn a "
            "small event into a real outage.\n\n"
            "Rank by imminence and blast radius — lead with what is most likely to page someone."
        ),
    ),
    # 11 — Governance & Hygiene -------------------------------------------------------------
    _pack(
        id="governance-hygiene",
        name="Governance & Hygiene",
        icon="🧹",
        category="cost",
        description="Where is the environment getting messy? Tags, policy coverage, orphans and missing locks.",
        sources=["policy", "cost", "identity", "change_explorer"],
        filters={"categories": ["Policy", "TagsMetadata", "RBAC"], "operations": [], "min_risk": "low"},
        materiality={"notify_threshold": "notable",
                     "always_notify_if": ["non_compliant", "policy_exemption", "policy_deleted",
                                          "idle_or_orphaned", "ownerless_app", "lock_removed"]},
        instructions=(
            "Give a tenant-cleanup readout for **{{scope_label}}** over the last {{lookback_hours}} "
            "hours: **where is the environment getting messy?**\n\n"
            "Surface: resources **missing an owner / environment / cost-center tag**; resources "
            "**created outside approved regions** or with **nonstandard naming**; resources **not "
            "covered by any policy assignment**; **policy exemptions expiring soon** or **created "
            "recently**; **missing locks on critical resources**; **subscriptions without a budget** "
            "or **without Activity Log export**; **empty resource groups**; resources **not modified "
            "in 180 days**; and orphans — **managed identities**, **role assignments** and **private "
            "endpoints** with nothing on the other end.\n\n"
            "Use the Policy signal for non-compliance and exemptions, the Cost signal for idle/"
            "orphaned inventory, and the Identity signal for ownerless apps. Group by the cleanup "
            "action so the list is directly actionable."
        ),
    ),
    # 12 — Tenant Daily Executive Summary ---------------------------------------------------
    _pack(
        id="tenant-executive-summary",
        name="Tenant Daily Executive Summary",
        icon="🗞️",
        category="general",
        description="The front page: one daily digest of change, security, cost, identity, backup and compliance drift.",
        sources=["change_explorer", "radar", "cost", "rbac", "assessments", "backup", "identity", "policy"],
        filters={"categories": [], "operations": [], "min_risk": "low"},
        materiality={"notify_threshold": "notable",
                     "always_notify_if": ["public_exposure", "public_network_access", "owner_grant",
                                          "backup_unprotected", "dr_unhealthy", "assessment_critical",
                                          "non_compliant", "retirement_soon", "cred_expiring",
                                          "security_control_deleted"]},
        instructions=(
            "Write the **daily executive front page** for **{{scope_label}}**, covering the last "
            "{{lookback_hours}} hours: **what should I care about today?**\n\n"
            "Open with a two-to-three sentence executive headline, then a short set of tiles, one "
            "line each, drawn from every signal available: **new vs deleted resources**; "
            "**high-risk network changes** (rules opened to the Internet); **VM events** (reboots, "
            "deallocations); **security drift** (new/unhealthy Defender or assessment findings); "
            "**cost drift** (projected daily increase and reclaimable waste); **logging gaps** "
            "(resources missing diagnostics); **backup issues** (failed jobs, unprotected resources, "
            "unhealthy DR); **identity risk** (new Owner/privileged grants, expiring credentials); "
            "**app health** (services with error increases); **policy drift** (new non-compliant "
            "resources); **upcoming retirements**; and the **noisiest workload**.\n\n"
            "Each tile should quantify the finding and name the single most important item behind "
            "it. This is a leadership digest — synthesize ruthlessly and lead with what matters most."
        ),
    ),
]


def by_id(pack_id: str) -> dict[str, Any] | None:
    for s in STARTERS:
        if s["id"] == pack_id:
            return dict(s)
    return None
