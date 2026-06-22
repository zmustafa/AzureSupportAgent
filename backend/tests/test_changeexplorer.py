"""Azure Workload Change Explorer backend tests.

Exercises the deterministic pipeline (classify, risk, normalize, explain, insights, export) and
asserts the demo scenario lands on the spec's exact expected counts. The run store is redirected
to tmp_path so tests never touch real ``.data``.
"""
import asyncio
import json

import pytest

from app.changeexplorer import (
    classify,
    demo,
    deps,
    export as export_mod,
    insights as insights_mod,
    normalize as normalize_mod,
    risk,
    runs as runs_store,
    service,
)
from app.changeexplorer.models import label_for_score


# --------------------------------------------------------------------------- enums / bands
def test_label_bands():
    assert label_for_score(95) == "Critical"
    assert label_for_score(90) == "Critical"
    assert label_for_score(89) == "High"
    assert label_for_score(70) == "High"
    assert label_for_score(60) == "Medium"
    assert label_for_score(40) == "Medium"
    assert label_for_score(39) == "Low"
    assert label_for_score(10) == "Low"
    assert label_for_score(9) == "Informational"
    assert label_for_score(0) == "Informational"


# --------------------------------------------------------------------------- classifier
@pytest.mark.parametrize("rtype,expected", [
    ("microsoft.network/applicationgateways", "Network"),
    ("microsoft.network/privatednszones/a", "DNS"),
    ("microsoft.network/networksecuritygroups", "Network"),
    ("microsoft.keyvault/vaults/certificates", "Certificate"),
    ("microsoft.keyvault/vaults/secrets", "Secret"),
    ("microsoft.keyvault/vaults", "KeyVault"),
    ("microsoft.authorization/roleassignments", "RBAC"),
    ("microsoft.managedidentity/userassignedidentities", "ManagedIdentity"),
    ("microsoft.web/sites/config", "AppConfiguration"),
    ("microsoft.sql/servers", "Database"),
    ("microsoft.storage/storageaccounts", "Storage"),
    ("microsoft.insights/diagnosticsettings", "Monitoring"),
    ("microsoft.compute/virtualmachines", "Compute"),
    ("microsoft.resources/tags", "TagsMetadata"),
])
def test_classify(rtype, expected):
    assert classify.classify(rtype, "write") == expected


def test_op_kind():
    assert classify.op_kind("Microsoft.Network/x/delete") == "delete"
    assert classify.op_kind("Microsoft.Network/x/write") == "write"
    assert classify.op_kind("Microsoft.Network/x/action") == "action"
    assert classify.op_kind("Microsoft.Network/x/read") == "read"


# --------------------------------------------------------------------------- classify by property path
# The real complaint: tenant-wide ARG resourcechanges often arrive with no resource type/id, only
# the changed property paths. The classifier must dissect those so they don't all show as Unknown.
@pytest.mark.parametrize("paths,expected", [
    (["properties.securityRules[3].access"], "Network"),
    (["properties.subnets[0].addressPrefix"], "Network"),
    (["properties.siteConfig.appSettings"], "AppConfiguration"),
    (["properties.accessPolicies[1]"], "KeyVault"),
    (["properties.networkAcls.defaultAction"], "Storage"),
])
def test_classify_by_property_path(paths, expected):
    # Empty type AND empty operation — the signal is purely in the property path.
    assert classify.classify("", "", paths) == expected


# --------------------------------------------------------------------------- AI overlay
def test_apply_ai_resolves_unknown_and_rescores():
    from app.changeexplorer import service
    ev = {
        "resourceType": "", "resourceName": "nsg-app", "operation": "Update", "actorType": "Unknown",
        "category": "Unknown", "riskScore": 0, "riskLabel": "Informational",
        "details": [{"propertyPath": "securityRules/allow-rdp", "beforeValue": None, "afterValue": "Allow TCP 3389 from 0.0.0.0/0"}],
    }
    service._apply_ai(ev, {"category": "Network", "summary": "Added an inbound NSG rule allowing RDP from the Internet.",
                            "impact": "Exposes RDP to the public Internet.", "why": "Public RDP is a top attack vector.",
                            "risk": 92}, production=True)
    assert ev["category"] == "Network"
    assert ev["riskLabel"] == "Critical" and ev["riskScore"] >= 90
    assert "Internet" in ev["plainEnglishSummary"]
    assert ev["confidence"] == "AI-analyzed"


