"""Handler-level regression tests for the IAM endpoints.

`test_iam.py` exercises `compose.compute_overview` directly and `test_route_authz_matrix.py`
sweeps every route UNAUTHENTICATED. Both passed while `GET /api/iam/overview` returned 500 to
every real caller, because the fault was in the handler and only reachable AFTER the auth
dependency ran: the endpoint passed `workload_id` to `_target()` without declaring the
parameter, so it raised `NameError` on each request. The UI rendered that as a bare
"No data." on an access-review screen -- indistinguishable from "nobody has access".

The handlers are awaited DIRECTLY rather than driven through a `TestClient`: a second
TestClient in the same pytest process rebinds the app's in-process `asyncio.Event` to another
event loop and breaks `test_route_authz_matrix`'s teardown. A direct call still executes the
handler body, which is where this class of fault lives.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from app.api import iam as iam_api
from app.core.security import Principal
from app.iam import cache


@pytest.fixture()
def isolated_cache(tmp_path, monkeypatch):
    """Point the cache index + blob dir at a tmp location, as test_iam.py does."""
    monkeypatch.setattr(cache, "_DATA", tmp_path)
    monkeypatch.setattr(cache, "_INDEX", tmp_path / "iam_cache.json")
    monkeypatch.setattr(cache, "_BLOBS", tmp_path / "iam")
    monkeypatch.setattr(cache, "_migrated", True)
    return tmp_path


def _principal() -> Principal:
    return Principal(
        subject="iam-test@example",
        email="iam-test@example.com",
        tenant_id="t1",
        role="admin",
        permissions=frozenset(["iam.read"]),
        display_name="IAM Test",
        auth_source="test",
    )


async def test_overview_handler_answers_an_authenticated_caller(isolated_cache):
    """The exact call the IAM screen makes on every visit."""
    body = await iam_api.overview(connection_id=None, principal=_principal())

    # An empty cache is a legitimate answer, but it must be a STATED one the UI can render
    # as "no scan yet" rather than an error it has to guess at.
    assert body["never_loaded"] is True
    assert "scopes" in body and "ttl_s" in body


async def test_overview_handler_accepts_an_explicit_connection(isolated_cache):
    """The screen always sends a connection id once a tenant is picked."""
    body = await iam_api.overview(connection_id="no-such-connection", principal=_principal())

    # Deliberately not asserting on `connection_configured`: an unresolvable id falls back to
    # the default connection, so its value depends on ambient config rather than this call.
    assert body["never_loaded"] is True
    assert "scopes" in body and "ttl_s" in body


# --------------------------------------------------------------------------- class guard
_IAM_API = pathlib.Path(iam_api.__file__)
_TARGET_WITH_WORKLOAD = "_target(principal, connection_id, workload_id)"


def _endpoints_using_target() -> list[tuple[str, bool, bool]]:
    """(name, declares workload_id, passes workload_id to _target) for each handler."""
    source = _IAM_API.read_text(encoding="utf-8")
    out: list[tuple[str, bool, bool]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        segment = ast.get_source_segment(source, node) or ""
        if "_target(principal, connection_id" not in segment:
            continue
        params = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
        out.append((node.name, "workload_id" in params, _TARGET_WITH_WORKLOAD in segment))
    return out


def test_the_endpoint_scan_actually_finds_endpoints():
    """Guards the guard: an empty scan would make both checks below vacuously pass."""
    found = _endpoints_using_target()
    assert len(found) >= 5, f"expected the IAM handlers that resolve a target, found {found}"


def test_no_endpoint_passes_a_workload_id_it_does_not_declare():
    """The `overview` regression: `NameError` at runtime, on every call, after auth."""
    broken = [name for name, declares, passes in _endpoints_using_target() if passes and not declares]
    assert not broken, (
        f"{broken} pass workload_id to _target() without declaring it as a parameter. "
        f"That raises NameError on every request once the caller is authenticated."
    )


def test_every_workload_scoped_endpoint_resolves_its_tenant_from_the_workload():
    """The `export_workbook` regression: it filtered rows by workload but resolved the tenant
    without it, so a workload owned by another connection could compose from the wrong tenant --
    the exact failure the workload-ownership work exists to prevent."""
    missing = [name for name, declares, passes in _endpoints_using_target() if declares and not passes]
    assert not missing, (
        f"{missing} accept workload_id but resolve the tenant with _target(principal, connection_id). "
        f"Pass workload_id so the workload's own connection wins over a stale picker."
    )
