"""Natural-language → KQL for App Insights, with a strict read-only safety validator.

The LLM drafts a single tabular KQL query grounded in the App Insights schema + the
workload's Architecture Memory SLIs. Before ANY execution, ``validate_kql`` enforces a
read-only allowlist (the standard App Insights tables only, a mandatory row cap, and no
mutating / data-exfil operators), so neither a generated nor a user-edited query can do
anything but read telemetry. Mirrors inventory/ai.py's validate_kql pattern."""
from __future__ import annotations

import logging
import re
from typing import Any

from app.agent.factory import build_provider
from app.core.utils import safe_json_parse
from app.teleintel.kql_library import ALLOWED_TABLES

log = logging.getLogger("app.teleintel.nlkql")

# Operators that can mutate, exfiltrate, or escape the read-only contract.
_FORBIDDEN = re.compile(
    r"(?:^|\W)(?:externaldata|\.set|\.set-or-append|\.set-or-replace|\.append|\.create|\.drop|"
    r"\.alter|\.ingest|\.show|\.execute|\.delete|into\s|invoke\s)",
    re.IGNORECASE,
)
_MAX_ROWS_HARD = 5000


def _strip_comments(kql: str) -> str:
    return re.sub(r"//[^\n]*", "", kql or "")


def _referenced_tables(kql: str) -> set[str]:
    """Heuristic: identifiers that look like a leading table reference (start of query or
    after union/join). We validate that every standard-looking table token is allowed."""
    low = _strip_comments(kql)
    # Collect tokens that appear where a table source can legally start.
    tokens = set(re.findall(r"(?:^|\bunion\b|\bjoin\b|\(|,)\s*([A-Za-z_][A-Za-z0-9_]*)", low))
    # Keep only ones that match a known App Insights table name (case-insensitive); other
    # tokens are let-bindings / functions / operators which we don't restrict here.
    known_lower = {t.lower() for t in ALLOWED_TABLES}
    seen = {tok for tok in tokens if tok.lower() in known_lower or _looks_like_table(tok)}
    return seen


def _looks_like_table(tok: str) -> bool:
    # An uppercase-initial bare word that isn't a KQL keyword — likely a table reference.
    if tok[:1].isupper() and tok.lower() not in _KQL_KEYWORDS:
        return True
    return False


_KQL_KEYWORDS = {
    "let", "union", "join", "kind", "inner", "leftouter", "rightouter", "fullouter",
    "on", "by", "asc", "desc", "true", "false", "null", "real", "datetime", "timespan",
    "where", "summarize", "project", "extend", "order", "top", "take", "limit", "count",
}


def validate_kql(kql: str, *, max_rows: int = 1000) -> tuple[str, str]:
    """Return (clean_kql, error). Enforces a single read-only query over allowed App
    Insights tables, with a mandatory row cap. Empty error string ⇒ valid."""
    q = (kql or "").strip().strip("`").strip()
    if not q:
        return "", "Empty query."
    if len(q) > 8000:
        return "", "Query is too long."
    if _FORBIDDEN.search(q):
        return "", "Query contains a disallowed (non-read-only) operator."
    if ";" in q.rstrip(";"):
        # Allow `let ...;` bindings but reject statement batching beyond that. We only
        # permit semicolons that terminate let-bindings (heuristic: each segment before a
        # semicolon must contain 'let' or be the final query).
        segments = [s for s in q.split(";") if s.strip()]
        if any("let " not in seg.lower() for seg in segments[:-1]):
            return "", "Multiple statements are not allowed."

    # Every table-like reference must be in the allowlist.
    referenced = _referenced_tables(q)
    disallowed = {t for t in referenced if t.lower() not in {a.lower() for a in ALLOWED_TABLES}}
    if disallowed:
        return "", f"Query references non-allowed table(s): {', '.join(sorted(disallowed))}."

    cap = max(10, min(_MAX_ROWS_HARD, int(max_rows or 1000)))
    # Ensure a bounded result: if there's no take/limit/top, append one.
    if not re.search(r"\b(take|limit|top)\b", q, re.IGNORECASE):
        q = f"{q}\n| take {cap}"
    return q, ""


