"""AI helpers for assessments: generate a custom check, and a remediation-ticket helper.

Mirrors the custom-agent designer pattern: a grounded LLM call returns a strict JSON
check definition (KQL control + metadata) the admin can review and save."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger("app.assessments.designer")

_GEN_SYSTEM = """\
You design Azure assessment CONTROLS for a Well-Architected reviewer. Given a plain-English
goal, produce ONE deterministic control implemented as an Azure Resource Graph (KQL)
query that flags VIOLATING resources.

Rules:
- The query body MUST start with a `| where type =~ '<arm/type>'` filter and keep ONLY
  resources that VIOLATE the control, ending with:
  `| project id, name, type, resourceGroup, subscriptionId`.
- Do NOT include the leading `Resources | where <scope>` — that scope is prepended at
  runtime. Your body is appended after it, so begin with `| where ...`.
- pillar is "security" or "reliability". severity is one of critical|error|warning|info.
- resource_types is the lowercased ARM type(s) the control inspects (drives applicability).
- frameworks maps to control ids you are confident about (cis, nist, iso); omit if unsure.

Reply with ONLY a JSON object (no code fence, no prose):
{
  "pillar": "security|reliability",
  "title": "<short control title>",
  "description": "<1-2 sentences: what it checks and why it matters>",
  "severity": "critical|error|warning|info",
  "resource_types": ["microsoft.<provider>/<type>"],
  "kql": "| where type =~ '...' | where <violation predicate> | project id, name, type, resourceGroup, subscriptionId",
  "remediation": "<how to fix>",
  "remediation_command": "<optional single az command template, or empty>",
  "frameworks": {"cis": [], "nist": [], "iso": []}
}
"""


async def _complete(messages: list[dict[str, Any]]) -> str:
    from app.agent.factory import build_provider_for

    provider = build_provider_for(None, None)
    parts: list[str] = []
    try:
        async for ev in provider.stream(messages, None):
            if ev.type == "token":
                parts.append(ev.text)
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
    return "".join(parts)


def _extract_json(text: str) -> Any:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


async def generate_check(goal: str) -> dict[str, Any] | None:
    """Generate a custom assessment check definition from a plain-English goal."""
    try:
        text = await _complete(
            [{"role": "system", "content": _GEN_SYSTEM}, {"role": "user", "content": goal.strip()[:2000]}]
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Assessment check generation failed: %s", exc)
        return None
    data = _extract_json(text)
    if not isinstance(data, dict):
        return None
    # Normalize / validate.
    pillar = str(data.get("pillar", "security")).lower()
    if pillar not in ("security", "reliability"):
        pillar = "security"
    sev = str(data.get("severity", "warning")).lower()
    if sev not in ("critical", "error", "warning", "info"):
        sev = "warning"
    rtypes = [str(t).lower() for t in (data.get("resource_types") or []) if t]
    kql = str(data.get("kql", "")).strip()
    fw = data.get("frameworks") or {}
    frameworks = {k: [str(x) for x in (fw.get(k) or [])] for k in ("cis", "nist", "iso") if fw.get(k)}
    return {
        "pillar": pillar,
        "title": str(data.get("title", "")).strip()[:200],
        "description": str(data.get("description", "")).strip()[:2000],
        "severity": sev,
        "resource_types": rtypes,
        "kql": kql,
        "remediation": str(data.get("remediation", "")).strip()[:2000],
        "remediation_command": str(data.get("remediation_command", "")).strip()[:1000],
        "frameworks": frameworks,
    }
