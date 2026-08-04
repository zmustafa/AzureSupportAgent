"""Network access control (IP allowlist) — see docs/improvement-plans/network-access-control/.

The single most important test in this file is
``test_forged_x_forwarded_for_does_not_bypass_allowlist``. The allowlist is only worth anything
if the address it matches on cannot be chosen by the caller; on 2026-08-04 the deployed app was
measured doing exactly that (a forged ``X-Forwarded-For`` was recorded as the client IP), which
also made the per-IP brute-force lockout evadable. If that test ever fails, the feature is
decorative and must not ship.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core import netaccess


def _rule(cidr: str, *, enabled: bool = True, label: str = "test") -> dict:
    return {"cidr": cidr, "label": label, "enabled": enabled}


@pytest.fixture(autouse=True)
def _clean_cache():
    netaccess.reset_cache()
    yield
    netaccess.reset_cache()


# =============================================================== client IP resolution


class _Req:
    def __init__(self, peer: str | None, headers: dict[str, str] | None = None):
        self.client = SimpleNamespace(host=peer) if peer else None
        self.headers = headers or {}


def _with_managed_ingress(monkeypatch):
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "trust_forwarded_headers", True, raising=False)
    monkeypatch.setattr(settings, "trusted_proxies", "", raising=False)


def test_forged_x_forwarded_for_does_not_bypass_allowlist(monkeypatch):
    """A caller must not be able to choose their own apparent IP by prepending to the header.

    The proxy APPENDS the address it actually saw, so anything injected by the caller sits to
    the LEFT of it. Reading the leftmost entry (the previous behaviour) let any caller claim to
    be an allowlisted address.
    """
    from app.core.clientip import client_ip

    _with_managed_ingress(monkeypatch)

    allowlisted = "203.0.113.10"
    attacker = "45.155.205.11"
    req = _Req("10.0.0.8", {"x-forwarded-for": f"{allowlisted}, {attacker}"})

    resolved = client_ip(req)
    assert resolved == attacker, "the appended (proxy-observed) address must win"

    rules = [_rule(f"{allowlisted}/32")]
    assert netaccess.matches(resolved, rules) is False


def test_multiple_injected_entries_still_resolve_to_the_appended_address(monkeypatch):
    from app.core.clientip import client_ip

    _with_managed_ingress(monkeypatch)
    req = _Req("10.0.0.8", {"x-forwarded-for": "203.0.113.1, 203.0.113.2, 198.51.100.9"})
    assert client_ip(req) == "198.51.100.9"


def test_internal_hops_are_skipped_not_counted(monkeypatch):
    """An extra internal proxy hop must not change the answer (no hard-coded proxy depth)."""
    from app.core.clientip import client_ip

    _with_managed_ingress(monkeypatch)
    assert client_ip(_Req("10.0.0.8", {"x-forwarded-for": "198.51.100.9, 10.0.0.8"})) == "198.51.100.9"
    assert (
        client_ip(_Req("10.0.0.8", {"x-forwarded-for": "198.51.100.9, 10.0.0.8, 172.16.4.2"}))
        == "198.51.100.9"
    )


def test_untrusted_peer_ignores_the_header_entirely(monkeypatch):
    from app.core.clientip import client_ip
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "trust_forwarded_headers", False, raising=False)
    monkeypatch.setattr(settings, "trusted_proxies", "", raising=False)
    assert client_ip(_Req("203.0.113.99", {"x-forwarded-for": "198.51.100.1"})) == "203.0.113.99"


def test_unparseable_header_falls_back_to_the_socket_peer(monkeypatch):
    from app.core.clientip import client_ip

    _with_managed_ingress(monkeypatch)
    assert client_ip(_Req("10.0.0.8", {"x-forwarded-for": "not-an-ip"})) == "10.0.0.8"


def test_all_private_chain_returns_rightmost_private(monkeypatch):
    """A wholly-internal deployment has no public entry; every caller must not collapse to one."""
    from app.core.clientip import client_ip

    _with_managed_ingress(monkeypatch)
    assert client_ip(_Req("10.0.0.8", {"x-forwarded-for": "192.168.1.5, 10.0.0.8"})) == "10.0.0.8"


# =============================================================== CIDR matching


@pytest.mark.parametrize(
    ("ip", "cidr", "expected"),
    [
        ("203.0.113.7", "203.0.113.7", True),          # single host, implicit /32
        ("203.0.113.7", "203.0.113.0/24", True),       # inside
        ("203.0.113.0", "203.0.113.0/24", True),       # lower boundary
        ("203.0.113.255", "203.0.113.0/24", True),     # upper boundary
        ("203.0.112.255", "203.0.113.0/24", False),    # one below
        ("203.0.114.0", "203.0.113.0/24", False),      # one above
        ("2001:db8::5", "2001:db8::/32", True),        # IPv6 inside
        ("2001:db9::5", "2001:db8::/32", False),       # IPv6 outside
        ("203.0.113.7", "2001:db8::/32", False),       # v4 against a v6 rule
        ("2001:db8::5", "203.0.113.0/24", False),      # v6 against a v4 rule
    ],
)
def test_matching(ip, cidr, expected):
    assert netaccess.matches(ip, [_rule(cidr)]) is expected


def test_disabled_rule_does_not_grant_access():
    assert netaccess.matches("203.0.113.7", [_rule("203.0.113.0/24", enabled=False)]) is False


def test_corrupt_stored_rule_does_not_raise():
    """A bad value on disk must not 500 every request; it simply cannot grant access."""
    assert netaccess.matches("203.0.113.7", [_rule("total-nonsense")]) is False
    assert netaccess.matches("203.0.113.7", [_rule("garbage"), _rule("203.0.113.0/24")]) is True


def test_unresolvable_client_ip_is_not_allowed():
    assert netaccess.matches(None, [_rule("0.0.0.0/0")]) is False


def test_scope_description_is_checkable_at_a_glance():
    assert netaccess.describe_scope(netaccess.parse_cidr("203.0.113.7")) == "Single IP address"
    assert netaccess.describe_scope(netaccess.parse_cidr("203.0.113.0/24")) == "256 addresses"
    assert netaccess.describe_scope(netaccess.parse_cidr("2001:db8::/48")) == "IPv6 /48"


def test_host_bits_are_normalised_not_rejected():
    assert str(netaccess.parse_cidr("203.0.113.7/24")) == "203.0.113.0/24"


def test_malformed_cidr_raises_operator_facing_error():
    with pytest.raises(netaccess.NetAccessError):
        netaccess.parse_cidr("999.1.1.1")
    with pytest.raises(netaccess.NetAccessError):
        netaccess.parse_cidr("")


# =============================================================== modes & safety


def test_break_glass_env_forces_off(monkeypatch):
    monkeypatch.setenv(netaccess.BREAK_GLASS_ENV, "true")
    assert netaccess.effective_mode({"mode": "enforce", "rules": [], "confirm_by": None}) == "off"


def test_expired_confirm_window_degrades_to_monitor():
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    cfg = {"mode": "enforce", "rules": [], "confirm_by": past}
    assert netaccess.effective_mode(cfg) == "monitor"


def test_live_confirm_window_still_enforces():
    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    cfg = {"mode": "enforce", "rules": [], "confirm_by": future}
    assert netaccess.effective_mode(cfg) == "enforce"


def test_confirmed_enforcement_has_no_deadline():
    assert netaccess.effective_mode({"mode": "enforce", "rules": [], "confirm_by": None}) == "enforce"


def test_unknown_mode_degrades_to_off():
    assert netaccess.effective_mode({"mode": "bogus", "rules": [], "confirm_by": None}) == "off"


# =============================================================== compiled cache


def test_rules_are_compiled_once_not_per_request(monkeypatch):
    import ipaddress

    calls = {"n": 0}
    real = ipaddress.ip_network

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(netaccess.ipaddress, "ip_network", counting)
    rules = [_rule("203.0.113.0/24"), _rule("198.51.100.0/24")]
    for _ in range(25):
        netaccess.matches("203.0.113.7", rules)
    assert calls["n"] == 2, "CIDRs must be parsed once, not on every request"


def test_changing_the_rules_recompiles():
    assert netaccess.matches("203.0.113.7", [_rule("203.0.113.0/24")]) is True
    assert netaccess.matches("203.0.113.7", [_rule("198.51.100.0/24")]) is False


# =============================================================== middleware behaviour


def _set_config(tmp_path, monkeypatch, cfg):
    from app.core import jsonstore

    path = tmp_path / "network_access.json"
    monkeypatch.setattr(netaccess, "_PATH", path, raising=False)
    jsonstore.write_json(path, cfg)
    netaccess.reset_cache()


#: The address the harness presents as its socket peer.
CLIENT_PEER = "198.51.100.5"


async def _drive(path: str = "/api/auth/login", peer: str = CLIENT_PEER, headers=()):
    """Run one request through `_IpAllowlist` alone and report what happened.

    Deliberately NOT a TestClient: a SECOND TestClient in the same pytest process rebinds the
    app's in-process asyncio.Event to a different event loop and blows up the teardown of
    `test_route_authz_matrix` (which owns the one legitimate client). Driving the middleware
    directly is also a sharper test — it isolates the middleware from routing and auth.
    """
    from app.main import _IpAllowlist

    reached = {"inner": False}

    async def inner(scope, receive, send):
        reached["inner"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"inner"})

    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [(k.encode(), v.encode()) for k, v in headers],
        "client": (peer, 12345),
    }
    await _IpAllowlist(inner).__call__(scope, receive, send)
    status = next((m["status"] for m in messages if m["type"] == "http.response.start"), None)
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, body.decode(), reached["inner"]


def test_middleware_sits_inside_the_security_headers_middleware():
    """A 403 from the allowlist must still carry CSP/HSTS/nosniff.

    Starlette builds the stack so the LAST-registered middleware is OUTERMOST, so the allowlist
    must be registered BEFORE `_SecurityHeaders` to end up inside it. Asserted because a
    refactor that reorders these would weaken the feature with no other visible symptom.
    """
    from app.main import app

    names = [m.cls.__name__ for m in app.user_middleware]
    assert "_IpAllowlist" in names and "_SecurityHeaders" in names
    # Lower index == outermost.
    assert names.index("_SecurityHeaders") < names.index("_IpAllowlist"), (
        "_IpAllowlist must be INSIDE _SecurityHeaders so its 403 carries the security headers"
    )


def test_middleware_runs_before_authentication():
    """It must be middleware, not a route dependency — a blocked caller never reaches routing."""
    from app.main import app

    assert "_IpAllowlist" in [m.cls.__name__ for m in app.user_middleware]


def test_off_mode_allows_everything(tmp_path, monkeypatch):
    _set_config(tmp_path, monkeypatch, {"mode": "off", "rules": [], "confirm_by": None})
    status, _body, reached = asyncio.run(_drive())
    assert reached is True and status == 200


def test_enforce_blocks_the_login_endpoint(tmp_path, monkeypatch):
    """Blocking unknown sources from reaching sign-in is the POINT of the feature.

    A test asserting that /api/auth/login stays reachable would encode the exact
    misunderstanding this feature exists to correct.
    """
    _set_config(
        tmp_path, monkeypatch, {"mode": "enforce", "rules": [_rule("203.0.113.0/24")], "confirm_by": None}
    )
    status, body, reached = asyncio.run(_drive("/api/auth/login"))
    assert status == 403
    assert reached is False, "the request must not reach routing or authentication"
    assert body == "Forbidden"


def test_enforce_response_leaks_nothing(tmp_path, monkeypatch):
    _set_config(
        tmp_path, monkeypatch, {"mode": "enforce", "rules": [_rule("203.0.113.0/24")], "confirm_by": None}
    )
    _status, body, _reached = asyncio.run(_drive("/api/admin/firewall"))
    lowered = body.lower()
    for leak in ("allowlist", "firewall", "your address", "not on", CLIENT_PEER):
        assert leak not in lowered


def test_health_probes_are_never_blocked(tmp_path, monkeypatch):
    """Blocking the ACA probes would kill the revision — a worse outage than any attacker."""
    _set_config(
        tmp_path, monkeypatch, {"mode": "enforce", "rules": [_rule("203.0.113.0/24")], "confirm_by": None}
    )
    for path in ("/healthz", "/readyz", "/version"):
        status, _body, reached = asyncio.run(_drive(path))
        assert reached is True and status == 200, path


def test_monitor_mode_records_but_never_blocks(tmp_path, monkeypatch):
    from app.core import netaccess_events

    netaccess_events.reset()
    _set_config(
        tmp_path, monkeypatch, {"mode": "monitor", "rules": [_rule("203.0.113.0/24")], "confirm_by": None}
    )
    status, _body, reached = asyncio.run(_drive("/api/auth/login"))
    assert reached is True and status == 200
    pending = netaccess_events.pending_snapshot()
    assert any(mode == "monitor" for _ip, mode in pending), "monitor hits must be recorded"
    assert not any(mode == "enforce" for _ip, mode in pending), (
        "a would-be block must never be recorded as an actual block"
    )
    netaccess_events.reset()


def test_allowed_source_passes_through(tmp_path, monkeypatch):
    _set_config(
        tmp_path,
        monkeypatch,
        {"mode": "enforce", "rules": [_rule(f"{CLIENT_PEER}/32")], "confirm_by": None},
    )
    _status, _body, reached = asyncio.run(_drive("/api/admin/firewall"))
    assert reached is True


def test_break_glass_env_unblocks_the_app(tmp_path, monkeypatch):
    _set_config(
        tmp_path, monkeypatch, {"mode": "enforce", "rules": [_rule("203.0.113.0/24")], "confirm_by": None}
    )
    assert asyncio.run(_drive())[0] == 403
    monkeypatch.setenv(netaccess.BREAK_GLASS_ENV, "true")
    assert asyncio.run(_drive())[2] is True


def test_forged_header_cannot_reach_a_protected_app(tmp_path, monkeypatch):
    """End-to-end version of the bypass test, through the actual middleware.

    An attacker on an unlisted address sets X-Forwarded-For to an allowlisted one. Under the
    old leftmost parse this granted access; it must now be refused.
    """
    _with_managed_ingress(monkeypatch)
    _set_config(
        tmp_path, monkeypatch, {"mode": "enforce", "rules": [_rule("203.0.113.0/24")], "confirm_by": None}
    )
    status, _body, reached = asyncio.run(
        _drive(
            "/api/auth/login",
            peer="10.0.0.8",
            headers=[("x-forwarded-for", "203.0.113.10, 45.155.205.11")],
        )
    )
    assert status == 403 and reached is False


# =============================================================== event aggregation


def test_blocks_are_aggregated_not_one_row_per_request():
    from app.core import netaccess_events

    netaccess_events.reset()
    for _ in range(500):
        netaccess_events.record("45.155.205.11", "enforce", "/api/auth/login")
    pending = netaccess_events.pending_snapshot()
    assert len(pending) == 1
    assert pending[("45.155.205.11", "enforce")].hits == 500
    netaccess_events.reset()


def test_tracked_ip_map_is_capped():
    """A distributed scan must not be able to grow process memory without bound."""
    from app.core import netaccess_events

    netaccess_events.reset()
    for i in range(netaccess_events.MAX_TRACKED_IPS + 500):
        netaccess_events.record(f"198.51.{i // 256}.{i % 256}", "enforce", "/")
    assert len(netaccess_events.pending_snapshot()) <= netaccess_events.MAX_TRACKED_IPS
    netaccess_events.reset()


# =============================================================== deploy-time seed
#
# This is the path a CUSTOMER's first boot takes: it decides whether a freshly deployed app
# comes up protected or wide open. It has no UI and no operator watching it, so it needs to be
# provably correct here.


def _isolate_config(tmp_path, monkeypatch):
    path = tmp_path / "network_access.json"
    monkeypatch.setattr(netaccess, "_PATH", path, raising=False)
    netaccess.reset_cache()
    return path


def test_no_seed_env_leaves_the_app_unrestricted(tmp_path, monkeypatch):
    _isolate_config(tmp_path, monkeypatch)
    monkeypatch.delenv(netaccess.SEED_ENV, raising=False)
    cfg = netaccess.load_config()
    assert cfg["mode"] == "off" and cfg["rules"] == []


def test_seed_creates_an_enforcing_config_on_first_read(tmp_path, monkeypatch):
    path = _isolate_config(tmp_path, monkeypatch)
    monkeypatch.setenv(netaccess.SEED_ENV, "203.0.113.0/24, 198.51.100.7")
    monkeypatch.delenv(netaccess.SEED_MODE_ENV, raising=False)
    cfg = netaccess.load_config()
    assert cfg["mode"] == "enforce"
    assert [r["cidr"] for r in cfg["rules"]] == ["203.0.113.0/24", "198.51.100.7/32"]
    assert path.exists(), "the seed must be persisted, not recomputed on every read"
    # A deployment-time policy has no operator session to confirm it from, so it must NOT be
    # provisional — auto-reverting it 15 minutes after boot would undo what the deployer asked.
    assert cfg["confirm_by"] is None


def test_seed_mode_can_be_monitor(tmp_path, monkeypatch):
    _isolate_config(tmp_path, monkeypatch)
    monkeypatch.setenv(netaccess.SEED_ENV, "203.0.113.0/24")
    monkeypatch.setenv(netaccess.SEED_MODE_ENV, "monitor")
    assert netaccess.load_config()["mode"] == "monitor"


def test_seed_never_applies_over_an_existing_config(tmp_path, monkeypatch):
    """The admin's saved policy must win; a lingering env var must not silently re-impose itself."""
    path = _isolate_config(tmp_path, monkeypatch)
    from app.core import jsonstore

    jsonstore.write_json(path, {"mode": "off", "rules": [], "confirm_by": None})
    monkeypatch.setenv(netaccess.SEED_ENV, "203.0.113.0/24")
    cfg = netaccess.load_config()
    assert cfg["mode"] == "off" and cfg["rules"] == []


