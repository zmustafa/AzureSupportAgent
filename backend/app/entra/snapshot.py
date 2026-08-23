"""Snapshot assembly, analysis and refresh orchestration.

One place assembles the cached domain payloads into a single object, runs the Conditional
Access engine, evaluates every signal and computes the score. Every endpoint reads the
result of this module; nothing else re-derives it. That is what stops two screens
disagreeing about how many administrators lack MFA.

The analysis is memoised on the domain generation timestamps, so repeated reads inside one
page load do not re-evaluate 60 signals over 100,000 users.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from app.entra import DOMAINS, cache, ca_engine, licences as licence_mod, model, permissions_probe, score as score_mod
from app.entra import azure_link as azure_link_mod
from app.entra import signals as sig
from app.entra.collectors import CollectContext
from app.entra.collectors import activations as activations_collector
from app.entra.collectors import apps as apps_collector
from app.entra.collectors import ca as ca_collector
from app.entra.collectors import governance as governance_collector
from app.entra.collectors import people as people_collector
from app.entra.collectors import pim as pim_collector
from app.entra.collectors import risk as risk_collector
from app.entra.collectors import roles as roles_collector
from app.entra.collectors import tenant as tenant_collector
from app.entra.graphclient import GraphClient

log = logging.getLogger("app.entra.snapshot")

COLLECTORS: dict[str, Any] = {
    "tenant": tenant_collector.collect,
    "people": people_collector.collect,
    "apps": apps_collector.collect,
    "roles": roles_collector.collect,
    "pim": pim_collector.collect,
    "activations": activations_collector.collect,
    "ca": ca_collector.collect,
    "risk": risk_collector.collect,
    "governance": governance_collector.collect,
}

# Deliberate order: cheap context first, then the big inventories, then policy which reads
# best when the operator has already seen the counts scroll past. `pim` follows `roles` so
# the progress log tells one continuous story about privilege. `risk` is last but one
# because it is by far the slowest — an operator watching the log has seen everything else
# land before the sign-in aggregation starts.
COLLECT_ORDER: tuple[str, ...] = (
    # activations sits after roles because it labels each session with the role's tier, and
    # before risk because risk is the long pole and should not delay a cheap domain.
    "tenant", "roles", "pim", "activations", "people", "apps", "ca", "governance", "risk",
)

_analysis_memo: dict[str, tuple[str, dict[str, Any]]] = {}


# --------------------------------------------------------------------------- settings
def settings() -> dict[str, Any]:
    from app.core.app_settings import load_settings

    s = load_settings()
    return {
        "ttl_s": int(s.get("entra_cache_ttl_s", 21600) or 21600),
        "stale_days": int(s.get("entra_stale_days", 90) or 90),
        # Guests are held to their own bar. Employees drift in and out of a tenant for
        # legitimate reasons (leave, secondment); an external identity that has not been used
        # is simply standing access nobody is exercising, so the default is stricter and
        # separately tunable rather than inheriting the employee threshold.
        "guest_stale_days": int(s.get("entra_guest_stale_days", 90) or 90),
        "expiry_window_days": int(s.get("entra_expiry_window_days", 90) or 90),
        "signin_lookback_days": int(s.get("entra_signin_lookback_days", 30) or 30),
        "activation_lookback_days": int(s.get("entra_activation_lookback_days", 90) or 90),
        "max_users": int(s.get("entra_max_users", 250000) or 250000),
        "max_activation_hours": 8.0,
        "beta": bool(s.get("entra_enable_beta_endpoints", True)),
        "graph_concurrency": int(s.get("entra_graph_concurrency", 12) or 12),
    }


def context_from_settings(tenant_id: str, suppressions: set[str] | None = None) -> sig.SignalContext:
    s = settings()
    return sig.SignalContext(
        now=datetime.now(timezone.utc),
        stale_days=s["stale_days"],
        guest_stale_days=s["guest_stale_days"],
        expiry_window_days=s["expiry_window_days"],
        signin_lookback_days=s["signin_lookback_days"],
        suppressions=suppressions or set(),
        tenant_id=tenant_id,
    )


# ----------------------------------------------------------------------- load + meta
def load(tenant_id: str) -> dict[str, Any]:
    """Read every domain sidecar. Never triggers collection."""
    domains: dict[str, dict[str, Any]] = {}
    data: dict[str, Any] = {}
    generated: list[str] = []
    for name in DOMAINS:
        payload = cache.read_domain(tenant_id, name)
        if payload is None:
            domains[name] = {
                "name": name, "status": model.STATUS_NOT_COLLECTED, "generated_at": "",
                "item_count": 0, "duration_ms": 0, "error": "", "missing_permissions": [],
                "truncated": False, "notes": [], "blockers": [],
            }
            continue
        meta = cache.meta_of(payload)
        domains[name] = meta
        data[name] = payload.get("data") or {}
        if meta.get("generated_at"):
            generated.append(meta["generated_at"])

    index = cache.tenant_index(tenant_id)
    newest = max(generated) if generated else ""
    return {
        "tenant_id": tenant_id,
        "loaded": bool(data),
        "generated_at": newest,
        "domains": domains,
        "data": data,
        "licences": index.get("licences") or licence_mod.empty_flags("Not detected yet."),
        "permissions": index.get("permissions") or {},
        "last_full": index.get("last_full", ""),
    }


def meta_envelope(snapshot: dict[str, Any], connection_id: str = "") -> dict[str, Any]:
    """The ``meta`` block every Entra endpoint returns.

    Rendered by the frontend *before* the data, which is what makes blindness impossible
    to hide."""
    ttl = settings()["ttl_s"]
    age = cache.age_seconds(snapshot.get("generated_at", ""))
    analysis = snapshot.get("_analysis") or {}
    score = analysis.get("score") or {}
    return {
        "tenant_id": snapshot.get("tenant_id", ""),
        "connection_id": connection_id,
        "loaded": bool(snapshot.get("loaded")),
        "generated_at": snapshot.get("generated_at", ""),
        "age_seconds": int(age) if age is not None else None,
        "ttl_s": ttl,
        "stale": (age is None) or (age >= ttl),
        "coverage": score.get("coverage"),
        "domains": snapshot.get("domains", {}),
        "licences": snapshot.get("licences", {}),
        "permissions_summary": _permissions_summary(snapshot.get("permissions") or {}),
        "blockers": collect_blockers(snapshot.get("domains") or {}),
        "truncated": any(d.get("truncated") for d in (snapshot.get("domains") or {}).values()),
        "last_full": snapshot.get("last_full", ""),
    }


# Order the reader should act in: what they can grant, then what they can assign, then what
# costs money, then what is inherent and needs no action at all.
_BLOCKER_ORDER = {
    model.BLOCKER_CONSENT: 0,
    model.BLOCKER_AZURE_ROLE: 1,
    model.BLOCKER_LICENCE: 2,
    model.BLOCKER_CAP: 3,
}


def collect_blockers(domains: dict[str, Any]) -> list[dict[str, Any]]:
    """Every distinct obstacle once, naming all the domains it affects.

    Deduplication is the point. One missing permission used to be reported by each domain
    that wanted it — three sentences for a single consent click — which buries the two or
    three things the reader actually has to do.
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for name in sorted(domains):
        for raw in (domains[name] or {}).get("blockers") or []:
            entry = dict(raw)
            key = (str(entry.get("kind") or ""), str(entry.get("scope") or entry.get("text") or ""))
            existing = merged.get(key)
            if existing is None:
                entry["domains"] = [name]
                merged[key] = entry
                continue
            if name not in existing["domains"]:
                existing["domains"].append(name)
            # Keep the most specific wording rather than whichever domain collected first.
            if len(str(entry.get("text") or "")) > len(str(existing.get("text") or "")):
                existing["text"] = entry["text"]
            for field in ("subject", "impact"):
                if not existing.get(field) and entry.get(field):
                    existing[field] = entry[field]
    return sorted(
        merged.values(),
        key=lambda b: (_BLOCKER_ORDER.get(str(b.get("kind")), 9), str(b.get("scope") or b.get("text"))),
    )