def test_apply_ai_blends_risk_upward_only():
    from app.changeexplorer import service
    ev = {"resourceType": "microsoft.network/applicationgateways", "resourceName": "agw", "operation": "write",
          "category": "Network", "riskScore": 90, "riskLabel": "Critical", "details": [], "actorType": "ServicePrincipal"}
    # A lower AI hint must NOT downgrade a deterministically-critical change.
    service._apply_ai(ev, {"category": "Network", "summary": "s", "impact": "i", "why": "w", "risk": 30}, production=True)
    assert ev["riskScore"] == 90 and ev["riskLabel"] == "Critical"


# --------------------------------------------------------------------------- AI enrichment concurrency
class _FakeStreamEvent:
    def __init__(self, text):
        self.type = "token"; self.text = text


class _ConcProvider:
    """Fake LLM provider that records peak concurrency across stream() calls and answers each
    batch with a valid JSON array (echoing back the 'i' indices it was given)."""
    def __init__(self, state):
        self.state = state

    async def stream(self, messages, tools, max_tokens=None):
        import asyncio as _aio, json as _json, re as _re
        self.state["active"] += 1
        self.state["peak"] = max(self.state["peak"], self.state["active"])
        try:
            await _aio.sleep(0.05)                       # hold the slot so overlap is observable
            user = messages[-1]["content"]
            idxs = [int(n) for n in _re.findall(r'"i":\s*(\d+)', user)]
            rows = [{"i": i, "category": "Network", "summary": "s", "impact": "x", "why": "y", "risk": 50} for i in idxs]
            yield _FakeStreamEvent(_json.dumps(rows))
        finally:
            self.state["active"] -= 1


def test_enrich_stream_runs_batches_concurrently(monkeypatch):
    import asyncio as _aio
    from app.changeexplorer import ai_enrich

    state = {"active": 0, "peak": 0}
    import app.agent.factory as _factory
    monkeypatch.setattr(_factory, "build_provider", lambda: _ConcProvider(state))

    # 60 events all Unknown → 6 batches of 10 (the _MAX_EVENTS cap). With _AI_CONCURRENCY=10 all
    # 6 batches run at once (peak == batch count, > the old 5-at-a-time limit).
    events = [{"category": "Unknown", "riskScore": 0, "resourceType": "", "resourceName": f"r{i}",
               "operation": "Update", "details": []} for i in range(60)]

    async def _drain():
        result, progress = {}, []
        async for ev in ai_enrich.enrich_stream(events):
            if "result" in ev:
                result = ev["result"]
            else:
                progress.append(ev)
        return result, progress

    result, progress = _aio.run(_drain())
    assert state["peak"] >= 6                       # all 6 batches ran in parallel (concurrency >= 6)
    assert len(result) == 60                        # every event enriched
    assert all(r["category"] == "Network" for r in result.values())
    assert any("parallel" in p.get("message", "") for p in progress)


def test_enrich_stream_no_provider_is_noop(monkeypatch):
    import asyncio as _aio
    from app.changeexplorer import ai_enrich
    import app.agent.factory as _factory

    def _boom():
        raise RuntimeError("no provider configured")
    monkeypatch.setattr(_factory, "build_provider", _boom)

    events = [{"category": "Unknown", "riskScore": 0, "resourceType": "", "details": []}]

    async def _drain():
        out = None
        async for ev in ai_enrich.enrich_stream(events):
            if "result" in ev:
                out = ev["result"]
        return out

    assert _aio.run(_drain()) == {}                 # graceful: deterministic results stand


# --------------------------------------------------------------------------- actor backfill
def test_collect_raw_backfills_actor_from_activity_log(monkeypatch):
    import asyncio as _aio
    from app.changeexplorer import service

    async def _fake_rg(predicate, s, e, conn):
        return [{"source": "ResourceGraph", "resourceId": "/x/nsg", "resourceName": "nsg", "resourceType": "",
                 "actor": "", "actorType": "Unknown", "correlationId": "corr-1", "changes": [], "raw": {}}], ""

    async def _fake_al(subs, s, e, conn, rids):
        return [{"source": "ActivityLog", "resourceId": "/x/nsg", "actor": "pipeline-spn",
                 "actorType": "ServicePrincipal", "correlationId": "corr-1", "operation": "write", "changes": [], "raw": {}}], ""

    monkeypatch.setattr(service.collectors, "collect_resource_graph_changes", _fake_rg)
    monkeypatch.setattr(service.collectors, "collect_activity_log", _fake_al)
    raw, _notes, limit = _aio.run(service._collect_raw({}, {"id": "c"}, {"predicate": "p", "subscriptions": ["s"]}, "a", "b"))
    rg = next(r for r in raw if r["source"] == "ResourceGraph")
    assert rg["actor"] == "pipeline-spn" and rg["actorType"] == "ServicePrincipal"
    assert limit == 0   # only one RG row → not capped


