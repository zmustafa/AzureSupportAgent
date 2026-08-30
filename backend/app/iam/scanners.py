"""Proactive IAM scanners — a named selection of signals, a schedule and a delta.

Nothing here detects anything. Every check already exists as a ``SignalSpec`` in
``signal_defs/``; a scanner is a **named selection of those signals with a cadence and a
severity floor**. If a scanner ever needs its own detection logic that logic belongs in
``signal_defs/`` and the scanner should select it — otherwise the same check is written twice
and two screens start disagreeing about the same tenant.

The part that decides whether this is useful or ignored is the delta:

    new         fingerprints absent from the previous run of THIS scanner
    resolved    fingerprints present last time and gone now
    persisting  fingerprints in both

Only ``new`` and ``resolved`` are worth notifying. A daily digest that repeats 1,007 known
findings trains people to filter the sender, and after that the tool detects nothing however
good the signals are.

**A scanner that could not look says so.** Its blocked state is derived from the signals it
selects: if every one of them came back unmeasured, the scanner reports `blocked` with the
reasons, and its counts are withheld rather than published as zero. On a large real tenant a
green "0 findings" card is the single most dangerous thing this screen could render.
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass
from typing import Any, Iterable

from app.iam import cache
from app.iam import signals as sig

log = logging.getLogger("app.iam.scanners")

SCANNER_STATE_KEY = cache.SCANNER_STATE
LEDGER_KEY = cache.FINDINGS_LEDGER

# Resolved ledger entries are dropped once the file grows past this. The ledger exists to make
# "age" answerable, not to be an audit log — the run history is in the scan-run table.
MAX_LEDGER_ENTRIES = 20_000

CADENCES = ("daily", "weekly")

_CADENCE_DAYS = {"daily": 1, "weekly": 7}


def _sev_rank(severity: str) -> int:
    """Lower is more severe. IAM's vocabulary is critical/error/warning/info, NOT Entra's
    info..critical scale — a shared helper here would silently invert every floor."""
    return sig.SEVERITY_RANK.get(severity, 3)


@dataclass(frozen=True)
class ScannerSpec:
    id: str
    name: str
    description: str
    cadence: str                       # daily | weekly — a hint for the scheduler, not a cron
    severity_floor: str = "warning"    # report findings AT OR ABOVE this severity
    pillars: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    signal_ids: tuple[str, ...] = ()
    only_critical: bool = False        # every critical signal, whatever its pillar
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

    def signals(self) -> list[sig.SignalSpec]:
        return [s for s in sig.all_signals() if self.selects(s)]

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "cadence": self.cadence, "severity_floor": self.severity_floor,
            "pillars": list(self.pillars), "tags": list(self.tags),
            "signal_ids": list(self.signal_ids), "only_critical": self.only_critical,
            "enabled": self.enabled, "signal_count": len(self.signals()),
        }


# Findings where a digest is the wrong delivery mechanism. Each is a state change that either
# indicates active compromise or removes a control that was protecting the tenant.
#
# Every id here is asserted against the live registry by a test. The first draft of this list
# invented 8 of its 9 ids from memory of what the signals "should" be called; every one of them
# silently selected nothing, so the escalation path would have delivered no notification at all
# while looking perfectly configured. A detector that cannot detect reads as a pass.
ALWAYS_IMMEDIATE: tuple[str, ...] = (
    "byp.rbac_not_only_door",
    "dp.credential_store_access",
    "esc.escalation_to_owner",
    "esc.escalation_from_guest",
    "esc.fic_loose_subject",
    "esc.identity_hijack_available",
    "gov.drift_self_grant",
    "gov.drift_privileged_added",
    "hyg.privileged_orphan",
    "lp.role_authorization_write",
    "priv.classic_administrators",
)

SCANNERS: tuple[ScannerSpec, ...] = (
    ScannerSpec(
        id="iam.daily_critical", name="Daily critical sweep",
        description="Every critical signal across every pillar. The one scanner nobody should "
                    "turn off.",
        cadence="daily", severity_floor="critical", only_critical=True,
    ),
    ScannerSpec(
        id="iam.privileged_review", name="Privileged access review",
        description="Standing versus JIT privilege, tier-0 concentration and classic "
                    "administrators.",
        cadence="weekly", severity_floor="warning", pillars=("priv",),
    ),
    ScannerSpec(
        id="iam.escalation_watch", name="Escalation path watch",
        description="Paths from ordinary access to Owner: role-assignment rights, managed "
                    "identities and federated credentials.",
        cadence="daily", severity_floor="error", pillars=("esc",),
    ),
    ScannerSpec(
        id="iam.external_access", name="External access",
        description="Guests, Lighthouse delegations and multi-tenant service principals.",
        cadence="weekly", severity_floor="warning", pillars=("ext",),
    ),
    ScannerSpec(
        id="iam.hygiene", name="Access hygiene",
        description="Assignments held by principals that no longer exist, stale access and "
                    "expired credentials.",
        cadence="weekly", severity_floor="warning", pillars=("hyg",),
    ),
    ScannerSpec(
        id="iam.bypass_watch", name="RBAC bypass watch",
        description="Shared keys, local authentication and admin users that reach data without "
                    "any role assignment.",
        cadence="daily", severity_floor="error", pillars=("byp",),
    ),
    ScannerSpec(
        id="iam.data_plane", name="Data-plane access",
        description="Who can reach the data itself — secrets, blobs, messages, indexes — and "
                    "which services decide that in a system Azure RBAC cannot show.",
        cadence="daily", severity_floor="warning", pillars=("dp",),
    ),
    ScannerSpec(
        id="iam.least_privilege", name="Least privilege",
        description="Over-broad roles, scope breadth and unused standing permission.",
        cadence="weekly", severity_floor="warning", pillars=("lp",),
    ),
    ScannerSpec(
        id="iam.drift", name="Access drift",
        description="What changed since the last snapshot: new privilege, widened scope and "
                    "assignments nobody can account for.",
        cadence="daily", severity_floor="warning", pillars=("gov",),
    ),
    ScannerSpec(
        id="iam.full_posture", name="Full posture snapshot",
        description="Every signal, and a score history point. The weekly baseline.",
        cadence="weekly", severity_floor="info",
        pillars=tuple(p["key"] for p in sig.PILLARS),
    ),
)

SCANNER_BY_ID = {s.id: s for s in SCANNERS}


def registry() -> list[ScannerSpec]:
    return list(SCANNERS)


def get(scanner_id: str) -> ScannerSpec | None:
    return SCANNER_BY_ID.get(scanner_id)


# ----------------------------------------------------------------------- selection
def select(scanner: ScannerSpec, findings: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The findings this scanner reports: its signals, at or above its severity floor."""
    wanted = {s.id for s in scanner.signals()}
    floor = _sev_rank(scanner.severity_floor)
    return [
        f for f in findings
        if f.get("signal_id") in wanted and _sev_rank(str(f.get("severity", ""))) <= floor
    ]


