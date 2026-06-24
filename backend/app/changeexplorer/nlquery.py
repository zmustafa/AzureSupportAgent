"""Natural-language change search for the Workload Change Explorer.

Turns a question like *"show me all VMs modified yesterday"* into a STRUCTURED query spec that
the UI applies to a loaded run's events. Two concerns are split deliberately:

* **WHEN (time window)** is parsed DETERMINISTICALLY (regex over relative phrases like "yesterday"
  / "last 7 days" / "today", plus explicit dates) relative to a supplied ``now``. We never trust
  the model for date math, so "yesterday" is always the exact, correct UTC day.
* **WHAT (filters)** — resource types, categories, actors, risk floor, operations, keyword — is
  produced by the LLM and then GROUNDED against the run's facets (hallucinated types/categories
  are dropped). Degrades to a keyword-only spec when no AI provider is available.

The endpoint returns the spec + whether the requested window is inside the loaded run; the client
applies the spec to its in-memory events (and offers to re-scan when the window isn't loaded).
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any

from app.changeexplorer.models import CATEGORIES, RISK_LABELS

log = logging.getLogger("app.changeexplorer.nlquery")

_AI_TIMEOUT_SECONDS = 14.0
_RISK_RANK = {"Critical": 4, "High": 3, "Medium": 2, "Low": 1, "Informational": 0}


# --------------------------------------------------------------------------- time parsing

def _day_bounds(d: datetime) -> tuple[datetime, datetime]:
    start = datetime.combine(d.date(), time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def parse_time_window(question: str, now: datetime) -> dict[str, str] | None:
    """Deterministically resolve a relative/explicit time phrase to a UTC ``{start_iso, end_iso,
    label}`` window, or ``None`` when the question names no time.

    ``now`` must be timezone-aware (UTC). Supported: today, yesterday, last/past N hours|days|weeks,
    last week, this week, this month, last N minutes, and an explicit ISO date (YYYY-MM-DD)."""
    q = question.lower()

    m = re.search(r"\b(?:last|past|previous)\s+(\d+)\s*(hour|hr|day|week|minute|min)s?\b", q)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = {
            "hour": timedelta(hours=n), "hr": timedelta(hours=n),
            "day": timedelta(days=n), "week": timedelta(weeks=n),
            "minute": timedelta(minutes=n), "min": timedelta(minutes=n),
        }[unit]
        return _win(now - delta, now, f"last {n} {unit}{'s' if n != 1 else ''}")

    if re.search(r"\byesterday\b", q):
        s, e = _day_bounds(now - timedelta(days=1))
        return _win(s, e, "yesterday")
    if re.search(r"\btoday\b", q):
        s, e = _day_bounds(now)
        return _win(s, now, "today")
    if re.search(r"\b(last|past)\s+week\b", q):
        return _win(now - timedelta(weeks=1), now, "last week")
    if re.search(r"\b(last|past)\s+month\b", q):
        return _win(now - timedelta(days=30), now, "last month")
    if re.search(r"\bthis\s+week\b", q):
        monday = now - timedelta(days=now.weekday())
        s, _ = _day_bounds(monday)
        return _win(s, now, "this week")
    if re.search(r"\bthis\s+month\b", q):
        s = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        return _win(s, now, "this month")

    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b.*?\b(\d{4})-(\d{2})-(\d{2})\b", q)
    if m:
        try:
            d1 = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
            d2 = datetime(int(m.group(4)), int(m.group(5)), int(m.group(6)), tzinfo=timezone.utc)
            _, e2 = _day_bounds(d2)  # inclusive end day
            lo, hi = sorted([d1, e2])
            return _win(lo, hi, f"{m.group(1)}-{m.group(2)}-{m.group(3)} → {m.group(4)}-{m.group(5)}-{m.group(6)}")
        except ValueError:
            return None

    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", q)
    if m:
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
            s, e = _day_bounds(d)
            return _win(s, e, m.group(0))
        except ValueError:
            return None
    return None


def _win(start: datetime, end: datetime, label: str) -> dict[str, str]:
    return {"start_iso": start.isoformat(), "end_iso": end.isoformat(), "label": label}


# --------------------------------------------------------------------------- AI parse (the "what")

_SYS = (
    "You convert a natural-language question about Azure resource CHANGES into a STRUCTURED JSON "
    "filter. Use ONLY the provided resource types, categories and actors; resolve casing/aliases "
    "to the exact provided spelling (e.g. 'VM' / 'virtual machine' -> "
    "'microsoft.compute/virtualmachines'). Do NOT include any time/date in the output — the time "
    "window is handled separately. Return STRICT JSON (omit empty fields):\n"
    "{\n"
    '  "explanation": "one short sentence describing the filter",\n'
    '  "resource_types": ["microsoft.compute/virtualmachines"],  // exact lowercase ARM types\n'
    '  "categories": ["RBAC", "Network"],            // from the provided category list\n'
    '  "actors": ["someone@contoso.com"],\n'
    '  "actor_types": ["ServicePrincipal"],          // User|ServicePrincipal|ManagedIdentity|AzurePolicy|System\n'
    '  "operations": ["Create","Update","Delete"],\n'
    '  "risk_min": "High",                            // Critical|High|Medium|Low|Informational\n'
    '  "name_contains": "",\n'
    '  "keyword": ""                                  // free-text fallback matched on name/summary/operation\n'
    "}\n"
    "Return ONLY the JSON object — no prose, no markdown."
)


async def _complete_json(system: str, user: str) -> Any:
    """Stream a completion and parse the JSON object out of it. None on any failure."""
    from app.agent.factory import build_provider
    from app.core.utils import safe_json_parse

    provider = build_provider()
    text = ""
    async for ev in provider.stream(
        [{"role": "system", "content": system}, {"role": "user", "content": user}], None
    ):
        if ev.type == "token":
            text += ev.text
    t = text.strip()
    if "```" in t:
        mm = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
        if mm:
            t = mm.group(1).strip()
    if not t.startswith("{"):
        mm = re.search(r"(\{.*\})", t, re.DOTALL)
        if mm:
            t = mm.group(1)
    return safe_json_parse(t, default=None)


def _keyword_spec(question: str) -> dict[str, Any]:
    """Fallback when the AI is unavailable: treat the whole question as a keyword search (minus
    obvious time words so 'VMs yesterday' still keyword-matches 'VMs')."""
    kw = re.sub(r"\b(yesterday|today|last|past|previous|week|month|day|days|hour|hours|this)\b", " ", question.lower())
    kw = re.sub(r"\s+", " ", kw).strip()
    return {"explanation": "Keyword search (AI unavailable).", "keyword": kw}


def _ground(parsed: dict[str, Any], facets: dict[str, Any]) -> dict[str, Any]:
    """Keep only filter values that exist in the run's facets / the canonical enums."""
    known_types = {t.lower() for t in (facets.get("resource_types") or [])}
    known_cats = set(CATEGORIES)
    known_actors = {a for a in (facets.get("actors") or [])}

    out: dict[str, Any] = {"explanation": str(parsed.get("explanation") or "").strip()}
    rtypes = [str(t).lower() for t in (parsed.get("resource_types") or []) if str(t).strip()]
    rtypes = [t for t in rtypes if t in known_types] if known_types else rtypes
    if rtypes:
        out["resource_types"] = rtypes
    cats = [str(c).strip() for c in (parsed.get("categories") or []) if str(c).strip() in known_cats]
    if cats:
        out["categories"] = cats
    actors = [str(a).strip() for a in (parsed.get("actors") or []) if str(a).strip()]
    actors = [a for a in actors if a in known_actors] if known_actors else actors
    if actors:
        out["actors"] = actors
    atypes = [str(a).strip() for a in (parsed.get("actor_types") or []) if str(a).strip()]
    if atypes:
        out["actor_types"] = atypes
    ops = [str(o).strip().capitalize() for o in (parsed.get("operations") or []) if str(o).strip()]
    ops = [o for o in ops if o in ("Create", "Update", "Delete")]
    if ops:
        out["operations"] = ops
    rmin = str(parsed.get("risk_min") or "").strip().capitalize()
    if rmin in RISK_LABELS:
        out["risk_min"] = rmin
    if str(parsed.get("name_contains") or "").strip():
        out["name_contains"] = str(parsed["name_contains"]).strip()
    if str(parsed.get("keyword") or "").strip():
        out["keyword"] = str(parsed["keyword"]).strip()
    return out


