"""End-to-end cross-tenant security tests.

These tests verify that the v39 tenant-guard helpers (`_tenant_arch_or_404`,
`_tenant_playbook_or_404`) and the v40 NotificationRule tenant filter are
actually wired into every relevant endpoint, by making real HTTP requests
through FastAPI with a swappable principal. The earlier unit tests in
`test_security_fixes_v39.py` only exercised the helpers — these tests close
the loop and would catch any future endpoint that forgets the guard.

Strategy
--------
We override the `get_principal` and `require_admin` FastAPI dependencies so the
tests can flip between Tenant A and Tenant B (and Admin/User) per request.
A single module-scoped `TestClient` is shared so the in-process asyncio.Event
machinery only binds to one loop (re-creating it per test triggers the
"bound to a different event loop" RuntimeError sibling tests have hit).
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import pytest


# --------------------------------------------------------------- shared fixtures


@pytest.fixture(scope="module")
def client():
    """Shared TestClient for the whole module. See module docstring for why."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _principal(tenant: str, role: str = "admin"):
    """Build a Principal whose tenant + role can be swapped via dependency overrides."""
    from app.core.security import Principal

    return Principal(
        subject=f"user@{tenant}",
        email=f"user@{tenant}.example",
        tenant_id=tenant,
        role=role,
        permissions=frozenset(["users.manage"] if role == "admin" else []),
        display_name=f"User-{tenant}",
        auth_source="test",
    )


@contextmanager
def _as(tenant: str, role: str = "admin") -> Iterator[None]:
    """Override `get_principal` + `require_admin` so the next request acts as the
    given (tenant, role)."""
    from app.core.security import get_principal, require_admin
    from app.main import app

    principal = _principal(tenant, role)
    app.dependency_overrides[get_principal] = lambda: principal
    app.dependency_overrides[require_admin] = lambda: principal
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_principal, None)
        app.dependency_overrides.pop(require_admin, None)


# =============================================================== C1 + C2 architecture IDOR


def test_architecture_endpoints_block_cross_tenant(client):
    """Tenant A creates an architecture; tenant B must get 404 on every endpoint
    that takes an architecture_id."""
    # Tenant A creates an architecture.
    with _as("alpha-tenant"):
        r = client.put("", json={"name": "ALPHA arch", "description": "", "nodes": [], "edges": []})
        # Endpoints are mounted under /api by main.py; redo with the full prefix.

    # Try again with the full prefix.
    with _as("alpha-tenant"):
        r = client.put(
            "/api/architectures",
            json={"name": "ALPHA arch", "description": "", "nodes": [], "edges": [], "groups": []},
        )
    assert r.status_code == 200, r.text
    arch_id = r.json()["architecture"]["id"]
    assert arch_id

    # ----- happy path: owner can read it
    with _as("alpha-tenant"):
        r = client.get(f"/api/architectures/{arch_id}")
    assert r.status_code == 200

    # ----- IDOR sweep: every architecture_id endpoint with tenant B must 404
    # (paths chosen by reading backend/app/api/architectures.py).
    cases_404 = [
        ("GET", f"/api/architectures/{arch_id}", None),
        ("DELETE", f"/api/architectures/{arch_id}", None),
        ("POST", f"/api/architectures/{arch_id}/state", {"state": "draft"}),
        ("POST", f"/api/architectures/{arch_id}/category", {"category_id": ""}),
        ("POST", f"/api/architectures/{arch_id}/workload", {"workload_id": ""}),
        ("POST", f"/api/architectures/{arch_id}/rebuild", {}),
        ("POST", f"/api/architectures/{arch_id}/clone", None),
        ("GET", f"/api/architectures/{arch_id}/revisions", None),
        ("GET", f"/api/architectures/{arch_id}/activity", None),
        ("GET", f"/api/architectures/{arch_id}/memory", None),
        ("PUT", f"/api/architectures/{arch_id}/memory", {"title": "", "sections": [], "enabled_for_investigations": True}),
        ("DELETE", f"/api/architectures/{arch_id}/memory", None),
        ("GET", f"/api/architectures/{arch_id}/memory/revisions", None),
        ("POST", f"/api/architectures/{arch_id}/enhance", {"goal": ""}),
        ("POST", f"/api/architectures/{arch_id}/ask", {"question": "what is this?"}),
        ("POST", f"/api/architectures/{arch_id}/drift", None),
        ("POST", f"/api/architectures/{arch_id}/restore", None),
        # purge needs the arch to be trashed first; the helper still enforces tenant.
        ("DELETE", f"/api/architectures/{arch_id}/purge", None),
    ]
    with _as("beta-tenant"):
        for method, path, body in cases_404:
            if body is None:
                r = client.request(method, path)
            else:
                r = client.request(method, path, json=body)
            assert r.status_code == 404, (
                f"cross-tenant {method} {path} returned {r.status_code} (expected 404). "
                f"Body: {r.text[:200]}"
            )

    # ----- owner can still trash + restore + purge (proves the guards don't break the happy path)
    with _as("alpha-tenant"):
        assert client.delete(f"/api/architectures/{arch_id}").status_code == 200
        # /restore moves it back out of trash.
        r = client.post(f"/api/architectures/{arch_id}/restore")
        assert r.status_code == 200, r.text
        # trash again then purge.
        assert client.delete(f"/api/architectures/{arch_id}").status_code == 200
        assert client.delete(f"/api/architectures/{arch_id}/purge").status_code == 200


