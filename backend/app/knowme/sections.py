"""Workload Know-Me — section catalog, scope-fact extraction, and Markdown rendering.

A *Know-Me* is a support-facing reference for ONE workload, transformed from that
workload's Architecture Memory. Where the Memory documents the system for the people who
build it, the Know-Me documents it for the responder who has to triage a case on it at
2 a.m. with no prior context: what it is, how it's built, how to triage it, what usually
breaks, and who to call.

Everything technical is auto-derived from the Architecture Memory (the authoritative
source) and the workload's real Azure scope (subscriptions, resource groups, resource
names — which we DO know and fill in). Everything the memory cannot know — people,
contract data, formal coverage windows, escalation routing, SLAs — is emitted as an
explicit ``⟦TODO: …⟧`` placeholder for a human to complete, never guessed.

This module is the single source of truth for the section contract (shared by the AI
generator, the registry, the API, and the UI), mirroring ``architectures.memory``.
"""
from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------- section catalog
# Stable section keys + display labels + author guidance. ``key`` is the storage/AI/render
# contract; ``label`` becomes a Markdown H2 heading. Order here IS the document order.
SECTION_CATALOG: list[dict[str, str]] = [
    {"key": "overview", "label": "Workload overview",
     "hint": "2–5 sentences: what it does, its pattern, region, and business criticality."},
    {"key": "solution_architecture", "label": "Solution / architecture overview",
     "hint": "Pattern, happy-path flow (resource-named), key components, network & identity."},
    {"key": "services_scope", "label": "Azure services & sub-workloads in scope",
     "hint": "The Azure resource types/services the responder is expected to cover."},
    {"key": "subscriptions_resources", "label": "Subscriptions & resources in scope",
     "hint": "Subscription(s), resource group(s), region, and named resources."},
    {"key": "diagnostics_triage", "label": "Diagnostics & first-look triage",
     "hint": "Ordered 'follow the data path' runbook: what to check, on which resource, what signals a problem."},
    {"key": "thresholds_slis", "label": "Critical thresholds, SLIs & monitoring",
     "hint": "SLIs, where they're observed, and observability gaps."},
    {"key": "resiliency_dr", "label": "Resiliency & DR posture",
     "hint": "Redundancy (LRS/ZRS/GRS), single- vs multi-region, autoscale, the primary SPOF."},
    {"key": "known_issues", "label": "Known issues, risks & proactive callouts",
     "hint": "Prioritized ❌/⚠ list of things a responder should know when handling cases."},
    {"key": "security_posture", "label": "Security posture (support-relevant)",
     "hint": "Public exposure, shared-key/SAS paths, Key Vault state, TLS — the blast radius."},
    {"key": "support_handling", "label": "Support & escalation handling",
     "hint": "Escalation routing, on-call group, operational runbook notes."},
    {"key": "contacts", "label": "Contacts",
     "hint": "Customer + support contacts. Human-completion only (⟦TODO⟧ rows)."},
    {"key": "data_compliance_cost", "label": "Data, compliance & cost notes",
     "hint": "Data stores/redundancy/encryption, tagging/policy posture, cost callouts."},
    {"key": "todo_checklist", "label": "Fields requiring human completion",
     "hint": "Consolidated checklist of every ⟦TODO⟧ before this Know-Me is 'Published'."},
]

_CATALOG_BY_KEY = {s["key"]: s for s in SECTION_CATALOG}
SECTION_KEYS = [s["key"] for s in SECTION_CATALOG]