def _permissions_summary(permissions: dict[str, Any]) -> dict[str, Any]:
    domains = permissions.get("domains") or {}
    blind = sorted(d for d, s in domains.items() if not s.get("ok"))
    missing: set[str] = set()
    for d in blind:
        for m in (domains.get(d) or {}).get("missing") or []:
            missing.add(m)
    return {
        "token_ok": permissions.get("token_ok", None),
        "token_error": permissions.get("token_error", ""),
        "granted_count": len(permissions.get("granted") or []),
        "blind_domains": blind,
        "missing": sorted(missing),
    }


# -------------------------------------------------------------------------- analysis
def analyze(tenant_id: str, *, force: bool = False) -> dict[str, Any]:
    """Load + analyze. Memoised on the domain generation timestamps."""
    snapshot = load(tenant_id)
    key = tenant_id or "default"
    stamp = "|".join(
        f"{n}:{(snapshot['domains'].get(n) or {}).get('generated_at', '')}" for n in DOMAINS
    ) + f"|{_state_stamp(tenant_id)}|rbac:{_rbac_signature()}"
    if not force:
        memo = _analysis_memo.get(key)
        if memo and memo[0] == stamp:
            # ``load`` returns a FRESH dict every call, so the derived joins the analysis
            # computed last time are not on it. Re-attach them, otherwise every endpoint
            # reading ``data['_azure_link']`` or ``data['_ca_analysis']`` silently loses
            # the join the moment the memo is warm.
            _attach_derived(snapshot, memo[1])
            return snapshot

    state = read_state(tenant_id)
    ctx = context_from_settings(tenant_id, suppressions=set(state.get("suppressed") or []))
    data = snapshot["data"]

    # The Azure bridge: read-only join with the RBAC cache that another feature already
    # maintains. Carries its own freshness so a stale join is never shown as current.
    data["_azure_link"] = azure_link_mod.build(
        tenant_id, entra_generated_at=snapshot.get("generated_at", "")
    )

    ca_analysis: dict[str, Any] = {}
    if model.domain_usable(snapshot["domains"].get("ca")):
        try:
            ca_analysis = ca_engine.analyze(
                data, confirmed_breakglass=state.get("breakglass") or {}, now=ctx.now
            )
        except Exception:  # noqa: BLE001 - a CA engine failure must not lose the rest
            log.exception("entra CA analysis failed for tenant %s", tenant_id)
            ca_analysis = {}
    data["_ca_analysis"] = ca_analysis

    _attach_federation_population(data)

    result = sig.evaluate_all(data, snapshot["domains"], ctx, snapshot.get("licences"))
    score = score_mod.compute(data, result, ctx)

    analysis = {
        "score": score,
        "findings": result.findings,
        "by_signal": result.by_signal,
        "not_measured": result.not_measured,
        "errors": result.errors,
        "ca": ca_analysis,
        "azure_link": data["_azure_link"],
        "generated_at": model.now_iso(),
    }
    _analysis_memo[key] = (stamp, analysis)
    _attach_derived(snapshot, analysis)
    return snapshot


