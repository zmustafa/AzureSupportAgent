"""The IAM signal registry — the spine of everything analytical on this screen.

**Never implement a check outside ``signal_defs/``.** One catalogue projects into the posture
score, the findings inbox, the scanners, notifications, exports and agent answers. A check
written anywhere else exists in exactly one of those places and quietly diverges from the rest.

Three states must never render alike, and the registry is what keeps them apart:

* **finding**      — we looked and found something.
* **ok**           — we looked and found nothing.
* **not measured** — we could not look (raise :class:`SignalUnavailable`), or the pillar has no
                     signals registered yet (``not_implemented``).

Returning ``[]`` when a signal could not be evaluated silently *improves* the score for a tenant
nobody measured. That is the one failure this product must not have.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("app.iam.signals")

# --------------------------------------------------------------------------- pillars
# Weights sum to 100. Coverage is computed at SIGNAL-WEIGHT granularity within each pillar, so a
# partially blind pillar honestly reduces coverage rather than scoring well on the half we could
# see.
PILLARS: list[dict[str, Any]] = [
    {"key": "priv", "label": "Privileged access", "weight": 20,
     "desc": "Standing versus JIT privilege, tier-0 concentration, classic administrators."},
    {"key": "esc", "label": "Escalation", "weight": 16,
     "desc": "Paths from ordinary access to Owner: managed identities, federated credentials."},
    {"key": "ext", "label": "External access", "weight": 12,
     "desc": "Guests, Lighthouse delegations and multi-tenant service principals."},
    {"key": "hyg", "label": "Hygiene", "weight": 10,
     "desc": "Orphaned assignments, stale principals, expired credentials."},
    {"key": "byp", "label": "RBAC bypass", "weight": 12,
     "desc": "Shared keys, local auth and admin users that reach data without a role assignment."},
    {"key": "dp", "label": "Data-plane access", "weight": 10,
     "desc": "Who reaches the data itself, and which services decide that outside Azure RBAC."},
    {"key": "lp", "label": "Least privilege", "weight": 9,
     "desc": "Over-broad roles, scope breadth and direct-assignment clusters."},
    {"key": "gov", "label": "Governance", "weight": 7,
     "desc": "Ownership coverage, review coverage, collection completeness."},
    {"key": "str", "label": "Structure", "weight": 4,
     "desc": "Assignment-limit headroom, custom-role sprawl, scope design."},
]
PILLAR_KEYS = {p["key"] for p in PILLARS}
PILLAR_WEIGHT = {p["key"]: p["weight"] for p in PILLARS}

SEVERITIES = ("critical", "error", "warning", "info")
SEVERITY_RANK = {"critical": 0, "error": 1, "warning": 2, "info": 3}

# Kinds a finding can be about. Adding one is deliberate — the UI groups and links by kind.
# `resource` is the odd one out and the reason the set is enforced: a bypass finding is about a
# RESOURCE, not about anyone's access, and letting it silently share the `scope` kind would send
# every deep link to the wrong screen.
OBJECT_KINDS = frozenset({
    "assignment", "principal", "role_definition", "scope", "identity", "delegation",
    "policy", "resource", "tenant",
})

# The same surface is read by differently-named collectors depending on which path the refresh
# took. A signal gated on one name reports "not collected" on every tenant that took the other
# route, which is indistinguishable from a genuine gap and is never noticed because the signal
# simply never fires. Keyed by the name a signal is expected to ask for.
COLLECTOR_ALIASES: dict[str, set[str]] = {
    "KeyVaultAccessPolicies": {"ArgKeyVaultAccessPolicies"},
    "AzureDenyAssignments": {"ArgDenyAssignments"},
    "AzureRoleAssignments": {"ArgRoleAssignments"},
    "AzureRoleDefinitions": {"ArgRoleDefinitions"},
    "ManagedIdentities": {"ArgManagedIdentities"},
}


class SignalUnavailable(Exception):
    """Raised inside an ``evaluate`` body when the check cannot honestly be performed.

    The scan ran, but the specific input this signal needs did not arrive — PIM was never
    collected, the directory could not be read, ownership is not configured. Returning ``[]``
    there would count "we could not look" as "nothing wrong"."""


@dataclass
class Finding:
    """One thing worth a human's attention.

    Aggregated per (signal, subject) on purpose. One finding per affected row explodes on real
    data — the Entra work produced 1,262 "patterns" before aggregation and 2 after."""

    signal_id: str
    title: str
    severity: str
    pillar: str
    object_kind: str
    subject: str                      # the thing the finding is about (scope, principal, role…)
    subject_label: str = ""
    detail: str = ""
    count: int = 1                    # how many underlying rows this represents
    evidence: dict[str, Any] = field(default_factory=dict)
    remediation: str = ""
    frameworks: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> str:
        """Stable identity across runs.

        Resolution is COMPUTED, never clicked: a fingerprint that stops appearing is resolved.
        It therefore must not include anything that changes between runs (counts, timestamps)
        or every finding would resolve and reappear on every scan."""
        raw = f"{self.signal_id}|{self.subject}".lower()
        # Not a security hash: this is a dedup/identity digest. usedforsecurity=False keeps the
        # digest byte-identical (so stored fingerprints stay comparable) while allowing FIPS hosts.
        return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]

    def public(self) -> dict[str, Any]:
        return {
            "id": self.fingerprint,
            "signal_id": self.signal_id,
            "title": self.title,
            "severity": self.severity,
            "pillar": self.pillar,
            "object_kind": self.object_kind,
            "subject": self.subject,
            "subject_label": self.subject_label or self.subject,
            "detail": self.detail,
            "count": self.count,
            "evidence": self.evidence,
            "remediation": self.remediation,
            "frameworks": list(self.frameworks),
        }


@dataclass
class SignalContext:
    """Everything a signal is allowed to read.

    Signals never read each other's output and never call Azure — they are pure functions over
    one composed snapshot, which is what makes them cheap enough to run on every request and
    testable without a connection."""

    tenant_id: str
    rows: list[dict[str, Any]]                     # the composed master rows
    kpis: dict[str, Any]
    scopes: list[dict[str, Any]]                   # per-scope cache metadata
    directory: dict[str, Any] = field(default_factory=dict)
    now: Any = None
    # Managed-identity and federated-credential inventories, when they were collected. Empty is
    # NOT the same as none existing, which is why the escalation signals check `collector_ran`
    # before concluding anything from an empty graph.
    identities: dict[str, Any] = field(default_factory=dict)
    federated: list[dict[str, Any]] = field(default_factory=list)
    # The RBAC-bypass sweep. `bypass_assessed` is the DENOMINATOR: an empty row list with a
    # non-zero denominator means "we looked and everything was closed", which is the opposite
    # of an empty list with a zero denominator.
    bypass_rows: list[dict[str, Any]] = field(default_factory=list)
    bypass_summary: dict[str, Any] = field(default_factory=dict)
    bypass_assessed: int = 0
    # The classified diff against the previous run, with attribution attached where it was
    # recoverable. `drift_available` is the gate: an empty change list on a tenant with only one
    # run means "there is nothing to compare against", not "nothing changed".
    drift: dict[str, Any] = field(default_factory=dict)
    drift_available: bool = False
    # Minutes offset from UTC for the reader's local time. After-hours judgement uses it —
    # judging raw UTC calls a Tokyo morning suspicious.
    utc_offset_minutes: int = 0
    # The usage slice from the SEPARATE usage job. It has its own freshness on purpose: the
    # Activity Log is per-subscription and slow, so it cannot ride along with an access refresh.
    # `usage.is_measured()` is the gate — an empty action set means "we have not looked", which
    # is not the same as "this principal used nothing".
    usage: dict[str, Any] = field(default_factory=dict)
    data_plane_logged: bool = False
    _rightsizing: Any = None
    _escalation: Any = None

    @property
    def rightsizing(self) -> dict[str, Any]:
        """Granted-vs-used analysis for this tenant.

        Read from cache, not computed. The analysis is pure CPU over the whole role catalogue and
        is written by the usage job that produces its input; recomputing it here would put two
        seconds of work on the findings endpoint, which is one of the hottest in the product."""
        if self._rightsizing is None:
            from app.iam import cache

            self._rightsizing = cache.read_rightsizing(self.tenant_id)
        return self._rightsizing

    def escalation(self) -> dict[str, Any]:
        """The escalation graph for this snapshot, built at most once per evaluation.

        Lazy because it is by far the most expensive thing a signal can ask for — it runs the
        effective-permission engine across every principal, primitive and scope — and only the
        `esc` pillar needs it."""
        if self._escalation is None:
            from app.iam import effective, escalation

            self._escalation = escalation.graph_for_tenant(
                self.tenant_id,
                self.rows,
                effective.build_role_index(self.directory.get("role_defs", [])),
                identities=self.identities,
                federated=self.federated,
            )
        return self._escalation

    # ---- derived helpers, computed once per evaluation -----------------------------
    @property
    def grants(self) -> list[dict[str, Any]]:
        """Rows that GRANT access. Deny rows are excluded — a signal counting them as access
        would report the control as the risk."""
        from app.iam import schema

        return [r for r in self.rows if r.get("effect") != schema.EFFECT_DENY]

    def collector_ran(self, *names: str) -> bool:
        """True when at least one cached scope ran one of these collectors and could read.

        This is how a signal tells "there is nothing there" from "we never looked".

        Names are matched through :data:`COLLECTOR_ALIASES` because the same surface is read by
        two differently-named collectors depending on the path taken: the tenant-wide Resource
        Graph sweep emits ``ArgKeyVaultAccessPolicies`` while the per-subscription ARM fallback
        emits ``KeyVaultAccessPolicies``. A signal naming only one of them reports "not
        collected" on every tenant that took the other route — which is exactly what
        `priv.keyvault_dual_grant_model` did on every ARG-swept tenant."""
        from app.iam import schema

        wanted: set[str] = set()
        for n in names:
            wanted.add(n)
            wanted |= COLLECTOR_ALIASES.get(n, set())

        bad = {schema.STATUS_FAILED, schema.STATUS_UNAUTHORIZED, schema.STATUS_THROTTLED}
        for meta in self.scopes:
            for c in meta.get("collectors", []) or []:
                if c.get("collector") in wanted and c.get("status") not in bad:
                    return True
        return False

    def require(self, condition: bool, reason: str) -> None:
        """Assert an input is present, or record an honest *not measured*."""
        if not condition:
            raise SignalUnavailable(reason)


@dataclass(frozen=True)
class SignalSpec:
    id: str                                        # pillar-namespaced, e.g. "priv.owner_standing"
    title: str
    pillar: str
    severity: str
    weight: int                                    # 1..10 within its pillar
    object_kind: str
    evaluate: Callable[[SignalContext], list[Finding]]
    why: str = ""                                  # why a human should care
    remediation: str = ""
    frameworks: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "pillar": self.pillar,
            "severity": self.severity,
            "weight": self.weight,
            "object_kind": self.object_kind,
            "why": self.why,
            "remediation": self.remediation,
            "frameworks": list(self.frameworks),
            "tags": list(self.tags),
        }


# --------------------------------------------------------------------------- registry
def _load_specs() -> list[SignalSpec]:
    """Every ``SIGNALS`` list in :mod:`app.iam.signal_defs`, discovered rather than listed.

    This used to be a hardcoded import list, which meant adding a new pillar file did **nothing**
    — the module was never imported, the pillar kept reporting `not_implemented`, and there was
    no error anywhere to explain why. Discovery makes forgetting impossible; a module without a
    ``SIGNALS`` list raises loudly instead of being skipped."""
    import importlib
    import pkgutil

    from app.iam import signal_defs

    specs: list[SignalSpec] = []
    for info in sorted(pkgutil.iter_modules(signal_defs.__path__), key=lambda m: m.name):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{signal_defs.__name__}.{info.name}")
        if not hasattr(module, "SIGNALS"):
            raise RuntimeError(
                f"app.iam.signal_defs.{info.name} has no SIGNALS list. Every module in this "
                "package must export one, or its checks are silently absent from the score."
            )
        specs.extend(module.SIGNALS)
    return specs


_REGISTRY: list[SignalSpec] | None = None


def all_signals() -> list[SignalSpec]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _load_specs()
    return _REGISTRY


def signals_for_pillar(pillar: str) -> list[SignalSpec]:
    return [s for s in all_signals() if s.pillar == pillar]


@dataclass
class SignalResult:
    spec: SignalSpec
    findings: list[Finding]
    measured: bool
    reason: str = ""          # why it was not measured

    def public(self) -> dict[str, Any]:
        return {
            **self.spec.public(),
            "measured": self.measured,
            "reason": self.reason,
            "finding_count": len(self.findings),
        }


def evaluate_all(ctx: SignalContext) -> list[SignalResult]:
    """Run every registered signal over one snapshot.

    A signal that raises :class:`SignalUnavailable` is recorded as *not measured*. A signal that
    raises anything else is a bug in that signal — it is logged and recorded as not measured
    rather than taking the whole screen down, because one broken check must not blind the other
    fifty."""
    out: list[SignalResult] = []
    for spec in all_signals():
        try:
            findings = spec.evaluate(ctx) or []
        except SignalUnavailable as exc:
            out.append(SignalResult(spec, [], measured=False, reason=str(exc)))
            continue
        except Exception:  # noqa: BLE001 - one bad signal must not blind the rest
            log.exception("iam signal %s raised", spec.id)
            out.append(SignalResult(spec, [], measured=False, reason="This check failed to run."))
            continue
        out.append(SignalResult(spec, findings, measured=True))
    return out