# Architecture Memory section keys → the Know-Me section(s) they primarily feed. Used to
# build the grounding context (and documented for the generator's mapping table).
MEMORY_TO_KNOWME: dict[str, list[str]] = {
    "overview": ["overview"],
    "pattern": ["overview", "solution_architecture"],
    "expected_flow": ["solution_architecture", "diagnostics_triage"],
    "components": ["solution_architecture", "services_scope"],
    "dependencies": ["services_scope", "solution_architecture"],
    "network_topology": ["solution_architecture", "subscriptions_resources", "security_posture"],
    "identity_access": ["solution_architecture", "security_posture"],
    "data_storage": ["data_compliance_cost", "resiliency_dr"],
    "security_model": ["security_posture"],
    "compliance": ["data_compliance_cost"],
    "resiliency_targets": ["resiliency_dr"],
    "scaling_performance": ["resiliency_dr"],
    "critical_thresholds": ["thresholds_slis"],
    "observability": ["thresholds_slis"],
    "runbook": ["support_handling"],
    "change_management": ["support_handling"],
    "cost_sizing": ["data_compliance_cost"],
    "known_gaps": ["known_issues"],
    "known_issues": ["known_issues"],
    "diagnostic_hints": ["diagnostics_triage"],
}


def section_label(key: str) -> str:
    meta = _CATALOG_BY_KEY.get(key)
    if meta:
        return meta["label"]
    return key.replace("_", " ").strip().title() or "Section"


# ---------------------------------------------------------------- field typing (A2)
# A ⟦TODO⟧ is a human-completion field. We infer a control TYPE, a GROUP (so repeated
# fields like "define threshold" collapse into one grid), and whether it's REQUIRED to
# publish — from the field_key + label. Type and group are detected independently so an
# "escalation owner email" is correctly typed ``email`` AND grouped ``escalation``. This
# typed schema is the contract the guided-fill UI, auto-fill resolver, and validation all bind to.
FIELD_TYPES = ("email", "person", "group", "duration", "datetime", "number", "url", "text")

# (regex, type) — first match wins.
_TYPE_RULES: list[tuple[str, str]] = [
    (r"\bemail\b|e-mail", "email"),
    (r"\brto\b|\brpo\b|recovery[_ ]?(time|point)|\bsla\b|\bslo\b|coverage[_ ]?(window|hours)|"
     r"support[_ ]?hours|business[_ ]?hours|response[_ ]?time|resolution[_ ]?time", "duration"),
    (r"threshold|target[_ ]?value|breach[_ ]?at|warn[_ ]?at|alert[_ ]?at|sli[_ ]?target", "number"),
    (r"\bdate\b|expiry|expires|renewal|effective|valid[_ ]?(from|until)", "datetime"),
    (r"url|link|portal|dashboard|wiki", "url"),
    (r"team\b|assignment[_ ]?group|support[_ ]?group|distribution[_ ]?list", "group"),
    (r"on[_ -]?call|oncall|escalation[_ ]?owner|escalation[_ ]?contact|duty[_ ]?manager|"
     r"contact|account[_ ]?manager|customer|owner|department", "person"),
]

# (regex, group) — first match wins. Groups cluster fields for grids + required-ness.
_GROUP_RULES: list[tuple[str, str]] = [
    (r"on[_ -]?call|oncall|escalation|coverage[_ ]?(window|hours)|assignment[_ ]?group|"
     r"support[_ ]?group|duty[_ ]?manager", "escalation"),
    (r"\brto\b|\brpo\b|recovery[_ ]?(time|point)", "resiliency"),
    (r"\bsla\b|\bslo\b|response[_ ]?time|resolution[_ ]?time", "sla"),
    (r"threshold|target[_ ]?value|breach[_ ]?at|warn[_ ]?at|alert[_ ]?at|sli[_ ]?target", "thresholds"),
    (r"contract|schedule[_ ]?id|agreement[_ ]?id|po[_ ]?number|case[_ ]?number|ticket|"
     r"\bdate\b|expiry|expires|renewal|effective", "contract"),
    # Network / connectivity scope (VNet, subnet, CIDR, private endpoint, IP, DNS, firewall…).
    (r"vnet|v-net|virtual[_ ]?network|subnet|cidr|address[_ ]?space|private[_ ]?endpoint|"
     r"private[_ ]?link|\bnsg\b|firewall|\bip\b|ip[_ ]?address|dns|fqdn|peering|gateway|"
     r"route[_ ]?table|egress|ingress", "network"),
    # Identity / access scope (tenant, principal, managed identity, RBAC role, app reg…).
    (r"tenant|principal|managed[_ ]?identity|\bmsi\b|service[_ ]?principal|\brbac\b|"
     r"role[_ ]?assignment|app[_ ]?registration|object[_ ]?id|client[_ ]?id|\bgroup[_ ]?id",
     "identity"),
    (r"region|location|friendly[_ ]?name|display[_ ]?name|sub(scription)?[_ ]?name|"
     r"resource[_ ]?group|\brg\b|sku|tier|capacity|instance[_ ]?count", "scope"),
    (r"url|link|portal|dashboard|runbook[_ ]?url|wiki|repo|pipeline", "links"),
    (r"cost[_ ]?cent(er|re)|department|business[_ ]?unit|\bowner\b|budget|charge[_ ]?back", "ownership"),
    (r"contact|account[_ ]?manager|customer|stakeholder", "contacts"),
]