def _attach_derived(snapshot: dict[str, Any], analysis: dict[str, Any]) -> None:
    """Put the analysis and its derived joins back onto a freshly loaded snapshot."""
    snapshot["_analysis"] = analysis
    snapshot["_ca_analysis"] = analysis.get("ca") or {}
    data = snapshot.setdefault("data", {})
    data["_ca_analysis"] = analysis.get("ca") or {}
    data["_azure_link"] = analysis.get("azure_link") or azure_link_mod.empty(
        "The Azure join has not been computed for this snapshot yet."
    )
    # The memo short-circuits `analyze`, so a cached snapshot would otherwise carry
    # federation rows with no population attached.
    _attach_federation_population(data)


def _attach_federation_population(data: dict[str, Any]) -> None:
    """Join each federation trust to the users who sign in through it.

    Two collectors own the halves of this: `tenant` knows which domains are federated,
    `people` knows every UPN. Neither reads the other during collection, so the join happens
    here — where both are already loaded and no extra Graph call is needed.

    A trust with no population attached is a trust whose user list could not be read; the
    keys are simply absent rather than zero, so nothing renders "0 users" for a domain that
    might have thousands.
    """
    from app.entra import federation as fed

    fabric = ((data.get("tenant") or {}).get("identity_fabric")) or {}
    trusts = fabric.get("federation") or []
    if not trusts:
        return
    users = (data.get("people") or {}).get("users")
    if not isinstance(users, list) or not users:
        return
    total = len(users)
    for trust in trusts:
        count = fed.population(users, str(trust.get("domain") or ""))
        trust["user_count"] = count
        trust["user_share"] = round(count / total, 4) if total else None
    fabric["user_total"] = total


