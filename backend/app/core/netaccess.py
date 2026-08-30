"""Network access control — the in-application IP allowlist ("application firewall").

Layer B of the design in ``docs/improvement-plans/network-access-control/``. Decides whether a
given client address may reach the application at all, BEFORE authentication runs, so an
unknown source never reaches the sign-in page and cannot attempt credentials.

Three modes:

* ``off``     — no evaluation at all (the default; a fresh deployment is never restricted).
* ``monitor`` — evaluate and RECORD what would be blocked, but allow everything through. This
                exists so an operator can see real data before restricting anything; going
                straight to ``enforce`` on a guessed CIDR is how people lock themselves out.
* ``enforce`` — non-matching sources get a bare 403.

Storage is a small JSON file on the same Azure Files mount as the other registries, so it can
be repaired out-of-band (`az containerapp exec`, or downloading the share) when the admin UI
is exactly what has become unreachable. ``jsonstore`` validates it against the file's mtime,
so such an edit is picked up WITHOUT a restart.

SAFETY — this feature can lock its own operator out, so it carries several guards:
  * ``enforce`` cannot be saved unless a rule covers the caller's own address;
  * switching to ``enforce`` arms a commit-confirm timer that reverts to ``monitor`` unless the
    operator confirms from a still-permitted address (JunOS ``commit confirmed``);
  * ``IP_ALLOWLIST_DISABLED=true`` bypasses everything, so recovery never depends on being able
    to reach the app.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "network_access.json"

MODES = ("off", "monitor", "enforce")

# Large enough for enterprise egress lists while bounding config, preview, and backup payloads.
# Every write path uses ``normalize_rules`` so bulk import cannot bypass this ceiling.
MAX_RULES = 5_000

#: How long an ``enforce`` switch stays provisional before auto-reverting to ``monitor``.
CONFIRM_WINDOW_MINUTES = 15

#: Env var that disables the whole feature. The documented break-glass: it is set with
#: ``az containerapp update --set-env-vars``, i.e. through the Azure control plane, so it works
#: precisely when the application's own front door is refusing the operator.
BREAK_GLASS_ENV = "IP_ALLOWLIST_DISABLED"

#: Optional deploy-time seed, e.g. "203.0.113.0/24,198.51.100.7". Applied only when no config
#: file exists yet, so it cannot silently overwrite what an admin later configures in the UI.
SEED_ENV = "IP_ALLOWLIST_SEED"
SEED_MODE_ENV = "IP_ALLOWLIST_SEED_MODE"

DEFAULTS: dict[str, Any] = {
    "mode": "off",
    "rules": [],
    # ISO-8601 instant at which an unconfirmed `enforce` reverts to `monitor`. None = confirmed.
    "confirm_by": None,
}


class NetAccessError(ValueError):
    """Raised for operator-facing validation failures (surfaced as a 400)."""


# --------------------------------------------------------------------------- parsing


def parse_cidr(value: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    """Parse a single address or CIDR into a network, or raise ``NetAccessError``.

    ``strict=False`` so "203.0.113.7/24" is accepted and normalized rather than rejected for
    having host bits set — an operator writing that means the /24, and refusing it teaches
    nothing.
    """
    raw = (value or "").strip()
    if not raw:
        raise NetAccessError("Enter an IP address or CIDR range.")
    try:
        return ipaddress.ip_network(raw, strict=False)
    except ValueError:
        raise NetAccessError(f"'{raw}' is not a valid IP address or CIDR range.") from None


def describe_scope(net: ipaddress.IPv4Network | ipaddress.IPv6Network) -> str:
    """Human summary of a rule's breadth, so '/22' is checkable at a glance in the UI."""
    if net.version == 6:
        return "Single IPv6 address" if net.prefixlen == 128 else f"IPv6 /{net.prefixlen}"
    if net.prefixlen == 32:
        return "Single IP address"
    return f"{net.num_addresses:,} addresses"