# Groups whose fields must be completed before a Know-Me can be Published.
_REQUIRED_GROUPS = {"escalation", "resiliency", "sla", "contract", "contacts"}


def _match(rules: list[tuple[str, str]], blob: str, default: str) -> str:
    for pattern, value in rules:
        if re.search(pattern, blob):
            return value
    return default


def classify_field(field_key: str, label: str) -> dict[str, Any]:
    """Infer {type, required, group} for a ⟦TODO⟧ field from its key + label."""
    blob = f"{field_key} {label}".lower()
    ftype = _match(_TYPE_RULES, blob, "text")
    group = _match(_GROUP_RULES, blob, "other")
    return {"type": ftype, "required": group in _REQUIRED_GROUPS, "group": group}


# ---------------------------------------------------------------- choice sets (rule enums)
# A ⟦TODO⟧ field can carry a *choice set*: a list of candidate values the UI offers as a
# dropdown / segmented control instead of a blank box. ``allow_custom`` decides whether the
# user may also type a free value (a picker) or must pick one (a strict select). These are
# DETERMINISTIC rules (the safest, most authoritative source); platform facts override them
# and AI may add more at generation time. First match wins.
#
# (regex over "field_key label", choices, allow_custom)
_CHOICE_RULES: list[tuple[str, list[str], bool]] = [
    # Severity / criticality / priority / impact / tier ranking.
    (r"criticalit|severity|priorit|impact[_ ]?level|business[_ ]?impact",
     ["Critical", "High", "Medium", "Low"], False),
    # Environment.
    (r"\benv(ironment)?\b",
     ["Production", "Staging", "QA", "Test", "Development"], True),
    # Storage redundancy / replication.
    (r"redundanc|replication|\blrs\b|\bzrs\b|\bgrs\b|\bgzrs\b",
     ["LRS", "ZRS", "GRS", "RA-GRS", "GZRS", "RA-GZRS"], True),
    # TLS minimum version.
    (r"\btls\b|transport[_ ]?(layer|security)|min(imum)?[_ ]?tls",
     ["TLS 1.2", "TLS 1.3"], True),
    # Data classification / sensitivity.
    (r"classification|sensitivit|data[_ ]?class",
     ["Public", "Internal", "Confidential", "Highly Confidential", "Restricted"], True),
    # Coverage window / support hours / on-call schedule.
    (r"coverage[_ ]?(window|hours)|support[_ ]?hours|on[_ -]?call[_ ]?(window|hours|schedule)",
     ["24×7", "Business hours (Mon–Fri 9–5)", "On-call only", "Follow-the-sun"], True),
    # RTO / RPO recovery targets.
    (r"\brto\b|recovery[_ ]?time",
     ["15 minutes", "1 hour", "4 hours", "8 hours", "24 hours", "72 hours"], True),
    (r"\brpo\b|recovery[_ ]?point",
     ["0 (synchronous)", "5 minutes", "15 minutes", "1 hour", "4 hours", "24 hours"], True),
    # SLA / SLO response & resolution times.
    (r"\bsla\b|\bslo\b|response[_ ]?time|resolution[_ ]?time",
     ["15 minutes", "1 hour", "4 hours", "8 hours", "1 business day", "Next business day"], True),
    # Backup / snapshot frequency.
    (r"backup[_ ]?(frequenc|schedule|interval)|snapshot[_ ]?(frequenc|interval)",
     ["Hourly", "Every 4 hours", "Daily", "Weekly", "Monthly"], True),
]

