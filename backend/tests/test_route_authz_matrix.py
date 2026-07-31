"""Every API route must require authentication.

WHY THIS EXISTS
---------------
Authorisation in this codebase is a per-route dependency:

    _read = require_permission("feature.read")

    @router.get("/data")
    async def get_data(principal: Principal = Depends(_read)): ...

There is **no default-deny**. A route that simply forgets `Depends(_read)` is
completely unauthenticated, looks entirely normal in review, and is invisible to
every other test in the suite. With 43 routers and 100+ endpoints, "we always
remember" is not a control.

This test walks the *live* route table and asserts that an unauthenticated caller
cannot get a success response out of anything except a documented, deliberately
public endpoint. New routes are covered automatically the moment they are added.

If this test fails for a route you just wrote, the fix is almost always to add the
permission dependency -- not to add the route to the allowlist below.
"""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.main import app

# --------------------------------------------------------------------------- allowlist

#: Routes that are deliberately reachable without a session, with the reason.
#: Adding to this set is a SECURITY DECISION -- it must come with a justification.
PUBLIC_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/auth/config"): "login page must know which IdPs are enabled",
    ("POST", "/api/auth/login"): "the login endpoint itself",
    ("GET", "/api/auth/oidc/{idp_id}/login"): "starts the OIDC redirect",
    ("GET", "/api/auth/oidc/{idp_id}/callback"): "OIDC returns here; state-bound",
    ("GET", "/api/auth/saml/{idp_id}/login"): "starts the SAML redirect",
    ("GET", "/api/auth/saml/{idp_id}/metadata"): "SP metadata is public by spec",
    ("POST", "/api/auth/saml/{idp_id}/acs"): "IdP posts the assertion here",
    # Deliberately public: logout must work for a caller whose session has ALREADY
    # expired or been revoked -- otherwise they cannot clear their own cookie. The
    # handler is an idempotent no-op when there is no session, so the only abuse is a
    # CSRF-able forced logout, which is a nuisance rather than a disclosure.
    ("POST", "/api/auth/logout"): "must work with an already-expired session",
}

#: Non-/api paths that serve the SPA or liveness probes.
_NON_API_PREFIXES = ("/healthz", "/readyz", "/version", "/docs", "/redoc", "/openapi.json")

#: Placeholder used for any path parameter. The value never matters: a guarded route
#: rejects on the dependency long before the handler looks the id up.
_DUMMY = "00000000-0000-0000-0000-000000000000"


def _concrete(path: str) -> str:
    """Replace every {param} with a harmless placeholder."""
    out: list[str] = []
    for segment in path.split("/"):
        out.append(_DUMMY if segment.startswith("{") and segment.endswith("}") else segment)
    return "/".join(out)


def _api_routes() -> list[tuple[str, str]]:
    """Every (method, path) under /api, excluding HEAD/OPTIONS."""
    found: list[tuple[str, str]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not route.path.startswith("/api"):
            continue
        for method in sorted(route.methods or set()):
            if method in {"HEAD", "OPTIONS"}:
                continue
            found.append((method, route.path))
    return sorted(set(found))


# --------------------------------------------------------------------------- the tests


@pytest.fixture(scope="module")
def client():
    # raise_server_exceptions=False so a handler blowing up surfaces as a 500 we can
    # assert on, rather than aborting the sweep.
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_the_route_table_is_not_empty():
    """Guards the guard: if route collection silently returned nothing, the sweep below
    would vacuously pass and we would ship an unprotected API believing it was tested."""
    routes = _api_routes()
    assert len(routes) > 100, f"expected the full API surface, only found {len(routes)}"


@pytest.mark.parametrize(("method", "path"), _api_routes(), ids=lambda v: str(v))
def test_route_rejects_unauthenticated_callers(client, method: str, path: str):
    """No /api route may return a success status to a caller with no session."""
    if (method, path) in PUBLIC_ROUTES:
        pytest.skip(f"deliberately public: {PUBLIC_ROUTES[(method, path)]}")

    response = client.request(method, _concrete(path))

    assert not (200 <= response.status_code < 300), (
        f"{method} {path} returned {response.status_code} to an UNAUTHENTICATED caller. "
        f"It is missing its require_permission(...) dependency, or it belongs in "
        f"PUBLIC_ROUTES with a written justification."
    )


@pytest.mark.parametrize(("method", "path"), _api_routes(), ids=lambda v: str(v))
def test_route_rejects_with_an_auth_status_not_a_crash(client, method: str, path: str):
    """Rejection should be 401/403 -- an auth decision -- rather than a 500.

    A 500 on an unauthenticated request means code ran before the auth check, which is
    both an information leak (stack traces, timing) and a DoS primitive.
    """
    if (method, path) in PUBLIC_ROUTES:
        pytest.skip("deliberately public")

    response = client.request(method, _concrete(path))

    assert response.status_code != 500, (
        f"{method} {path} raised a 500 for an unauthenticated caller -- handler code is "
        f"executing before the permission dependency rejects."
    )
    # 401 unauthenticated / 403 no permission are the intended answers.
    # 404 and 405 are acceptable (route shape), 422 means validation ran first.
    assert response.status_code in {401, 403, 404, 405, 422}, (
        f"{method} {path} rejected with an unexpected {response.status_code}"
    )


def test_public_allowlist_only_contains_real_routes():
    """A stale allowlist entry silently widens the exemption if a path is ever renamed
    back into existence. Keep it honest."""
    live = set(_api_routes())
    stale = [entry for entry in PUBLIC_ROUTES if entry not in live]
    assert not stale, f"PUBLIC_ROUTES lists routes that no longer exist: {stale}"


def test_public_allowlist_stays_small():
    """Every entry is unauthenticated attack surface. Growth should be deliberate."""
    assert len(PUBLIC_ROUTES) <= 10, (
        "the unauthenticated surface grew -- each addition needs a security review"
    )
