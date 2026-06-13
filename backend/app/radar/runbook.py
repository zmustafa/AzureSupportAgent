"""Migration-runbook drafting for a Radar event.

Produces a step-by-step migration runbook for a retirement / breaking-change event,
grounded in the affected workload's Architecture Memory (the ``known_gaps`` and any
``runbook`` / ``operational_runbook`` sections) plus the impacted-resource list. Uses the
LLM when available (with a generous token cap to avoid the JSON/markdown truncation that
bit the architecture + memory designers); always falls back to a solid deterministic
template so the action never fails."""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("app.radar.runbook")

_RUNBOOK_KEYS = ("runbook", "operational_runbook", "known_gaps", "remediation", "expected_flow")


def _memory_context(architecture_id: str) -> str:
    if not architecture_id:
        return ""
    try:
        from app.architectures.memory import get_memory

        mem = get_memory(architecture_id)
    except Exception:  # noqa: BLE001
        return ""
    if not mem:
        return ""
    parts: list[str] = []
    for s in mem.get("sections", []) or []:
        if s.get("key") in _RUNBOOK_KEYS and s.get("content"):
            parts.append(f"## {s.get('label') or s['key']}\n{s['content']}")
    return "\n\n".join(parts)


def _template(event: dict[str, Any], memory_ctx: str) -> str:
    impacted = event.get("impacted_resources") or []
    lines = [
        f"# Migration runbook — {event.get('title') or event.get('service') or 'Azure lifecycle event'}",
        "",
        f"- **Type:** {event.get('change_type', 'retirement').replace('_', ' ').title()}",
        f"- **Tracking ID:** {event.get('tracking_id', '')}",
        f"- **Planned date:** {event.get('retirement_date') or 'TBD'}"
        + (f" ({event['days_until']} days away)" if event.get("days_until") is not None else ""),
        f"- **Recommended replacement:** {event.get('recommended_replacement') or 'See migration guidance.'}",
        f"- **Reference:** {event.get('migration_url') or 'n/a'}",
        f"- **Impacted resources:** {len(impacted)}",
        "",
        "## Steps",
        "1. Confirm the exact deadline and impacted resources in Azure Service Health / Advisor.",
        "2. Identify the owner of each impacted resource and notify them with this runbook.",
        "3. Stand up the replacement configuration in a non-production environment first.",
        "4. Validate functionality and performance against the replacement.",
        "5. Cut over production during a maintenance window; keep a rollback path until verified.",
        "6. Decommission the retiring resource/SKU/version and close the tracking item.",
        "",
        "## Impacted resources",
    ]
    for r in impacted[:50]:
        owner = r.get("owner") or "UNOWNED"
        lines.append(f"- `{r.get('name', '')}` ({r.get('type', '')}) — RG {r.get('resource_group', '')} / {r.get('region', '')} — owner: {owner}")
    if memory_ctx:
        lines += ["", "## From Architecture Memory", memory_ctx]
    return "\n".join(lines)


async def draft_runbook(event: dict[str, Any], *, architecture_id: str = "") -> dict[str, Any]:
    """Return {ok, runbook, used_ai}. Never raises."""
    memory_ctx = _memory_context(architecture_id)
    fallback = _template(event, memory_ctx)
    try:
        from app.agent.factory import build_provider

        provider = build_provider()
    except Exception:  # noqa: BLE001
        return {"ok": True, "runbook": fallback, "used_ai": False}

    impacted = event.get("impacted_resources") or []
    impacted_block = "\n".join(
        f"- {r.get('name', '')} ({r.get('type', '')}) RG={r.get('resource_group', '')} region={r.get('region', '')} owner={r.get('owner') or 'UNOWNED'}"
        for r in impacted[:50]
    )
    system = (
        "You are an Azure migration engineer. Draft a concise, actionable migration runbook "
        "in Markdown for the given retirement / breaking-change event. Include: a summary, "
        "prerequisites, ordered migration steps (with az CLI commands where helpful), "
        "validation, rollback, and a per-owner action list. Ground it in the supplied "
        "Architecture Memory and impacted-resource list. Do not invent resources."
    )
    user = (
        f"EVENT: {event.get('title', '')}\n"
        f"Type: {event.get('change_type', '')}\nTracking ID: {event.get('tracking_id', '')}\n"
        f"Planned date: {event.get('retirement_date', '')}\n"
        f"Recommended replacement: {event.get('recommended_replacement', '')}\n"
        f"Reference: {event.get('migration_url', '')}\n\n"
        f"IMPACTED RESOURCES:\n{impacted_block or '(none resolved)'}\n\n"
        f"ARCHITECTURE MEMORY CONTEXT:\n{memory_ctx or '(none)'}"
    )
    text = ""
    try:
        async for ev in provider.stream(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            None,
            max_tokens=8000,
        ):
            if ev.type == "token":
                text += ev.text
    except Exception as exc:  # noqa: BLE001
        log.warning("Runbook LLM draft failed: %s", exc)
        return {"ok": True, "runbook": fallback, "used_ai": False}

    text = text.strip()
    if len(text) < 80:
        return {"ok": True, "runbook": fallback, "used_ai": False}
    return {"ok": True, "runbook": text, "used_ai": True}