def test_malformed_seed_does_not_crash_or_lock_out(tmp_path, monkeypatch):
    """A typo in a deployment parameter must not brick the app on boot."""
    _isolate_config(tmp_path, monkeypatch)
    monkeypatch.setenv(netaccess.SEED_ENV, "not-an-ip, 999.999.999.999")
    cfg = netaccess.load_config()
    assert cfg["mode"] == "off" and cfg["rules"] == []


def test_seed_rejects_an_allow_everything_range(tmp_path, monkeypatch):
    _isolate_config(tmp_path, monkeypatch)
    monkeypatch.setenv(netaccess.SEED_ENV, "0.0.0.0/0")
    cfg = netaccess.load_config()
    assert cfg["mode"] == "off" and cfg["rules"] == []


def test_partially_valid_seed_keeps_the_good_entries(tmp_path, monkeypatch):
    _isolate_config(tmp_path, monkeypatch)
    monkeypatch.setenv(netaccess.SEED_ENV, "garbage, 203.0.113.0/24")
    cfg = netaccess.load_config()
    assert [r["cidr"] for r in cfg["rules"]] == ["203.0.113.0/24"]


# =============================================================== commit-confirm persistence


def test_revert_if_expired_persists_the_downgrade(tmp_path, monkeypatch):
    path = _isolate_config(tmp_path, monkeypatch)
    from app.core import jsonstore

    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    jsonstore.write_json(path, {"mode": "enforce", "rules": [_rule("203.0.113.0/24")], "confirm_by": past})
    reverted = netaccess.revert_if_expired()
    assert reverted is not None
    assert reverted["mode"] == "monitor" and reverted["confirm_by"] is None
    # Durable, not just in memory — a restart must not resurrect the unconfirmed enforcement.
    assert netaccess.load_config()["mode"] == "monitor"
    # The rules survive the downgrade; only the mode changes.
    assert [r["cidr"] for r in netaccess.load_config()["rules"]] == ["203.0.113.0/24"]


