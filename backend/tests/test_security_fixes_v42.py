"""Regression tests for the v42 security remediation set."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
from starlette.requests import Request


def _run(coro):
    return asyncio.run(coro)


def _request(path: str, method: str = "POST") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "scheme": "https",
            "server": ("test", 443),
            "client": ("127.0.0.1", 1234),
        }
    )


def test_managed_ingress_client_ip_uses_valid_forwarded_address():
    from app.api.auth import _client_ip
    from app.core.config import get_settings

    settings = get_settings()
    saved = settings.trust_forwarded_headers
    settings.trust_forwarded_headers = True
    try:
        req = SimpleNamespace(
            client=SimpleNamespace(host="10.0.0.8"),
            headers={"x-forwarded-for": "198.51.100.42, 10.0.0.8"},
        )
        assert _client_ip(req) == "198.51.100.42"
        # A TRUSTED header that yields no attributable address no longer falls back to the
        # socket peer: behind a trusted proxy that peer IS the proxy, and returning it would
        # let infrastructure be treated as the client (and satisfy an IP allowlist). See
        # app/core/clientip.py and tests/test_netaccess.py.
        req.headers["x-forwarded-for"] = "not-an-ip"
        assert _client_ip(req) is None
    finally:
        settings.trust_forwarded_headers = saved


def test_expensive_route_classification_covers_costly_operations():
    from app.core.cost_controls import is_expensive_request

    for path in (
        "/api/chats/c1/messages/stream",
        "/api/missions/run",
        "/api/assessments/enqueue",
        "/api/changeexplorer/analyze/stream",
        "/api/performance/refresh/stream",
        "/api/workloads/autopilot/trace",
    ):
        assert is_expensive_request("POST", path), path
    assert is_expensive_request("GET", "/api/rbac/refresh/stream")
    assert not is_expensive_request("GET", "/api/chats/c1/stream")
    assert not is_expensive_request("POST", "/api/auth/logout")


def test_cost_controls_enforce_shared_request_and_token_limits(tmp_path, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.models  # noqa: F401
    from app.core.cost_controls import enforce_cost_controls
    from app.core.db import Base
    from app.models import AuditLog, Usage

    cfg = {
        "expensive_requests_per_user_hour": 1,
        "expensive_requests_per_tenant_hour": 10,
        "monthly_tokens_per_user": 100,
        "monthly_tokens_per_tenant": 1000,
    }
    monkeypatch.setattr("app.core.cost_controls.load_settings", lambda: cfg)

    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'limits.db'}")
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as db:
            await enforce_cost_controls(
                _request("/api/missions/run"), db, tenant_id="t1", user_id="u1"
            )
            with pytest.raises(HTTPException) as exc:
                await enforce_cost_controls(
                    _request("/api/missions/run"), db, tenant_id="t1", user_id="u1"
                )
            assert exc.value.status_code == 429
            assert await db.scalar(
                __import__("sqlalchemy").select(__import__("sqlalchemy").func.count(AuditLog.id))
            ) == 1

            db.add(
                Usage(
                    tenant_id="t1", user_id="u2", chat_id="c1", model="m",
                    prompt_tokens=80, completion_tokens=20, created_at=datetime.now(timezone.utc),
                )
            )
            await db.commit()
            with pytest.raises(HTTPException) as token_exc:
                await enforce_cost_controls(
                    _request("/api/missions/run"), db, tenant_id="t1", user_id="u2"
                )
            assert token_exc.value.status_code == 429
            assert "token" in str(token_exc.value.detail).lower()
        await engine.dispose()

    _run(run())


class _BoundedStream:
    def __init__(self, size: int):
        self.remaining = size
        self.max_requested = 0

    def read(self, size: int = -1) -> bytes:
        self.max_requested = max(self.max_requested, size)
        if self.remaining <= 0:
            return b""
        take = self.remaining if size < 0 else min(size, self.remaining)
        self.remaining -= take
        return b"x" * take


def test_upload_reader_stops_after_limit_plus_one():
    from app.core.uploads import read_upload_limited

    async def run():
        stream = _BoundedStream(10_000)
        upload = UploadFile(stream, filename="large.bin", headers=Headers())
        with pytest.raises(HTTPException) as exc:
            await read_upload_limited(upload, 1024)
        assert exc.value.status_code == 413
        assert stream.remaining == 10_000 - 1025
        assert stream.max_requested <= 64 * 1024

    _run(run())


def test_csrf_rejects_originless_cookie_write_but_allows_api_client():
    from app.main import _CsrfGuard

    app = FastAPI()
    app.add_middleware(_CsrfGuard)

    @app.post("/api/write")
    async def write():
        return {"ok": True}

    with TestClient(app) as client:
        client.cookies.set("azsupagent_session", "ambient")
        blocked = client.post("/api/write")
        assert blocked.status_code == 403
        allowed = client.post("/api/write", headers={"Sec-Fetch-Site": "same-origin"})
        assert allowed.status_code == 200
        client.cookies.clear()
        assert client.post("/api/write").status_code == 200


def test_unknown_api_paths_are_not_spa_fallbacks():
    from app.main import _is_api_fallback_path

    assert _is_api_fallback_path("api/does-not-exist")
    assert _is_api_fallback_path("/api/does-not-exist")
    assert not _is_api_fallback_path("alerts-manager/overlaps")
    assert not _is_api_fallback_path("apiary")


def test_pypdf_is_patched():
    from importlib.metadata import version

    assert tuple(map(int, version("pypdf").split(".")[:3])) >= (6, 14, 2)