def blocked_reasons(scanner: ScannerSpec, results: list[sig.SignalResult]) -> list[str]:
    """Why this scanner could not look, or an empty list when at least one signal ran.

    A scanner is blocked only when EVERY signal it selects was unmeasured. One measured signal
    is enough to make the count meaningful — the unmeasured ones are still reported separately
    as `unmeasured` so the card can say "3 of 8 checks could not be performed" rather than
    quietly narrowing its own denominator."""
    wanted = {s.id for s in scanner.signals()}
    mine = [r for r in results if r.spec.id in wanted]
    if not mine:
        return []
    if any(r.measured for r in mine):
        return []
    seen: list[str] = []
    for r in mine:
        reason = r.reason or "the input this check needs was not collected"
        if reason not in seen:
            seen.append(reason)
    return seen


def unmeasured_for(scanner: ScannerSpec, results: list[sig.SignalResult]) -> list[dict[str, str]]:
    """The scanner's own checks that could not be performed. Never folded into a pass."""
    wanted = {s.id for s in scanner.signals()}
    return [
        {"signal_id": r.spec.id, "title": r.spec.title, "reason": r.reason}
        for r in results if r.spec.id in wanted and not r.measured
    ]


# --------------------------------------------------------------------------- state
def read_runs(tenant_id: str) -> dict[str, Any]:
    payload = cache.read_state(tenant_id, SCANNER_STATE_KEY)
    return payload if isinstance(payload, dict) else {}


def write_runs(tenant_id: str, runs: dict[str, Any]) -> None:
    cache.write_state(tenant_id, SCANNER_STATE_KEY, runs)


def read_ledger(tenant_id: str) -> dict[str, Any]:
    """First-seen / last-seen per fingerprint — what makes "age" answerable.

    Deliberately separate from the finding STATE table (the user's suppressions and
    acceptances): a scan updates the ledger on every run and must never touch a decision a
    human made."""
    payload = cache.read_state(tenant_id, LEDGER_KEY)
    return payload if isinstance(payload, dict) else {}


