"""BOLA / IDOR matrix: object-level authorisation across the two scoping dimensions.

Plan reference: docs/improvement-plans/security-hardening/07-authn-authz-pentest.md#73

This codebase scopes data two independent ways, and BOTH have produced real bugs:

* ``tenant_id``     -- guarded by helpers like ``_tenant_arch_or_404``.
                       Covered end-to-end by tests/test_security_e2e.py.
* ``connection_id`` -- guarded by ``get_connection`` (exact) versus
                       ``resolve_connection`` (**silently falls back to the default
                       connection**).

THE FALLBACK IS THE DANGEROUS ONE. It has previously produced a cross-tenant defect:
an endpoint passed a request-supplied id to ``resolve_connection``, the id did not
resolve, and the caller was answered from the DEFAULT connection -- another tenant's
data, returned with ``ok: true``. The fix was to use exact ``get_connection`` and 404.

These tests pin the semantics so the behaviour cannot drift silently, and act as a
tripwire when new call sites appear.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.azure_connections import (
    get_connection,
    get_default_connection,
    resolve_connection,
)

_API_DIR = Path(__file__).resolve().parents[1] / "app" / "api"


# ============================================================ resolve_connection semantics


def test_resolve_connection_falls_back_to_default_for_an_unknown_id():
    """PINNED BEHAVIOUR, not an endorsement.

    An id that does not exist yields the DEFAULT connection -- which belongs to a
    different tenant. Any endpoint that passes a *user-supplied* id here will answer
    with the wrong tenant's data while appearing to succeed.

    If this test ever fails because the fallback was removed, that is an IMPROVEMENT:
    delete the test and celebrate.
    """
    default = get_default_connection()
    if default is None:
        pytest.skip("no connections configured in this environment")

    resolved = resolve_connection("this-connection-id-does-not-exist")

    assert resolved is not None, "unknown id returned None -- fallback may have been removed"
    assert resolved["id"] == default["id"], (
        "unknown connection id silently resolved to the DEFAULT connection. Callers that "
        "accept this id from a request must use get_connection() and 404 instead."
    )


def test_get_connection_is_exact_and_does_not_fall_back():
    """The safe primitive. Endpoints taking a request-supplied id must use THIS."""
    assert get_connection("this-connection-id-does-not-exist") is None
    assert get_connection("") is None


def test_resolve_connection_with_a_real_id_returns_that_connection():
    """Sanity: the fallback must not fire for a valid id, or every scope would collapse
    onto the default connection and the test above would be vacuous."""
    default = get_default_connection()
    if default is None:
        pytest.skip("no connections configured in this environment")
    assert resolve_connection(default["id"])["id"] == default["id"]


# ============================================================ call-site tripwire


def _api_resolve_connection_sites() -> dict[str, int]:
    """Count resolve_connection call sites per file under app/api/.

    app/api/ is where request-supplied values enter the system, so a new call site here
    is exactly where the cross-tenant fallback bug class reappears.
    """
    counts: dict[str, int] = {}
    pattern = re.compile(r"\bresolve_connection\s*\(")
    for path in sorted(_API_DIR.glob("*.py")):
        hits = len(pattern.findall(path.read_text(encoding="utf-8")))
        if hits:
            counts[path.name] = hits
    return counts


#: Reviewed 2026-07-31. Each entry was checked against the cross-tenant fallback pattern:
#: does a REQUEST-supplied connection id reach resolve_connection, and is a silent
#: fallback to another tenant acceptable there?
#:
#: These are CALL counts (the regex excludes `from ... import resolve_connection` lines).
#: Raising a number here REQUIRES that review. Do not update it to make the test pass.
_REVIEWED_CALL_SITES = {
    "alert_analysis.py": 1,
    "architectures.py": 9,
    "assessments.py": 1,
    "backup_manager.py": 1,
    "changeexplorer.py": 2,
    "chats.py": 3,
    "connections.py": 1,
    "entra.py": 1,
    "graph.py": 1,
    "iam.py": 1,  # renamed from rbac.py; same reviewed call site, unchanged count
    "identity.py": 3,
    "inventory.py": 1,
    "ownership.py": 4,
    "policy.py": 1,
    "quota.py": 6,
    "reservations.py": 2,
    "tagintel.py": 3,
    "workloads.py": 16,
}


def test_no_unreviewed_resolve_connection_call_sites_appear():
    """Tripwire. A NEW resolve_connection call in app/api/ has the exact shape of the
    cross-tenant fallback bug, and it is invisible in review because the code looks
    completely ordinary."""
    current = _api_resolve_connection_sites()

    added = {f: n for f, n in current.items() if f not in _REVIEWED_CALL_SITES}
    grew = {
        f: (_REVIEWED_CALL_SITES[f], n)
        for f, n in current.items()
        if f in _REVIEWED_CALL_SITES and n > _REVIEWED_CALL_SITES[f]
    }

    assert not added, (
        f"NEW file(s) in app/api/ call resolve_connection: {added}. If the id comes from "
        f"the request, use get_connection() + 404 instead -- resolve_connection silently "
        f"falls back to the DEFAULT connection (another tenant). Then add it here."
    )
    assert not grew, (
        f"resolve_connection call count grew: {grew}. Review each new call for the "
        f"cross-tenant fallback bug before updating this baseline."
    )


def test_the_tripwire_baseline_is_not_stale():
    """If call sites were REMOVED, shrink the baseline -- otherwise the tripwire silently
    leaves headroom for a new one to be added unnoticed."""
    current = _api_resolve_connection_sites()
    shrunk = {
        f: (_REVIEWED_CALL_SITES[f], current.get(f, 0))
        for f in _REVIEWED_CALL_SITES
        if current.get(f, 0) < _REVIEWED_CALL_SITES[f]
    }
    assert not shrunk, (
        f"resolve_connection call sites were removed: {shrunk}. Lower the baseline so the "
        f"tripwire stays tight."
    )


def test_connections_api_uses_exact_lookup_for_request_supplied_ids():
    """Regression pin.

    A connections endpoint once passed a path parameter to resolve_connection and so
    answered from the default connection when the id did not resolve.
    """
    source = (_API_DIR / "connections.py").read_text(encoding="utf-8")
    assert "get_connection(" in source, (
        "connections.py must resolve request-supplied ids with the EXACT get_connection()"
    )
