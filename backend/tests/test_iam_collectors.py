"""Unit tests for the IAM parity collectors added in P0.5: deny assignments, Key Vault access
policies and classic administrators.

Each collector is exercised over a recorded ARM response shape with ``_get_all`` monkeypatched,
so no test issues a live call. The assertions pin the decisions that are easy to get wrong and
expensive when wrong — a deny counted as a grant, an RBAC vault reported as having legacy
policies, a classic admin silently dropped.
"""
from __future__ import annotations

import pytest

from app.iam import collectors, schema

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _patch_get_all(monkeypatch, value, err=None, code=200):
    async def fake(_token, _url, _params=None):
        return value, err, code

    monkeypatch.setattr(collectors, "_get_all", fake)


# --------------------------------------------------------------------------- deny assignments
_DENY_ONE_PRINCIPAL = [
    {
        "id": "/subscriptions/s1/providers/Microsoft.Authorization/denyAssignments/da1",
        "name": "da1",
        "properties": {
            "denyAssignmentName": "Blueprint lock",
            "scope": "/subscriptions/s1/resourceGroups/rg-locked",
            "principals": [{"id": "u-1", "type": "User"}],
            "excludePrincipals": [],
            "doNotApplyToChildScopes": False,
            "createdOn": "2026-01-01T00:00:00Z",
        },
    }
]


async def test_deny_assignment_is_marked_deny_and_never_privileged(monkeypatch):
    """A deny REMOVES access. Flagging it privileged would inflate the privileged KPI with rows
    that mean the opposite of a grant."""
    _patch_get_all(monkeypatch, _DENY_ONE_PRINCIPAL)
    rows, st = await collectors.collect_deny_assignments(
        "tok", scope="/subscriptions/s1", subscription_id="s1", subscription_name="Sub 1", tenant_id="t1"
    )
    assert st.status == schema.STATUS_SUCCEEDED
    assert len(rows) == 1
    row = rows[0]
    assert row["effect"] == schema.EFFECT_DENY
    assert row["surface"] == schema.SURFACE_DENY
    assert row["accessModel"] == schema.ACCESS_DENY
    assert row["roleIsPrivileged"] is False
    # Scope comes from the deny's own scope, not the query scope.
    assert row["scope"].endswith("/rg-locked")
    assert row["resourceGroup"] == "rg-locked"


async def test_all_principals_deny_is_named_not_left_as_a_guid(monkeypatch):
    """`SystemDefined` is the all-principals wildcard. Rendering a bare id there reads as one
    unresolved user and badly understates the blast radius."""
    payload = [
        {
            "id": "/subscriptions/s1/providers/Microsoft.Authorization/denyAssignments/da2",
            "properties": {
                "denyAssignmentName": "Managed app lock",
                "principals": [{"id": "00000000-0000-0000-0000-000000000000", "type": "SystemDefined"}],
                "excludePrincipals": [{"id": "sp-owner", "type": "ServicePrincipal"}],
            },
        }
    ]
    _patch_get_all(monkeypatch, payload)
    rows, _st = await collectors.collect_deny_assignments(
        "tok", scope="/subscriptions/s1", subscription_id="s1", subscription_name="Sub 1", tenant_id="t1"
    )
    assert rows[0]["principalDisplayName"] == "All principals"
    # The carve-out is surfaced rather than silently dropped.
    assert "excludes 1" in rows[0]["roleName"]


async def test_deny_with_multiple_principals_expands(monkeypatch):
    payload = [
        {
            "id": "da3",
            "properties": {"denyAssignmentName": "Lock", "principals": [{"id": "a", "type": "User"}, {"id": "b", "type": "Group"}]},
        }
    ]
    _patch_get_all(monkeypatch, payload)
    rows, _ = await collectors.collect_deny_assignments(
        "tok", scope="/subscriptions/s1", subscription_id="s1", subscription_name="", tenant_id="t1"
    )
    assert {r["principalId"] for r in rows} == {"a", "b"}


