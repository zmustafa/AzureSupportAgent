"""Conditional Access responses must be JSON-clean and stable.

FastAPI's encoder accepts a `set`, so returning one is not a crash. It is a subtler problem:
set iteration order is arbitrary, so an unchanged tenant serialises in a different order on
every call, and the policy-as-code export from `/ca/export` shows phantom diffs. That artifact
exists to be committed and compared, which makes ordering part of its contract.

The class-resolution dict attached to every policy was originally built from sets and returned
verbatim. Nothing in the existing suite would have noticed: those tests call the endpoint
coroutines directly and inspect the dict, never serialising and never comparing two runs.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi.encoders import jsonable_encoder

from app.api import entra as entra_api
from app.entra import cache, demo
from app.entra import snapshot as snapshot_mod


class _Principal:
    tenant_id = demo.DEMO_TENANT
    subject = "dev"


@pytest.fixture(autouse=True)
def _demo_tenant(tmp_path, monkeypatch):
    cache.set_root_for_tests(tmp_path / "entra")
    snapshot_mod._analysis_memo.clear()  # noqa: SLF001 - test isolation

    import app.core.azure_connections as ac

    monkeypatch.setattr(
        ac, "resolve_connection",
        lambda cid: {"id": "conn-demo", "tenant_id": demo.DEMO_TENANT} if cid == "conn-demo" else None,
    )
    demo.seed()
    yield
    cache.clear_memo()


def _run(coro):
    return asyncio.run(coro)


def _encodes(payload: Any) -> str:
    try:
        return json.dumps(jsonable_encoder(payload))
    except (TypeError, ValueError) as exc:  # pragma: no cover - only on regression
        pytest.fail(f"response is not JSON-encodable: {exc}")


@pytest.mark.parametrize("name", ["ca_policies", "ca_coverage", "ca_conflicts", "ca_breakglass"])
def test_the_ca_endpoints_return_json_encodable_payloads(name):
    fn = getattr(entra_api, name)
    body = _run(fn(connection_id="conn-demo", principal=_Principal()))
    assert _encodes(body)


def test_the_policy_export_is_json_encodable():
    body = _run(entra_api.ca_export(format="json", connection_id="conn-demo",
                                    principal=_Principal()))
    assert _encodes(body)


def test_the_class_resolution_attached_to_a_policy_is_ordered_not_a_set():
    """The exact regression: sets on the policy dict make the export unstable."""
    body = _run(entra_api.ca_policies(connection_id="conn-demo", principal=_Principal()))
    policies = body["policies"]
    assert policies, "the demo tenant must ship policies for this test to mean anything"
    checked = 0
    for p in policies:
        for detail in (p.get("class_coverage") or {}).values():
            assert isinstance(detail["hit"], list), "hit must be an ordered list, not a set"
            assert isinstance(detail["missed"], list), "missed must be an ordered list"
            assert detail["hit"] == sorted(detail["hit"]), "hit must be sorted for stable diffs"
            checked += 1
    assert checked, "no policy resolved to any class - this test would pass vacuously"


def test_the_policy_export_is_stable_across_runs():
    """A policy-as-code artifact that changes without the tenant changing is unusable."""
    first = _encodes(_run(entra_api.ca_export(
        format="json", connection_id="conn-demo", principal=_Principal())))
    snapshot_mod._analysis_memo.clear()  # noqa: SLF001 - force a genuine recomputation
    cache.clear_memo()
    second = _encodes(_run(entra_api.ca_export(
        format="json", connection_id="conn-demo", principal=_Principal())))
    assert first == second, "the same tenant exported twice must produce the same bytes"


def test_the_coverage_matrix_is_json_encodable_including_the_derived_block():
    body = _run(entra_api.ca_coverage(connection_id="conn-demo", principal=_Principal()))
    assert "derived" in body
    assert _encodes(body["derived"])