def _is_everything(net: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
    return net.prefixlen == 0


def normalize_rules(
    rules: Iterable[Mapping[str, Any]],
    *,
    mode: str,
    actor: str | None = None,
) -> list[dict[str, Any]]:
    """Validate and canonicalize a complete policy rule list.

    This is the single authority used by ordinary saves, import previews, and exports. When
    ``actor`` is omitted, existing provenance is preserved; supplying it stamps the rules as an
    explicit save, matching the endpoint's historical behavior.
    """
    if mode not in MODES:
        raise NetAccessError(f"Unknown mode '{mode}'.")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    timestamp = datetime.now(UTC).isoformat()
    for rule in rules:
        if len(normalized) >= MAX_RULES:
            raise NetAccessError(f"A policy can contain at most {MAX_RULES:,} ranges.")
        net = parse_cidr(str(rule.get("cidr", "")))
        key = str(net)
        enabled = bool(rule.get("enabled", True))
        if mode == "enforce" and enabled and _is_everything(net):
            raise NetAccessError(
                f"'{key}' allows every address, which disables enforcement. "
                "Remove it or use Off mode."
            )
        label = str(rule.get("label", "") or "").strip()
        if not label:
            raise NetAccessError(f"'{key}' needs a label.")
        if key in seen:
            raise NetAccessError(f"'{key}' is listed more than once.")
        seen.add(key)
        normalized.append(
            {
                "cidr": key,
                "label": label[:128],
                "enabled": enabled,
                "created_by": (
                    actor if actor is not None else str(rule.get("created_by", "") or "")
                ),
                "created_at": (
                    timestamp
                    if actor is not None
                    else str(rule.get("created_at", "") or timestamp)
                ),
            }
        )
    return normalized


# --------------------------------------------------------------------------- config I/O


def _seed_from_env() -> dict[str, Any] | None:
    """Build an initial config from the deploy-time seed, or None when unset/unusable."""
    raw = (os.getenv(SEED_ENV) or "").strip()
    if not raw:
        return None
    rules: list[dict[str, Any]] = []
    for entry in raw.split(","):
        candidate = entry.strip()
        if not candidate:
            continue
        try:
            net = parse_cidr(candidate)
        except NetAccessError:
            # A malformed seed must never make the app unreachable or crash on boot: skip it.
            continue
        if _is_everything(net):
            continue
        rules.append(
            {
                "cidr": str(net),
                "label": "Seeded at deployment",
                "enabled": True,
                "created_by": "deployment",
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
    if not rules:
        return None
    mode = (os.getenv(SEED_MODE_ENV) or "enforce").strip().lower()
    if mode not in MODES:
        mode = "enforce"
    # A seeded config is deliberately NOT provisional: there is no operator session to confirm
    # it from, and auto-reverting a deployment-time policy 15 minutes after boot would silently
    # undo exactly what the deployer asked for.
    return {"mode": mode, "rules": rules, "confirm_by": None}


def load_config() -> dict[str, Any]:
    """Current configuration, seeded from the environment on first ever read."""
    stored = jsonstore.read_json(_PATH, None)
    if not isinstance(stored, dict):
        seeded = _seed_from_env()
        if seeded is not None:
            def _seed(current: Any) -> Any:
                return current if isinstance(current, dict) else seeded

            stored = jsonstore.mutate_json(_PATH, None, _seed)
            if isinstance(stored, dict):
                return stored
        return dict(DEFAULTS)
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in stored.items() if k in DEFAULTS})
    if cfg["mode"] not in MODES:
        cfg["mode"] = "off"
    if not isinstance(cfg["rules"], list):
        cfg["rules"] = []
    return cfg


def write_config(cfg: dict[str, Any]) -> dict[str, Any]:
    jsonstore.write_json(_PATH, cfg)
    return cfg


_DEFAULT_WRITE_CONFIG = write_config


def mutate_config(mutator) -> dict[str, Any]:  # noqa: ANN001
    """Apply one short read-modify-write transaction to the network access policy."""
    def _mutate(stored: Any) -> dict[str, Any]:
        cfg = dict(DEFAULTS)
        if isinstance(stored, dict):
            cfg.update({k: v for k, v in stored.items() if k in DEFAULTS})
        replacement = mutator(cfg)
        return cfg if replacement is None else replacement

    return jsonstore.mutate_json(_PATH, {}, _mutate)


# --------------------------------------------------------------------------- evaluation

# Compiled network bases grouped by prefix, cached against the exact enabled rule set. A linear
# scan was acceptable for a handful of hand-entered rules but not for an imported 5,000-range
# list. Matching now performs at most 33 IPv4 or 129 IPv6 prefix lookups, independent of list
# size. The underlying JSON read is mtime-validated, so an out-of-band edit invalidates this too.
_COMPILED_KEY: tuple[str, ...] | None = None
_COMPILED: dict[int, tuple[tuple[int, frozenset[int]], ...]] = {4: (), 6: ()}


def _compiled(
    rules: list[dict[str, Any]],
) -> dict[int, tuple[tuple[int, frozenset[int]], ...]]:
    global _COMPILED_KEY, _COMPILED
    key = tuple(
        str(r.get("cidr", "")) for r in rules if isinstance(r, dict) and r.get("enabled", True)
    )
    if key != _COMPILED_KEY:
        grouped: dict[int, dict[int, set[int]]] = {4: {}, 6: {}}
        for cidr in key:
            try:
                net = ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                # A corrupt stored value must not 500 every request. Skipping it is the safe
                # direction: in `enforce` a rule that cannot be parsed cannot grant access.
                continue
            grouped[net.version].setdefault(net.prefixlen, set()).add(int(net.network_address))

        compiled: dict[int, tuple[tuple[int, frozenset[int]], ...]] = {}
        for version, bits in ((4, 32), (6, 128)):
            rows: list[tuple[int, frozenset[int]]] = []
            for prefix, bases in sorted(grouped[version].items(), reverse=True):
                mask = ((1 << prefix) - 1) << (bits - prefix) if prefix else 0
                rows.append((mask, frozenset(bases)))
            compiled[version] = tuple(rows)
        _COMPILED_KEY, _COMPILED = key, compiled
    return _COMPILED


def reset_cache() -> None:
    """Drop the compiled-network cache (used by tests and after a save)."""
    global _COMPILED_KEY, _COMPILED
    _COMPILED_KEY, _COMPILED = None, {4: (), 6: ()}


def matches(ip: str | None, rules: list[dict[str, Any]]) -> bool:
    """True when ``ip`` falls inside any enabled rule."""
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    value = int(addr)
    return any((value & mask) in bases for mask, bases in _compiled(rules)[addr.version])


def matching_rule(ip: str | None, rules: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The first enabled rule covering ``ip``, for showing the operator *why* they are allowed."""
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    for rule in rules:
        if not isinstance(rule, dict) or not rule.get("enabled", True):
            continue
        try:
            net = ipaddress.ip_network(str(rule.get("cidr", "")), strict=False)
        except ValueError:
            continue
        if addr.version == net.version and addr in net:
            return rule
    return None


def break_glass_active() -> bool:
    return (os.getenv(BREAK_GLASS_ENV) or "").strip().lower() in {"1", "true", "yes"}


def effective_mode(cfg: dict[str, Any] | None = None) -> str:
    """Mode after applying break-glass and the commit-confirm timer.

    The timer is evaluated on READ rather than by a background job, so an expired provisional
    ``enforce`` stops blocking immediately and correctly even if the scheduler is not running,
    the process restarted, or the clock jumped. The persisted revert is a separate, best-effort
    step — see ``revert_if_expired``.
    """
    if break_glass_active():
        return "off"
    cfg = cfg or load_config()
    mode = cfg.get("mode", "off")
    if mode != "enforce":
        return mode if mode in MODES else "off"
    confirm_by = cfg.get("confirm_by")
    if confirm_by and _expired(confirm_by):
        return "monitor"
    return "enforce"


def _expired(iso_ts: str) -> bool:
    try:
        deadline = datetime.fromisoformat(iso_ts)
    except (TypeError, ValueError):
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return datetime.now(UTC) >= deadline


def confirm_deadline() -> str:
    return (datetime.now(UTC) + timedelta(minutes=CONFIRM_WINDOW_MINUTES)).isoformat()


def revert_if_expired() -> dict[str, Any] | None:
    """Persist the auto-revert when the confirm window has lapsed. Returns the new config."""
    reverted = False

    def _mutate(cfg: dict[str, Any]) -> None:
        nonlocal reverted
        confirm_by = cfg.get("confirm_by")
        if cfg.get("mode") == "enforce" and confirm_by and _expired(confirm_by):
            cfg["mode"] = "monitor"
            cfg["confirm_by"] = None
            reverted = True

    cfg = mutate_config(_mutate)
    return cfg if reverted else None