async def test_deny_collector_records_a_permission_failure_without_raising(monkeypatch):
    _patch_get_all(monkeypatch, [], err="HTTP 403: AuthorizationFailed", code=403)
    rows, st = await collectors.collect_deny_assignments(
        "tok", scope="/subscriptions/s1", subscription_id="s1", subscription_name="", tenant_id="t1"
    )
    assert rows == []
    assert st.status == schema.STATUS_UNAUTHORIZED
    assert "403" in st.message


# --------------------------------------------------------------------------- Key Vault policies
def _vault(name: str, *, rbac: bool, policies: list[dict]) -> dict:
    return {
        "id": f"/subscriptions/s1/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/{name}",
        "name": name,
        "properties": {"enableRbacAuthorization": rbac, "accessPolicies": policies},
    }


async def test_rbac_authorization_vaults_are_skipped(monkeypatch):
    """An RBAC vault's grants are ordinary role assignments already collected elsewhere;
    emitting them here would double-count every Key Vault grant."""
    _patch_get_all(monkeypatch, [_vault("kv-rbac", rbac=True, policies=[{"objectId": "u1", "permissions": {"secrets": ["get"]}}])])
    rows, st = await collectors.collect_keyvault_policies(
        "tok", subscription_id="s1", subscription_name="Sub 1", tenant_id="t1"
    )
    assert rows == [] and st.status == schema.STATUS_SUCCEEDED


async def test_legacy_vault_policies_are_collected_with_readable_permissions(monkeypatch):
    _patch_get_all(monkeypatch, [
        _vault("kv-legacy", rbac=False, policies=[
            {"objectId": "u-julia", "permissions": {"secrets": ["get", "list"], "keys": []}},
        ]),
    ])
    rows, st = await collectors.collect_keyvault_policies(
        "tok", subscription_id="s1", subscription_name="Sub 1", tenant_id="t1"
    )
    assert st.status == schema.STATUS_SUCCEEDED and len(rows) == 1
    row = rows[0]
    assert row["surface"] == schema.SURFACE_KEY_VAULT
    assert row["principalId"] == "u-julia"
    assert "secrets(get,list)" in row["roleName"]
    # Empty permission families are omitted rather than rendered as "keys()".
    assert "keys(" not in row["roleName"]
    # Access policies are a data-plane grant model by definition.
    assert row["roleHasDataActions"] is True
    assert row["resourceName"] == "kv-legacy"
    # get/list alone is not privileged.
    assert row["roleIsPrivileged"] is False


@pytest.mark.parametrize("perm,expected", [
    ("get", False), ("list", False), ("purge", True), ("delete", True), ("set", True), ("all", True),
])
async def test_destructive_permissions_mark_a_policy_privileged(monkeypatch, perm, expected):
    _patch_get_all(monkeypatch, [_vault("kv", rbac=False, policies=[{"objectId": "u1", "permissions": {"secrets": [perm]}}])])
    rows, _ = await collectors.collect_keyvault_policies("tok", subscription_id="s1", subscription_name="", tenant_id="t1")
    assert rows[0]["roleIsPrivileged"] is expected


async def test_empty_access_policy_grants_nothing_and_is_dropped(monkeypatch):
    _patch_get_all(monkeypatch, [_vault("kv", rbac=False, policies=[{"objectId": "u1", "permissions": {"secrets": [], "keys": []}}])])
    rows, _ = await collectors.collect_keyvault_policies("tok", subscription_id="s1", subscription_name="", tenant_id="t1")
    assert rows == []