def _state_stamp(tenant_id: str) -> str:
    state = read_state(tenant_id)
    return f"{len(state.get('suppressed') or [])}:{len(state.get('breakglass') or {})}"


def _rbac_signature() -> str:
    """So a fresh Azure RBAC scan invalidates the cross-plane join."""
    try:
        from app.iam import cache as rbac_cache

        return str(rbac_cache.cache_version())
    except Exception:  # noqa: BLE001 - the RBAC module is optional here
        return "-"


def invalidate(tenant_id: str) -> None:
    _analysis_memo.pop(tenant_id or "default", None)
    cache.clear_memo()


# ----------------------------------------------------------------------- user state
def read_state(tenant_id: str) -> dict[str, Any]:
    """Persistent USER state. Never rewritten by a collection run."""
    state = cache.read_state(tenant_id, "findings_state", {})
    if not isinstance(state, dict):
        state = {}
    state.setdefault("suppressed", [])
    state.setdefault("breakglass", {})
    state.setdefault("findings", {})
    return state


def write_state(tenant_id: str, state: dict[str, Any]) -> None:
    cache.write_state(tenant_id, "findings_state", state)
    invalidate(tenant_id)


# -------------------------------------------------------------------------- refresh
async def refresh(
    tenant_id: str,
    connection: dict[str, Any] | None,
    *,
    domains: list[str] | None = None,
    connection_id: str = "",
    progress: Callable[[str, str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Collect the requested domains and persist them. The only path that calls Graph."""
    wanted = [d for d in (domains or list(COLLECT_ORDER)) if d in COLLECTORS]
    wanted = [d for d in COLLECT_ORDER if d in wanted]  # keep a deterministic order
    s = settings()

    async def say(level: str, message: str) -> None:
        if progress:
            await progress(level, message)

    await say("info", f"Connecting to Microsoft Graph for tenant {tenant_id}…")
    # Wider than the client default because the slowest phases here are `$batch` fan-outs
    # (group owners, guest domains, app assignments) that sit idle waiting on the gate. The
    # client narrows itself on 429 and recovers, so this is a ceiling rather than a promise.
    async with GraphClient(connection, beta=s["beta"],
                           concurrency=int(s.get("graph_concurrency") or 12)) as client:
        token, token_err = await client.probe_token()
        if not token:
            await say("error", f"No Microsoft Graph token: {token_err}")
            for name in wanted:
                cache.write_domain(tenant_id, name, model.blind_payload(name, token_err))
            cache.set_domain_meta(tenant_id, "_auth", {"status": model.STATUS_BLIND, "error": token_err})
            invalidate(tenant_id)
            return {"ok": False, "error": token_err, "domains": wanted}

        await say("info", "Detecting licences and granted permissions…")
        flags = await licence_mod.detect(client)
        permissions = await permissions_probe.build(client, live=not flags.get("detected"))
        _persist_context(tenant_id, flags, permissions)
        tiers = ", ".join(t["name"] for t in permissions.get("tiers") or [] if t.get("complete")) or "partial"
        await say("ok", f"Licences: P1={flags.get('p1')} P2={flags.get('p2')} · consent tiers complete: {tiers}")

        ctx = CollectContext(
            tenant_id=tenant_id,
            connection_id=connection_id,
            licences=flags,
            permissions=permissions,
            max_users=s["max_users"],
            expiry_window_days=s["expiry_window_days"],
            stale_days=s["stale_days"],
            signin_lookback_days=s["signin_lookback_days"],
            activation_lookback_days=int(s.get("activation_lookback_days", 90) or 90),
            max_activation_hours=float(s.get("max_activation_hours", 8.0) or 8.0),
            beta=s["beta"],
            progress=progress,
        )

        for name in wanted:
            await say("info", f"Collecting {name}…")
            payload = await COLLECTORS[name](client, ctx)
            cache.write_domain(tenant_id, name, payload)
            if name == "activations":
                # Fold into the durable ledger before anything can age out of the source.
                # Graph forgets directory audits after 30 days; this is what lets the tab
                # answer questions older than that.
                from app.entra import activations_ledger

                sessions = (payload.get("data") or {}).get("sessions") or []
                led = activations_ledger.append(tenant_id, sessions)
                if led["added"] or led["trimmed"]:
                    await say("info",
                              f"Activation ledger: +{led['added']:,} new, "
                              f"{led['total']:,} retained"
                              + (f", {led['trimmed']:,} trimmed at the cap"
                                 if led["trimmed"] else ""))
            status = payload.get("status")
            level = "ok" if status == model.STATUS_OK else ("warn" if status in
                                                            (model.STATUS_PARTIAL, model.STATUS_BLIND,
                                                             model.STATUS_UNLICENSED) else "error")
            await say(level, f"{name}: {status} ({payload.get('item_count', 0):,} item(s))"
                             + (f" — {payload.get('error')[:160]}" if payload.get("error") else ""))

        stats = client.stats.as_dict()
        cache.set_domain_meta(tenant_id, "_graph", {"status": "ok", "generated_at": model.now_iso(), **stats})
        await say("ok", f"Graph: {stats['requests']} request(s), {stats['batches']} batch(es), "
                        f"{stats['throttled']} throttle event(s)")

    invalidate(tenant_id)
    full = set(wanted) >= set(COLLECT_ORDER)
    snapshot = analyze(tenant_id, force=True)
    if full:
        cache.mark_full_refresh(tenant_id)
        _record_history(tenant_id, snapshot)
    await say("ok", f"Identity posture {snapshot['_analysis']['score']['score']}/100 "
                    f"(measured {snapshot['_analysis']['score']['coverage']:.0%} of the model)")
    return {"ok": True, "domains": wanted, "score": snapshot["_analysis"]["score"]["score"]}


def _persist_context(tenant_id: str, flags: dict[str, Any], permissions: dict[str, Any]) -> None:
    cache.set_tenant_meta(tenant_id, licences=flags, permissions=permissions)


def _record_history(tenant_id: str, snapshot: dict[str, Any]) -> None:
    """Only a successful FULL refresh writes a history point — a partial one would make the
    trend line lie about what changed."""
    analysis = snapshot.get("_analysis") or {}
    score = analysis.get("score") or {}
    if not score:
        return
    # Roll the previous run's findings forward BEFORE overwriting, so "new since last scan"
    # still has something to compare against.
    prior = previous_findings(tenant_id)
    cache.write_domain(tenant_id, "findings", model.domain_payload(
        "findings",
        {"findings": analysis.get("findings") or [], "previous": prior},
        item_count=len(analysis.get("findings") or []),
    ))
    cache.append_score_history(
        tenant_id,
        score_mod.history_entry(score, uuid.uuid4().hex[:12], snapshot.get("generated_at") or model.now_iso()),
    )


def previous_findings(tenant_id: str) -> list[dict[str, Any]]:
    """Findings as of the last completed full refresh (the delta baseline)."""
    payload = cache.read_domain(tenant_id, "findings")
    if not payload:
        return []
    return (payload.get("data") or {}).get("findings") or []