def test_collect_raw_flags_change_limit_when_capped(monkeypatch):
    """When the Resource Graph feed returns a full page (RG_CHANGE_LIMIT rows), _collect_raw
    reports the cap so the UI can show the 'showing the N most recent' banner."""
    import asyncio as _aio
    from app.changeexplorer import service, collectors

    async def _fake_rg(predicate, s, e, conn):
        rows = [{"source": "ResourceGraph", "resourceId": f"/x/r{i}", "actor": "a",
                 "actorType": "User", "correlationId": "", "changes": [], "raw": {}}
                for i in range(collectors.RG_CHANGE_LIMIT)]
        return rows, "capped note"

    async def _fake_al(subs, s, e, conn, rids):
        return [], ""

    monkeypatch.setattr(service.collectors, "collect_resource_graph_changes", _fake_rg)
    monkeypatch.setattr(service.collectors, "collect_activity_log", _fake_al)
    raw, notes, limit = _aio.run(service._collect_raw({}, {"id": "c"}, {"predicate": "p", "subscriptions": ["s"]}, "a", "b"))
    assert len(raw) == collectors.RG_CHANGE_LIMIT
    assert limit == collectors.RG_CHANGE_LIMIT   # cap surfaced
    assert "capped note" in notes


# --------------------------------------------------------------------------- concurrency + 429 backoff
class _Cap:
    """Minimal stand-in for command_runner.CaptureResult."""
    def __init__(self, ok=True, stdout="[]", error="", stderr=""):
        self.ok = ok; self.stdout = stdout; self.error = error; self.stderr = stderr


def test_is_throttled_and_retry_after():
    from app.changeexplorer import collectors
    assert collectors._is_throttled(_Cap(ok=True)) is False
    assert collectors._is_throttled(_Cap(ok=False, error="Forbidden")) is False
    t = _Cap(ok=False, error="(429) TooManyRequests; Retry-After: 7")
    assert collectors._is_throttled(t) is True
    assert collectors._retry_after_seconds(t) == 7.0
    # Throttle signal in stderr (no explicit retry-after) is still detected.
    assert collectors._is_throttled(_Cap(ok=False, stderr="Rate limit is exceeded")) is True


def test_collect_activity_log_runs_at_least_5_concurrent(monkeypatch):
    """The per-subscription Activity Log calls fan out concurrently (>= 5 in flight)."""
    import asyncio as _aio
    import app.exec.command_runner as cr
    from app.changeexplorer import collectors

    state = {"active": 0, "peak": 0}

    async def _fake(cmd, connection, read_only=True, **kw):
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        await _aio.sleep(0.05)          # hold the slot so overlap is observable
        state["active"] -= 1
        return _Cap(ok=True, stdout="[]")

    monkeypatch.setattr(cr, "run_command_capture", _fake)
    subs = [f"sub-{i}" for i in range(10)]
    # A service-principal connection uses the `az` CLI fan-out (the path under test here).
    conn = {"id": "c", "auth_method": "service_principal"}
    rows, note = _aio.run(collectors.collect_activity_log(subs, "s", "e", conn))
    assert rows == [] and note == ""
    assert state["peak"] >= 5            # proves concurrent execution (cap is 8)