# =============================================================== C3 playbook IDOR


def test_playbook_endpoints_block_cross_tenant(client):
    # Tenant A creates a playbook.
    with _as("alpha-tenant"):
        r = client.put(
            "/api/playbooks",
            json={
                "name": "Alpha pb",
                "description": "",
                "connection_id": "",
                "steps": [],
                "alert": {"enabled": False, "min_severity": "warning"},
                "enabled": True,
            },
        )
    assert r.status_code == 200, r.text
    pb_id = r.json()["playbook"]["id"]
    assert pb_id

    # ----- happy path: owner reads + exports
    with _as("alpha-tenant"):
        r = client.get(f"/api/playbooks/{pb_id}/export")
    assert r.status_code == 200

    # ----- IDOR sweep
    cases_404 = [
        ("DELETE", f"/api/playbooks/{pb_id}", None),
        ("GET", f"/api/playbooks/{pb_id}/export", None),
        ("POST", f"/api/playbooks/{pb_id}/run", None),
    ]
    with _as("beta-tenant"):
        for method, path, body in cases_404:
            r = client.request(method, path, json=body) if body else client.request(method, path)
            assert r.status_code == 404, f"cross-tenant {method} {path} returned {r.status_code}"

    # ----- PUT-update against another tenant's id must 404 (not silently overwrite).
    with _as("beta-tenant"):
        r = client.put(
            "/api/playbooks",
            json={
                "id": pb_id,  # tenant A's id
                "name": "HIJACKED",
                "description": "",
                "connection_id": "",
                "steps": [],
                "alert": {"enabled": False, "min_severity": "warning"},
                "enabled": True,
            },
        )
    assert r.status_code == 404, f"PUT-overwrite leak: {r.status_code} {r.text[:200]}"

    # ----- verify the original is intact (tenant_id + name unchanged)
    with _as("alpha-tenant"):
        r = client.get("/api/playbooks")
        rows = r.json()["playbooks"]
        match = next((p for p in rows if p["id"] == pb_id), None)
        assert match is not None, "tenant A's playbook disappeared!"
        assert match["name"] == "Alpha pb"
        assert match.get("tenant_id") == "alpha-tenant"


