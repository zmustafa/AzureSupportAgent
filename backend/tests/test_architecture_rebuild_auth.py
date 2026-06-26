"""The architecture generate/rebuild job must fail fast — with one clear, actionable message —
when the connection's Azure token is expired/invalid, instead of burning the Resource Graph +
AI phases and surfacing a vaguer lower-level error. This mirrors the assessment runner's
pre-flight auth probe (deploy v34).
"""
from __future__ import annotations

import asyncio

import pytest

from app.architectures import jobs as arch_jobs


def _make_job(**over):
    """Build a bare _Job with the fields the runner reads."""
    return arch_jobs._Job(
        id="job-test",
        tenant_id="t1",
        workload_id="wl1",
        workload_name="Demo workload",
        connection_id="c1",
        created_by="tester",
        **over,
    )


@pytest.mark.asyncio
async def test_rebuild_fails_fast_on_expired_token(monkeypatch):
    # A linked workload exists…
    monkeypatch.setattr("app.workloads.registry.get_workload", lambda _wid: {"name": "Demo workload", "connection_id": "c1"})
    # …and resolves to a real connection object…
    monkeypatch.setattr("app.core.azure_connections.resolve_connection", lambda _cid: {"name": "lu", "auth_method": "az_cli_token"})
    # …whose ARM token is expired.
    async def _expired(_conn):
        return None, "Pasted token has expired — paste a fresh one."
    monkeypatch.setattr("app.azure.credentials.get_arm_token", _expired)

    # If the pre-flight probe DIDN'T fire, the runner would call these — make them explode so the
    # test fails loudly rather than silently passing for the wrong reason.
    async def _boom(*_a, **_k):
        raise AssertionError("dump_resources should not be reached when the token is expired")
    monkeypatch.setattr("app.architectures.reverse.dump_resources", _boom)

    mgr = arch_jobs._Manager()
    job = _make_job(target_architecture_id="arch-1")
    await mgr._run(job)

    assert job.status == "error"
    assert "lu" in job.error                      # names the offending connection
    assert "expired" in job.error.lower()         # surfaces the real reason
    assert "Settings" in job.error                # actionable next step


@pytest.mark.asyncio
async def test_rebuild_proceeds_when_token_valid(monkeypatch):
    monkeypatch.setattr("app.workloads.registry.get_workload", lambda _wid: {"name": "Demo workload", "connection_id": "c1"})
    monkeypatch.setattr("app.core.azure_connections.resolve_connection", lambda _cid: {"name": "lu", "auth_method": "az_cli_token"})

    async def _ok(_conn):
        return "valid-token", None
    monkeypatch.setattr("app.azure.credentials.get_arm_token", _ok)

    # With a valid token the runner advances to the query phase — short-circuit there so the test
    # stays fast and offline.
    async def _no_resources(_wl, _conn):
        return {"resources": [], "count": 0, "predicate": "x", "error": "No resources found in this workload's scope."}
    monkeypatch.setattr("app.architectures.reverse.dump_resources", _no_resources)

    mgr = arch_jobs._Manager()
    job = _make_job(target_architecture_id="arch-1")
    await mgr._run(job)

    # It got PAST the auth probe (reached the query phase) and failed for the resource reason,
    # not an auth reason.
    assert job.status == "error"
    assert "No resources" in job.error
    assert "expired" not in job.error.lower()