def test_collect_activity_log_retries_on_throttle(monkeypatch):
    """A 429 on a subscription is retried with backoff, then succeeds."""
    import asyncio as _aio
    import app.exec.command_runner as cr
    from app.changeexplorer import collectors

    monkeypatch.setattr(collectors, "_backoff_delay", lambda *a, **k: 0.0)  # no real sleeping
    calls = {"n": 0}

    async def _fake(cmd, connection, read_only=True, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Cap(ok=False, error="(429) TooManyRequests")
        return _Cap(ok=True, stdout=json.dumps([
            {"operationName": {"value": "Microsoft.Network/x/write"}, "caller": "a@b.com",
             "resourceId": "/sub/r1", "subscriptionId": "sub-0", "correlationId": "c1"}
        ]))

    monkeypatch.setattr(cr, "run_command_capture", _fake)
    conn = {"id": "c", "auth_method": "service_principal"}
    rows, note = _aio.run(collectors.collect_activity_log(["sub-0"], "s", "e", conn))
    assert calls["n"] == 2 and note == "" and len(rows) == 1 and rows[0]["actor"] == "a@b.com"


def test_collect_activity_log_gives_up_after_max_retries(monkeypatch):
    """Persistent throttling stops after _MAX_RETRIES and reports a note (never raises)."""
    import asyncio as _aio
    import app.exec.command_runner as cr
    from app.changeexplorer import collectors

    monkeypatch.setattr(collectors, "_backoff_delay", lambda *a, **k: 0.0)
    calls = {"n": 0}

    async def _fake(cmd, connection, read_only=True, **kw):
        calls["n"] += 1
        return _Cap(ok=False, error="429 TooManyRequests")

    monkeypatch.setattr(cr, "run_command_capture", _fake)
    conn = {"id": "c", "auth_method": "service_principal"}
    rows, note = _aio.run(collectors.collect_activity_log(["sub-0"], "s", "e", conn))
    assert rows == [] and "sub-0" in note
    assert calls["n"] == collectors._MAX_RETRIES + 1   # first try + N retries


def test_activity_note_explains_access_errors():
    from app.changeexplorer import collectors
    sub = "f701d843-7424-4c28-ab54-0c1171fb0822"
    # The classic CLI 'not recognized' → actionable connection/tenant hint (no raw dump).
    n1 = collectors._activity_note(sub, "Subscription 'f701d843-7424-4c28-ab54-0c1171fb0822' not recognized.")
    assert "f701d843" in n1 and "connection" in n1.lower() and "not recognized" not in n1
    # Authorization failure → access-denied wording.
    n2 = collectors._activity_note(sub, "The client does not have authorization to perform action")
    assert "denied" in n2.lower()
    # Unknown error → generic, truncated.
    n3 = collectors._activity_note(sub, "some other failure")
    assert "f701d843" in n3 and "failed" in n3.lower()


# ----------------------------------------------- Activity Log REST path (pasted-token / MSI conns)
def test_collect_activity_log_uses_rest_for_pasted_token(monkeypatch):
    """A non-service-principal connection (pasted ARM token) reads the Activity Log over ARM REST
    with the connection's token — NOT the `az` CLI, which has no login for the token's tenant.
    This is the GIT-Prod-Sbs-on-'mat' bug: ARG worked but Activity Log failed 'not recognized'."""
    import asyncio as _aio
    import app.azure.credentials as creds
    import app.azure.arm as arm
    from app.changeexplorer import collectors

    async def _fake_token(conn):
        return "TOKEN", None

    captured = {"subs": []}

    async def _fake_events(token, sub, s, e, *, max_events=1000):
        captured["subs"].append(sub)
        assert token == "TOKEN"
        # Two events: one Succeeded write (kept) + one Failed (filtered out client-side).
        return ([
            {"operationName": {"value": "Microsoft.Storage/storageAccounts/write"},
             "status": {"value": "Succeeded"}, "caller": "spn-guid",
             "claims": {"idtyp": "app"}, "resourceId": f"/subscriptions/{sub}/rg/r1",
             "resourceType": {"value": "Microsoft.Storage/storageAccounts"},
             "resourceGroupName": "rg", "subscriptionId": sub, "correlationId": "c1",
             "eventTimestamp": "2026-06-21T17:00:00Z"},
            {"operationName": {"value": "Microsoft.Storage/storageAccounts/delete"},
             "status": {"value": "Failed"}, "caller": "x", "resourceId": "/y", "claims": {}},
        ], None)

    # The `az` CLI must NOT be called for a pasted-token connection.
    async def _boom_cli(*a, **k):  # pragma: no cover - asserts it's never reached
        raise AssertionError("CLI path must not run for a pasted-token connection")

    monkeypatch.setattr(creds, "get_arm_token", _fake_token)
    monkeypatch.setattr(arm, "list_activity_log_events", _fake_events)
    import app.exec.command_runner as cr
    monkeypatch.setattr(cr, "run_command_capture", _boom_cli)

    conn = {"id": "mat", "auth_method": "az_cli_token"}
    rows, note = _aio.run(collectors.collect_activity_log(["sub-A", "sub-B"], "s", "e", conn))
    assert note == "" and len(rows) == 2          # one kept row per subscription
    assert sorted(captured["subs"]) == ["sub-A", "sub-B"]
    r = rows[0]
    assert r["source"] == "ActivityLog" and r["actorType"] == "ServicePrincipal"
    assert r["operation"] == "Microsoft.Storage/storageAccounts/write"


def test_collect_activity_log_rest_filters_by_resource_ids(monkeypatch):
    """resource_ids restricts the REST rows to the workload's resources (prefix match)."""
    import asyncio as _aio
    import app.azure.credentials as creds
    import app.azure.arm as arm
    from app.changeexplorer import collectors

    async def _fake_token(conn):
        return "T", None

    async def _fake_events(token, sub, s, e, *, max_events=1000):
        return ([
            {"operationName": {"value": "x/write"}, "status": {"value": "Succeeded"},
             "resourceId": "/subscriptions/s/rg/keep", "claims": {}, "caller": "u@b.com"},
            {"operationName": {"value": "x/write"}, "status": {"value": "Succeeded"},
             "resourceId": "/subscriptions/s/rg/drop", "claims": {}, "caller": "u@b.com"},
        ], None)

    monkeypatch.setattr(creds, "get_arm_token", _fake_token)
    monkeypatch.setattr(arm, "list_activity_log_events", _fake_events)
    conn = {"id": "mat", "auth_method": "az_cli_token"}
    rows, note = _aio.run(collectors.collect_activity_log(
        ["s"], "s", "e", conn, resource_ids=["/subscriptions/s/rg/keep"]))
    assert note == "" and len(rows) == 1 and rows[0]["resourceId"].endswith("/keep")


def test_collect_activity_log_pasted_token_no_token_surfaces_error(monkeypatch):
    """A pasted-token connection that can't mint a token surfaces the auth error and never falls
    back to the CLI (there's no ambient `az` login for it)."""
    import asyncio as _aio
    import app.azure.credentials as creds
    from app.changeexplorer import collectors

    async def _no_token(conn):
        return None, "Pasted token has expired — paste a fresh one."

    async def _boom_cli(*a, **k):  # pragma: no cover
        raise AssertionError("CLI path must not run when a pasted-token has no token")

    monkeypatch.setattr(creds, "get_arm_token", _no_token)
    import app.exec.command_runner as cr
    monkeypatch.setattr(cr, "run_command_capture", _boom_cli)
    conn = {"id": "mat", "auth_method": "az_cli_token"}
    rows, note = _aio.run(collectors.collect_activity_log(["s"], "s", "e", conn))
    assert rows == [] and "expired" in note.lower()


def test_list_activity_log_events_pages_via_nextlink(monkeypatch):
    """The ARM REST helper follows ``nextLink`` and stops at ``max_events``."""
    import asyncio as _aio
    import app.azure.arm as arm

    class _Resp:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload
        def json(self):
            return self._payload

    pages = [
        {"value": [{"id": "e1"}, {"id": "e2"}], "nextLink": "https://management.azure.com/next"},
        {"value": [{"id": "e3"}]},
    ]
    seen = {"urls": []}

    class _Client:
        def __init__(self, *a, **k):
            self._i = 0
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, headers=None, params=None):
            seen["urls"].append((url, params))
            p = pages[self._i]
            self._i += 1
            return _Resp(p)

    monkeypatch.setattr(arm.httpx, "AsyncClient", _Client)
    events, err = _aio.run(arm.list_activity_log_events("T", "sub-1", "s", "e"))
    assert err is None and [x["id"] for x in events] == ["e1", "e2", "e3"]
    # First call carries params (filter); the nextLink follow-up carries the full URL + no params.
    assert seen["urls"][0][1] is not None and "subscriptions/sub-1" in seen["urls"][0][0]
    assert seen["urls"][1][0] == "https://management.azure.com/next" and seen["urls"][1][1] is None