def test_revert_is_a_no_op_while_the_window_is_open(tmp_path, monkeypatch):
    path = _isolate_config(tmp_path, monkeypatch)
    from app.core import jsonstore

    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    jsonstore.write_json(path, {"mode": "enforce", "rules": [], "confirm_by": future})
    assert netaccess.revert_if_expired() is None
    assert netaccess.load_config()["mode"] == "enforce"


def test_revert_ignores_confirmed_enforcement(tmp_path, monkeypatch):
    path = _isolate_config(tmp_path, monkeypatch)
    from app.core import jsonstore

    jsonstore.write_json(path, {"mode": "enforce", "rules": [], "confirm_by": None})
    assert netaccess.revert_if_expired() is None
    assert netaccess.load_config()["mode"] == "enforce"


def test_scheduler_wires_the_revert_and_audits_it(tmp_path, monkeypatch):
    """The scheduler hook must actually call the revert and record it.

    Covers the wiring, not just the logic: without this, `revert_if_expired` could be correct
    and still never run in production.
    """
    path = _isolate_config(tmp_path, monkeypatch)
    from app.core import jsonstore

    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    jsonstore.write_json(path, {"mode": "enforce", "rules": [], "confirm_by": past})

    from app.automations import scheduler as sched_mod

    added: list = []

    class _FakeDb:
        def add(self, obj):
            added.append(obj)

        async def commit(self):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(sched_mod, "SessionLocal", lambda: _FakeDb())
    asyncio.run(sched_mod.Scheduler()._network_access_maintenance())

    assert netaccess.load_config()["mode"] == "monitor"
    actions = [getattr(a, "action", None) for a in added]
    assert "firewall.auto_reverted" in actions, f"expected an audit row, got {actions}"


