"""Proactive scanners — scheduling, state and delivery over the Signal Registry.

Nothing in this module detects anything. Every check already exists as a ``SignalSpec``;
a scanner is a **named selection of signals with a schedule and a severity floor**. If a
scanner ever needs its own detection logic, that logic belongs in ``signal_defs/`` and the
scanner should select it — otherwise the same check ends up written twice and two screens
start disagreeing.

The part that decides whether this is useful or ignored is the delta:

    new         fingerprints absent from the previous run of THIS scanner
    resolved    fingerprints present last time and gone now
    persisting  fingerprints in both, with an age

Only ``new`` and ``resolved`` are notified. A daily digest that repeats 400 known findings
trains people to filter the sender, and after that the tool detects nothing no matter how
good the signals are. A short list of exceptions bypasses the digest entirely because they
are the events where an hour matters.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.entra import cache, model
from app.entra import signals as sig

log = logging.getLogger("app.entra.scanners")

_STATE_KEY = "scanner_runs"
_LEDGER_KEY = "findings_ledger"

SEVERITY_ORDER = ("info", "low", "medium", "high", "critical")


def _sev_rank(severity: str) -> int:
    return SEVERITY_ORDER.index(severity) if severity in SEVERITY_ORDER else 0


@dataclass(frozen=True)
class ScannerSpec:
    id: str
    name: str
    description: str
    cadence: str                       # daily | weekly — the scheduler's hint, not a cron
    severity_floor: str = "medium"
    pillars: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    signal_ids: tuple[str, ...] = ()
    only_critical: bool = False        # select every critical signal, whatever its pillar
    requires_domains: tuple[str, ...] = ()
    enabled: bool = True

    def selects(self, spec: sig.SignalSpec) -> bool:
        if self.only_critical:
            return spec.severity == "critical"
        if self.signal_ids and spec.id in self.signal_ids:
            return True
        if self.pillars and spec.pillar in self.pillars:
            return True
        if self.tags and set(self.tags) & set(spec.tags):
            return True
        return False

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "cadence": self.cadence, "severity_floor": self.severity_floor,
            "pillars": list(self.pillars), "tags": list(self.tags),
            "signal_ids": list(self.signal_ids), "only_critical": self.only_critical,
            "requires_domains": list(self.requires_domains), "enabled": self.enabled,
            "signal_count": len(self.signals()),
        }

    def signals(self) -> list[sig.SignalSpec]:
        return [s for s in sig.registry() if s.scannable and self.selects(s)]


# Events where a digest is the wrong delivery mechanism. Each one is a state change that
# either indicates active compromise or removes a control that was protecting the tenant.
ALWAYS_IMMEDIATE: tuple[str, ...] = (
    "ca.breakglass_over_covered",
    "ca.exclusion_privileged",
    "priv.standing_global_admin",
    "priv.privileged_guest",
    "priv.pim_no_mfa_on_activation",
    "priv.cross_plane_power",
    "app.consent_grant_capable",
    "app.admin_consent_all_principals",
    "app.user_consent_unrestricted",
    "risk.privileged_user_at_risk",
    "risk.risky_workload_identity",
    "risk.legacy_auth_success",
)

SCANNERS: tuple[ScannerSpec, ...] = (
    ScannerSpec(
        id="entra.daily_critical", name="Daily critical sweep",
        description="Every critical signal across every pillar. The one scanner nobody should "
                    "turn off.",
        cadence="daily", severity_floor="critical", only_critical=True,
    ),
    ScannerSpec(
        id="entra.credential_expiry", name="Credential expiry watch",
        description="Application secrets and certificates approaching or past expiry \u2014 the "
                    "most common cause of a self-inflicted outage.",
        cadence="daily", severity_floor="low",
        signal_ids=("app.secret_expired", "app.secret_expiring", "app.cert_expiring",
                    "app.cert_expired", "app.secret_long_lived", "app.secret_never_rotated"),
        requires_domains=("apps",),
    ),
    ScannerSpec(
        id="entra.privileged_review", name="Privileged access review",
        description="Standing privilege, PIM configuration health, separation of duties and "
                    "cross-plane power.",
        cadence="weekly", severity_floor="medium", pillars=("priv",),
        requires_domains=("roles",),
    ),
    ScannerSpec(
        id="entra.ca_drift", name="Conditional Access drift",
        description="Policy coverage, conflicts, exclusions and disabled or stale report-only "
                    "policies.",
        cadence="daily", severity_floor="medium", pillars=("ca",), requires_domains=("ca",),
    ),
    ScannerSpec(
        id="entra.breakglass", name="Break-glass health",
        description="Emergency access accounts captured by a policy, or missing entirely.",
        cadence="daily", severity_floor="low",
        signal_ids=("ca.breakglass_over_covered", "ca.breakglass_missing",
                    "mon.hybrid_sync_stale"),
    ),
    ScannerSpec(
        id="entra.consent_watch", name="Consent and OAuth watch",
        description="Tenant-wide grants, consent-capable permissions, risky redirect URIs and "
                    "federated credentials.",
        cadence="daily", severity_floor="medium", pillars=("app",), requires_domains=("apps",),
    ),
    ScannerSpec(
        id="entra.guest_lifecycle", name="Guest and lifecycle hygiene",
        description="Stale accounts, disabled users retaining access, guest sprawl and "
                    "external collaboration settings.",
        cadence="weekly", severity_floor="medium", pillars=("ppl",),
        requires_domains=("people",),
    ),
    ScannerSpec(
        id="entra.auth_posture", name="MFA and authentication posture",
        description="Registration coverage, method strength and the tenant authentication "
                    "methods policy.",
        cadence="weekly", severity_floor="medium", pillars=("auth",),
        requires_domains=("people",),
    ),
    ScannerSpec(
        id="entra.risk_sweep", name="Risk sweep",
        description="Identity Protection risk joined to privilege, plus the deterministic "
                    "sign-in patterns.",
        cadence="daily", severity_floor="medium", pillars=("risk",), requires_domains=("risk",),
    ),
    ScannerSpec(
        id="entra.governance_sweep", name="Governance sweep",
        description="Review coverage and quality, entitlement hygiene and lifecycle workflow "
                    "health.",
        cadence="weekly", severity_floor="medium", pillars=("gov",),
    ),
    ScannerSpec(
        id="entra.monitoring", name="Monitoring and hybrid",
        description="Directory synchronisation health and log export coverage.",
        cadence="daily", severity_floor="medium", pillars=("mon",),
    ),
    ScannerSpec(
        id="entra.full_posture", name="Full posture snapshot",
        description="Every signal, and a score history point. The weekly baseline.",
        cadence="weekly", severity_floor="low",
        pillars=tuple(p["key"] for p in sig.PILLARS),
    ),
)

SCANNER_BY_ID = {s.id: s for s in SCANNERS}


def registry() -> list[ScannerSpec]:
    return list(SCANNERS)


# ----------------------------------------------------------------------- selection
def select(scanner: ScannerSpec, findings: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Findings this scanner reports: its signals, at or above its severity floor."""
    wanted = {s.id for s in scanner.signals()}
    floor = _sev_rank(scanner.severity_floor)
    return [f for f in findings
            if f.get("signal_id") in wanted and _sev_rank(str(f.get("severity", ""))) >= floor]