async def parse_query(question: str, *, now: datetime, facets: dict[str, Any]) -> dict[str, Any]:
    """Parse a NL change query into ``{time_window, ...filters}``. Time is deterministic; the
    rest is AI-grounded (keyword fallback when no provider)."""
    window = parse_time_window(question, now)
    try:
        user = (
            f"Question: {question}\n\n"
            f"Available resource types (lowercase ARM): {_json.dumps((facets.get('resource_types') or [])[:200])}\n"
            f"Available categories: {_json.dumps(CATEGORIES)}\n"
            f"Available actors: {_json.dumps((facets.get('actors') or [])[:100])}\n"
            "Return only the JSON object."
        )
        parsed = await asyncio.wait_for(_complete_json(_SYS, user), timeout=_AI_TIMEOUT_SECONDS)
    except (Exception, asyncio.TimeoutError) as exc:  # noqa: BLE001
        log.info("changeexplorer nlquery AI unavailable: %s", exc)
        parsed = None

    spec = _ground(parsed, facets) if isinstance(parsed, dict) else _keyword_spec(question)
    spec["time_window"] = window
    return spec


# --------------------------------------------------------------------------- apply (server-side, also tested)

def _parse_iso(s: str) -> datetime | None:
    """Parse an ISO timestamp to an aware UTC datetime, tolerating a trailing 'Z'. None on failure.
    (Run times are stored as '...Z' while our windows use '+00:00'; comparing the raw STRINGS is
    wrong because 'Z' > '+', so all time math goes through this.)"""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def apply_spec(events: list[dict[str, Any]], spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Filter a run's events by a parsed spec. All clauses are ANDed. (The UI applies the same
    logic to its in-memory events; this mirror exists for the endpoint + tests.)"""
    rtypes = {t.lower() for t in spec.get("resource_types", [])}
    cats = set(spec.get("categories", []))
    actors = set(spec.get("actors", []))
    atypes = set(spec.get("actor_types", []))
    ops = set(spec.get("operations", []))
    risk_floor = _RISK_RANK.get(spec.get("risk_min", ""), None)
    name_sub = str(spec.get("name_contains") or "").lower()
    keyword = str(spec.get("keyword") or "").lower()
    win = spec.get("time_window")
    win_start = _parse_iso(win.get("start_iso")) if win else None
    win_end = _parse_iso(win.get("end_iso")) if win else None

    out: list[dict[str, Any]] = []
    for e in events:
        if rtypes and (e.get("resourceType", "") or "").lower() not in rtypes:
            continue
        if cats and e.get("category", "") not in cats:
            continue
        if actors and e.get("actor", "") not in actors:
            continue
        if atypes and e.get("actorType", "") not in atypes:
            continue
        if ops and e.get("operation", "") not in ops:
            continue
        if risk_floor is not None and _RISK_RANK.get(e.get("riskLabel", ""), 0) < risk_floor:
            continue
        if name_sub and name_sub not in (e.get("resourceName", "") or "").lower():
            continue
        if keyword:
            hay = f"{e.get('resourceName','')} {e.get('plainEnglishSummary','')} {e.get('operation','')}".lower()
            if keyword not in hay:
                continue
        if win_start and win_end:
            et = _parse_iso(e.get("eventTime", ""))
            if et is None or not (win_start <= et < win_end):
                continue
        out.append(e)
    return out


def window_in_run(window: dict[str, str] | None, run_start: str, run_end: str) -> bool:
    """True when the requested window is fully covered by the loaded run's [start, end]."""
    if not window:
        return True  # no time constraint → the loaded run is fine
    rs, re_ = _parse_iso(run_start), _parse_iso(run_end)
    ws, we = _parse_iso(window.get("start_iso", "")), _parse_iso(window.get("end_iso", ""))
    if not (rs and re_ and ws and we):
        return False
    return rs <= ws and we <= re_
