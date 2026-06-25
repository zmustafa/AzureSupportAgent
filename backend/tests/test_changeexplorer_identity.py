"""Change Explorer — actor identity attribution tests (classify, backfill, Graph resolve).

Covers the trust layer of the forensic screen: refining callers/claims into actor kinds,
recognizing Azure platform/automation writes (so they aren't flagged as "unknown actor"),
the proximity backfill that recovers attribution when no correlation id is recorded, and the
fail-open Microsoft Graph object-id -> friendly-name resolution.
"""
import pytest

from app.changeexplorer import identity, insights as insights_mod


# --------------------------------------------------------------------------- classify_actor
def test_classify_user_by_upn():
    kind, platform = identity.classify_actor("P-Zeeshan.Mustafa@contoso.com", {"idtyp": "user"})
    assert kind == "User"
    assert platform is False


def test_classify_user_by_idtyp_without_at():
    kind, _ = identity.classify_actor("Zeeshan Mustafa", {"idtyp": "user"})
    assert kind == "User"


def test_classify_service_principal_app_claim():
    kind, platform = identity.classify_actor(
        "11111111-2222-3333-4444-555555555555",
        {"idtyp": "app", "appid": "99999999-2222-3333-4444-555555555555"},
        correlation_id="abc12345-0000-0000-0000-000000000abc",
    )
    assert kind == "ServicePrincipal"
    assert platform is False


def test_classify_bare_guid_defaults_to_spn():
    kind, _ = identity.classify_actor("e1dd8f92-9741-4a3c-83de-edcc6dc0977a", None,
                                      correlation_id="2ea4c038-b7d0-0000-0000-000000000000")
    assert kind == "ServicePrincipal"


def test_classify_platform_zero_correlation():
    # Empty caller + zero correlation id = Azure-internal cascade write.
    kind, platform = identity.classify_actor("", None, correlation_id=identity._ZERO_GUID)
    assert platform is True
    assert kind == "AzurePlatform"


def test_classify_platform_known_appid():
    kind, platform = identity.classify_actor(
        "00000000-1111-2222-3333-444444444444",
        {"idtyp": "app", "appid": "7319c514-987d-4e9b-ac3d-d38c4f427f4c"},  # Azure Policy
        correlation_id="real-corr-1234",
    )
    assert platform is True
    assert kind == "AzurePlatform"


def test_is_guid():
    assert identity.is_guid("e1dd8f92-9741-4a3c-83de-edcc6dc0977a")
    assert not identity.is_guid("P-Zeeshan.Mustafa@contoso.com")
    assert not identity.is_guid("")


# --------------------------------------------------------------------------- extract_actor_meta
def test_extract_meta_ip_and_oid():
    claims = {
        "idtyp": "user",
        "ipaddr": "52.10.20.30",
        "http://schemas.microsoft.com/identity/claims/objectidentifier": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    }
    meta = identity.extract_actor_meta("P-Tim.Liang@contoso.com", claims, "corr-1")
    assert meta["kind"] == "User"
    assert meta["ip"] == "52.10.20.30"
    assert meta["object_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_extract_meta_on_behalf_of():
    # An app token that still carries a user UPN -> on-behalf-of.
    claims = {"idtyp": "app", "upn": "P-Tim.Liang@contoso.com",
              "appid": "99999999-2222-3333-4444-555555555555"}
    meta = identity.extract_actor_meta("99999999-2222-3333-4444-555555555555", claims, "corr-2")
    assert meta["kind"] == "ServicePrincipal"
    assert meta["on_behalf_of"] == "P-Tim.Liang@contoso.com"
    assert meta["app_id"] == "99999999-2222-3333-4444-555555555555"


# --------------------------------------------------------------------------- insights: platform vs unknown
def test_unknown_actor_insight_excludes_platform():
    events = [
        {"changeId": "c1", "actorType": "Unknown", "actorKind": "AzurePlatform", "riskScore": 30,
         "riskLabel": "Low", "category": "Network", "operation": "Update"},
        {"changeId": "c2", "actorType": "Unknown", "actorKind": "Unknown", "riskScore": 30,
         "riskLabel": "Low", "category": "Compute", "operation": "Update"},
    ]
    out = insights_mod.build_insights("run1", events)
    unknown = [i for i in out if i["insightType"] == "unknown_actor"]
    assert len(unknown) == 1
    # Only the genuinely-unknown change is counted, not the platform one.
    assert "1 change(s)" in unknown[0]["title"]


# --------------------------------------------------------------------------- by_actor rollup
def test_by_actor_groups_on_object_id_and_shows_display():
    events = [
        {"actor": "abc-oid", "actorObjectId": "abc-oid", "actorDisplay": "Contoso Deploy SPN",
         "actorResolved": True, "actorKind": "ServicePrincipal", "actorIp": "10.0.0.1",
         "riskScore": 80, "riskLabel": "High", "category": "Network", "resourceId": "r1",
         "eventTime": "2026-06-25T12:00:00Z"},
        {"actor": "abc-oid", "actorObjectId": "abc-oid", "actorDisplay": "",
         "actorKind": "ServicePrincipal", "riskScore": 40, "riskLabel": "Medium",
         "category": "Compute", "resourceId": "r2", "eventTime": "2026-06-25T12:05:00Z"},
    ]
    rows = insights_mod.by_actor(events)
    assert len(rows) == 1
    r = rows[0]
    assert r["actor"] == "Contoso Deploy SPN"   # resolved name wins
    assert r["actorResolved"] is True
    assert r["changes"] == 2
    assert r["ips"] == ["10.0.0.1"]
    assert r["highestRiskLabel"] == "High"


# --------------------------------------------------------------------------- Graph resolve (fail-open)
@pytest.mark.asyncio
async def test_resolve_display_names_no_token_fails_open(monkeypatch):
    async def _no_token(_conn):
        return None, "no graph token"

    monkeypatch.setattr("app.azure.credentials.get_graph_token", _no_token)
    identity._CACHE.clear()
    out, note = await identity.resolve_display_names(
        ["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"], [], {"tenant_id": "t1"})
    assert out == {}
    assert "Graph" in note or "object-ids" in note


@pytest.mark.asyncio
async def test_resolve_display_names_success(monkeypatch):
    oid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    async def _token(_conn):
        return "fake-token", None

    class _Resp:
        status_code = 200

        def json(self):
            return {"value": [{"id": oid, "displayName": "Network RNM",
                               "@odata.type": "#microsoft.graph.servicePrincipal"}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp()

        async def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr("app.azure.credentials.get_graph_token", _token)
    monkeypatch.setattr("httpx.AsyncClient", _Client)
    identity._CACHE.clear()
    out, note = await identity.resolve_display_names([oid], [], {"tenant_id": "t2"})
    assert out.get(oid, {}).get("display") == "Network RNM"
    assert out[oid]["kind"] == "ServicePrincipal"
    assert note == ""