def unavailable_reason(scanner: ScannerSpec, domain_meta: dict[str, Any]) -> str:
    """Why this scanner cannot run, or an empty string when it can.

    A scanner whose domain is blind must say so rather than report zero findings — "no
    findings" and "could not look" are the same picture and opposite facts."""
    for name in scanner.requires_domains:
        meta = domain_meta.get(name) or {}
        if not model.domain_usable(meta):
            return model.domain_reason(meta, name)
    return ""


# --------------------------------------------------------------------------- state
def read_runs(tenant_id: str) -> dict[str, Any]:
    state = cache.read_state(tenant_id, _STATE_KEY, {})
    return state if isinstance(state, dict) else {}


def write_runs(tenant_id: str, runs: dict[str, Any]) -> None:
    cache.write_state(tenant_id, _STATE_KEY, runs)


def read_ledger(tenant_id: str) -> dict[str, Any]:
    """First-seen / last-seen per fingerprint. This is what makes 'age' meaningful.

    Deliberately separate from ``findings_state`` (the user's workflow decisions): a
    collection run updates the ledger constantly and must never touch a suppression."""
    state = cache.read_state(tenant_id, _LEDGER_KEY, {})
    return state if isinstance(state, dict) else {}


def update_ledger(tenant_id: str, findings: list[dict[str, Any]], *, now: str = "") -> dict[str, Any]:
    """Record first-seen, last-seen and resolution. Returns the updated ledger."""
    now = now or model.now_iso()
    ledger = read_ledger(tenant_id)
    live = {f["fingerprint"] for f in findings}
    for f in findings:
        entry = ledger.setdefault(f["fingerprint"], {"first_seen": now, "signal_id": f["signal_id"]})
        entry["last_seen"] = now
        entry["signal_id"] = f["signal_id"]
        entry["severity"] = f.get("severity", "")
        entry.pop("resolved_at", None)
    for fp, entry in ledger.items():
        if fp not in live and not entry.get("resolved_at"):
            # Resolution is COMPUTED, never clicked. A fingerprint that stopped appearing is
            # resolved, and that is the only reason the inbox can be trusted.
            entry["resolved_at"] = now
    # Bound the file: resolved entries older than the retention window are dropped.
    if len(ledger) > 20_000:
        keep = {fp: e for fp, e in ledger.items() if not e.get("resolved_at")}
        ledger = keep
    cache.write_state(tenant_id, _LEDGER_KEY, ledger)
    return ledger


def age_days(entry: dict[str, Any], ctx: sig.SignalContext) -> int | None:
    return ctx.days_since(str(entry.get("first_seen") or ""))