# =============================================================== backup coverage


def test_network_access_is_included_in_backups():
    """A restore that silently drops the allowlist would leave the deployment open."""
    from app.backup.registry import FILE_SECTIONS

    ids = {s.id: s for s in FILE_SECTIONS}
    assert "network_access" in ids, "network_access.json must be a backup section"
    assert ids["network_access"].filename == "network_access.json"
    assert ids["network_access"].secret_kind == "", "a CIDR is not a credential"


def test_restoring_an_enforcing_backup_cannot_lock_the_operator_out():
    """A backup from another deployment carries ITS allowlist, not this operator's address."""
    from app.backup.registry import _defuse_network_access, FILE_SECTIONS

    spec = next(s for s in FILE_SECTIONS if s.id == "network_access")
    payload = {
        "mode": "enforce",
        "rules": [{"cidr": "203.0.113.0/24", "label": "their office", "enabled": True}],
        "confirm_by": None,
    }
    out = _defuse_network_access(spec, payload)
    assert out["mode"] == "monitor", "an imported enforce policy must be downgraded"
    # The policy itself is preserved so it can be reviewed and re-enforced deliberately.
    assert out["rules"] == payload["rules"]


def test_defusing_leaves_other_sections_and_modes_untouched():
    from app.backup.registry import _defuse_network_access, FILE_SECTIONS

    spec = next(s for s in FILE_SECTIONS if s.id == "network_access")
    monitor = {"mode": "monitor", "rules": [], "confirm_by": None}
    assert _defuse_network_access(spec, monitor) is monitor
    other = next(s for s in FILE_SECTIONS if s.id == "app_settings")
    blob = {"mode": "enforce"}
    assert _defuse_network_access(other, blob) is blob