# A field whose label is a yes/no question gets a Yes/No choice set. Detected when the label
# begins with an interrogative auxiliary, ends with "?", or carries a "(yes/no)" hint.
_YESNO_LEAD = re.compile(r"^\s*(is|are|do|does|did|has|have|should|can|could|will|would|must)\b", re.IGNORECASE)
_YESNO_HINT = re.compile(r"\(\s*y\s*/\s*n\s*\)|\(\s*yes\s*/\s*no\s*\)|yes[_ ]?/[_ ]?no", re.IGNORECASE)
_YESNO_CHOICES = ["Yes", "No", "Unknown", "N/A"]


def infer_choices(field_key: str, label: str) -> dict[str, Any] | None:
    """Derive a deterministic *choice set* for a ⟦TODO⟧ field, or None when it's free text.

    Returns ``{choices: [...], allow_custom: bool, source: 'rule'}``. Yes/No questions and a
    catalog of well-known enums (criticality, environment, redundancy, TLS, RTO/RPO, SLA,
    data classification, coverage window, backup frequency) are recognized from the label."""
    lbl = (label or "").strip()
    blob = f"{field_key} {lbl}".lower()
    if (_YESNO_LEAD.search(lbl) or lbl.endswith("?") or _YESNO_HINT.search(blob)):
        return {"choices": list(_YESNO_CHOICES), "allow_custom": False, "source": "rule"}
    for pattern, choices, allow_custom in _CHOICE_RULES:
        if re.search(pattern, blob):
            return {"choices": list(choices), "allow_custom": allow_custom, "source": "rule"}
    return None


def _split_choices(raw: str) -> list[str]:
    """Parse a token's ``choices=A; B; C`` payload into a clean list (semicolon- or
    pipe-separated, deduped, order-preserving)."""
    out: list[str] = []
    for part in re.split(r"[;|]", raw or ""):
        v = part.strip()
        if v and v not in out:
            out.append(v)
    return out[:12]


def default_sections() -> list[dict[str, str]]:
    """An empty Know-Me skeleton (all sections, no content) — every section is always present."""
    return [{"key": s["key"], "label": s["label"], "content": ""} for s in SECTION_CATALOG]


# ---------------------------------------------------------------- scope facts
_SUB_RE = re.compile(r"/subscriptions/([0-9a-fA-F-]{36})", re.IGNORECASE)
_RG_RE = re.compile(r"/resourcegroups/([^/]+)", re.IGNORECASE)
# ⟦TODO: <label> | key=<field_key> | choices=A; B; C⟧ — the token grammar the generator
# emits and the parser recognizes. ``key=`` maps the field to a fillable form field;
# the optional ``choices=`` lets the model attach AI-inferred candidate values (P2).
TODO_RE = re.compile(
    r"⟦TODO:\s*(?P<label>[^⟧|]+?)"
    r"(?:\s*\|\s*key=(?P<key>[a-z0-9_]+))?"
    r"(?:\s*\|\s*choices=(?P<choices>[^⟧]+?))?"
    r"\s*⟧"
)


