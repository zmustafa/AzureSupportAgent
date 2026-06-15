"""Architecture Memory registry (JSON).

An architecture *memory* is a structured, Markdown-friendly knowledge base that an
architecture owns — think "Memory.md" for AI. It captures the intended design, security
model, resiliency targets, known gaps, diagnostic hints, etc. across labelled sections.
It is used to inform Deep Investigations (injected as expert context) when an incident
occurs on the linked workload.

Persisted under backend/.data/architecture_memory.json, keyed by architecture_id (1:1
with an architecture, but each memory keeps its own id so it can be managed standalone).
No secrets, so no encryption — consistent with the architectures registry.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "architecture_memory.json"

# ---------------------------------------------------------------- section catalog
# Stable section keys + display labels + author guidance, grouped for the editor. The
# `key` is the contract used for storage, AI generation, and rendering; labels become
# Markdown headings. This is the single source of truth shared by API + AI + UI.
SECTION_CATALOG: list[dict[str, Any]] = [
    {"group": "Purpose & shape", "key": "overview", "label": "Overview",
     "hint": "What the system does and how business-critical it is."},
    {"group": "Purpose & shape", "key": "pattern", "label": "Architecture pattern",
     "hint": "e.g. Internet-facing web application, event-driven, batch."},
    {"group": "Purpose & shape", "key": "expected_flow", "label": "Expected flow",
     "hint": "The happy path. e.g. User → Front Door → App Service → SQL."},
    {"group": "Purpose & shape", "key": "components", "label": "Components & responsibilities",
     "hint": "Key resources and the role each one plays."},
    {"group": "Purpose & shape", "key": "dependencies", "label": "Dependencies",
     "hint": "Upstream/downstream and external dependencies (identity, APIs)."},

    {"group": "Topology & access", "key": "network_topology", "label": "Network topology",
     "hint": "VNets/subnets, peering, ingress/egress, DNS, private endpoints."},
    {"group": "Topology & access", "key": "identity_access", "label": "Identity & access",
     "hint": "Managed identities, RBAC, who has admin."},
    {"group": "Topology & access", "key": "data_storage", "label": "Data & storage",
     "hint": "Data stores, classification, encryption, backup/retention."},

    {"group": "Security & compliance", "key": "security_model", "label": "Security model",
     "hint": "WAF, network restrictions, private endpoints, Key Vault."},
    {"group": "Security & compliance", "key": "compliance", "label": "Compliance & governance",
     "hint": "Required tags, policies, regulatory constraints."},

    {"group": "Resilience & performance", "key": "resiliency_targets", "label": "Resiliency targets",
     "hint": "RTO, RPO, availability SLO, multi-region/failover."},
    {"group": "Resilience & performance", "key": "scaling_performance", "label": "Scaling & performance",
     "hint": "Autoscale rules, expected load, bottlenecks."},
    {"group": "Resilience & performance", "key": "critical_thresholds", "label": "Critical thresholds & SLIs",
     "hint": "What 'abnormal' looks like. e.g. latency > 500ms, queue depth > 1000."},

    {"group": "Operations", "key": "observability", "label": "Observability",
     "hint": "Where logs/metrics/traces go, dashboards, key alerts, health checks."},
    {"group": "Operations", "key": "runbook", "label": "Runbook / operational notes",
     "hint": "Common ops, restart procedures, escalation contacts."},
    {"group": "Operations", "key": "change_management", "label": "Change management",
     "hint": "IaC/pipeline, change windows, recent major changes."},
    {"group": "Operations", "key": "cost_sizing", "label": "Cost & sizing notes",
     "hint": "SKUs, reserved capacity, cost drivers."},

    {"group": "Risk & diagnostics", "key": "known_gaps", "label": "Known gaps & risks",
     "hint": "Accepted risks, missing redundancy, single points of failure."},
    {"group": "Risk & diagnostics", "key": "known_issues", "label": "Known issues & past incidents",
     "hint": "Recurring problems, prior RCAs."},
    {"group": "Risk & diagnostics", "key": "diagnostic_hints", "label": "Diagnostic hints",
     "hint": "Where to look first. e.g. check Front Door health → App Service → SQL DTU."},
]

_CATALOG_BY_KEY = {s["key"]: s for s in SECTION_CATALOG}

# Sections a brand-new memory is pre-seeded with (the highest-signal ones for both
# documentation and incident response).
DEFAULT_SECTION_KEYS = [
    "overview", "pattern", "expected_flow", "security_model",
    "resiliency_targets", "known_gaps", "diagnostic_hints",
]

# The highest-value sections for an investigation, in priority order — used when the
# rendered memory must be capped to fit a prompt budget.
INVESTIGATION_PRIORITY_KEYS = [
    "expected_flow", "diagnostic_hints", "known_gaps", "critical_thresholds",
    "security_model", "resiliency_targets", "dependencies", "components",
    "network_topology", "observability", "known_issues",
]


def section_label(key: str) -> str:
    """Display label for a section key (falls back to a title-cased key for custom ones)."""
    meta = _CATALOG_BY_KEY.get(key)
    if meta:
        return meta["label"]
    return key.replace("_", " ").strip().title() or "Section"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    if _PATH.exists():
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"memories": {}}


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def default_sections() -> list[dict[str, str]]:
    """The starter section list (label + key + empty content) for a new memory."""
    return [
        {"key": k, "label": section_label(k), "content": ""}
        for k in DEFAULT_SECTION_KEYS
    ]


def merge_ai_sections(
    existing_sections: list[dict[str, Any]] | None,
    ai_sections: dict[str, str],
) -> list[dict[str, str]]:
    """Merge an AI (re)draft into a memory's section list for a FULL "Generate with AI".

    OVERWRITES each existing section the model returned non-empty content for (a full draft
    is a regenerate, not just a gap-fill — the old behavior of only filling EMPTY sections
    meant a fully-populated memory silently kept its old content and appeared "not saved").
    Sections the model left out keep their existing content (so a partial draft never wipes
    good text). Author order is preserved; brand-new catalog sections are appended."""
    sections = [dict(s) for s in (existing_sections or default_sections())]
    present = {s.get("key") for s in sections}
    for s in sections:
        new_content = ai_sections.get(s.get("key", ""))
        if new_content and str(new_content).strip():
            s["content"] = str(new_content)
            s.pop("needs_review", None)  # a fresh draft clears the review flag
    for key, content in ai_sections.items():
        if key and key not in present and str(content or "").strip():
            sections.append({"key": key, "label": section_label(key), "content": str(content)})
    return sections


def get_memory(architecture_id: str) -> dict[str, Any] | None:
    """Return the memory for an architecture, or None if none exists yet."""
    raw = _read().get("memories", {}).get(architecture_id)
    return dict(raw) if raw is not None else None


def list_memories(tenant_id: str | None = None) -> list[dict[str, Any]]:
    """All memories (optionally tenant-scoped), newest-updated first."""
    out = [dict(m) for m in _read().get("memories", {}).values()]
    if tenant_id is not None:
        out = [m for m in out if (m.get("tenant_id") or "") in ("", tenant_id)]
    out.sort(key=lambda m: m.get("updated_at", ""), reverse=True)
    return out


def _clean_sections(sections: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Normalize incoming sections: keep order, fill labels, drop blanks-only entries."""
    cleaned: list[dict[str, str]] = []
    for s in sections or []:
        if not isinstance(s, dict):
            continue
        key = str(s.get("key") or "").strip()
        if not key:
            continue
        entry: dict[str, Any] = {
            "key": key,
            "label": str(s.get("label") or section_label(key)),
            "content": str(s.get("content") or ""),
        }
        # Preserve the optional "needs review" flag (review workflow) when set.
        if s.get("needs_review"):
            entry["needs_review"] = True
        cleaned.append(entry)
    return cleaned