# =============================================================== retention


async def test_purge_drops_records_past_the_retention_window():
    """The in-memory buffer is capped, but the TABLE is not — a sustained scan would grow it
    without bound if nothing pruned it."""
    from sqlalchemy import delete, select

    from app.core import netaccess_events
    from app.core.db import SessionLocal
    from app.models import IpBlockEvent

    async with SessionLocal() as db:
        await db.execute(delete(IpBlockEvent))
        await db.commit()
        now = datetime.now(UTC)
        db.add(
            IpBlockEvent(
                ip="203.0.113.1", mode="enforce", hits=1, last_path="/",
                first_seen=now, last_seen=now,
            )
        )
        db.add(
            IpBlockEvent(
                ip="203.0.113.2", mode="enforce", hits=1, last_path="/",
                first_seen=now - timedelta(days=netaccess_events.RETENTION_DAYS + 5),
                last_seen=now - timedelta(days=netaccess_events.RETENTION_DAYS + 5),
            )
        )
        await db.commit()

        removed = await netaccess_events.purge(db)
        assert removed >= 1

        surviving = {
            r.ip for r in (await db.execute(select(IpBlockEvent))).scalars().all()
        }
        assert "203.0.113.1" in surviving, "recent activity must be kept"
        assert "203.0.113.2" not in surviving, "stale activity must be pruned"

        await db.execute(delete(IpBlockEvent))
        await db.commit()


# =============================================================== permissions


def test_firewall_permissions_are_registered_and_scoped():
    from app.auth.permissions import (
        PERMISSIONS,
        SYSTEM_ROLES,
    )

    assert "firewall.read" in PERMISSIONS
    assert "firewall.manage" in PERMISSIONS

    roles = {name: perms for name, _desc, perms in SYSTEM_ROLES}
    assert "firewall.manage" in roles["admin"]
    # An auditor must be able to EVIDENCE the network policy without being able to change it.
    assert "firewall.read" in roles["auditor"]
    assert "firewall.manage" not in roles["auditor"]
    # Changing who can reach the application is not an operator capability.
    assert "firewall.manage" not in roles["operator"]
    assert "firewall.manage" not in roles["user"]