# --------------------------------------------------------------------------- classic admins
async def test_classic_administrators_split_a_multi_role_entry(monkeypatch):
    """ARM returns `role` as a delimited list. Taking it verbatim produces one row labeled
    "ServiceAdministrator;AccountAdministrator", which no filter or pivot can group."""
    _patch_get_all(monkeypatch, [
        {"id": "/subscriptions/s1/.../classicAdministrators/a1",
         "properties": {"emailAddress": "ken@contoso.example", "role": "ServiceAdministrator;AccountAdministrator"}},
        {"id": "/subscriptions/s1/.../classicAdministrators/a2",
         "properties": {"emailAddress": "co@contoso.example", "role": "CoAdministrator"}},
    ])
    rows, st = await collectors.collect_classic_admins(
        "tok", subscription_id="s1", subscription_name="Sub 1", tenant_id="t1"
    )
    assert st.status == schema.STATUS_SUCCEEDED
    assert {r["roleName"] for r in rows} == {"ServiceAdministrator", "AccountAdministrator", "CoAdministrator"}
    # Classic admins are identified by e-mail, not object id — carrying it in both the id and the
    # name keeps them from rendering as nameless orphans.
    assert all(r["principalId"] == r["principalDisplayName"] for r in rows)
    assert all(r["roleIsPrivileged"] for r in rows)
    assert all(r["surface"] == schema.SURFACE_CLASSIC for r in rows)


async def test_classic_admins_unavailable_is_skipped_not_failed(monkeypatch):
    """Subscriptions created after the classic model was retired 404 here. Reporting that as a
    failure sends the operator chasing a permission problem that does not exist."""
    _patch_get_all(monkeypatch, [], err="HTTP 404: NotFound", code=404)
    rows, st = await collectors.collect_classic_admins(
        "tok", subscription_id="s1", subscription_name="", tenant_id="t1"
    )
    assert rows == [] and st.status == schema.STATUS_SKIPPED


async def test_classic_admins_real_permission_error_is_reported(monkeypatch):
    _patch_get_all(monkeypatch, [], err="HTTP 403: AuthorizationFailed", code=403)
    _rows, st = await collectors.collect_classic_admins(
        "tok", subscription_id="s1", subscription_name="", tenant_id="t1"
    )
    assert st.status == schema.STATUS_UNAUTHORIZED


# --------------------------------------------------------------------------- shared contract
@pytest.mark.parametrize("collector_call", ["deny", "keyvault", "classic"])
async def test_every_new_collector_emits_full_schema_rows(monkeypatch, collector_call):
    """A ragged row breaks the grid, the CSV writer and the workbook. `make_row` guarantees the
    full column set — this pins that no collector bypasses it."""
    if collector_call == "deny":
        _patch_get_all(monkeypatch, _DENY_ONE_PRINCIPAL)
        rows, _ = await collectors.collect_deny_assignments(
            "tok", scope="/subscriptions/s1", subscription_id="s1", subscription_name="", tenant_id="t1")
    elif collector_call == "keyvault":
        _patch_get_all(monkeypatch, [_vault("kv", rbac=False, policies=[{"objectId": "u1", "permissions": {"secrets": ["get"]}}])])
        rows, _ = await collectors.collect_keyvault_policies("tok", subscription_id="s1", subscription_name="", tenant_id="t1")
    else:
        _patch_get_all(monkeypatch, [{"id": "a1", "properties": {"emailAddress": "k@x.example", "role": "CoAdministrator"}}])
        rows, _ = await collectors.collect_classic_admins("tok", subscription_id="s1", subscription_name="", tenant_id="t1")

    assert rows, "fixture should produce at least one row"
    for r in rows:
        assert set(r.keys()) == set(schema.COLUMNS)


# --------------------------------------------------------------------------- SSRF / token egress
# Collectors build URLs as f"{_ARM}{scope}/..." with a caller-supplied scope and attach the
# connection's bearer token to every request, so a scope that can move the HOST exfiltrates that
# token. CodeQL flagged this as py/partial-ssrf (alert 554).


class _ExplodingClient:
    """Fails the test if a request is ever constructed. Proves the guard runs BEFORE the token
    is attached, rather than merely discarding the response afterwards."""

    def __init__(self, *a, **kw):
        raise AssertionError("a bearer-token request was issued for a refused host")