def upsert_memory(
    architecture_id: str,
    *,
    workload_id: str = "",
    title: str = "",
    sections: list[dict[str, Any]] | None = None,
    enabled_for_investigations: bool | None = None,
    source: str | None = None,
    ai: dict[str, Any] | None = None,
    tenant_id: str = "",
    actor: str = "",
    reason: str = "Edited",
) -> dict[str, Any]:
    """Create or update an architecture's memory (read-modify-write)."""
    data = _read()
    memories = data.setdefault("memories", {})
    existing = memories.get(architecture_id, {})
    merged: dict[str, Any] = dict(existing)
    merged["id"] = existing.get("id") or str(uuid.uuid4())
    merged["architecture_id"] = architecture_id
    if workload_id or "workload_id" not in merged:
        merged["workload_id"] = workload_id or merged.get("workload_id", "")
    if tenant_id or "tenant_id" not in merged:
        merged["tenant_id"] = tenant_id or merged.get("tenant_id", "")
    if title or "title" not in merged:
        merged["title"] = title or merged.get("title", "")
    if sections is not None:
        merged["sections"] = _clean_sections(sections)
    elif "sections" not in merged:
        merged["sections"] = default_sections()
    if enabled_for_investigations is not None:
        merged["enabled_for_investigations"] = bool(enabled_for_investigations)
    elif "enabled_for_investigations" not in merged:
        merged["enabled_for_investigations"] = True
    if source is not None:
        merged["source"] = source
    elif "source" not in merged:
        merged["source"] = "manual"
    if ai is not None:
        merged["ai"] = ai
    elif "ai" not in merged:
        merged["ai"] = {}
    merged["created_at"] = existing.get("created_at") or _now()
    merged["updated_at"] = _now()
    if actor:
        merged["updated_by"] = actor
        if not existing:
            merged.setdefault("created_by", actor)
    memories[architecture_id] = merged
    _write(data)
    # Auto-snapshot a revision (deduped by content signature) so history is captured.
    # First write is labelled "Created" unless the caller gave a specific reason.
    from app.architectures import memory_revisions

    snap_reason = ("Created" if reason == "Edited" else reason) if not existing else reason
    memory_revisions.snapshot(architecture_id, merged, reason=snap_reason, actor=actor)
    return dict(merged)


