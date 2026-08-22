"""Page-aware, resumable Application Registrations refresh tests."""
from __future__ import annotations

import asyncio
import copy
import re
import time
from types import SimpleNamespace
from typing import Any

import pytest

from app.entra.graphclient import GraphError, GraphPage, GraphPermissionError, GraphResponse
from app.identity import appregs, appregs_cache, appregs_job


def _raw(index: int) -> dict[str, Any]:
    return {
        "id": f"object-{index}",
        "appId": f"app-{index}",
        "displayName": f"Application {index:04d}",
        "signInAudience": "AzureADMyOrg",
        "publisherDomain": "example.test",
        "tags": [],
        "passwordCredentials": [],
        "keyCredentials": [],
        "requiredResourceAccess": [],
        "owners": [{"displayName": "Owner"}],
    }


def _raw_guid(index: int) -> dict[str, Any]:
    row = _raw(index)
    row["appId"] = f"10000000-0000-0000-0000-{index:012d}"
    return row


class FakeGraphClient:
    pages: dict[str, GraphPage] = {}
    calls: list[str] = []
    retry_events: list[tuple[int, int, float]] = []
    batch_requests: list[str] = []
    service_principals: dict[str, GraphResponse | dict[str, Any] | None] = {}
    beta: bool = True
    # path -> rows, or an exception to raise
    reports: dict[str, Any] = {}

    def __init__(self, *_args, **_kwargs) -> None:
        self.stats = SimpleNamespace(retries=0, throttled=0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def beta_available(self, beta: bool = True) -> bool:
        return (not beta) or self.beta

    async def get_all(self, path: str, **_kwargs):
        configured = self.reports.get(path)
        if isinstance(configured, BaseException):
            raise configured
        return list(configured or []), False

    async def get(self, *_args, **_kwargs):
        return {"value": [{"appRoles": [], "oauth2PermissionScopes": []}]}

    async def get_count(self, _collection: str):
        first = self.pages.get("first")
        return first.total if isinstance(first, GraphPage) else None

    async def get_page(self, _path: str, *, next_link: str = "", on_retry=None, **_kwargs):
        key = next_link or "first"
        self.calls.append(key)
        value = self.pages[key]
        if key == "throttle" and on_retry is not None:
            self.stats.retries += 1
            self.stats.throttled += 1
            await on_retry(429, 1, 4.0)
        if isinstance(value, BaseException):
            raise value
        return value

    async def batch(self, requests, *, on_retry=None):  # noqa: ARG002
        responses: list[GraphResponse] = []
        for request in requests:
            match = re.search(r"appId='([^']+)'", request.url)
            app_id = match.group(1) if match else ""
            self.batch_requests.append(app_id)
            configured = self.service_principals.get(app_id)
            if isinstance(configured, GraphResponse):
                responses.append(GraphResponse(
                    id=request.id,
                    status=configured.status,
                    body=configured.body,
                    headers=configured.headers,
                    throttled=configured.throttled,
                ))
            elif isinstance(configured, dict):
                responses.append(GraphResponse(id=request.id, status=200, body=configured))
            else:
                responses.append(GraphResponse(id=request.id, status=404, body={}))
        return responses


@pytest.fixture(autouse=True)
def _reset_job_state(monkeypatch, tmp_path):
    appregs_job._jobs.clear()
    appregs_job._conds.clear()
    appregs_job._tasks.clear()
    FakeGraphClient.pages = {}
    FakeGraphClient.calls = []
    FakeGraphClient.retry_events = []
    FakeGraphClient.batch_requests = []
    FakeGraphClient.service_principals = {}
    FakeGraphClient.beta = True
    FakeGraphClient.reports = {}
    monkeypatch.setattr(appregs, "GraphClient", FakeGraphClient)
    monkeypatch.setattr(appregs_cache, "_CACHE_PATH", tmp_path / "snapshots.json")
    monkeypatch.setattr(appregs_cache, "_CHECKPOINT_PATH", tmp_path / "checkpoints.json")
    monkeypatch.setattr(appregs_cache, "_mem_cache", None)
    monkeypatch.setattr(appregs_cache, "_checkpoint_cache", None)


@pytest.mark.asyncio
async def test_capped_refresh_reports_real_total_and_truncates_only_when_next_link_exists():
    FakeGraphClient.pages = {
        "first": GraphPage([_raw(i) for i in range(50)], "https://graph.microsoft.com/v1.0/applications?$skiptoken=next", 60),
    }
    checkpoints: list[dict[str, Any]] = []
    progress: list[tuple[str, dict[str, Any]]] = []

    async def save(state: dict[str, Any]) -> None:
        checkpoints.append(state)

    async def report(_level: str, message: str, meta: dict[str, Any]) -> None:
        progress.append((message, meta))

    apps, meta = await appregs._collect_real(
        {"id": "c1"}, limit=50, on_checkpoint=save, progress=report,
    )
    assert len(apps) == 50
    assert meta["graph_total"] == 60
    assert meta["pages"] == 1
    assert meta["truncated"] is True
    assert meta["stop_reason"] == "configured_limit"
    assert checkpoints[-1]["next_link"].endswith("$skiptoken=next")
    assert any(item[1].get("percent") == pytest.approx(83.3) for item in progress)


@pytest.mark.asyncio
async def test_exactly_the_limit_is_complete_when_graph_has_no_next_page():
    FakeGraphClient.pages = {"first": GraphPage([_raw(i) for i in range(50)], "", 50)}
    apps, meta = await appregs._collect_real({"id": "c1"}, limit=50)
    assert len(apps) == 50
    assert meta["complete"] is True
    assert meta["truncated"] is False
    assert meta["stop_reason"] == "complete"


# --------------------------------------------------------------- sign-in activity
SIGNIN_PATH = "/reports/servicePrincipalSignInActivities"
CRED_PATH = "/reports/appCredentialSignInActivities"


def _app_with_creds(index: int, key_ids: list[str]) -> dict[str, Any]:
    row = _raw(index)
    row["passwordCredentials"] = [
        {"keyId": k, "displayName": f"secret-{k}", "endDateTime": "2099-01-01T00:00:00Z"}
        for k in key_ids
    ]
    return row


@pytest.mark.asyncio
async def test_signin_activity_joins_by_app_id_case_insensitively():
    rows = [_raw(0), _raw(1)]
    rows[0]["appId"] = "APP-UPPER"
    rows[1]["appId"] = "app-quiet"
    FakeGraphClient.pages = {"first": GraphPage(rows, "", 2)}
    FakeGraphClient.reports = {
        SIGNIN_PATH: [
            {
                "appId": "app-upper",  # Graph casing differs from the application object
                "lastSignInActivity": {"lastSignInDateTime": "2026-08-20T09:00:00Z"},
                "delegatedClientSignInActivity": {"lastSignInDateTime": "2026-08-19T09:00:00Z"},
                "applicationAuthenticationClientSignInActivity": {"lastSignInDateTime": "2026-08-20T09:00:00Z"},
            },
        ],
    }
    apps, meta = await appregs._collect_real({"id": "c1"}, limit=50)
    by_id = {a["appId"]: a for a in apps}

    assert meta["signin_activity"]["measured"] is True
    assert meta["signin_activity"]["apps_with_activity"] == 1
    assert by_id["APP-UPPER"]["lastSignIn"] == "2026-08-20T09:00:00Z"
    assert by_id["APP-UPPER"]["lastSignInApplication"] == "2026-08-20T09:00:00Z"
    assert by_id["APP-UPPER"]["lastSignInDelegated"] == "2026-08-19T09:00:00Z"
    # Measured, but this one has no row: known WITHOUT a date.
    assert by_id["app-quiet"]["lastSignInKnown"] is True
    assert by_id["app-quiet"]["lastSignIn"] is None
    assert appregs.signin_bucket(by_id["app-quiet"]) == appregs.SIGNIN_BUCKET_NONE


@pytest.mark.asyncio
async def test_denied_signin_report_never_reads_as_never_signed_in():
    FakeGraphClient.pages = {"first": GraphPage([_raw(i) for i in range(3)], "", 3)}
    FakeGraphClient.reports = {SIGNIN_PATH: GraphPermissionError(403, "Insufficient privileges")}

    apps, meta = await appregs._collect_real({"id": "c1"}, limit=50)

    block = meta["signin_activity"]
    assert block["measured"] is False
    assert "AuditLog.Read.All" in block["reason"]
    assert all(a["lastSignInKnown"] is False and a["lastSignIn"] is None for a in apps)
    assert all(appregs.signin_bucket(a) == appregs.SIGNIN_BUCKET_UNKNOWN for a in apps)
    # A denied report must not be reported as a fleet of dormant applications.
    assert appregs.aggregate(apps)["summary"]["noRecentSignIn"] == 0
    # The inventory itself still succeeded.
    assert len(apps) == 3 and meta["complete"] is True


@pytest.mark.asyncio
async def test_beta_disabled_reports_not_measured_without_failing_the_refresh():
    FakeGraphClient.beta = False
    FakeGraphClient.pages = {"first": GraphPage([_raw(0)], "", 1)}

    apps, meta = await appregs._collect_real({"id": "c1"}, limit=50)

    assert len(apps) == 1
    assert meta["signin_activity"]["measured"] is False
    assert "beta" in meta["signin_activity"]["reason"].lower()
    assert apps[0]["lastSignInKnown"] is False


@pytest.mark.asyncio
async def test_credential_usage_joins_by_key_id_and_degrades_on_its_own():
    FakeGraphClient.pages = {
        "first": GraphPage([_app_with_creds(0, ["KEY-A", "key-b"])], "", 1),
    }
    FakeGraphClient.reports = {
        SIGNIN_PATH: [{"appId": "app-0", "lastSignInActivity": {"lastSignInDateTime": "2026-08-20T09:00:00Z"}}],
        CRED_PATH: [{"keyId": "key-a", "signInActivity": {"lastSignInDateTime": "2026-08-18T09:00:00Z"}}],
    }
    apps, meta = await appregs._collect_real({"id": "c1"}, limit=50)
    creds = {c["keyId"]: c for c in apps[0]["credentials"]}

    assert meta["signin_activity"]["credentials"] == {"measured": True, "reason": "", "count": 1}
    assert creds["KEY-A"]["lastUsed"] == "2026-08-18T09:00:00Z"
    # Measured but unused — the retirement candidate, distinct from unmeasured.
    assert creds["key-b"]["lastUsedKnown"] is True and creds["key-b"]["lastUsed"] is None

    # The credential report failing must not take the per-app dates down with it.
    FakeGraphClient.reports[CRED_PATH] = GraphError(400, "not licensed")
    apps, meta = await appregs._collect_real({"id": "c1"}, limit=50)
    assert apps[0]["lastSignIn"] == "2026-08-20T09:00:00Z"
    assert meta["signin_activity"]["measured"] is True
    assert meta["signin_activity"]["credentials"]["measured"] is False
    assert all(c["lastUsedKnown"] is False for c in apps[0]["credentials"])


@pytest.mark.asyncio
async def test_enterprise_app_state_distinguishes_active_deactivated_absent_and_unknown():
    rows = [_raw_guid(i) for i in range(1, 6)]
    # Duplicate display names prove the join is by immutable appId rather than presentation.
    rows[0]["displayName"] = rows[1]["displayName"] = "Synthetic application"
    ids = [row["appId"] for row in rows]
    FakeGraphClient.service_principals = {
        ids[0]: {
            "id": "sp-active", "appId": ids[0], "accountEnabled": True,
            "servicePrincipalType": "Application", "disabledByMicrosoftStatus": "",
        },
        ids[1]: {
            "id": "sp-deactivated", "appId": ids[1], "accountEnabled": False,
            "servicePrincipalType": "Application", "disabledByMicrosoftStatus": "",
        },
        ids[3]: {
            "id": "sp-incomplete", "appId": ids[3], "accountEnabled": None,
            "servicePrincipalType": "Application", "disabledByMicrosoftStatus": "",
        },
        ids[4]: GraphResponse(id="ignored", status=403, body={"error": {"message": "denied"}}),
    }
    client = FakeGraphClient()

    await appregs._attach_enterprise_app_states(client, rows, {})

    assert [row["enterpriseAppState"] for row in rows] == [
        "active", "deactivated", "not_instantiated", "unknown", "unknown",
    ]
    assert rows[0]["servicePrincipalId"] == "sp-active"
    assert rows[1]["servicePrincipalId"] == "sp-deactivated"
    assert rows[2]["enterpriseAppStateReadStatus"] == "not_found"
    assert rows[3]["enterpriseAppStateReadStatus"] == "incomplete"
    assert rows[4]["enterpriseAppStateReadStatus"] == "unreadable"


@pytest.mark.asyncio
async def test_enterprise_state_lookup_deduplicates_repeated_app_ids_and_keeps_provider_status_separate():
    first = _raw_guid(1)
    duplicate = {**_raw_guid(2), "appId": first["appId"]}
    FakeGraphClient.service_principals = {
        first["appId"]: {
            "id": "sp-1", "appId": first["appId"], "accountEnabled": True,
            "servicePrincipalType": "Application",
            "disabledByMicrosoftStatus": "ProviderPolicyState",
        },
    }

    await appregs._attach_enterprise_app_states(FakeGraphClient(), [first, duplicate], {})

    assert FakeGraphClient.batch_requests == [first["appId"]]
    assert first["enterpriseAppState"] == duplicate["enterpriseAppState"] == "active"
    assert first["disabledByMicrosoftStatus"] == "ProviderPolicyState"


@pytest.mark.asyncio
async def test_checkpoint_contains_enterprise_state_and_resume_repeats_no_completed_lookups():
    next_url = "https://graph.microsoft.com/v1.0/applications?$skiptoken=resume-state"
    page_rows = [_raw_guid(i) for i in range(1, 51)]
    FakeGraphClient.pages = {"first": GraphPage(page_rows, next_url, 60)}
    FakeGraphClient.service_principals = {
        page_rows[0]["appId"]: {
            "id": "sp-1", "appId": page_rows[0]["appId"], "accountEnabled": False,
            "servicePrincipalType": "Application", "disabledByMicrosoftStatus": "",
        },
    }
    checkpoints: list[dict[str, Any]] = []

    async def save(state: dict[str, Any]) -> None:
        checkpoints.append(copy.deepcopy(state))

    apps, meta = await appregs._collect_real(
        {"id": "c1"}, limit=50, on_checkpoint=save,
    )
    assert meta["truncated"] is True
    assert len(FakeGraphClient.batch_requests) == 50
    assert checkpoints[-1]["schema"] == appregs.APPREGS_CHECKPOINT_SCHEMA
    assert checkpoints[-1]["apps_raw"][0]["enterpriseAppState"] == "deactivated"

    calls_before_resume = list(FakeGraphClient.batch_requests)
    resumed_apps, resumed_meta = await appregs._collect_real(
        {"id": "c1"}, limit=50, checkpoint=checkpoints[-1],
    )
    assert FakeGraphClient.batch_requests == calls_before_resume
    assert resumed_meta["resumed"] is True
    assert resumed_apps[0]["enterpriseAppState"] == "deactivated"


@pytest.mark.asyncio
async def test_full_tenant_mode_follows_every_page():
    next_url = "https://graph.microsoft.com/v1.0/applications?$skiptoken=next"
    FakeGraphClient.pages = {
        "first": GraphPage([_raw(i) for i in range(50)], next_url, 60),
        next_url: GraphPage([_raw(i) for i in range(50, 60)], "", None),
    }
    apps, meta = await appregs._collect_real({"id": "c1"}, limit=50, full=True)
    assert len(apps) == 60
    assert FakeGraphClient.calls == ["first", next_url]
    assert meta["mode"] == "full"
    assert meta["complete"] is True
    assert meta["graph_total"] == 60


@pytest.mark.asyncio
async def test_checkpoint_resume_skips_completed_pages():
    next_url = "https://graph.microsoft.com/v1.0/applications?$skiptoken=resume"
    FakeGraphClient.pages = {next_url: GraphPage([_raw(i) for i in range(50, 60)], "", None)}
    checkpoint = {
        "schema": appregs.APPREGS_CHECKPOINT_SCHEMA,
        "mode": "full",
        "target_limit": appregs.APPREGS_FULL_SAFETY_LIMIT,
        "page_size": appregs.APPREGS_PAGE_SIZE,
        "pages": 1,
        "graph_total": 60,
        "next_link": next_url,
        "enumeration_complete": False,
        "apps_raw": [_raw(i) for i in range(50)],
    }
    apps, meta = await appregs._collect_real(
        {"id": "c1"}, limit=50, full=True, checkpoint=checkpoint,
    )
    assert len(apps) == 60
    assert FakeGraphClient.calls == [next_url]
    assert meta["resumed"] is True
    assert meta["pages"] == 2


@pytest.mark.asyncio
async def test_expired_checkpoint_restarts_once_from_page_one():
    expired = "https://graph.microsoft.com/v1.0/applications?$skiptoken=expired"
    FakeGraphClient.pages = {
        expired: GraphError(410, "expired"),
        "first": GraphPage([_raw(i) for i in range(3)], "", 3),
    }
    checkpoint = {
        "schema": appregs.APPREGS_CHECKPOINT_SCHEMA,
        "mode": "full",
        "target_limit": appregs.APPREGS_FULL_SAFETY_LIMIT,
        "page_size": appregs.APPREGS_PAGE_SIZE,
        "pages": 1,
        "graph_total": 60,
        "next_link": expired,
        "enumeration_complete": False,
        "apps_raw": [_raw(50)],
    }
    messages: list[str] = []

    async def report(_level: str, message: str, _meta: dict[str, Any]) -> None:
        messages.append(message)

    apps, meta = await appregs._collect_real(
        {"id": "c1"}, limit=50, full=True, checkpoint=checkpoint, progress=report,
    )
    assert FakeGraphClient.calls == [expired, "first"]
    assert len(apps) == 3 and meta["complete"] is True
    assert any("continuation expired" in message for message in messages)


@pytest.mark.asyncio
async def test_throttle_progress_is_structured_and_retry_after_visible():
    FakeGraphClient.pages = {"first": GraphPage([_raw(1)], "", 1)}

    async def throttled_get_page(self, _path, *, on_retry=None, **_kwargs):
        self.stats.retries = 1
        self.stats.throttled = 1
        assert on_retry is not None
        await on_retry(429, 1, 7.0)
        return GraphPage([_raw(1)], "", 1)

    FakeGraphClient.get_page = throttled_get_page
    events: list[dict[str, Any]] = []

    async def report(_level: str, _message: str, meta: dict[str, Any]) -> None:
        events.append(meta)

    _apps, meta = await appregs._collect_real({"id": "c1"}, limit=50, progress=report)
    throttle = next(item for item in events if item.get("phase") == "throttle")
    assert throttle["status"] == 429 and throttle["retry"] == 1
    assert throttle["delay_seconds"] == pytest.approx(7.0)
    assert throttle["current"] == 0
    assert throttle["throttles"] == 1 and throttle["retries"] == 1
    assert meta["retries"] == 1 and meta["throttles"] == 1


@pytest.mark.asyncio
async def test_cancel_after_page_preserves_checkpoint_and_previous_snapshot(monkeypatch):
    previous = {"source": "microsoft_graph", "apps": [{"id": "old"}], "summary": {"total": 1}}
    appregs_cache.set_("t1", "c1", previous)
    reached_page = asyncio.Event()

    async def collect(*_args, on_checkpoint=None, **_kwargs):
        assert on_checkpoint is not None
        await on_checkpoint({
            "schema": appregs.APPREGS_CHECKPOINT_SCHEMA, "mode": "full", "target_limit": appregs.APPREGS_FULL_SAFETY_LIMIT,
            "page_size": 250, "pages": 1, "graph_total": 1000,
            "next_link": "https://graph.microsoft.com/v1.0/applications?$skiptoken=x",
            "enumeration_complete": False, "apps_raw": [_raw(i) for i in range(250)],
        })
        reached_page.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(appregs, "collect_app_registrations", collect)
    job = appregs_job.start_job(
        key="t1|c1", tenant_id="t1", connection={"id": "c1"}, connection_id="c1",
        limit=500, mode="full",
    )
    await reached_page.wait()
    task = appregs_job._tasks["t1|c1"]
    assert appregs_job.cancel_job("t1|c1") is True
    await task
    assert job["status"] == "cancelled"
    assert job["resume_available"] is True
    assert appregs_cache.get("t1", "c1")["payload"] == previous
    assert len(appregs_cache.get_checkpoint("t1", "c1")["apps_raw"]) == 250


@pytest.mark.asyncio
async def test_failed_job_preserves_previous_snapshot_and_exposes_resume(monkeypatch):
    previous = {"source": "microsoft_graph", "apps": [{"id": "old"}], "summary": {"total": 1}}
    appregs_cache.set_("t1", "c1", previous)

    async def collect(*_args, on_checkpoint=None, **_kwargs):
        assert on_checkpoint is not None
        await on_checkpoint({
            "schema": appregs.APPREGS_CHECKPOINT_SCHEMA, "mode": "capped", "target_limit": 500, "page_size": 250,
            "pages": 1, "graph_total": 700,
            "next_link": "https://graph.microsoft.com/v1.0/applications?$skiptoken=x",
            "enumeration_complete": False, "apps_raw": [_raw(i) for i in range(250)],
        })
        raise RuntimeError("provider detail must not escape")

    monkeypatch.setattr(appregs, "collect_app_registrations", collect)
    job = appregs_job.start_job(
        key="t1|c1", tenant_id="t1", connection={"id": "c1"}, connection_id="c1", limit=500,
    )
    task = appregs_job._tasks["t1|c1"]
    await task
    assert job["status"] == "error"
    assert job["error"] == "Refresh failed. The previous completed snapshot was preserved."
    assert "provider detail" not in job["error"]
    assert appregs_cache.get("t1", "c1")["payload"] == previous
    paused = appregs_job.recoverable_job("t1", "c1")
    assert paused and paused["status"] == "paused" and paused["current"] == 250


@pytest.mark.asyncio
async def test_restart_resume_uses_checkpoint_limit_even_if_setting_changed(monkeypatch):
    appregs_cache.set_checkpoint("t1", "c1", {
        "schema": appregs.APPREGS_CHECKPOINT_SCHEMA, "mode": "capped", "configured_limit": 500,
        "target_limit": 500, "page_size": 250, "pages": 1, "graph_total": 700,
        "next_link": "https://graph.microsoft.com/v1.0/applications?$skiptoken=x",
        "enumeration_complete": False, "apps_raw": [_raw(1)],
    })
    seen: dict[str, Any] = {}

    async def collect(*_args, **kwargs):
        seen.update(kwargs)
        return {
            "source": "microsoft_graph", "apps": [], "summary": {"total": 0},
            "enumeration": {"mode": "capped"},
        }

    monkeypatch.setattr(appregs, "collect_app_registrations", collect)
    job = appregs_job.start_job(
        key="t1|c1", tenant_id="t1", connection={"id": "c1"}, connection_id="c1",
        limit=1000, mode="capped",
    )
    await appregs_job._tasks["t1|c1"]
    assert job["status"] == "done"
    assert seen["limit"] == 500
    assert seen["checkpoint"]["configured_limit"] == 500


@pytest.mark.asyncio
async def test_deliberate_mode_change_discards_checkpoint_with_visible_warning(monkeypatch):
    appregs_cache.set_checkpoint("t1", "c1", {
        "schema": appregs.APPREGS_CHECKPOINT_SCHEMA, "mode": "capped", "configured_limit": 500,
        "target_limit": 500, "page_size": 250, "pages": 1, "graph_total": 700,
        "next_link": "https://graph.microsoft.com/v1.0/applications?$skiptoken=x",
        "enumeration_complete": False, "apps_raw": [_raw(1)],
    })

    async def collect(*_args, **kwargs):
        assert kwargs["checkpoint"] is None
        return {
            "source": "microsoft_graph", "apps": [], "summary": {"total": 0},
            "enumeration": {"mode": "full"},
        }

    monkeypatch.setattr(appregs, "collect_app_registrations", collect)
    job = appregs_job.start_job(
        key="t1|c1", tenant_id="t1", connection={"id": "c1"}, connection_id="c1",
        limit=500, mode="full",
    )
    await appregs_job._tasks["t1|c1"]
    assert job["status"] == "done"
    assert any("cannot be used for full mode" in row["message"] for row in job["progress"])


def test_checkpoint_roundtrip_and_expiry(monkeypatch):
    state = {"mode": "capped", "apps_raw": [_raw(1)]}
    appregs_cache.set_checkpoint("t1", "c1", state)
    assert appregs_cache.get_checkpoint("t1", "c1")["apps_raw"][0]["id"] == "object-1"
    key = appregs_cache._key("t1", "c1")
    appregs_cache._checkpoint_cache[key]["updated_ts"] = time.time() - appregs_cache.CHECKPOINT_TTL_SECONDS - 1
    assert appregs_cache.get_checkpoint("t1", "c1") is None


def test_start_does_not_spawn_a_second_task_while_cancelling():
    existing = {
        "id": "existing", "status": "cancelling", "progress": [],
    }
    appregs_job._jobs["t1|c1"] = existing
    returned = appregs_job.start_job(
        key="t1|c1", tenant_id="t1", connection={"id": "c1"}, connection_id="c1",
    )
    assert returned is existing
    assert "t1|c1" not in appregs_job._tasks


@pytest.mark.parametrize(("requested", "expected"), [(10, 50), (500, 500), (9000, 5000)])
def test_admin_configurable_limit_is_clamped(monkeypatch, tmp_path, requested, expected):
    from app.core import app_settings

    monkeypatch.setattr(app_settings, "_PATH", tmp_path / "settings.json")
    saved = app_settings.save_settings({"app_registrations_limit": requested})
    assert saved["app_registrations_limit"] == expected