def test_salvage_truncated_json_recovers_complete_rows():
    """A capture truncated mid-array must still yield every COMPLETE row — the bug that turned a
    big change result (1000 rows of diffs > 256 KB) into a misleading '0 changes'."""
    from app.changeexplorer import collectors
    full = [{"targetId": f"/r/{i}", "ct": "Update", "changes": {}} for i in range(5)]
    good = json.dumps(full)
    truncated = good[: good.rindex("}") - 5]   # chop the last object mid-way → invalid JSON
    rows = collectors._parse_rows(truncated)
    assert len(rows) == 4 and rows[0]["targetId"] == "/r/0" and rows[3]["targetId"] == "/r/3"
    assert len(collectors._parse_rows(good)) == 5   # a complete array still parses fully


def test_trim_value_bounds_large_diffs():
    from app.changeexplorer import collectors
    big = "x" * (collectors._MAX_VALUE_CHARS + 500)
    out = collectors._trim_value(big)
    assert out.endswith("…(truncated)") and len(out) <= collectors._MAX_VALUE_CHARS + 20
    assert collectors._trim_value("short") == "short"
    assert collectors._trim_value(None) is None and collectors._trim_value(42) == 42



def test_collect_resource_graph_changes_retries_on_throttle(monkeypatch):
    import asyncio as _aio
    import app.exec.command_runner as cr
    from app.changeexplorer import collectors

    monkeypatch.setattr(collectors, "_backoff_delay", lambda *a, **k: 0.0)
    calls = {"n": 0}

    async def _fake_kql(kql, connection, output="json", **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Cap(ok=False, error="Response 429: rate limit is exceeded")
        return _Cap(ok=True, stdout=json.dumps([
            {"ts": "2026-06-20T00:00:00Z", "ct": "Update", "targetId": "/sub/r1", "name": "r1",
             "type": "microsoft.network/networksecuritygroups", "resourceGroup": "rg",
             "subscriptionId": "sub-0", "location": "eastus", "correlationId": "c1",
             "changes": {"properties.x": {"previousValue": "a", "newValue": "b", "changeType": "Update"}}}
        ]))

    monkeypatch.setattr(cr, "run_kql_capture", _fake_kql)
    rows, note = _aio.run(collectors.collect_resource_graph_changes("type=~'x'", "s", "e", {"id": "c"}))
    assert calls["n"] == 2 and note == "" and len(rows) == 1 and rows[0]["changes"][0]["after"] == "b"


# --------------------------------------------------------------------------- streaming pipeline
def test_analyze_stream_emits_progress_then_done():
    import asyncio as _aio
    from app.demo_catalog import CONTOSO_ID, workload_meta, nodes_for
    from app.changeexplorer import service

    wl = {"id": CONTOSO_ID, "name": workload_meta(CONTOSO_ID)["name"], "nodes": nodes_for(CONTOSO_ID)}

    async def _drain():
        phases, run = [], None
        async for ev in service.analyze_stream(
            tenant_id="t1", workload=wl, connection=None,
            start_iso="2026-06-20T00:00:00+00:00", end_iso="2026-06-21T00:00:00+00:00",
            scope_mode="workload", requested_by="tester",
        ):
            if ev.get("phase") == "done":
                run = ev["run"]
            else:
                phases.append(ev.get("phase"))
        return phases, run

    phases, run = _aio.run(_drain())
    assert "scope" in phases and "normalize" in phases and "insights" in phases
    assert run is not None and run["status"] == "succeeded" and run["totalChanges"] >= 1


# --------------------------------------------------------------------------- risk transparency
def test_risk_tags_forced_low():
    ev = {"category": "TagsMetadata", "operation": "Microsoft.Resources/tags/write", "resourceType": "microsoft.resources/tags", "actorType": "ServicePrincipal"}
    r = risk.score(ev, production=True)
    assert r["score"] <= 15 and r["label"] == "Low"
    assert any("Metadata-only" in f["label"] for f in r["factors"])


def test_risk_appgw_critical():
    ev = {"category": "Network", "operation": "Microsoft.Network/applicationGateways/write",
          "resourceType": "microsoft.network/applicationgateways", "actorType": "ServicePrincipal"}
    r = risk.score(ev, production=True, dependency_role=deps.ROLE_PUBLIC_INGRESS)
    assert r["score"] >= 90 and r["label"] == "Critical"


def test_risk_delete_higher_than_write():
    base = {"category": "Storage", "resourceType": "microsoft.storage/storageaccounts", "actorType": "User"}
    w = risk.score({**base, "operation": "Microsoft.Storage/storageAccounts/write"}, production=True)["score"]
    d = risk.score({**base, "operation": "Microsoft.Storage/storageAccounts/delete"}, production=True)["score"]
    assert d > w


def test_risk_factors_present():
    ev = {"category": "Certificate", "operation": "write", "resourceType": "microsoft.keyvault/vaults/certificates", "actorType": "ServicePrincipal"}
    r = risk.score(ev, production=True, dependency_role=deps.ROLE_SECRET)
    labels = [f["label"] for f in r["factors"]]
    assert any("Base for Certificate" in l for l in labels)
    assert any("Production" in l for l in labels)


# --------------------------------------------------------------------------- deps
def test_dependency_roles():
    assert deps.role_for("microsoft.network/applicationgateways") == deps.ROLE_PUBLIC_INGRESS
    assert deps.role_for("microsoft.keyvault/vaults/certificates") == deps.ROLE_SECRET
    assert deps.role_for("microsoft.network/privatednszones") == deps.ROLE_PRIVATE_NET
    assert deps.role_for("microsoft.sql/servers") == deps.ROLE_DATABASE
    assert deps.blast_radius(deps.ROLE_PUBLIC_INGRESS).startswith("This resource sits on the public ingress")


# --------------------------------------------------------------------------- normalize + explain
def test_normalize_builds_event_and_details():
    raw = demo.raw_changes()[2]  # NSG rule
    ev = normalize_mod.normalize(raw, run_id="r1", tenant_id="t1", workload_id="w1")
    assert ev["runId"] == "r1" and ev["resourceName"] == "nsg-app-prod"
    assert ev["subscriptionId"] == demo.DEMO_SUB
    assert len(ev["details"]) == 1 and ev["details"][0]["propertyPath"].startswith("securityRules")


# --------------------------------------------------------------------------- the demo scenario
def _run_demo():
    return asyncio.run(service.analyze(
        tenant_id="t1", workload=demo.demo_workload(), connection=None,
        start_iso="2026-06-20T13:00:00+00:00", end_iso="2026-06-20T15:00:00+00:00",
        scope_mode="workload", requested_by="tester", force_demo=True,
    ))


def test_demo_scenario_counts():
    run = _run_demo()
    assert run["demo"] is True
    assert run["totalChanges"] == 7
    assert run["criticalCount"] == 1
    assert run["highCount"] == 3
    assert run["mediumCount"] == 1
    assert run["lowCount"] == 2
    assert run["informationalCount"] == 0


def test_demo_scenario_rollups():
    run = _run_demo()
    head = run["headline"]
    assert head["most_active_actor"] == demo.DEMO_ACTOR
    # Highest-risk resource is the App Gateway (the Critical change).
    top = run["resources"][0]
    assert top["resourceName"] == "agw-contoso-prod"
    assert top["highestRiskLabel"] == "Critical"
    # Most risky category resolves to Network (App Gateway = 90).
    assert head["most_risky_category"] == "Network"


def test_demo_each_change_label():
    run = _run_demo()
    by_name = {}
    for e in run["events"]:
        by_name.setdefault(e["resourceName"], e)
    # Spot-check the spec's expected labels.
    assert by_name["agw-contoso-prod"]["riskLabel"] == "Critical"
    assert by_name["nsg-app-prod"]["riskLabel"] == "High"
    assert by_name["kv-contoso-prod"]["riskLabel"] == "High"
    assert by_name["privatelink.database.windows.net"]["riskLabel"] == "High"


def test_demo_events_have_explanations():
    run = _run_demo()
    for e in run["events"]:
        assert e["plainEnglishSummary"]
        assert e["possibleImpact"]
        assert e["whyRisk"]
        assert e["confidence"] in ("High", "Medium", "Low")
        assert e["category"] in __import__("app.changeexplorer.models", fromlist=["CATEGORIES"]).CATEGORIES


def test_demo_insights_and_facets():
    run = _run_demo()
    assert run["insights"], "expected at least one insight"
    types = {i["insightType"] for i in run["insights"]}
    assert "highest_risk" in types
    facets = run["facets"]
    assert "Critical" in facets["risks"]
    assert demo.DEMO_ACTOR in facets["actors"]


# --------------------------------------------------------------------------- export
def test_export_csv_and_high():
    run = _run_demo()
    csv_all = export_mod.to_csv(run["events"])
    assert "eventTime" in csv_all.splitlines()[0]
    assert len(csv_all.strip().splitlines()) == 8  # header + 7
    csv_high = export_mod.to_csv(run["events"], high_risk_only=True)
    assert len(csv_high.strip().splitlines()) == 5  # header + 4 (1 critical + 3 high)


def test_export_reports():
    run = _run_demo()
    assert "Change report" in export_mod.executive_summary(run)
    assert "Technical change summary" in export_mod.technical_summary(run)
    assert "RCA-style" in export_mod.rca_summary(run)
    assert "Change review" in export_mod.servicenow_text(run)
    q = export_mod.validation_queries(run)
    assert "AzureActivity" in q["kql"]


# --------------------------------------------------------------------------- run store
@pytest.fixture()
def _runs_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(runs_store, "_PATH", tmp_path / "changeexplorer_runs.json")


def test_run_store_crud(_runs_tmp):
    run = _run_demo()
    runs_store.save_run("t1", demo.DEMO_WORKLOAD_ID, run)
    rows = runs_store.list_runs("t1", demo.DEMO_WORKLOAD_ID)
    assert len(rows) == 1 and rows[0]["totalChanges"] == 7
    rid = run["runId"]
    assert runs_store.get_run("t1", rid)["runId"] == rid
    assert runs_store.soft_delete("t1", rid) is True
    assert runs_store.list_runs("t1", demo.DEMO_WORKLOAD_ID) == []
    assert runs_store.list_trashed("t1", demo.DEMO_WORKLOAD_ID)[0]["runId"] == rid
    assert runs_store.restore("t1", rid) is True
    assert len(runs_store.list_runs("t1", demo.DEMO_WORKLOAD_ID)) == 1
    assert runs_store.purge("t1", rid) is True
    assert runs_store.get_run("t1", rid, include_deleted=True) is None


# --------------------------------------------------------------------------- catalog demo workloads
def test_is_catalog_demo():
    from app.demo_catalog import CONTOSO_ID, ZAVA_WEB_ID, ZAVA_CRM_ID
    assert demo.is_catalog_demo(CONTOSO_ID)
    assert demo.is_catalog_demo(ZAVA_WEB_ID)
    assert demo.is_catalog_demo(ZAVA_CRM_ID)
    assert not demo.is_catalog_demo("some-real-workload")


def test_catalog_changes_shape():
    from app.demo_catalog import ZAVA_WEB_ID
    rows = demo.catalog_changes(ZAVA_WEB_ID, "2026-06-20T00:00:00+00:00", "2026-06-21T00:00:00+00:00")
    assert rows, "expected synthetic changes for the catalog demo workload"
    # Chronological, well-formed raw rows with operations + before/after diffs.
    times = [r["eventTime"] for r in rows]
    assert times == sorted(times)
    for r in rows:
        assert r["resourceId"] and r["operation"] and r["actor"] and r["changes"]
    # Multiple actor types appear (pipeline SPN, user, policy, MI).
    assert len({r["actorType"] for r in rows}) >= 2


def test_catalog_demo_analysis_runs():
    from app.demo_catalog import CONTOSO_ID, workload_meta, nodes_for
    wl = {"id": CONTOSO_ID, "name": workload_meta(CONTOSO_ID)["name"], "nodes": nodes_for(CONTOSO_ID)}
    run = asyncio.run(service.analyze(
        tenant_id="t1", workload=wl, connection=None,
        start_iso="2026-06-20T00:00:00+00:00", end_iso="2026-06-21T00:00:00+00:00",
        scope_mode="workload", requested_by="tester",
    ))
    assert run["demo"] is True
    assert run["totalChanges"] >= 3
    # Every event is fully enriched (category + plain-English explanation + risk).
    for e in run["events"]:
        assert e["category"] and e["plainEnglishSummary"] and e["riskLabel"]


def test_catalog_demo_tags_have_signal():
    # The shared demo tags must give Tag Intelligence real signal: messy keys + coverage gaps.
    from app.demo_catalog import CONTOSO_ID, resources_for
    from app.tagintel import analysis
    rs = [{"id": r["id"], "name": r["name"], "type": r["type"].lower(), "resource_group": r["resourceGroup"],
           "subscription_id": r["subscriptionId"], "tags": r["tags"], "workloads": []} for r in resources_for(CONTOSO_ID)]
    cen = analysis.census(rs)
    assert cen["distinct_keys"] >= 8
    # Near-duplicate key clusters (CostCenter / costcenter / Cost Center) are present.
    clusters = analysis.key_clusters(rs)
    assert any("costcenter" in c["canonical"].lower() for c in clusters)
    # Environment value variants (Production / Prod / PRD) are present.
    vclusters = analysis.value_clusters(rs)
    assert any(c["key"] == "Environment" for c in vclusters)


# --------------------------------------------------------------------------- subscription scope
def test_resolve_subscription_builds_synthetic_workload():
    from app.api import changeexplorer as cx
    wl, _conn = cx._resolve_subscription("sub-123", "Prod Sub", None)
    assert wl["id"] == "sub:sub-123"
    assert wl["name"] == "Prod Sub"
    assert wl["nodes"][0]["kind"] == "subscription"
    assert wl["nodes"][0]["subscription_id"] == "sub-123"
    # Name falls back to a truncated id when not provided.
    wl2, _ = cx._resolve_subscription("0123456789abcdef", "", None)
    assert wl2["name"].startswith("Subscription 01234567")


def test_subscription_scope_analysis_runs():
    # A subscription scope with no connection yields an empty-but-valid run (no Azure query).
    from app.api import changeexplorer as cx
    wl, conn = cx._resolve_subscription("sub-xyz", "Test Sub", None)
    run = asyncio.run(service.analyze(
        tenant_id="t1", workload=wl, connection=conn,
        start_iso="2026-06-20T00:00:00+00:00", end_iso="2026-06-21T00:00:00+00:00",
        scope_mode="workload", requested_by="tester",
    ))
    assert run["workloadId"] == "sub:sub-xyz"
    assert run["status"] == "succeeded"