@pytest.mark.parametrize(
    "url",
    [
        "https://management.azure.com/subscriptions/s1/providers/Microsoft.Authorization/roleAssignments",
        "https://graph.microsoft.com/v1.0/directoryObjects/getByIds",
    ],
)
def test_legitimate_arm_and_graph_hosts_are_allowed(url):
    assert collectors._host_error(url) is None


@pytest.mark.parametrize(
    "hostile,parsed_host",
    [
        # userinfo: everything before "@" is credentials, so the real host is evil.com
        ("https://management.azure.com@evil.com/providers/x", "evil.com"),
        # registrable domain the attacker can own outright
        ("https://management.azure.com.evil.com/providers/x", "management.azure.com.evil.com"),
    ],
)
def test_host_moving_urls_are_refused(hostile, parsed_host):
    import httpx

    # non-vacuity: the string really does resolve to an attacker host, and the naive prefix
    # test these guards replace would have accepted it.
    assert (httpx.URL(hostile).host or "") == parsed_host
    assert hostile.startswith("https://management.azure.com"), "prefix test would pass this"

    err = collectors._host_error(hostile)
    assert err and "refusing to send a bearer token" in err


def test_plain_http_is_refused_because_the_request_carries_a_token():
    err = collectors._host_error("http://management.azure.com/subscriptions/s1")
    assert err and "https" in err


def test_protocol_relative_scope_does_not_escape_but_is_still_allowed_host():
    """Guards against over-claiming: httpx normalizes "//evil.com" back onto the base host, so
    this is NOT an escape and must not be cited as one."""
    import httpx

    url = f"{collectors._ARM}//evil.com/providers/x"
    assert (httpx.URL(url).host or "") == "management.azure.com"
    assert collectors._host_error(url) is None


async def test_hostile_scope_never_issues_a_token_bearing_request(monkeypatch):
    monkeypatch.setattr(collectors.httpx, "AsyncClient", _ExplodingClient)

    value, err, code = await collectors._get_all(
        "super-secret-token", f"{collectors._ARM}@evil.com/providers/Microsoft.Authorization/roleAssignments"
    )

    assert value == [] and code == 0
    assert err and "refusing to send a bearer token" in err


async def test_graph_post_refuses_a_hostile_host(monkeypatch):
    monkeypatch.setattr(collectors.httpx, "AsyncClient", _ExplodingClient)

    value, err, _code = await collectors._graph_post("super-secret-token", "https://evil.com/getByIds", {})

    assert value == [] and err and "refusing to send a bearer token" in err


async def test_collector_reports_a_hostile_scope_instead_of_leaking_the_token(monkeypatch):
    """End-to-end through a real collector: the refusal surfaces as a normal collector error."""
    monkeypatch.setattr(collectors.httpx, "AsyncClient", _ExplodingClient)

    index, st = await collectors.collect_role_definitions("super-secret-token", "@evil.com")

    assert index == {}
    assert st.status != schema.STATUS_SUCCEEDED
    assert "refusing to send a bearer token" in st.message


async def test_nextlink_to_another_host_is_refused_mid_pagination(monkeypatch):
    """The token is re-sent on every hop, so page 2 is as dangerous as page 1. Pinning to the
    FIRST url's host is not enough -- that host is only trustworthy once allowlisted."""
    pages = {
        f"{collectors._ARM}/subscriptions/s1/providers/Microsoft.Authorization/roleAssignments": {
            "value": [{"id": "a1"}],
            "nextLink": "https://evil.com/steal?page=2",
        }
    }
    seen: list[str] = []

    class _Resp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            seen.append(url)
            return _Resp(pages[url])

    monkeypatch.setattr(collectors.httpx, "AsyncClient", _Client)

    value, err, _code = await collectors._get_all("super-secret-token", next(iter(pages)))

    assert [r["id"] for r in value] == ["a1"], "page 1 should still be returned"
    assert err and "refusing to follow nextLink" in err
    assert "https://evil.com/steal?page=2" not in seen, "the token must never reach evil.com"
