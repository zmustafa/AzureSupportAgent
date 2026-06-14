"""Runtime application settings (admin-configurable, persisted to JSON).

Mirrors the pattern of llm_config: a small JSON file under backend/.data so admins can
tune behavior from the dashboard WITHOUT a restart. Read on each request.

Covers the "advanced settings" found in ChatGPT/Claude-style apps:
- custom_instructions: a global persona/system prompt prepended to every chat.
- max_tokens: response length cap applied to providers that support it.
- auto_title: auto-name new chats from the first user message.
- scope_clarification: ask the user to pick a subscription for ambiguous questions.
- mgmt_group_clarification: ask the user to pick a management group for governance-scoped
  questions (policy/compliance/org-wide) before the agent runs.
- suggestions: show follow-up suggestion chips.
- response_style: a preset that nudges verbosity/tone.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "app_settings.json"

# Severities that carry a scoring weight (in worst→least order, for badges/sorting).
ASSESSMENT_SEVERITIES = ("critical", "error", "warning", "info")
_DEFAULT_SEVERITY_WEIGHTS = {"critical": 10, "error": 6, "warning": 3, "info": 1}
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _is_hex_color(v: Any) -> bool:
    """True when v is a '#rrggbb' hex color string."""
    return isinstance(v, str) and bool(_HEX_COLOR_RE.match(v.strip()))

RESPONSE_STYLES = {
    "default": "",
    "concise": "Prefer concise, direct answers. Lead with the result; minimize preamble.",
    "detailed": (
        "Give thorough, well-structured answers with headings, tables where useful, and "
        "clear next steps."
    ),
    "expert": (
        "Assume a senior cloud engineer audience. Be precise and technical; skip basics "
        "and focus on root cause, evidence, and exact remediation steps."
    ),
}

def _env_mcp_read_only_default() -> bool:
    """Initial value for the runtime mcp_read_only toggle: the MCP_READ_ONLY env/.env
    setting. Once an admin changes it in the dashboard, the saved value wins."""
    try:
        from app.core.config import get_settings

        return bool(get_settings().mcp_read_only)
    except Exception:  # noqa: BLE001 - settings not importable in some contexts
        return True


def _env_auto_execute_writes_default() -> bool:
    """Initial value for the runtime 'auto-execute writes' toggle, seeded from the
    AGENT_WRITE_POLICY env/.env setting ('off' => auto-execute, 'gated' => not)."""
    try:
        from app.core.config import get_settings

        return get_settings().agent_write_policy == "off"
    except Exception:  # noqa: BLE001 - settings not importable in some contexts
        return False


DEFAULTS: dict[str, Any] = {
    "custom_instructions": "",
    "response_style": "default",
    "max_tokens": 32000,
    "auto_title": True,
    "scope_clarification": True,
    # Ask the user to pick a management group for governance-scoped questions
    # (policy/compliance/org-wide). Opt-in: defaults off so it never double-prompts.
    "mgmt_group_clarification": False,
    # On the first message of a new chat, propose up to 5 sharper problem statements
    # (matched from the built-in Azure problem catalog) for the user to pick from.
    # Opt-in: defaults off.
    "propose_problems": False,
    "suggestions": True,
    # --- Deep investigation -------------------------------------------------------
    # Run multiple hypothesis sub-agents at once (parallel validation), then combine
    # their evidence at the conclusion. Speeds up deep investigations significantly.
    "deep_parallel_enabled": True,
    # How many sub-agents may validate hypotheses simultaneously (1-12). Ignored when
    # deep_parallel_enabled is False (then validation runs one at a time).
    "deep_parallel_count": 3,
    # Verbosity of the live "Working on your request…" progress feed shown in the chat:
    #   compact  — only high-level phases (sending, responding, writing the answer)
    #   normal   — phases + tool names + result summaries (no params/reasoning)
    #   detailed — everything, incl. the model's reasoning blocks and tool parameters
    "progress_detail": "detailed",
    "retention_days": 0,  # 0 = keep forever
    # MCP tool surface: when True only read/investigation tools are exposed; when
    # False, mutating tools are also exposed (still gated for approval). Seeded from
    # the MCP_READ_ONLY env default; the dashboard toggle overrides it at runtime.
    "mcp_read_only": _env_mcp_read_only_default(),
    # When True, the EntraID (Microsoft Graph) MCP server is also exposed to the default
    # assistant, so directory questions (users, groups, app/SP secrets, MFA, sign-ins,
    # conditional access) can be answered live. Custom agents opt in per-agent via
    # allow_all_entra. Off by default — an admin enables it once a connection with Graph
    # permissions is configured.
    "entra_mcp_enabled": False,
    # When True, mutating/write tools execute IMMEDIATELY (no approval pause). When
    # False, writes are gated. Seeded from AGENT_WRITE_POLICY; dashboard overrides it.
    "auto_execute_writes": _env_auto_execute_writes_default(),
    # --- Built-in utility tools (network diagnostics + web fetch) ------------------
    # First-party read-only tools the agent can call: web fetch, HTTP probe, DNS lookup,
    # TCP port check, ping, traceroute. ON by default (read-only, SSRF-guarded); an admin
    # can flip this kill-switch off, disable individual tools, or restrict egress.
    "builtin_tools_enabled": True,
    # Tool names to hide even when builtins are enabled (e.g. ["net_traceroute"]).
    "builtin_tools_disabled": [],
    # Optional egress controls applied to every built-in network tool. Denylist wins; if
    # an allowlist is set, only matching hosts (exact or sub-domain) are permitted.
    "network_egress_denylist": [],
    "network_egress_allowlist": [],
    # Per-call timeout (seconds) for built-in network tools (capped at 30).
    "network_tool_timeout_seconds": 10,
    # --- Sandbox troubleshooting VMs ----------------------------------------------
    # Master kill-switch for the sandbox-VM tools (vm_exec / vm_list / vm_read_file) the
    # agent can use to run diagnostic commands FROM an onboarded VM inside a workload's
    # VNet. ON by default; an admin can flip it off without removing the onboarded VMs.
    "sandbox_tools_enabled": True,
    # Per-command timeout (seconds) for SSH commands on a sandbox VM.
    "sandbox_command_timeout_seconds": 60,
    # When a vm_exec command fails because a diagnostic tool isn't installed, automatically
    # install it with the box's package manager (e.g. apt-get) and retry the command once.
    # These are disposable sandboxes, so this is on by default; flip off to require the
    # agent to install tools explicitly. Never auto-installs in read-only / deep-investigation
    # mode, and never when no package manager / sudo is available.
    "sandbox_auto_install": True,
    # --- Advanced agent tuning (see AdminView "Advanced" card for explanations) ---
    # Max tool calls the agent may chain in a single turn before it must answer.
    "max_tool_iterations": 16,
    # Max characters of a normal tool result fed back to the model (protects context).
    "tool_result_limit": 20000,
    # Max characters of a tool DISCOVERY ("learn") result — these list a service's
    # sub-commands and are large, so they need more room than normal results.
    "tool_discovery_limit": 60000,
    # Seconds to wait for a single LLM streaming request before timing out.
    "request_timeout_seconds": 180,
    # --- Host command execution (Run button on az-cli code blocks) ----------------
    # Master switch. When False, the Run button is hidden and the exec endpoint 403s.
    # OFF by default — an admin must explicitly opt in.
    "command_execution_enabled": False,
    # Which CLI binaries may be executed. Only these are ever allowed (az-cli focused).
    "command_allowlist": ["az"],
    # Wall-clock seconds before a running command is killed.
    "command_timeout_seconds": 120,
    # --- Assessments: scoring (admin-tunable) -------------------------------------
    # How much a FAILING control of each severity drags down the 0-100 pillar score.
    # Higher = more impact. Read at scoring time so changes apply to new runs only.
    "assessment_severity_weights": dict(_DEFAULT_SEVERITY_WEIGHTS),
    # Score color bands for the dashboard: >= good is green (healthy), >= warn is amber
    # (at risk), below warn is red (poor).
    "assessment_score_good": 80,
    "assessment_score_warn": 50,
    # --- Assessments: execution engine (admin-tunable) ----------------------------
    # Max controls evaluated concurrently per run (each may issue paged Resource Graph
    # queries). Bounds load + ARG throttling while keeping a 60+ control run fast.
    "assessment_concurrency": 6,
    # Wall-clock seconds before a single control is abandoned and marked 'error' (so a
    # hung query can never block the whole run).
    "assessment_check_timeout_s": 90,
    # Overall wall-clock budget for a run's control phase; controls not yet finished when
    # the budget elapses are marked 'error' (excluded from the score, surfaced honestly).
    "assessment_run_budget_s": 1800,
    # Confidence threshold: a run whose evaluated-control coverage is >= this percent is
    # 'high' confidence; >= (this - 15) is 'medium'; below that is 'low' (score provisional).
    "assessment_confidence_high_pct": 98,
    # --- Architecture: taxonomy (admin-tunable) -----------------------------------
    # Per-category hex color overrides for diagram node accents. Empty = built-in
    # palette. Keys must be known category ids; values are '#rrggbb'.
    "architecture_category_colors": {},
    # --- Identity dashboard --------------------------------------------------------
    # Default expiry window (days) for the Identity overview (presets 30/60/90).
    "identity_expiry_days": 90,
    # Server-side cache TTL (seconds) for identity snapshots — the Graph aggregation is
    # slow, so the dashboard serves a cached snapshot until it ages past this. Default 6h.
    "identity_cache_ttl_s": 21600,
    # Max privileged users scanned for MFA status per refresh (the scan is N Graph calls;
    # results are labelled "sampled" when more privileged users exist than this cap).
    "identity_mfa_scan_cap": 50,
    # --- AMBA Monitoring Coverage --------------------------------------------------
    # Server-side cache TTL (seconds) for coverage snapshots — the Resource Graph scans
    # are slow, so the dashboard serves a cached snapshot until it ages past this (6h).
    "amba_cache_ttl_s": 21600,
    # How a ⚠ misconfigured alert counts toward coverage %: True = full gap (0 credit),
    # False = half credit (0.5). Either way it's surfaced distinctly in the matrix.
    "amba_misconfig_counts_as_gap": True,
    # Tolerance (%) for treating an existing alert's threshold as matching the baseline
    # before flagging it as misconfigured.
    "amba_threshold_tolerance_pct": 10,
    # --- Telemetry Coverage (diagnostic settings auditor) --------------------------
    # Server-side cache TTL (seconds) for telemetry snapshots (per-resource diag reads
    # are slow). Default 6h.
    "telemetry_cache_ttl_s": 21600,
    # Approved Log Analytics workspace ids (ARM ids). Destinations not on this list are
    # flagged as drift. Empty = don't flag destination drift.
    "telemetry_approved_workspaces": [],
    # Max resources scanned for diagnostic settings per refresh (each is one Azure call).
    "telemetry_per_resource_scan_cap": 200,
    # --- Backup & DR Coverage ------------------------------------------------------
    # Server-side cache TTL (seconds) for backup/DR snapshots. Default 6h.
    "backupdr_cache_ttl_s": 21600,
    # A DR failover test older than this many days is flagged stale.
    "backupdr_stale_drill_days": 180,
    # A successful backup job must be within this many hours to be green.
    "backupdr_last_job_sla_hours": 24,
    # Max resources scanned for backup/DR facts per refresh.
    "backupdr_per_resource_scan_cap": 200,
    # --- Evidence Locker (investigation snapshots) ---------------------------------
    # Retention (days) for standard-class snapshots; audit-class is retained ~7y and never
    # auto-purged. The scheduler purge spares audit-class.
    "evidence_retention_standard_days": 90,
    "evidence_retention_audit_days": 2555,
    # Include a (gated) metrics window by default in new snapshots.
    "evidence_include_metrics_default": False,
    # --- Retirement & Breaking-Change Radar ----------------------------------------
    # Server-side cache TTL (seconds) for radar snapshots. Default 6h.
    "radar_cache_ttl_s": 21600,
    # Deadline lead-time thresholds (days) for the scheduled digest: an item pages when it
    # crosses the smallest applicable threshold. Also used by the countdown rail bands.
    "radar_digest_lead_days": [90, 60, 30],
    # Optional public Azure Updates RSS feed — the ONLY net-new external fetch. Off by
    # default; items may lag announcements by ~2 weeks (advisory only).
    "radar_azure_updates_feed_enabled": False,
    "radar_azure_updates_feed_url": "",
    # --- Telemetry Intelligence (AI correlation & triage over App Insights) ---------
    # Server-side cache TTL (seconds) for teleintel timeline + smart-detection snapshots.
    "teleintel_cache_ttl_s": 21600,
    # Default query window (ISO-8601 duration) for telemetry queries.
    "teleintel_default_timespan": "P1D",
    # Max rows returned by an NL/edited KQL query (also enforced as a `take` cap).
    "teleintel_max_rows": 1000,
    # --- Performance Profiler (profile a workload against AMBA) ---------------------
    "perfprofile_cache_ttl_s": 21600,
    "perfprofile_window": "P1D",
    "perfprofile_interval": "PT15M",
    "perfprofile_scan_cap": 200,
}

# Binaries an admin is permitted to add to the allowlist (defense-in-depth: even if the
# settings file is tampered with, only these can ever run).
ALLOWED_COMMAND_BINARIES = ["az", "azd", "kubectl"]


def load_settings() -> dict[str, Any]:
    data = dict(DEFAULTS)
    if _PATH.exists():
        try:
            saved = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                data.update({k: v for k, v in saved.items() if k in DEFAULTS})
        except (json.JSONDecodeError, OSError):
            pass
    return data


def save_settings(updates: dict[str, Any]) -> dict[str, Any]:
    current = load_settings()
    for k, v in updates.items():
        if k in DEFAULTS and v is not None:
            current[k] = v
    # Clamp numeric ranges.
    current["max_tokens"] = max(256, min(32000, int(current["max_tokens"])))
    current["retention_days"] = max(0, int(current["retention_days"]))
    current["max_tool_iterations"] = max(1, min(50, int(current["max_tool_iterations"])))
    current["tool_result_limit"] = max(2000, min(200000, int(current["tool_result_limit"])))
    current["tool_discovery_limit"] = max(2000, min(400000, int(current["tool_discovery_limit"])))
    current["request_timeout_seconds"] = max(30, min(600, int(current["request_timeout_seconds"])))
    current["command_timeout_seconds"] = max(5, min(900, int(current["command_timeout_seconds"])))
    current["deep_parallel_count"] = max(1, min(12, int(current.get("deep_parallel_count", 3))))
    current["deep_parallel_enabled"] = bool(current.get("deep_parallel_enabled", True))
    # Sanitize the command allowlist: keep only permitted, de-duplicated binaries.
    raw_allow = current.get("command_allowlist") or ["az"]
    if not isinstance(raw_allow, list):
        raw_allow = ["az"]
    seen: list[str] = []
    for b in raw_allow:
        b = str(b).strip().lower()
        if b in ALLOWED_COMMAND_BINARIES and b not in seen:
            seen.append(b)
    current["command_allowlist"] = seen or ["az"]
    current["command_execution_enabled"] = bool(current.get("command_execution_enabled", False))
    # Built-in utility tools: coerce flags + clamp timeout + normalize string lists.
    current["builtin_tools_enabled"] = bool(current.get("builtin_tools_enabled", True))
    current["network_tool_timeout_seconds"] = max(
        1, min(30, int(current.get("network_tool_timeout_seconds", 10)))
    )
    for _key in ("builtin_tools_disabled", "network_egress_denylist", "network_egress_allowlist"):
        raw = current.get(_key) or []
        if not isinstance(raw, list):
            raw = []
        cleaned: list[str] = []
        for item in raw:
            s = str(item).strip().lower()
            if s and s not in cleaned:
                cleaned.append(s)
        current[_key] = cleaned
    # Assessment severity weights: keep only the 4 known severities, clamp 0..100, and
    # merge onto defaults so a partial update can't drop a severity.
    weights = dict(_DEFAULT_SEVERITY_WEIGHTS)
    raw_weights = current.get("assessment_severity_weights") or {}
    if isinstance(raw_weights, dict):
        for sev in weights:
            if sev in raw_weights:
                try:
                    weights[sev] = max(0, min(100, int(raw_weights[sev])))
                except (TypeError, ValueError):
                    pass
    current["assessment_severity_weights"] = weights
    # Score bands: clamp and ensure 'good' stays strictly above 'warn'.
    good = max(1, min(100, int(current.get("assessment_score_good", 80) or 80)))
    warn = max(0, min(99, int(current.get("assessment_score_warn", 50) or 50)))
    if warn >= good:
        warn = max(0, good - 1)
    current["assessment_score_good"] = good
    current["assessment_score_warn"] = warn
    # Architecture category color overrides: known category ids + valid hex only.
    raw_colors = current.get("architecture_category_colors") or {}
    clean_colors: dict[str, str] = {}
    if isinstance(raw_colors, dict):
        try:
            from app.architectures.catalog import CATEGORY_META as _CM

            known = set(_CM.keys())
        except Exception:  # noqa: BLE001
            known = set()
        for cid, col in raw_colors.items():
            if cid in known and _is_hex_color(col):
                clean_colors[cid] = str(col).strip().lower()
    current["architecture_category_colors"] = clean_colors
    # Identity dashboard: clamp expiry window, cache TTL and the MFA scan cap.
    current["identity_expiry_days"] = max(1, min(365, int(current.get("identity_expiry_days", 90) or 90)))
    current["identity_cache_ttl_s"] = max(0, min(604800, int(current.get("identity_cache_ttl_s", 21600) or 21600)))
    current["identity_mfa_scan_cap"] = max(1, min(2000, int(current.get("identity_mfa_scan_cap", 50) or 50)))
    # AMBA: clamp cache TTL + tolerance; coerce the misconfig flag.
    current["amba_cache_ttl_s"] = max(0, min(604800, int(current.get("amba_cache_ttl_s", 21600) or 21600)))
    current["amba_misconfig_counts_as_gap"] = bool(current.get("amba_misconfig_counts_as_gap", True))
    current["amba_threshold_tolerance_pct"] = max(0, min(100, int(current.get("amba_threshold_tolerance_pct", 10) or 10)))
    # Telemetry: clamp cache TTL + scan cap; normalize the approved-workspace list.
    current["telemetry_cache_ttl_s"] = max(0, min(604800, int(current.get("telemetry_cache_ttl_s", 21600) or 21600)))
    current["telemetry_per_resource_scan_cap"] = max(1, min(2000, int(current.get("telemetry_per_resource_scan_cap", 200) or 200)))
    raw_ws = current.get("telemetry_approved_workspaces") or []
    if not isinstance(raw_ws, list):
        raw_ws = []
    ws_clean: list[str] = []
    for w in raw_ws:
        s = str(w).strip()
        if s and s not in ws_clean:
            ws_clean.append(s)
    current["telemetry_approved_workspaces"] = ws_clean
    # Backup/DR: clamp cache TTL, stale-drill window, last-job SLA, scan cap.
    current["backupdr_cache_ttl_s"] = max(0, min(604800, int(current.get("backupdr_cache_ttl_s", 21600) or 21600)))
    current["backupdr_stale_drill_days"] = max(1, min(3650, int(current.get("backupdr_stale_drill_days", 180) or 180)))
    current["backupdr_last_job_sla_hours"] = max(1, min(8760, int(current.get("backupdr_last_job_sla_hours", 24) or 24)))
    current["backupdr_per_resource_scan_cap"] = max(1, min(2000, int(current.get("backupdr_per_resource_scan_cap", 200) or 200)))
    # Evidence Locker: clamp retention windows.
    current["evidence_retention_standard_days"] = max(1, min(3650, int(current.get("evidence_retention_standard_days", 90) or 90)))
    current["evidence_retention_audit_days"] = max(1, min(36500, int(current.get("evidence_retention_audit_days", 2555) or 2555)))
    current["evidence_include_metrics_default"] = bool(current.get("evidence_include_metrics_default", False))
    # Radar: clamp cache TTL, sanitize lead-day thresholds, feed toggle/url.
    current["radar_cache_ttl_s"] = max(0, min(604800, int(current.get("radar_cache_ttl_s", 21600) or 21600)))
    raw_lead = current.get("radar_digest_lead_days")
    if not isinstance(raw_lead, list):
        raw_lead = [90, 60, 30]
    lead_clean = sorted({max(1, min(1095, int(x))) for x in raw_lead if str(x).strip().lstrip("-").isdigit()}, reverse=True)
    current["radar_digest_lead_days"] = lead_clean or [90, 60, 30]
    current["radar_azure_updates_feed_enabled"] = bool(current.get("radar_azure_updates_feed_enabled", False))
    current["radar_azure_updates_feed_url"] = str(current.get("radar_azure_updates_feed_url", "") or "")[:500]
    # Telemetry Intelligence: clamp cache TTL, timespan, row cap.
    current["teleintel_cache_ttl_s"] = max(0, min(604800, int(current.get("teleintel_cache_ttl_s", 21600) or 21600)))
    _ts = str(current.get("teleintel_default_timespan", "P1D") or "P1D").strip().upper()
    current["teleintel_default_timespan"] = _ts if re.match(r"^P(T?\d+[DHM])+$|^P\d+D$", _ts) else "P1D"
    current["teleintel_max_rows"] = max(10, min(5000, int(current.get("teleintel_max_rows", 1000) or 1000)))
    # Performance Profiler: clamp cache TTL + scan cap; sanitize window/interval.
    current["perfprofile_cache_ttl_s"] = max(0, min(604800, int(current.get("perfprofile_cache_ttl_s", 21600) or 21600)))
    _pw = str(current.get("perfprofile_window", "P1D") or "P1D").strip().upper()
    current["perfprofile_window"] = _pw if re.match(r"^P(T?\d+[DHM])+$|^P\d+D$", _pw) else "P1D"
    _pi = str(current.get("perfprofile_interval", "PT15M") or "PT15M").strip().upper()
    current["perfprofile_interval"] = _pi if re.match(r"^PT\d+[MH]$", _pi) else "PT15M"
    current["perfprofile_scan_cap"] = max(1, min(2000, int(current.get("perfprofile_scan_cap", 200) or 200)))
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current


def system_prompt_additions() -> str:
    """Extra system-prompt text from custom instructions + response style."""
    s = load_settings()
    parts: list[str] = []
    style = RESPONSE_STYLES.get(s.get("response_style", "default"), "")
    if style:
        parts.append(style)
    ci = (s.get("custom_instructions") or "").strip()
    if ci:
        parts.append("User's custom instructions:\n" + ci)
    return "\n\n".join(parts)


def generation_params() -> dict[str, Any]:
    """Generation controls (max_tokens) for providers that support them."""
    s = load_settings()
    return {"max_tokens": int(s["max_tokens"])}


def agent_runtime_params() -> dict[str, Any]:
    """Advanced agent tuning knobs (tool iteration budget, result caps, timeout).

    Read at request time so changes in the dashboard apply without a restart.
    """
    s = load_settings()
    return {
        "max_tool_iterations": int(s.get("max_tool_iterations", 16)),
        "tool_result_limit": int(s.get("tool_result_limit", 20000)),
        "tool_discovery_limit": int(s.get("tool_discovery_limit", 60000)),
        "request_timeout_seconds": int(s.get("request_timeout_seconds", 180)),
    }


def request_timeout_seconds() -> int:
    """Per-request LLM streaming timeout (seconds), from the dashboard setting."""
    return int(load_settings().get("request_timeout_seconds", 180))


def deep_parallelism() -> int:
    """How many hypothesis sub-agents a deep investigation may run at once.

    Returns 1 when parallelism is disabled (sequential), else the configured count
    (clamped 1-12). Read at run time so dashboard changes apply without a restart.
    """
    s = load_settings()
    if not bool(s.get("deep_parallel_enabled", True)):
        return 1
    return max(1, min(12, int(s.get("deep_parallel_count", 3))))


def assessment_weights() -> dict[str, int]:
    """Live per-severity scoring weights (admin-tunable, merged onto defaults).

    Read at scoring time so dashboard changes apply to subsequent runs."""
    base = dict(_DEFAULT_SEVERITY_WEIGHTS)
    saved = load_settings().get("assessment_severity_weights") or {}
    if isinstance(saved, dict):
        for sev in base:
            if sev in saved:
                try:
                    base[sev] = max(0, min(100, int(saved[sev])))
                except (TypeError, ValueError):
                    pass
    return base


def assessment_score_bands() -> dict[str, int]:
    """Score color thresholds {good, warn} for the assessment dashboard."""
    s = load_settings()
    good = max(1, min(100, int(s.get("assessment_score_good", 80) or 80)))
    warn = max(0, min(99, int(s.get("assessment_score_warn", 50) or 50)))
    if warn >= good:
        warn = max(0, good - 1)
    return {"good": good, "warn": warn}


def assessment_execution() -> dict[str, int]:
    """Live assessment execution-engine knobs (admin-tunable), clamped to safe ranges.

    Read at run time so dashboard changes apply to subsequent runs without a restart."""
    s = load_settings()

    def _clamp(key: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(s.get(key, default))))
        except (TypeError, ValueError):
            return default

    return {
        "concurrency": _clamp("assessment_concurrency", 6, 1, 16),
        "check_timeout_s": _clamp("assessment_check_timeout_s", 90, 10, 600),
        "run_budget_s": _clamp("assessment_run_budget_s", 1800, 60, 7200),
        "confidence_high_pct": _clamp("assessment_confidence_high_pct", 98, 50, 100),
    }


def architecture_category_colors() -> dict[str, str]:
    """Admin hex-color overrides for architecture node categories ({} = built-in)."""
    saved = load_settings().get("architecture_category_colors") or {}
    if not isinstance(saved, dict):
        return {}
    return {k: str(v).strip().lower() for k, v in saved.items() if _is_hex_color(v)}


def effective_write_policy(env_default: str = "gated") -> str:
    """Resolve the effective write policy from the runtime dashboard setting.

    Returns 'off' (writes execute immediately) or 'gated' (writes pause for approval).
    Falls back to the provided env default if the setting is unavailable."""
    try:
        s = load_settings()
        if "auto_execute_writes" in s:
            return "off" if bool(s["auto_execute_writes"]) else "gated"
    except Exception:  # noqa: BLE001
        pass
    return env_default