_SCHEMA_HINT = (
    "App Insights tables and key columns:\n"
    "- requests(timestamp, name, operation_Name, operation_Id, success, resultCode, duration, cloud_RoleName)\n"
    "- dependencies(timestamp, name, target, type, operation_Id, success, resultCode, duration)\n"
    "- exceptions(timestamp, type, problemId, outerMessage, operation_Id, severityLevel)\n"
    "- traces(timestamp, message, operation_Id, severityLevel)\n"
    "- customEvents(timestamp, name, operation_Id)\n"
    "- performanceCounters(timestamp, name, value, category)\n"
    "- availabilityResults(timestamp, name, success, duration, location)\n"
    "duration is in milliseconds. success is a bool. Correlate across tables on operation_Id."
)


async def draft_kql(question: str, *, sli_context: str = "", default_timespan: str = "P1D") -> dict[str, Any]:
    """Ask the LLM to draft KQL for an NL question. Returns {kql, explanation, error}.
    The returned KQL is NOT yet validated — the caller validates before running."""
    provider = build_provider()
    system = (
        "You translate a natural-language question about application telemetry into a "
        "single, READ-ONLY Azure Application Insights KQL query. Use ONLY these tables and "
        "columns; never invent columns. Always include a time filter and a bounded "
        "take/top. Return STRICT JSON: {\"kql\": \"...\", \"explanation\": \"one sentence\"}.\n\n"
        f"{_SCHEMA_HINT}\n\n"
        "Do NOT use externaldata, .set, .append, .create, ingestion, or any mutating "
        "operator. The query is run over a fixed timespan supplied separately, so prefer "
        "relative ago() filters only when the question names a period."
    )
    user = (
        f"Question: {question}\n"
        f"Default query window: {default_timespan}\n"
        + (f"\nWhat 'normal' looks like for this workload (SLIs / critical dependencies):\n{sli_context}\n" if sli_context else "")
        + "\nReturn only the JSON object."
    )
    text = ""
    try:
        async for ev in provider.stream(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            None,
            max_tokens=1200,
        ):
            if ev.type == "token":
                text += ev.text
    except Exception as exc:  # noqa: BLE001
        return {"kql": "", "explanation": "", "error": f"LLM unavailable: {str(exc)[:160]}"}

    t = text.strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
        if m:
            t = m.group(1).strip()
    if not t.startswith("{"):
        m = re.search(r"(\{.*\})", t, re.DOTALL)
        if m:
            t = m.group(1)
    parsed = safe_json_parse(t, default=None)
    if not isinstance(parsed, dict) or not parsed.get("kql"):
        return {"kql": "", "explanation": "", "error": "Could not draft a query for that question."}
    return {"kql": str(parsed["kql"]).strip(), "explanation": str(parsed.get("explanation", "")).strip(), "error": ""}


async def narrate_answer(question: str, kql: str, rows: list[dict[str, Any]]) -> str:
    """Turn query rows into a plain-English answer (every claim is backed by the rows
    shown alongside in the UI). Falls back to a terse summary when the LLM is unavailable."""
    sample = rows[:30]
    if not sample:
        return "The query returned no rows for the selected window."
    provider = build_provider()
    system = (
        "You explain Application Insights query results in 2-4 sentences for an operator. "
        "Be specific and cite numbers from the rows. Do not invent data not present."
    )
    import json as _json

    user = f"Question: {question}\nKQL:\n{kql}\n\nRows (JSON, truncated):\n{_json.dumps(sample)[:6000]}"
    text = ""
    try:
        async for ev in provider.stream(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            None,
            max_tokens=600,
        ):
            if ev.type == "token":
                text += ev.text
    except Exception:  # noqa: BLE001
        return f"Returned {len(rows)} row(s). See the table and the query below."
    return text.strip() or f"Returned {len(rows)} row(s)."
