"""Backend half of the capability-aware navigation contract.

The SPA now uses explicit read capabilities to decide which links and direct routes are
available. These assertions pin the matching FastAPI dependencies so a future endpoint
refactor cannot turn a visible link into a 403 (or expose a write endpoint to a read role).
"""
from __future__ import annotations

import inspect

import pytest
from fastapi.routing import APIRoute

from app.main import app


def _route(method: str, path: str) -> APIRoute:
    matches = [
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path == path
        and method.upper() in (route.methods or set())
    ]
    assert len(matches) == 1, f"expected one {method} {path} route, found {len(matches)}"
    return matches[0]


def _required_permissions(method: str, path: str) -> set[str]:
    found: set[str] = set()

    def walk(dependant) -> None:
        call = getattr(dependant, "call", None)
        if callable(call):
            try:
                closure = inspect.getclosurevars(call).nonlocals
            except TypeError:
                closure = {}
            permission = closure.get("permission")
            if isinstance(permission, str):
                found.add(permission)
        for child in getattr(dependant, "dependencies", ()):
            walk(child)

    walk(_route(method, path).dependant)
    return found


@pytest.mark.parametrize(
    ("method", "path", "permission"),
    [
        ("GET", "/api/chats", "chat.use"),
        ("GET", "/api/notifications", "notifications.read"),
        ("POST", "/api/notifications/read-all", "notifications.read"),
        ("GET", "/api/admin/settings", "settings.read"),
        ("GET", "/api/admin/ai-prompts", "settings.read"),
        ("GET", "/api/admin/llm/config", "settings.read"),
        ("GET", "/api/admin/mcp/tools", "settings.read"),
        ("PUT", "/api/admin/settings", "settings.write"),
        ("GET", "/api/admin/usage", "monitor.view"),
        ("GET", "/api/admin/tool-calls", "monitor.view"),
        ("GET", "/api/admin/monitor", "monitor.view"),
        ("GET", "/api/admin/monitor/dashboards", "monitor.view"),
        ("POST", "/api/admin/monitor/widgets/run", "monitor.view"),
        ("PUT", "/api/admin/monitor/dashboards", "settings.write"),
        ("GET", "/api/admin/audit", "audit.read"),
        ("GET", "/api/admin/siem-export", "audit.read"),
        ("POST", "/api/admin/siem-export", "settings.write"),
        ("GET", "/api/radar/reference", "radar.read"),
        ("PUT", "/api/radar/reference", "radar.manage"),
        ("POST", "/api/radar/refresh", "radar.manage"),
        ("POST", "/api/radar/state", "radar.manage"),
    ],
)
def test_visible_route_uses_its_catalog_permission(method: str, path: str, permission: str):
    assert permission in _required_permissions(method, path)


def test_read_routes_do_not_inherit_their_old_settings_write_guard():
    for method, path in (
        ("GET", "/api/admin/settings"),
        ("GET", "/api/admin/usage"),
        ("GET", "/api/admin/audit"),
        ("GET", "/api/admin/monitor"),
        ("GET", "/api/admin/monitor/dashboards"),
    ):
        assert "settings.write" not in _required_permissions(method, path), (method, path)