# ------------------------------------------------------------------------- running
def run(scanner: ScannerSpec, tenant_id: str, analysis: dict[str, Any],
        domain_meta: dict[str, Any], *, now: str = "") -> dict[str, Any]:
    """Evaluate one scanner against an already-computed analysis. Pure apart from state.

    Never triggers collection: a scanner reports on the snapshot that exists, so a scanner
    schedule and a refresh schedule stay independently controllable."""
    now = now or model.now_iso()
    blocked = unavailable_reason(scanner, domain_meta)
    current = [] if blocked else select(scanner, analysis.get("findings") or [])

    runs = read_runs(tenant_id)
    previous_ids = set((runs.get(scanner.id) or {}).get("fingerprints") or [])
    current_ids = {f["fingerprint"] for f in current}
    by_fp = {f["fingerprint"]: f for f in current}

    new = [by_fp[fp] for fp in sorted(current_ids - previous_ids)]
    resolved = sorted(previous_ids - current_ids)
    persisting = sorted(current_ids & previous_ids)

    result = {
        "scanner_id": scanner.id,
        "name": scanner.name,
        "at": now,
        "blocked": blocked,
        "counts": {"total": len(current), "new": len(new), "resolved": len(resolved),
                   "persisting": len(persisting)},
        "by_severity": model.count_by_severity(current),
        # `counts.total` and `by_severity` describe everything the scanner reports; the
        # findings themselves are served by GET /scanners/{id}/findings instead. Shipping
        # them here too added 1.3 MB to a run-all response that nothing read.
        "new": new,
        "resolved_fingerprints": resolved,
        "immediate": [f for f in new if f.get("signal_id") in ALWAYS_IMMEDIATE],
        "first_run": scanner.id not in runs,
    }
    if not blocked:
        runs[scanner.id] = {"at": now, "fingerprints": sorted(current_ids),
                            "counts": result["counts"]}
        write_runs(tenant_id, runs)
    return result


def due(scanner: ScannerSpec, tenant_id: str, ctx: sig.SignalContext) -> bool:
    """Has enough time passed since the last run of this scanner?"""
    last = (read_runs(tenant_id).get(scanner.id) or {}).get("at") or ""
    if not last:
        return True
    days = ctx.days_since(last)
    if days is None:
        return True
    return days >= (7 if scanner.cadence == "weekly" else 1)


def summarize(result: dict[str, Any], *, limit: int = 8) -> str:
    """The notification body. States what changed and why it matters — never a JSON dump."""
    counts = result["counts"]
    if result["blocked"]:
        return f"{result['name']} could not run: {result['blocked']}"
    if result["first_run"]:
        lead = (f"{result['name']} ran for the first time and found {counts['total']} "
                f"finding(s).")
    else:
        lead = (f"{result['name']}: {counts['new']} new, {counts['resolved']} resolved, "
                f"{counts['persisting']} still open.")
    lines = [lead]
    for finding in result["new"][:limit]:
        spec = sig.by_id(str(finding.get("signal_id") or ""))
        why = f" \u2014 {spec.why}" if spec else ""
        lines.append(f"\u2022 [{finding.get('severity', '').upper()}] "
                     f"{finding.get('title', '')}{why}")
    remaining = len(result["new"]) - limit
    if remaining > 0:
        lines.append(f"\u2026and {remaining} more.")
    return "\n".join(lines)


def notification_severity(result: dict[str, Any]) -> str:
    """Map the worst NEW finding onto the notification engine's severity vocabulary."""
    worst = max((_sev_rank(str(f.get("severity", ""))) for f in result["new"]), default=-1)
    if worst < 0:
        return "info"
    label = SEVERITY_ORDER[worst]
    return {"critical": "critical", "high": "error", "medium": "warning"}.get(label, "info")


def should_notify(result: dict[str, Any]) -> bool:
    """Notify on change, or on anything in the always-immediate list. Never on silence."""
    if result["blocked"]:
        return False
    if result["immediate"]:
        return True
    return bool(result["counts"]["new"] or result["counts"]["resolved"])


@dataclass
class SweepResult:
    ran: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    @property
    def new_total(self) -> int:
        return sum(r["counts"]["new"] for r in self.ran)

    @property
    def immediate(self) -> list[dict[str, Any]]:
        return [f for r in self.ran for f in r["immediate"]]


def sweep(tenant_id: str, analysis: dict[str, Any], domain_meta: dict[str, Any],
          ctx: sig.SignalContext, *, force: bool = False,
          only: Iterable[str] | None = None) -> SweepResult:
    """Run every due scanner. Returns what ran and what was skipped, with reasons."""
    wanted = set(only or ()) or {s.id for s in SCANNERS}
    out = SweepResult()
    for scanner in SCANNERS:
        if scanner.id not in wanted:
            continue
        if not scanner.enabled:
            out.skipped.append({"scanner_id": scanner.id, "reason": "disabled"})
            continue
        if not force and not due(scanner, tenant_id, ctx):
            out.skipped.append({"scanner_id": scanner.id, "reason": "not due"})
            continue
        out.ran.append(run(scanner, tenant_id, analysis, domain_meta, now=model.now_iso()))
    return out