def test_playbook_import_pins_caller_tenant(client):
    """H4: a bundle with an arbitrary tenant_id must land in the importer's tenant."""
    # Tenant A creates a playbook + exports it.
    with _as("alpha-tenant"):
        r = client.put(
            "/api/playbooks",
            json={
                "name": "Export source",
                "description": "",
                "connection_id": "",
                "steps": [],
                "alert": {"enabled": False, "min_severity": "warning"},
                "enabled": True,
            },
        )
        src_id = r.json()["playbook"]["id"]
        bundle = client.get(f"/api/playbooks/{src_id}/export").json()

    # Try to smuggle a foreign tenant_id inside the bundle.
    if isinstance(bundle.get("playbook"), dict):
        bundle["playbook"]["tenant_id"] = "evil-tenant"

    # Beta imports the (modified) bundle.
    with _as("beta-tenant"):
        r = client.post("/api/playbooks/import", json={"bundle": bundle})
    assert r.status_code == 200, r.text
    imported = r.json().get("playbook") or {}
    assert imported.get("tenant_id") == "beta-tenant", (
        f"H4: imported playbook landed in {imported.get('tenant_id')!r}, not beta-tenant"
    )

    # ----- cleanup
    for tenant, pid in (("alpha-tenant", src_id), ("beta-tenant", imported.get("id"))):
        if pid:
            with _as(tenant):
                client.delete(f"/api/playbooks/{pid}")


# =============================================================== M6 notification rule scoping


def test_notification_rules_are_tenant_scoped(client):
    # Tenant A creates a rule.
    with _as("alpha-tenant"):
        r = client.put(
            "/api/notifications/rules",
            json={
                "name": "Alpha alert",
                "event_types": [],
                "sources": [],
                "min_severity": "warning",
                "in_app": True,
                "connector_ids": [],
            },
        )
    assert r.status_code == 200, r.text
    rule_id = r.json()["rule"]["id"]

    # Tenant B must NOT see it via list.
    with _as("beta-tenant"):
        r = client.get("/api/notifications/rules")
        beta_rules = r.json()["rules"]
        assert all(rl["id"] != rule_id for rl in beta_rules), "Tenant B saw Tenant A's rule!"

    # Tenant B cannot edit it (PUT with id).
    with _as("beta-tenant"):
        r = client.put(
            "/api/notifications/rules",
            json={
                "id": rule_id,
                "name": "HIJACKED",
                "event_types": [],
                "sources": [],
                "min_severity": "warning",
                "in_app": True,
                "connector_ids": [],
            },
        )
    assert r.status_code == 404

    # Tenant B cannot delete it.
    with _as("beta-tenant"):
        r = client.delete(f"/api/notifications/rules/{rule_id}")
    assert r.status_code == 404

    # Tenant A can still see + delete.
    with _as("alpha-tenant"):
        ids = [rl["id"] for rl in client.get("/api/notifications/rules").json()["rules"]]
        assert rule_id in ids
        assert client.delete(f"/api/notifications/rules/{rule_id}").status_code == 200


# =============================================================== C4 security headers (live)


def test_security_headers_on_every_response(client):
    """C4: defense-in-depth headers ride along on every response — both /api and /healthz."""
    for path in ("/healthz", "/readyz"):
        r = client.get(path)
        assert r.status_code == 200
        # Always-on headers (HTTP and HTTPS).
        assert r.headers.get("x-frame-options") == "DENY", path
        assert r.headers.get("x-content-type-options") == "nosniff", path
        assert r.headers.get("referrer-policy") == "strict-origin-when-cross-origin", path
        csp = r.headers.get("content-security-policy", "")
        assert "frame-ancestors 'none'" in csp, path
        assert "default-src 'self'" in csp, path


# =============================================================== L1 docs disabled


def test_local_environment_keeps_docs_for_dev(client):
    """In local dev environment (default in tests) /docs is allowed."""
    # The app is constructed in local mode, so /docs is enabled. The 404 case is
    # covered by `test_docs_disabled_by_constructor_when_not_local` in v40.
    r = client.get("/docs")
    assert r.status_code in (200, 307)  # 307 if FastAPI redirects to a trailing slash
