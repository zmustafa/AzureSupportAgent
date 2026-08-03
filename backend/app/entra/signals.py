"""The Signal Registry — one declarative catalogue behind every surface.

The posture score, the findings list, the area screens, the proactive scanners, the
assessment controls, the notifications, the agent answers and the exports are all
*projections over this catalogue*. If a check is implemented anywhere other than
``signal_defs/``, that is a bug: it is how the same logic ends up written five times and
two screens end up disagreeing about what "privileged" means.

Every ``evaluate`` implementation must be **pure and total**:

* no network, no disk, no clock (use ``ctx.now``),
* a missing domain returns ``[]`` and the runner records *not measured* — never raises,
* every finding carries ``evidence`` with the values that triggered it, because that is
  what makes the score verifiable instead of a black box.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from app.entra import model

log = logging.getLogger("app.entra.signals")

# --------------------------------------------------------------------------- pillars
PILLARS: list[dict[str, Any]] = [
    {"key": "auth", "label": "Authentication", "weight": 15,
     "blurb": "MFA coverage, method strength, legacy authentication and the tenant methods policy."},
    {"key": "ca", "label": "Conditional Access", "weight": 20,
     "blurb": "Policy coverage, conflicts, exclusions, break-glass exposure and risk policies."},
    {"key": "priv", "label": "Privileged Access", "weight": 20,
     "blurb": "Standing versus eligible roles, privileged guests and service principals, separation of duties."},
    {"key": "app", "label": "Applications & Consent", "weight": 15,
     "blurb": "Credential hygiene, granted Graph permissions, consent posture and ownership."},
    {"key": "ppl", "label": "Users & Guests", "weight": 10,
     "blurb": "Stale and disabled accounts, guest sprawl, ownerless groups and external collaboration."},
    {"key": "risk", "label": "Risk Signals", "weight": 8,
     "blurb": "Identity Protection risky users, risky workload identities and sign-in anomalies."},
    {"key": "gov", "label": "Governance", "weight": 7,
     "blurb": "Access reviews, entitlement management and lifecycle workflows."},
    {"key": "mon", "label": "Monitoring & Hybrid", "weight": 5,
     "blurb": "Log export, break-glass alerting and directory synchronisation health."},
]
PILLAR_BY_KEY = {p["key"]: p for p in PILLARS}

IMPACT_BINARY = "binary"
IMPACT_RATIO = "ratio"
IMPACT_SATURATING = "saturating"

_SEVERITY_FACTOR = {"critical": 1.0, "high": 0.7, "medium": 0.4, "low": 0.2, "info": 0.0}


# --------------------------------------------------------------------------- context
@dataclass
class SignalContext:
    """Tunables. Thresholds are settings, never literals inside an evaluate body."""

    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    stale_days: int = 90
    expiry_window_days: int = 90
    signin_lookback_days: int = 30
    max_global_admins: int = 5
    min_global_admins: int = 2
    max_activation_hours: float = 8.0
    guest_ratio_threshold: float = 0.25
    # Business hours used by the out-of-hours activation check. Activation timestamps are
    # UTC; ``utc_offset_hours`` shifts them into the tenant's working day before the window
    # is applied. Left at 0 the check is explicitly UTC and says so in its finding, because
    # judging a London tenant's 09:00 as "out of hours" would be confidently wrong.
    business_hours_start: int = 7
    business_hours_end: int = 19
    utc_offset_hours: float = 0.0
    max_credentials_per_app: int = 2
    max_credential_lifetime_days: int = 730
    credential_rotation_days: int = 365
    max_findings_per_signal: int = 500
    suppressions: set[str] = field(default_factory=set)
    tenant_id: str = ""

    def days_since(self, ts: str) -> int | None:
        dt = _parse(ts)
        return None if dt is None else int((self.now - dt).total_seconds() // 86400)

    def days_until(self, ts: str) -> int | None:
        dt = _parse(ts)
        return None if dt is None else int((dt - self.now).total_seconds() // 86400)


def _parse(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class SignalUnavailable(Exception):
    """Raised inside an ``evaluate`` body when the check cannot honestly be performed.

    The domain collected fine, but the specific capability it depends on did not (for
    example the MFA registration report needs P1, so ``mfa_registered`` is ``None`` for
    everyone). Returning ``[]`` in that situation would silently *improve* the score for a
    tenant we simply could not measure — the exact "blind counted as clean" failure this
    product must never have. Raising this records an honest *not measured* instead.
    """


# ------------------------------------------------------------------------ SignalSpec
@dataclass(frozen=True)
class SignalSpec:
    id: str
    title: str
    question: str
    why: str
    pillar: str
    severity: str
    weight: int
    object_kind: str
    evaluate: Callable[[dict[str, Any], SignalContext], list[dict[str, Any]]]
    domains: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    licence: str = "free"
    benchmarks: tuple[str, ...] = ()
    remediation: str = ""
    remediation_steps: tuple[str, ...] = ()
    doc_link: str = ""
    impact: str = IMPACT_BINARY
    saturation: int = 3
    population: Callable[[dict[str, Any]], int] | None = None
    default_enabled: bool = True
    scannable: bool = True
    tags: tuple[str, ...] = ()

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "question": self.question,
            "why": self.why,
            "pillar": self.pillar,
            "severity": self.severity,
            "weight": self.weight,
            "object_kind": self.object_kind,
            "domains": list(self.domains),
            "requires": list(self.requires),
            "licence": self.licence,
            "benchmarks": list(self.benchmarks),
            "remediation": self.remediation,
            "remediation_steps": list(self.remediation_steps),
            "doc_link": self.doc_link,
            "tags": list(self.tags),
        }


# --------------------------------------------------------------------------- registry
def _load_specs() -> list[SignalSpec]:
    from app.entra.signal_defs import activations as activation_defs
    from app.entra.signal_defs import app as app_defs
    from app.entra.signal_defs import auth as auth_defs
    from app.entra.signal_defs import ca as ca_defs
    from app.entra.signal_defs import ca_appclass as ca_appclass_defs
    from app.entra.signal_defs import fed as fed_defs
    from app.entra.signal_defs import gov as gov_defs
    from app.entra.signal_defs import mon as mon_defs
    from app.entra.signal_defs import ppl as ppl_defs
    from app.entra.signal_defs import priv as priv_defs
    from app.entra.signal_defs import priv_pim as priv_pim_defs
    from app.entra.signal_defs import risk as risk_defs

    specs: list[SignalSpec] = []
    for module in (auth_defs, ca_defs, ca_appclass_defs, priv_defs, priv_pim_defs,
                   activation_defs, app_defs, ppl_defs, risk_defs, gov_defs, mon_defs, fed_defs):
        specs.extend(module.SPECS)
    seen: set[str] = set()
    for s in specs:
        if s.id in seen:
            raise ValueError(f"duplicate Entra signal id: {s.id}")
        seen.add(s.id)
        if s.pillar not in PILLAR_BY_KEY:
            raise ValueError(f"signal {s.id} declares unknown pillar {s.pillar}")
    return specs


_REGISTRY: list[SignalSpec] | None = None


def registry() -> list[SignalSpec]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _load_specs()
    return _REGISTRY


def by_id(signal_id: str) -> SignalSpec | None:
    return next((s for s in registry() if s.id == signal_id), None)


def registry_version() -> int:
    """Bumped implicitly by the catalogue size; recorded on every score history point so a
    later comparison can say "the model changed" instead of silently drifting."""
    return len(registry())


# ------------------------------------------------------------------------ evaluation
@dataclass
class EvaluationResult:
    findings: list[dict[str, Any]]
    measured: set[str]
    not_measured: dict[str, str]
    by_signal: dict[str, int]
    errors: dict[str, str]

    def public_not_measured(self) -> list[dict[str, str]]:
        return [{"signal_id": k, "reason": v} for k, v in sorted(self.not_measured.items())]


def evaluate_all(
    snapshot_data: dict[str, Any],
    domain_meta: dict[str, dict[str, Any]],
    ctx: SignalContext,
    licences: dict[str, Any] | None = None,
) -> EvaluationResult:
    """Run every enabled signal. Pure: no I/O, deterministic for a given snapshot+ctx."""
    from app.entra.licences import licence_label, licence_ok

    licences = licences or {}
    findings: list[dict[str, Any]] = []
    measured: set[str] = set()
    not_measured: dict[str, str] = {}
    by_signal: dict[str, int] = {}
    errors: dict[str, str] = {}

    for spec in registry():
        if not spec.default_enabled:
            not_measured[spec.id] = "Disabled."
            continue
        if not licence_ok(licences, spec.licence):
            not_measured[spec.id] = f"Requires {licence_label(spec.licence)}."
            continue
        missing_domain = next(
            (d for d in spec.domains if not model.domain_usable(domain_meta.get(d))), None
        )
        if missing_domain:
            not_measured[spec.id] = model.domain_reason(domain_meta.get(missing_domain), missing_domain)
            continue
        try:
            produced = spec.evaluate(snapshot_data, ctx) or []
        except SignalUnavailable as exc:
            not_measured[spec.id] = str(exc) or "The data this check needs was not collected."
            continue
        except Exception as exc:  # noqa: BLE001 - one bad signal must not lose the rest
            log.exception("entra signal %s failed", spec.id)
            not_measured[spec.id] = f"Evaluation failed: {type(exc).__name__}"
            errors[spec.id] = f"{type(exc).__name__}: {str(exc)[:200]}"
            continue

        measured.add(spec.id)
        if len(produced) > ctx.max_findings_per_signal:
            total = len(produced)
            produced = produced[: ctx.max_findings_per_signal]
            for f in produced:
                f.setdefault("evidence", {})["truncated_total"] = total
        live = [f for f in produced if f.get("fingerprint") not in ctx.suppressions]
        by_signal[spec.id] = len(live)
        findings.extend(live)

    return EvaluationResult(
        findings=model.sort_findings(findings),
        measured=measured,
        not_measured=not_measured,
        by_signal=by_signal,
        errors=errors,
    )


# --------------------------------------------------------------------------- scoring
def penalty_units(spec: SignalSpec, count: int, snapshot_data: dict[str, Any]) -> float:
    """Score cost of a signal's findings.

    Three impact shapes, chosen per signal, are what keep the score meaningful for both a
    200-user and a 200,000-user tenant:

    * ``binary`` — a tenant-level fact is either true or it is not.
    * ``ratio`` — normalised by population, so growth alone never moves the score.
    * ``saturating`` — small-N criticals: one permanent Global Administrator is bad in any
      tenant, and thirty is not ten times worse than three.
    """
    if count <= 0:
        return 0.0
    factor = _SEVERITY_FACTOR.get(spec.severity, 0.0)
    if spec.impact == IMPACT_BINARY:
        impact = 1.0
    elif spec.impact == IMPACT_SATURATING:
        impact = min(1.0, count / max(1, spec.saturation))
    else:  # ratio
        population = spec.population(snapshot_data) if spec.population else 0
        impact = min(1.0, count / population) if population > 0 else 1.0
    return spec.weight * factor * impact


def max_units(spec: SignalSpec) -> float:
    return float(spec.weight)


# ------------------------------------------------------------------- helper accessors
def domain(snapshot_data: dict[str, Any], name: str) -> dict[str, Any]:
    """Data payload for a domain (empty dict when absent)."""
    value = snapshot_data.get(name)
    return value if isinstance(value, dict) else {}


def enabled_users(snapshot_data: dict[str, Any]) -> list[dict[str, Any]]:
    return [u for u in domain(snapshot_data, "people").get("users") or [] if u.get("enabled")]


def enabled_members(snapshot_data: dict[str, Any]) -> list[dict[str, Any]]:
    return [u for u in enabled_users(snapshot_data) if u.get("user_type") != "Guest"]


def enabled_guests(snapshot_data: dict[str, Any]) -> list[dict[str, Any]]:
    return [u for u in enabled_users(snapshot_data) if u.get("user_type") == "Guest"]


def user_index(snapshot_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(u["id"]): u for u in domain(snapshot_data, "people").get("users") or [] if u.get("id")}


def principal_label(snapshot_data: dict[str, Any], principal_id: str, fallback: str = "") -> str:
    """Best available human name for a principal, from any domain that knows it.

    A raw GUID in a finding TITLE is unusable: the reader cannot tell who it is without
    leaving the product, so a critical finding reads as noise. Signals that only consulted
    the user index put bare ids at the top of the queue for every service principal, which
    is exactly the population the privileged signals care most about.
    """
    from app.entra.collectors.roles import principal_names

    # Azure role assignments frequently carry the object id in the name field when the ARM
    # side could not resolve the principal either. Accepting that as a fallback let a GUID
    # win over every real lookup below and printed it as if it were a name.
    if fallback.strip().lower() == principal_id.strip().lower():
        fallback = ""

    u = user_index(snapshot_data).get(principal_id) or {}
    if u.get("upn") or u.get("display_name"):
        return str(u.get("upn") or u.get("display_name"))

    from_roles = principal_names(domain(snapshot_data, "roles")).get(principal_id)
    if from_roles:
        return from_roles

    for sp in domain(snapshot_data, "apps").get("service_principals") or []:
        if str(sp.get("object_id") or "") == principal_id:
            name = str(sp.get("display_name") or "")
            if name:
                return f"{name} (service principal)"
            break  # found it, but it has no name — fall through and say so honestly

    for group in domain(snapshot_data, "people").get("groups") or []:
        if str(group.get("id") or "") == principal_id:
            name = str(group.get("display_name") or "")
            if name:
                return f"{name} (group)"
            break

    if fallback:
        return fallback
    # Say WHAT it is even when we cannot say who: this tells the reader the name lookup
    # failed, rather than implying the GUID is the name.
    return f"unresolved principal {principal_id}"


def pop_enabled_users(snapshot_data: dict[str, Any]) -> int:
    return len(enabled_users(snapshot_data))


def pop_enabled_members(snapshot_data: dict[str, Any]) -> int:
    return len(enabled_members(snapshot_data))


def pop_enabled_guests(snapshot_data: dict[str, Any]) -> int:
    return len(enabled_guests(snapshot_data))


def pop_applications(snapshot_data: dict[str, Any]) -> int:
    return len(domain(snapshot_data, "apps").get("applications") or [])


def pop_service_principals(snapshot_data: dict[str, Any]) -> int:
    return len(domain(snapshot_data, "apps").get("service_principals") or [])


def pop_groups(snapshot_data: dict[str, Any]) -> int:
    return len(domain(snapshot_data, "people").get("groups") or [])


def pop_policies(snapshot_data: dict[str, Any]) -> int:
    return max(1, len(domain(snapshot_data, "ca").get("policies") or []))


def cap(items: Sequence[Any], ctx: SignalContext) -> list[Any]:
    return list(items)[: ctx.max_findings_per_signal]