def scope_facts(workload: dict[str, Any] | None, arch: dict[str, Any] | None) -> dict[str, Any]:
    """Derive the workload's REAL Azure scope from its nodes + the architecture's nodes —
    no Azure call (the Memory was already grounded on live data). Returns subscriptions
    (guid + friendly name), resource groups, regions, and named resources, so the generator
    can fill Section 4 / the header with facts instead of ⟦TODO⟧ placeholders.
    """
    subs: dict[str, str] = {}           # guid -> friendly name (name if known, else guid)
    rgs: set[str] = set()
    regions: set[str] = set()
    resources: list[dict[str, str]] = []  # [{name, type, rg, location}]

    def _note_sub(guid: str, name: str = "") -> None:
        guid = (guid or "").lower()
        if not guid:
            return
        if guid not in subs or (name and subs[guid] == guid):
            subs[guid] = name or subs.get(guid) or guid

    for node in (workload or {}).get("nodes", []) or []:
        kind = node.get("kind")
        nid = node.get("id", "") or ""
        sub = (node.get("subscription_id") or "").lower() or (_SUB_RE.search(nid).group(1).lower() if _SUB_RE.search(nid) else "")
        loc = node.get("location") or ""
        if loc:
            regions.add(loc)
        if kind == "subscription":
            _note_sub(sub or nid, node.get("name", ""))
        elif kind == "resource_group":
            _note_sub(sub)
            rg = node.get("resource_group") or node.get("name", "")
            if rg:
                rgs.add(rg)
        elif kind == "resource":
            _note_sub(sub)
            rg = node.get("resource_group") or (_RG_RE.search(nid).group(1) if _RG_RE.search(nid) else "")
            if rg:
                rgs.add(rg)
            resources.append({
                "name": node.get("name", "") or nid.rsplit("/", 1)[-1],
                "type": node.get("resource_type", "") or "",
                "rg": rg,
                "location": loc,
            })

    # Architecture nodes are the resources the Memory was built from — richer names/types.
    for n in (arch or {}).get("nodes", []) or []:
        nm = n.get("name", "")
        if not nm:
            continue
        if not any(r["name"] == nm for r in resources):
            resources.append({"name": nm, "type": n.get("type", "") or "", "rg": "", "location": ""})

    return {
        "subscriptions": [{"id": g, "name": n} for g, n in subs.items()],
        "resource_groups": sorted(rgs),
        "regions": sorted(regions),
        "resources": resources[:200],
    }


def scope_facts_block(facts: dict[str, Any]) -> str:
    """Render scope facts as a compact authoritative block for the generation prompt."""
    subs = facts.get("subscriptions") or []
    sub_lines = "\n".join(
        f"  - {s['id']}" + (f"  ({s['name']})" if s.get('name') and s['name'] != s['id'] else "")
        for s in subs
    ) or "  (none resolvable from workload nodes — emit ⟦TODO⟧ for subscription GUIDs)"
    rgs = ", ".join(facts.get("resource_groups") or []) or "(none resolved)"
    regions = ", ".join(facts.get("regions") or []) or "(not determined)"
    res = facts.get("resources") or []
    res_lines = "\n".join(
        f"  - {r['name']}" + (f" [{r['type']}]" if r.get("type") else "") + (f" in {r['rg']}" if r.get("rg") else "")
        for r in res[:120]
    ) or "  (none)"
    return (
        "REAL AZURE SCOPE (authoritative — use these exact GUIDs/names; do NOT mark them ⟦TODO⟧):\n"
        f"Subscriptions:\n{sub_lines}\n"
        f"Resource groups: {rgs}\n"
        f"Region(s): {regions}\n"
        f"Named resources:\n{res_lines}\n"
    )