def update_ledger(
    tenant_id: str, findings: list[dict[str, Any]], *, now: str = ""
) -> dict[str, Any]:
    """Record first-seen, last-seen and resolution. Returns the updated ledger."""
    now = now or _now_iso()
    live = {str(f.get("id", "")) for f in findings if f.get("id")}

    def _mutate(ledger: dict[str, Any]) -> dict[str, Any] | None:
        for finding in findings:
            fingerprint = str(finding.get("id", ""))
            if not fingerprint:
                continue
            entry = ledger.setdefault(
                fingerprint,
                {"first_seen": now, "signal_id": finding.get("signal_id", "")},
            )
            entry["last_seen"] = now
            entry["signal_id"] = finding.get("signal_id", "")
            entry["severity"] = finding.get("severity", "")
            entry.pop("resolved_at", None)
        for fingerprint, entry in ledger.items():
            if fingerprint not in live and not entry.get("resolved_at"):
                entry["resolved_at"] = now
        if len(ledger) > MAX_LEDGER_ENTRIES:
            return {fp: entry for fp, entry in ledger.items() if not entry.get("resolved_at")}
        return None

    return cache.mutate_state(tenant_id, LEDGER_KEY, _mutate)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def age_days(entry: dict[str, Any], *, now: str = "") -> int | None:
    first = str(entry.get("first_seen") or "")
    if not first:
        return None
    try:
        seen = _dt.datetime.fromisoformat(first.replace("Z", "+00:00"))
        ref = _dt.datetime.fromisoformat((now or _now_iso()).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, (ref - seen).days)


# ------------------------------------------------------------------------- running
def run(
    scanner: ScannerSpec,
    tenant_id: str,
    findings: list[dict[str, Any]],
    results: list[sig.SignalResult],
    *,
    now: str = "",
    persist: bool = True,
) -> dict[str, Any]:
    """Evaluate one scanner against an already-computed finding list. Pure apart from state.

    Never triggers collection. A scanner reports on the snapshot that exists, so the scanner
    schedule and the refresh schedule stay independently controllable — and a scanner can never
    be the reason an Azure call happened.

    ``persist=False`` is how the UI reads a card without moving the baseline. Viewing must not
    consume the delta: if a GET recorded a run, the first person to open the screen would turn
    everyone else's "3 new" into "0 new"."""
    now = now or _now_iso()
    blocked = blocked_reasons(scanner, results)
    current = [] if blocked else select(scanner, findings)

    by_fp = {str(f.get("id", "")): f for f in current if f.get("id")}
    current_ids = set(by_fp)
    result: dict[str, Any] = {}

    def _build(runs: dict[str, Any]) -> None:
        previous = runs.get(scanner.id) or {}
        previous_ids = set(previous.get("fingerprints") or [])
        new = [by_fp[fp] for fp in sorted(current_ids - previous_ids)]
        resolved = sorted(previous_ids - current_ids)
        persisting = sorted(current_ids & previous_ids)
        result.update({
            "scanner_id": scanner.id,
            "name": scanner.name,
            "at": now,
            "blocked": blocked,
            "counts": None if blocked else {
                "total": len(current), "new": len(new),
                "resolved": len(resolved), "persisting": len(persisting),
            },
            "by_severity": {} if blocked else {
                severity: sum(1 for finding in current if finding.get("severity") == severity)
                for severity in sig.SEVERITIES
            },
            "new": new,
            "resolved_fingerprints": resolved,
            "immediate": [f for f in new if f.get("signal_id") in ALWAYS_IMMEDIATE],
            "unmeasured": unmeasured_for(scanner, results),
            "first_run": scanner.id not in runs,
            "last_run_at": str(previous.get("at") or ""),
        })
        if persist and not blocked:
            runs[scanner.id] = {
                "at": now,
                "fingerprints": sorted(current_ids),
                "counts": result["counts"],
            }

    if persist and not blocked:
        cache.mutate_state(tenant_id, SCANNER_STATE_KEY, lambda runs: _build(runs))
    else:
        _build(read_runs(tenant_id))
    return result


def summarize(card: dict[str, Any]) -> dict[str, Any]:
    """A scanner card without the finding bodies.

    :func:`run` returns the full ``new`` and ``immediate`` finding objects because the
    notification path needs them to write a message. The CARD LIST does not: it renders counts
    only, and on a first run every finding is "new", so shipping them inline made the nine-card
    response **3.2 MB** on a realistic tenant — for data no component reads. The detail lives at
    `/iam/scanners/{id}/findings`.

    The counts are kept verbatim, so nothing a reader sees is derived from the dropped fields."""
    out = {k: v for k, v in card.items() if k not in ("new", "immediate", "resolved_fingerprints")}
    out["immediate_count"] = len(card.get("immediate") or [])
    return out


def due(scanner: ScannerSpec, tenant_id: str, *, now: str = "") -> bool:
    """Has enough time passed since the last run of this scanner?

    A scanner that has never run is always due — otherwise a newly added scanner would sit
    silent until its first cadence elapsed, which on a weekly scanner is a week of no cover."""
    last = str((read_runs(tenant_id).get(scanner.id) or {}).get("at") or "")
    if not last:
        return True
    try:
        then = _dt.datetime.fromisoformat(last.replace("Z", "+00:00"))
        ref = _dt.datetime.fromisoformat((now or _now_iso()).replace("Z", "+00:00"))
    except ValueError:
        return True
    return (ref - then) >= _dt.timedelta(days=_CADENCE_DAYS.get(scanner.cadence, 1))