def delete_memory(architecture_id: str) -> bool:
    data = _read()
    if architecture_id in data.get("memories", {}):
        del data["memories"][architecture_id]
        _write(data)
        from app.architectures import memory_revisions

        memory_revisions.delete_for(architecture_id)
        return True
    return False


def restore_revision(architecture_id: str, revision_id: str, actor: str = "") -> dict[str, Any] | None:
    """Restore a past revision's content onto the live memory. The pre-restore version is
    itself snapshotted (via the upsert auto-snapshot) so nothing is lost."""
    from app.architectures import memory_revisions

    if get_memory(architecture_id) is None:
        return None
    rev = memory_revisions.get_revision(architecture_id, revision_id)
    if rev is None:
        return None
    return upsert_memory(
        architecture_id,
        title=rev.get("title", ""),
        sections=rev.get("sections", []),
        enabled_for_investigations=rev.get("enabled_for_investigations", True),
        source=rev.get("source", "manual"),
        ai=rev.get("ai", {}),
        actor=actor,
        reason="Restored from history",
    )


def render_markdown(memory: dict[str, Any], architecture_name: str = "", workload_name: str = "") -> str:
    """Render a memory into the single Markdown 'Memory.md' document (preview + injection)."""
    title = (memory.get("title") or "").strip() or (
        f"{architecture_name} — Memory" if architecture_name else "Architecture Memory"
    )
    lines: list[str] = [f"# {title}", ""]
    if workload_name:
        lines += [f"> **Linked workload:** {workload_name}", ""]
    for s in memory.get("sections", []) or []:
        content = str(s.get("content") or "").strip()
        if not content:
            continue
        label = s.get("label") or section_label(str(s.get("key", "")))
        lines += [f"## {label}", "", content, ""]
    return "\n".join(lines).strip() + "\n"


def render_for_investigation(
    memory: dict[str, Any],
    architecture_name: str = "",
    workload_name: str = "",
    max_chars: int = 4000,
) -> str:
    """Render the memory for injection into a Deep Investigation, prioritizing the most
    diagnostically-useful sections and capping the total size to a prompt budget."""
    by_key = {str(s.get("key")): s for s in memory.get("sections", []) or []}
    # Order: priority sections first, then any remaining non-empty sections in author order.
    ordered_keys: list[str] = []
    for k in INVESTIGATION_PRIORITY_KEYS:
        if k in by_key:
            ordered_keys.append(k)
    for s in memory.get("sections", []) or []:
        k = str(s.get("key"))
        if k not in ordered_keys:
            ordered_keys.append(k)

    title = (memory.get("title") or "").strip() or (
        f"{architecture_name} — Memory" if architecture_name else "Architecture Memory"
    )
    header = f"# {title}"
    if workload_name:
        header += f"\n> Linked workload: {workload_name}"
    parts: list[str] = [header]
    used = len(header)
    for k in ordered_keys:
        s = by_key.get(k)
        if not s:
            continue
        content = str(s.get("content") or "").strip()
        if not content:
            continue
        label = s.get("label") or section_label(k)
        block = f"\n\n## {label}\n{content}"
        if used + len(block) > max_chars:
            # Truncate the final block to fit, then stop.
            remaining = max_chars - used
            if remaining > 120:  # only include a partial block if it's meaningfully long
                parts.append(block[:remaining] + "…")
            break
        parts.append(block)
        used += len(block)
    return "".join(parts).strip()