def parse_todos(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Scan every section's Markdown for ⟦TODO⟧ tokens and return a de-duplicated index.

    Each entry is a typed human-completion field::

        {id, field_key, label, section_key, status:'open', value:'',
         type, required, group, suggestions:[], source:'human', confidence}

    ``id`` is stable (section_key + field_key/slug) so re-parsing keeps a filled value and
    any accepted suggestion mappable across regenerations.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for s in sections or []:
        skey = str(s.get("key") or "")
        for m in TODO_RE.finditer(str(s.get("content") or "")):
            label = (m.group("label") or "").strip()
            field_key = (m.group("key") or "").strip()
            if not label:
                continue
            slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:48]
            tid = f"{skey}:{field_key or slug}"
            if tid in seen:
                continue
            seen.add(tid)
            meta = classify_field(field_key, label)
            # Choice set precedence: AI choices on the token (highest signal the model had
            # the workload context) → deterministic rule enum → none (free text).
            ai_choices = _split_choices(m.group("choices") or "")
            if ai_choices:
                choices, allow_custom, choice_source = ai_choices, True, "ai"
            else:
                cs = infer_choices(field_key, label)
                if cs:
                    choices, allow_custom, choice_source = cs["choices"], cs["allow_custom"], cs["source"]
                else:
                    choices, allow_custom, choice_source = [], True, ""
            out.append({
                "id": tid, "field_key": field_key, "label": label,
                "section_key": skey, "status": "open", "value": "",
                "type": meta["type"], "required": meta["required"], "group": meta["group"],
                "suggestions": [], "source": "human", "confidence": None,
                "choices": choices, "allow_custom": allow_custom, "choice_source": choice_source,
                "multi": False,
            })
    return out


def _todo_value_map(todos: list[dict[str, Any]] | None) -> dict[str, str]:
    """Map a filled todo's id → value (only status='done' with a non-empty value)."""
    out: dict[str, str] = {}
    for t in todos or []:
        if t.get("status") == "done" and str(t.get("value") or "").strip():
            out[str(t.get("id"))] = str(t["value"]).strip()
    return out


# ---------------------------------------------------------------- content cleaning
_HEADING_RE = re.compile(r"^\s{0,3}#{1,4}\s+(.+?)\s*#*\s*$")
# A trivial "stock" diagram a model emits when it has no real architecture to draw:
# every node label is a generic word (Client / Service / Database / API …) with no real
# resource name. We never want to render this — it's noise, not the workload's topology.
_GENERIC_NODE_WORDS = {
    "client", "clients", "user", "users", "browser", "service", "services", "api",
    "apis", "app", "apps", "application", "frontend", "front", "backend", "back",
    "server", "servers", "database", "databases", "db", "datastore", "store",
    "cache", "queue", "gateway", "loadbalancer", "lb", "internet", "cloud", "system",
}
_MERMAID_NODE_RE = re.compile(r'[\[\(\{>]+\s*"?([^"\]\)\}>|]+?)"?\s*[\]\)\}]+')


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def is_placeholder_mermaid(code: str) -> bool:
    """True for the trivial stock diagram (all-generic node labels, e.g. Client→Service→
    Database) a model emits when it has no real topology — we strip these on render."""
    body = (code or "").strip()
    if not body:
        return True
    labels = _MERMAID_NODE_RE.findall(body)
    cleaned: list[str] = []
    for raw in labels:
        head = re.split(r"<br\s*/?>", raw, maxsplit=1)[0]
        n = re.sub(r"[^a-z0-9]+", "", head.lower())
        if n:
            cleaned.append(n)
    if not cleaned:
        return False
    return len(cleaned) <= 6 and all(
        any(w == n or (len(n) > 3 and w in n) for w in _GENERIC_NODE_WORDS) for n in cleaned
    )


def _strip_placeholder_mermaid(content: str) -> str:
    """Remove ```mermaid fenced blocks whose diagram is a generic placeholder."""
    if "```mermaid" not in (content or "").lower():
        return content

    def _repl(m: re.Match[str]) -> str:
        return "" if is_placeholder_mermaid(m.group(1)) else m.group(0)

    return re.sub(r"```mermaid\s*\n(.*?)```", _repl, content, flags=re.DOTALL | re.IGNORECASE)


def strip_leading_heading(content: str, label: str) -> str:
    """Drop a leading Markdown heading line that just repeats the section ``label`` (the
    view + export already render the label as the section heading, so it would show twice)."""
    if not content:
        return content
    lines = content.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return content
    m = _HEADING_RE.match(lines[i])
    if not m:
        return content
    heading, lbl = _norm(m.group(1)), _norm(label)
    if not lbl or (heading != lbl and not heading.startswith(lbl)):
        return content
    rest = lines[i + 1:]
    while rest and not rest[0].strip():
        rest.pop(0)
    return "\n".join(lines[:i] + rest).strip()


def clean_section_content(content: str, label: str) -> str:
    """Normalize a section's content for display/export: drop a redundant leading heading
    and any generic placeholder Mermaid diagram."""
    return strip_leading_heading(_strip_placeholder_mermaid(content or ""), label)


def apply_todo_values(content: str, section_key: str, value_by_id: dict[str, str], *, mark_open: bool = True) -> str:
    """Substitute filled ⟦TODO⟧ tokens with their values inline (the overlay model). Open
    tokens become a readable ``**[TODO: label]**`` marker when ``mark_open`` (export/read),
    or are left verbatim for the editor."""
    def _sub(m: re.Match[str]) -> str:
        label = (m.group("label") or "").strip()
        field_key = (m.group("key") or "").strip()
        slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:48]
        tid = f"{section_key}:{field_key or slug}"
        val = value_by_id.get(tid)
        if val:
            return val
        return f"**[TODO: {label}]**" if mark_open else m.group(0)

    return TODO_RE.sub(_sub, content or "")


def render_markdown(
    know_me: dict[str, Any],
    workload_name: str = "",
    *,
    apply_values: bool = True,
    cover: bool = False,
) -> str:
    """Render a Know-Me into a single Markdown document (preview / export / injection).

    When ``apply_values`` (default), filled ⟦TODO⟧ fields are substituted inline and open
    ones become ``**[TODO: …]**`` markers — so the rendered doc reads naturally. When
    ``cover`` (used for export), a title block + status/meta line + a table-of-contents are
    prepended so a downloaded ``.md`` / PDF opens like a standalone reference."""
    from datetime import datetime, timezone

    title = (know_me.get("title") or "").strip() or (
        f"Know-Me — {workload_name}" if workload_name else "Workload Know-Me"
    )
    value_by_id = _todo_value_map(know_me.get("todos")) if apply_values else {}

    # First pass: gather the sections that actually have content (after cleaning).
    rendered: list[tuple[str, str]] = []  # (label, content)
    for s in know_me.get("sections", []) or []:
        label = s.get("label") or section_label(str(s.get("key", "")))
        content = clean_section_content(str(s.get("content") or ""), label).strip()
        if not content:
            continue
        if apply_values:
            content = apply_todo_values(content, str(s.get("key", "")), value_by_id, mark_open=True)
        rendered.append((label, content))

    lines: list[str] = [f"# {title}", ""]
    if cover:
        status = str(know_me.get("status") or "draft").replace("_", " ")
        desc = str(know_me.get("description") or "").strip()
        meta = [f"**Workload:** {workload_name or '—'}", f"**Status:** {status.title()}"]
        updated = str(know_me.get("updated_at") or "")
        if updated:
            try:
                meta.append("**Updated:** " + datetime.fromisoformat(updated).strftime("%Y-%m-%d"))
            except ValueError:
                pass
        meta.append("**Generated:** " + datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        lines += ["  ·  ".join(meta), ""]
        if desc:
            lines += [f"_{desc}_", ""]
        if rendered:
            lines += ["## Contents", ""]
            for label, _ in rendered:
                anchor = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
                lines.append(f"- [{label}](#{anchor})")
            lines += ["", "---", ""]
    elif workload_name:
        lines += [f"> **Workload:** {workload_name}", ""]

    for label, content in rendered:
        lines += [f"## {label}", "", content, ""]
    return "\n".join(lines).strip() + "\n"
