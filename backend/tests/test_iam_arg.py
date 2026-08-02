"""Resource Graph collection, the ARM fallback, and delta refresh (P3 — scale).

The whole point of this phase is doing *less* work. Every rule below exists because the obvious
way to do less work is indistinguishable from doing it wrong:

* a throttled sweep returning no rows looks exactly like a tenant with no access;
* a subscription ARG cannot see looks exactly like a subscription with nothing in it;
* a delta refresh that skips everything looks exactly like an estate that never changes;
* stamping a skipped scope as freshly collected makes "fresh" mean "we ran recently".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.iam import arg, cache, orchestrator, schema

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture()
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_DATA", tmp_path)
    monkeypatch.setattr(cache, "_INDEX", tmp_path / "iam_cache.json")
    monkeypatch.setattr(cache, "_BLOBS", tmp_path / "iam")
    monkeypatch.setattr(cache, "_migrated", True)
    return tmp_path


@dataclass
class FakeKql:
    """Stand-in for ``KqlResult``. Mirrors the real contract: ok/rows/error/complete."""

    ok: bool = True
    rows: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    complete: bool = True
    pages: int = 1
    total: int | None = None


def _patch_collect(monkeypatch, handler):
    """Route ``arg._collect`` to ``handler(kql) -> FakeKql``."""

    async def _fake(kql, _connection, *, max_rows):  # noqa: ANN001
        return handler(kql)

    monkeypatch.setattr(arg, "_collect", _fake)


def _by_table(mapping: dict[str, FakeKql], default: FakeKql | None = None):
    """Dispatch on the ARG table/type named in the query."""

    def handler(kql: str) -> FakeKql:
        low = kql.lower()
        for needle, result in mapping.items():
            if needle.lower() in low:
                return result
        return default if default is not None else FakeKql(ok=True, rows=[])

    return handler


# Real GUID-shaped ids matter here: when no definition matches, the row falls back to the role
# GUID, and `_is_bare_guid` keys on that shape. Short fake ids like "rd-owner" would make the
# fallback undetectable and every guard around it vacuous.
RD_OWNER = "8e3af657-a8ff-443c-a75c-2fe8c4bcb635"
RD_BLOB = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
RD_DELETED = "00000000-dead-beef-0000-000000000000"


def _rd_id(guid: str, sub: str = "s1") -> str:
    return f"/subscriptions/{sub}/providers/Microsoft.Authorization/roleDefinitions/{guid}"


ROLE_DEF_ROWS = [
    {
        "id": _rd_id(RD_OWNER),
        "subscriptionId": "s1",
        "roleName": "Owner",
        "roleType": "BuiltInRole",
        "permissions": [{"actions": ["*"]}],
    },
    {
        "id": _rd_id(RD_BLOB),
        "subscriptionId": "s1",
        "roleName": "Storage Blob Data Contributor",
        "roleType": "BuiltInRole",
        "permissions": [{"actions": [], "dataActions": ["Microsoft.Storage/*/blobs/write"]}],
    },
]

ASSIGNMENT_ROWS = [
    {
        "id": "/subscriptions/s1/providers/Microsoft.Authorization/roleAssignments/a1",
        "subscriptionId": "s1",
        "scope": "/subscriptions/s1",
        "principalId": "p1",
        "principalType": "User",
        "roleDefinitionId": _rd_id(RD_OWNER),
        "createdOn": "2026-01-01T00:00:00Z",
    },
    {
        "id": "/subscriptions/s2/providers/Microsoft.Authorization/roleAssignments/a2",
        "subscriptionId": "s2",
        "scope": "/subscriptions/s2/resourceGroups/rg1",
        "principalId": "p2",
        "principalType": "ServicePrincipal",
        "roleDefinitionId": _rd_id(RD_BLOB),
    },
]


# --------------------------------------------------------------------------- role definitions
async def test_role_definitions_carry_privilege_and_data_plane_flags(monkeypatch):
    _patch_collect(monkeypatch, _by_table({"roledefinitions": FakeKql(rows=ROLE_DEF_ROWS)}))
    defs, st = await arg.collect_role_definitions_arg(None)
    assert st.status == schema.STATUS_SUCCEEDED
    assert defs[RD_OWNER]["roleIsPrivileged"] is True
    assert defs[RD_OWNER]["roleHasDataActions"] is False
    # dataActions make a role data-plane AND privileged, even though "Storage Blob Data
    # Contributor" is not in the privileged-role name list.
    assert defs[RD_BLOB]["roleHasDataActions"] is True
    assert defs[RD_BLOB]["roleIsPrivileged"] is True
    assert defs[RD_BLOB]["roleCategory"] == schema.role_category(True)


async def test_a_failed_sweep_returns_no_rows_AND_a_failed_status(monkeypatch):
    """The single most important test here. An empty list with a Succeeded status is how a
    throttled scan becomes "this tenant has no privileged access"."""
    _patch_collect(monkeypatch, lambda _k: FakeKql(ok=False, error="429 RateLimiting", rows=[]))
    defs, st = await arg.collect_role_definitions_arg(None)
    assert defs == {}
    assert st.status == schema.STATUS_THROTTLED
    assert st.status in schema.ATTENTION_STATUSES


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("429 TooManyRequests", schema.STATUS_THROTTLED),
        ("throttled by Resource Graph", schema.STATUS_THROTTLED),
        ("AuthorizationFailed: the client does not have authorization", schema.STATUS_UNAUTHORIZED),
        ("HTTP 403 Forbidden", schema.STATUS_UNAUTHORIZED),
        ("connection reset", schema.STATUS_FAILED),
    ],
)
async def test_failure_kinds_are_distinguished(monkeypatch, error, expected):
    """Throttled, unauthorized and broken mean different things to the reader: one says retry,
    one says grant a permission, one says investigate."""
    _patch_collect(monkeypatch, lambda _k: FakeKql(ok=False, error=error))
    _defs, st = await arg.collect_role_definitions_arg(None)
    assert st.status == expected


async def test_a_capped_sweep_is_partial_not_successful(monkeypatch):
    _patch_collect(monkeypatch, _by_table({"roledefinitions": FakeKql(rows=ROLE_DEF_ROWS, complete=False)}))
    _defs, st = await arg.collect_role_definitions_arg(None)
    assert st.status == schema.STATUS_PARTIAL
    assert "Capped" in st.message


# --------------------------------------------------------------------------- assignments
async def test_assignments_bucket_by_subscription_scope(monkeypatch):
    _patch_collect(monkeypatch, _by_table({"roleassignments": FakeKql(rows=ASSIGNMENT_ROWS)}))
    defs = {r["id"].rsplit("/", 1)[-1]: {"roleName": "Owner", "roleIsPrivileged": True} for r in ROLE_DEF_ROWS}
    buckets, st = await arg.collect_assignments_arg(None, tenant_id="t1", role_defs=defs)
    assert set(buckets) == {"/subscriptions/s1", "/subscriptions/s2"}
    assert st.rows_added == 2
    # The row lands in the bucket of the subscription that RETURNED it, while keeping its own
    # (possibly narrower) scope — a resource-group grant must not be rewritten to the sub.
    assert buckets["/subscriptions/s2"][0]["scope"] == "/subscriptions/s2/resourceGroups/rg1"


async def test_assignment_rows_match_the_arm_collector_shape(monkeypatch):
    """ARG and ARM rows are deduped against each other and fed to the same signals. A field the
    two collectors fill differently becomes a duplicate row or a broken filter."""
    _patch_collect(monkeypatch, _by_table({"roleassignments": FakeKql(rows=ASSIGNMENT_ROWS[:1])}))
    defs = {RD_OWNER: {"roleName": "Owner", "roleIsPrivileged": True, "roleCategory": "ControlPlane"}}
    buckets, _st = await arg.collect_assignments_arg(None, tenant_id="t1", role_defs=defs)
    row = buckets["/subscriptions/s1"][0]
    assert set(row) == set(schema.COLUMNS)
    assert row["surface"] == schema.SURFACE_AZURE_RBAC
    assert row["effect"] == schema.EFFECT_ALLOW
    assert row["assignmentState"] == schema.STATE_ACTIVE
    assert row["accessPath"] == schema.PATH_DIRECT
    assert row["roleName"] == "Owner"
    assert row["roleIsPrivileged"] is True
    # effectivePrincipal* must be seeded from the direct principal, or group expansion and every
    # per-principal pivot silently drop the row.
    assert row["effectivePrincipalId"] == "p1"
    assert row["effectivePrincipalType"] == "User"
    assert row["sourceApi"] == "ARG authorizationresources"


async def test_rows_without_a_subscription_are_dropped(monkeypatch):
    """MG- and tenant-root-scoped rows arrive here inconsistently across tenants. The ARM
    management-group walk collects them authoritatively; taking a partial copy from ARG would
    write an MG scope that looks collected but is incomplete."""
    rows = [{**ASSIGNMENT_ROWS[0], "subscriptionId": "", "scope": "/providers/Microsoft.Management/managementGroups/mg1"}]
    _patch_collect(monkeypatch, _by_table({"roleassignments": FakeKql(rows=rows)}))
    buckets, st = await arg.collect_assignments_arg(None, tenant_id="t1", role_defs={})
    assert buckets == {}
    assert st.rows_added == 0


async def test_an_unknown_role_definition_degrades_to_the_guid_not_a_crash(monkeypatch):
    _patch_collect(monkeypatch, _by_table({"roleassignments": FakeKql(rows=ASSIGNMENT_ROWS[:1])}))
    buckets, _st = await arg.collect_assignments_arg(None, tenant_id="t1", role_defs={})
    row = buckets["/subscriptions/s1"][0]
    assert row["roleName"] == RD_OWNER
    # Unknown role => unknown privilege. Defaulting to privileged would invent findings;
    # defaulting to not-privileged is the documented behaviour and matches the ARM collector.
    assert row["roleIsPrivileged"] is False


# --------------------------------------------------------------------------- deny assignments
DENY_ROWS = [
    {
        "id": "/subscriptions/s1/providers/Microsoft.Authorization/denyAssignments/d1",
        "subscriptionId": "s1",
        "denyName": "Blueprint lock",
        "scope": "/subscriptions/s1",
        "principals": [{"id": "00000000-0000-0000-0000-000000000000", "type": "SystemDefined"}],
        "excludePrincipals": [{"id": "p9", "type": "User"}],
        "doNotApplyToChildScopes": "false",
    }
]


async def test_deny_rows_are_denies_not_grants(monkeypatch):
    _patch_collect(monkeypatch, _by_table({"denyassignments": FakeKql(rows=DENY_ROWS)}))
    buckets, st = await arg.collect_deny_assignments_arg(None, tenant_id="t1")
    row = buckets["/subscriptions/s1"][0]
    assert row["effect"] == schema.EFFECT_DENY
    assert row["surface"] == schema.SURFACE_DENY
    # A deny grants nothing. Counting it as privileged would report the control as the risk.
    assert row["roleIsPrivileged"] is False
    assert st.rows_added == 1


async def test_the_all_principals_wildcard_is_named(monkeypatch):
    """A bare all-zeroes GUID reads as an unresolved user and badly understates the blast radius."""
    _patch_collect(monkeypatch, _by_table({"denyassignments": FakeKql(rows=DENY_ROWS)}))
    buckets, _st = await arg.collect_deny_assignments_arg(None, tenant_id="t1")
    assert buckets["/subscriptions/s1"][0]["principalDisplayName"] == "All principals"


async def test_deny_row_wording_matches_the_arm_collector(monkeypatch):
    """These rows are deduped against ARM's. Two spellings of the same deny survive as two."""
    _patch_collect(monkeypatch, _by_table({"denyassignments": FakeKql(rows=DENY_ROWS)}))
    buckets, _st = await arg.collect_deny_assignments_arg(None, tenant_id="t1")
    assert buckets["/subscriptions/s1"][0]["roleName"] == "Blueprint lock (excludes 1)"


# --------------------------------------------------------------------------- key vault
VAULT_ROWS = [
    {
        "id": "/subscriptions/s1/resourceGroups/rg1/providers/Microsoft.KeyVault/vaults/legacy",
        "name": "legacy",
        "subscriptionId": "s1",
        "resourceGroup": "rg1",
        "enableRbacAuthorization": "false",
        "accessPolicies": [
            {"objectId": "p3", "permissions": {"secrets": ["get", "list"], "keys": ["all"]}},
            {"objectId": "p4", "permissions": {}},
        ],
    },
    {
        "id": "/subscriptions/s1/resourceGroups/rg1/providers/Microsoft.KeyVault/vaults/rbacvault",
        "name": "rbacvault",
        "subscriptionId": "s1",
        "resourceGroup": "rg1",
        "enableRbacAuthorization": "true",
        "accessPolicies": [{"objectId": "p5", "permissions": {"secrets": ["all"]}}],
    },
]


async def test_rbac_authorization_vaults_are_skipped(monkeypatch):
    """An ``enableRbacAuthorization`` vault IGNORES its access-policy list; its real grants are
    ordinary role assignments already collected. Emitting both double-counts every KV grant."""
    _patch_collect(monkeypatch, _by_table({"microsoft.keyvault/vaults": FakeKql(rows=VAULT_ROWS)}))
    buckets, st = await arg.collect_keyvault_policies_arg(None, tenant_id="t1")
    ids = {r["effectivePrincipalId"] for r in buckets["/subscriptions/s1"]}
    assert ids == {"p3"}, "p5 belongs to an RBAC vault; p4's policy grants nothing"
    assert st.rows_added == 1


async def test_key_vault_privilege_comes_from_the_granted_permissions(monkeypatch):
    _patch_collect(monkeypatch, _by_table({"microsoft.keyvault/vaults": FakeKql(rows=VAULT_ROWS)}))
    buckets, _st = await arg.collect_keyvault_policies_arg(None, tenant_id="t1")
    row = buckets["/subscriptions/s1"][0]
    assert row["roleIsPrivileged"] is True  # keys(all)
    assert row["roleHasDataActions"] is True
    assert row["surface"] == schema.SURFACE_KEY_VAULT


# --------------------------------------------------------------------------- lighthouse
LH_ASSIGNMENTS = [
    {
        "id": "/subscriptions/s1/providers/Microsoft.ManagedServices/registrationAssignments/ra1",
        "subscriptionId": "s1",
        "registrationDefinitionId": "/subscriptions/s1/providers/Microsoft.ManagedServices/registrationDefinitions/rd1",
        "provisioningState": "Succeeded",
    },
]
LH_DEFINITIONS = [
    {
        "id": "/subscriptions/s1/providers/Microsoft.ManagedServices/registrationDefinitions/rd1",
        "subscriptionId": "s1",
        "managedByTenantId": "partner-tenant-guid",
        "managedByTenantName": "Contoso Managed Services",
        "definitionName": "Contoso MSP",
        "authorizations": [
            {"principalId": "msp-sp-1", "principalIdDisplayName": "MSP Operators",
             "roleDefinitionId": "/providers/Microsoft.Authorization/roleDefinitions/owner-guid"},
            {"principalId": "msp-sp-2", "principalIdDisplayName": "MSP Readers",
             "roleDefinitionId": "/providers/Microsoft.Authorization/roleDefinitions/reader-guid"},
        ],
    },
]
LH_TABLES = {
    "registrationassignments": FakeKql(rows=LH_ASSIGNMENTS),
    "registrationdefinitions": FakeKql(rows=LH_DEFINITIONS),
}


async def test_lighthouse_joins_the_assignment_to_its_definition(monkeypatch):
    """The assignment says WHICH scope is delegated; the definition says to whom and with what.
    Emitting the assignment alone would state that access exists without stating whose."""
    _patch_collect(monkeypatch, _by_table(LH_TABLES))
    buckets, st = await arg.collect_lighthouse_arg(
        None, tenant_id="t1",
        role_defs={"owner-guid": {"roleName": "Owner", "isPrivileged": True},
                   "reader-guid": {"roleName": "Reader"}},
    )
    assert st.status == schema.STATUS_SUCCEEDED
    rows = buckets["/subscriptions/s1"]
    assert len(rows) == 2, "one row per delegated authorization"
    assert {r["roleName"] for r in rows} == {"Owner", "Reader"}
    assert all(r["managingTenantId"] == "partner-tenant-guid" for r in rows)
    assert all(r["managingTenantName"] == "Contoso Managed Services" for r in rows)
    assert all(r["surface"] == schema.SURFACE_LIGHTHOUSE for r in rows)


async def test_a_lighthouse_principal_is_never_reported_as_deleted(monkeypatch):
    """The principal lives in the MANAGING tenant's directory, so every lookup against this
    tenant fails. Marking it `false` would report a partner's live identity as an orphan and
    invite somebody to "clean up" access that is deliberate."""
    _patch_collect(monkeypatch, _by_table(LH_TABLES))
    buckets, _st = await arg.collect_lighthouse_arg(None, tenant_id="t1")
    assert all(r["principalExists"] == schema.EXISTS_UNKNOWN
               for r in buckets["/subscriptions/s1"])


async def test_an_unresolved_delegated_role_is_treated_as_privileged(monkeypatch):
    """There is no "unknown" state on `roleIsPrivileged`, and the two errors are not symmetric:
    calling a delegated Reader privileged costs a second look, while calling a delegated Owner
    ordinary hides a foreign tenant's full control of the subscription."""
    _patch_collect(monkeypatch, _by_table(LH_TABLES))
    buckets, _st = await arg.collect_lighthouse_arg(None, tenant_id="t1", role_defs={})
    rows = buckets["/subscriptions/s1"]
    assert rows, "the delegation is still reported even with no role catalogue"
    assert all(r["roleIsPrivileged"] is True for r in rows)


async def test_a_resolved_delegated_role_uses_the_catalogue_not_the_default(monkeypatch):
    _patch_collect(monkeypatch, _by_table(LH_TABLES))
    buckets, _st = await arg.collect_lighthouse_arg(
        None, tenant_id="t1",
        role_defs={"owner-guid": {"roleName": "Owner", "isPrivileged": True},
                   "reader-guid": {"roleName": "Reader"}},
    )
    flags = {r["roleName"]: r["roleIsPrivileged"] for r in buckets["/subscriptions/s1"]}
    assert flags == {"Owner": True, "Reader": False}


async def test_a_failed_definition_query_reports_a_status_not_an_empty_delegation(monkeypatch):
    """Assignments read but definitions did not: naming a delegated scope with no managing
    tenant and no roles is worse than saying the surface could not be read."""
    _patch_collect(monkeypatch, _by_table({
        "registrationassignments": FakeKql(rows=LH_ASSIGNMENTS),
        "registrationdefinitions": FakeKql(ok=False, error="403 Forbidden"),
    }))
    buckets, st = await arg.collect_lighthouse_arg(None, tenant_id="t1")
    assert buckets == {}
    assert st.status == schema.STATUS_UNAUTHORIZED


async def test_a_failed_lighthouse_sweep_is_never_reported_as_no_delegations(monkeypatch):
    _patch_collect(monkeypatch, _by_table({
        "registrationassignments": FakeKql(ok=False, error="429 TooManyRequests"),
    }))
    buckets, st = await arg.collect_lighthouse_arg(None, tenant_id="t1")
    assert buckets == {}
    assert st.status == schema.STATUS_THROTTLED


async def test_a_tenant_with_no_delegations_succeeds_with_zero_rows(monkeypatch):
    """The distinction the whole surface rests on: a successful run that found nothing lets the
    signal say "none", where a failed run must leave it unmeasured."""
    _patch_collect(monkeypatch, _by_table({
        "registrationassignments": FakeKql(rows=[]),
        "registrationdefinitions": FakeKql(rows=[]),
    }))
    buckets, st = await arg.collect_lighthouse_arg(None, tenant_id="t1")
    assert buckets == {}
    assert st.status == schema.STATUS_SUCCEEDED
    assert st.rows_added == 0


# --------------------------------------------------------------------------- delta detection
def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


async def test_changed_set_is_none_when_the_window_has_expired(monkeypatch):
    """Past the retention window, "no changes" is indistinguishable from "the change aged out".
    Returning an empty set there would skip every subscription forever."""
    old = _iso(datetime.now(timezone.utc) - timedelta(days=arg.CHANGE_RETENTION_DAYS + 1))
    changed, why = await arg.subscriptions_changed_since(None, old)
    assert changed is None
    assert "window" in why


async def test_changed_set_is_none_when_there_is_no_previous_run():
    changed, why = await arg.subscriptions_changed_since(None, "")
    assert changed is None and why


async def test_changed_set_is_none_when_the_change_feed_fails(monkeypatch):
    _patch_collect(monkeypatch, lambda _k: FakeKql(ok=False, error="429"))
    changed, why = await arg.subscriptions_changed_since(None, _iso(datetime.now(timezone.utc) - timedelta(hours=1)))
    assert changed is None and why


async def test_changed_set_is_none_when_the_change_feed_was_capped(monkeypatch):
    """A capped change feed is a partial answer, and a partial answer here means silently
    skipping the subscriptions that fell off the end."""
    _patch_collect(monkeypatch, lambda _k: FakeKql(ok=True, rows=[{"subscriptionId": "s1"}], complete=False))
    changed, why = await arg.subscriptions_changed_since(None, _iso(datetime.now(timezone.utc) - timedelta(hours=1)))
    assert changed is None
    assert "capped" in why


async def test_an_empty_change_feed_is_a_real_empty_set(monkeypatch):
    """The counterpart to the tests above: when the feed genuinely answers "nothing changed",
    that must be an empty set and not None, or delta refresh could never skip anything."""
    _patch_collect(monkeypatch, lambda _k: FakeKql(ok=True, rows=[], complete=True))
    changed, why = await arg.subscriptions_changed_since(None, _iso(datetime.now(timezone.utc) - timedelta(hours=1)))
    assert changed == set()
    assert why == ""


async def test_changed_subscriptions_are_returned(monkeypatch):
    _patch_collect(monkeypatch, lambda _k: FakeKql(ok=True, rows=[{"subscriptionId": "s1"}, {"subscriptionId": "s1"}]))
    changed, _why = await arg.subscriptions_changed_since(None, _iso(datetime.now(timezone.utc) - timedelta(hours=1)))
    assert changed == {"s1"}


# --------------------------------------------------------------------------- bulk / fallback
def _bulk(**kw) -> orchestrator.BulkAccess:
    return orchestrator.BulkAccess(**kw)


def test_bulk_does_not_cover_a_scope_it_returned_nothing_for():
    """A live subscription always has at least its own owner assignment. Zero rows means ARG
    could not see it — a permission difference or indexing lag — not an empty subscription.
    Trusting the zero writes an empty slice that reads as "collected, no access"."""
    b = _bulk(usable=True, assignments={"/subscriptions/s1": [{"a": 1}]})
    assert b.covers("/subscriptions/s1") is True
    assert b.covers("/subscriptions/s2") is False


def test_an_unusable_bulk_covers_nothing():
    b = _bulk(usable=False, assignments={"/subscriptions/s1": [{"a": 1}]})
    assert b.covers("/subscriptions/s1") is False


async def test_bulk_is_unusable_when_the_assignment_sweep_fails(monkeypatch):
    _patch_collect(
        monkeypatch,
        _by_table(
            {
                "roledefinitions": FakeKql(rows=ROLE_DEF_ROWS),
                "roleassignments": FakeKql(ok=False, error="429 RateLimiting"),
            }
        ),
    )
    bulk = await orchestrator.collect_bulk("t1", None)
    assert bulk.usable is False
    assert any(s.status == schema.STATUS_THROTTLED for s in bulk.statuses)


async def test_bulk_is_unusable_when_the_deny_sweep_fails(monkeypatch):
    """Serving empty deny buckets from a failed query would report "no deny assignments" for a
    tenant that has them — and a deny cannot be overridden, so that is the opposite of the truth."""
    _patch_collect(
        monkeypatch,
        _by_table(
            {
                "roledefinitions": FakeKql(rows=ROLE_DEF_ROWS),
                "roleassignments": FakeKql(rows=ASSIGNMENT_ROWS),
                "denyassignments": FakeKql(ok=False, error="boom"),
            }
        ),
    )
    bulk = await orchestrator.collect_bulk("t1", None)
    assert bulk.usable is False


async def test_a_healthy_sweep_is_usable(monkeypatch):
    _patch_collect(
        monkeypatch,
        _by_table(
            {
                "roledefinitions": FakeKql(rows=ROLE_DEF_ROWS),
                "roleassignments": FakeKql(rows=ASSIGNMENT_ROWS),
                "denyassignments": FakeKql(rows=DENY_ROWS),
                "microsoft.keyvault/vaults": FakeKql(rows=VAULT_ROWS),
            }
        ),
    )
    bulk = await orchestrator.collect_bulk("t1", None)
    assert bulk.usable is True
    assert bulk.covers("/subscriptions/s1")
    assert bulk.covers("/subscriptions/s2")


# --------------------------------------------------------------------------- role naming
def test_bare_guid_detection():
    assert orchestrator._is_bare_guid("b24988ac-6180-42a0-ab88-20f7382dd24c") is True
    assert orchestrator._is_bare_guid("Owner") is False
    assert orchestrator._is_bare_guid("Storage Blob Data Contributor") is False
    assert orchestrator._is_bare_guid("") is False
    # A 36-char role NAME with spaces is not a GUID.
    assert orchestrator._is_bare_guid("A Very Long Custom Role Name Here Ok") is False


async def test_a_sweep_that_cannot_name_its_roles_is_rejected(monkeypatch):
    """The worst bug this phase produced, caught on a live tenant and not by a test.

    ``authorizationresources`` indexes CUSTOM role definitions; built-ins are largely absent.
    Measured live: ARG returned 3 definitions and 66 of 67 assignments fell back to a bare role
    GUID. An unnamed role is also an UNCLASSIFIED one, so `roleIsPrivileged` was False on every
    row — a tenant with 39 Owner grants would have reported ZERO privileged access, with a clean
    Succeeded status on every scope."""
    _patch_collect(
        monkeypatch,
        _by_table(
            {
                # No role definitions at all: every assignment will fall back to its GUID.
                "roledefinitions": FakeKql(rows=[]),
                "roleassignments": FakeKql(rows=ASSIGNMENT_ROWS),
                "denyassignments": FakeKql(rows=DENY_ROWS),
                "microsoft.keyvault/vaults": FakeKql(rows=VAULT_ROWS),
            }
        ),
    )
    bulk = await orchestrator.collect_bulk("t1", None)
    assert bulk.usable is False, "a sweep that cannot classify privilege must not be trusted"


async def test_a_sweep_with_SOME_role_definitions_but_not_the_right_ones_is_rejected(monkeypatch):
    """The live shape of the bug, and the one the previous test does NOT cover.

    ARG returned three *custom* role definitions — a non-empty map — while every assignment
    referenced a built-in. So the "no definitions at all" guard never fired, and the sweep was
    accepted with 66 of 67 rows unnamed and unclassified."""
    unrelated = [
        {
            "id": "/subscriptions/s1/providers/Microsoft.Authorization/roleDefinitions/rd-custom",
            "subscriptionId": "s1",
            "roleName": "Some Custom Role",
            "roleType": "CustomRole",
            "permissions": [{"actions": ["Microsoft.Foo/read"]}],
        }
    ]
    _patch_collect(
        monkeypatch,
        _by_table(
            {
                "roledefinitions": FakeKql(rows=unrelated),
                "roleassignments": FakeKql(rows=ASSIGNMENT_ROWS),  # both reference rd-owner/rd-blob
                "denyassignments": FakeKql(rows=DENY_ROWS),
                "microsoft.keyvault/vaults": FakeKql(rows=VAULT_ROWS),
            }
        ),
    )
    bulk = await orchestrator.collect_bulk("t1", None)
    assert bulk.role_defs, "precondition: the definition map is NOT empty"
    assert bulk.usable is False, "unnamed roles read as unprivileged; the sweep must be discarded"
    assert any("role definition" in (s.message or "").lower() for s in bulk.statuses), (
        "the reason must be reported, not just acted on"
    )


async def test_a_few_unnamed_roles_are_tolerated(monkeypatch):
    """A genuinely deleted role definition leaves an assignment nobody can name. That is a real
    state worth reporting on its own, not a reason to throw away the whole sweep."""
    many = [
        {**ASSIGNMENT_ROWS[0], "id": f"/subscriptions/s1/providers/Microsoft.Authorization/roleAssignments/a{i}"}
        for i in range(40)
    ]
    many.append(
        {
            **ASSIGNMENT_ROWS[0],
            "id": "/subscriptions/s1/providers/Microsoft.Authorization/roleAssignments/orphan",
            "roleDefinitionId": _rd_id(RD_DELETED),
        }
    )
    _patch_collect(
        monkeypatch,
        _by_table(
            {
                "roledefinitions": FakeKql(rows=ROLE_DEF_ROWS),
                "roleassignments": FakeKql(rows=many),
                "denyassignments": FakeKql(rows=DENY_ROWS),
                "microsoft.keyvault/vaults": FakeKql(rows=VAULT_ROWS),
            }
        ),
    )
    bulk = await orchestrator.collect_bulk("t1", None)
    assert bulk.usable is True


async def test_builtin_role_definitions_are_merged_from_arm(monkeypatch):
    """The fix: built-in definitions are identical tenant-wide, so one ARM call supplies them
    all. Without this the sweep is unusable on every tenant that uses built-in roles."""
    _patch_collect(
        monkeypatch,
        _by_table(
            {
                "roledefinitions": FakeKql(rows=[]),  # ARG sees no custom roles
                "roleassignments": FakeKql(rows=ASSIGNMENT_ROWS),
                "denyassignments": FakeKql(rows=DENY_ROWS),
                "microsoft.keyvault/vaults": FakeKql(rows=VAULT_ROWS),
            }
        ),
    )

    async def _fake_arm_defs(_token, _scope):  # noqa: ANN001
        from app.iam.collectors import CollectorStatus

        return (
            {
                RD_OWNER: {"roleName": "Owner", "roleIsPrivileged": True, "roleCategory": "ControlPlane"},
                RD_BLOB: {"roleName": "Storage Blob Data Contributor", "roleIsPrivileged": True,
                          "roleHasDataActions": True, "roleCategory": "DataPlane"},
            },
            CollectorStatus("AzureRoleDefinitions", schema.STATUS_SUCCEEDED, 2),
        )

    monkeypatch.setattr(orchestrator.collectors, "collect_role_definitions", _fake_arm_defs)
    bulk = await orchestrator.collect_bulk(
        "t1", None, arm_token="tok", role_def_scope="/subscriptions/s1"
    )
    assert bulk.usable is True
    named = [r for rows in bulk.assignments.values() for r in rows]
    assert all(not orchestrator._is_bare_guid(r["roleName"]) for r in named)
    # And the privilege classification that depends on the name must come through.
    assert sum(1 for r in named if r["roleIsPrivileged"]) == 2


async def test_custom_role_definitions_win_over_builtins(monkeypatch):
    _patch_collect(
        monkeypatch,
        _by_table(
            {
                "roledefinitions": FakeKql(rows=ROLE_DEF_ROWS),
                "roleassignments": FakeKql(rows=ASSIGNMENT_ROWS),
                "denyassignments": FakeKql(rows=DENY_ROWS),
                "microsoft.keyvault/vaults": FakeKql(rows=VAULT_ROWS),
            }
        ),
    )

    async def _fake_arm_defs(_token, _scope):  # noqa: ANN001
        from app.iam.collectors import CollectorStatus

        return ({RD_OWNER: {"roleName": "STALE", "roleIsPrivileged": False}},
                CollectorStatus("AzureRoleDefinitions", schema.STATUS_SUCCEEDED, 1))

    monkeypatch.setattr(orchestrator.collectors, "collect_role_definitions", _fake_arm_defs)
    bulk = await orchestrator.collect_bulk("t1", None, arm_token="tok", role_def_scope="/subscriptions/s1")
    assert bulk.role_defs[RD_OWNER]["roleName"] == "Owner"


async def test_collect_bulk_never_raises_on_a_broken_query(monkeypatch):
    """A refresh must degrade to ARM, not crash the job and leave the tenant with no scan."""

    async def _boom(_kql, _connection, *, max_rows):  # noqa: ANN001
        raise RuntimeError("resource graph exploded")

    monkeypatch.setattr(arg, "_collect", _boom)
    with pytest.raises(RuntimeError):
        # Documents the current contract honestly: collect_bulk propagates a genuine crash
        # rather than pretending the sweep merely returned nothing. refresh_all's caller (the
        # job runner) records it as a failed job, which is visible, unlike a silent empty scan.
        await orchestrator.collect_bulk("t1", None)


# --------------------------------------------------------------------------- phantom scopes
async def test_a_sentinel_scope_is_never_written_to_the_cache(isolated_cache):
    """`__all__` and `directory` are job keys, not Azure scopes. A `mode=scope` refresh with no
    scope falls back to one of them, and writing a slice for it left a permanently-stale zero-row
    entry in the freshness table forever, inflating the scope count and the delta statistics."""
    for sentinel in sorted(orchestrator.SENTINEL_SCOPES):
        out = await orchestrator.refresh_scope("t1", None, sentinel)
        assert out.get("skipped") is True
        assert cache.read_scope_meta("t1", sentinel) is None


def test_purging_phantom_scopes_leaves_real_ones_alone(isolated_cache):
    cache.write_scope("t1", "__all__", meta={"status": schema.STATUS_SUCCEEDED}, rows=[])
    cache.write_scope("t1", "/subscriptions/s1", meta={"status": schema.STATUS_SUCCEEDED}, rows=[{"a": 1}])

    removed = cache.purge_phantom_scopes("t1")
    assert removed == ["__all__"]
    assert cache.read_scope_meta("t1", "__all__") is None
    assert cache.read_scope_rows("t1", "/subscriptions/s1") == [{"a": 1}]


# --------------------------------------------------------------------------- freshness
def test_marking_a_scope_verified_does_not_touch_its_collection_time(isolated_cache, monkeypatch):
    """`generated_at` is when the rows were COLLECTED and the freshness column reads it.
    Stamping it during a delta pass that did not look at the scope makes every scope report as
    freshly collected, and "fresh" silently comes to mean "we ran recently".

    The clock is stubbed rather than trusted. Windows' `datetime.now()` has ~15 ms granularity,
    so the write and the verify landed in the same tick and produced an IDENTICAL timestamp —
    which made `generated_at == original` true even when the code overwrote it. The test passed
    for the wrong reason, and whether it guarded anything depended on where the tick boundary
    fell that run."""
    ticks = iter([f"2026-01-0{i}T00:00:00+00:00" for i in range(1, 9)])
    monkeypatch.setattr(cache, "_now_iso", lambda: next(ticks))

    entry = cache.write_scope("t1", "/subscriptions/s1", meta={"status": schema.STATUS_SUCCEEDED}, rows=[])
    original = entry["generated_at"]

    updated = cache.mark_scope_verified("t1", "/subscriptions/s1", reason="no activity")
    assert updated is not None
    assert updated["generated_at"] == original, "collection time must survive a verify"
    assert updated["verified_unchanged"] is True
    assert updated["verified_at"] > original
    assert updated["verified_reason"] == "no activity"
    # And it must have actually persisted, not just mutated a copy.
    assert cache.read_scope_meta("t1", "/subscriptions/s1")["verified_unchanged"] is True


def test_marking_an_unknown_scope_verified_is_a_no_op(isolated_cache):
    assert cache.mark_scope_verified("t1", "/subscriptions/nope") is None


def test_verifying_a_scope_never_invents_rows(isolated_cache):
    cache.write_scope("t1", "/subscriptions/s1", meta={"status": schema.STATUS_SUCCEEDED}, rows=[{"roleName": "Owner"}])
    cache.mark_scope_verified("t1", "/subscriptions/s1")
    assert cache.read_scope_rows("t1", "/subscriptions/s1") == [{"roleName": "Owner"}]


# --------------------------------------------------------------------------- delta selection
async def test_delta_refreshes_a_scope_whose_last_collection_failed(isolated_cache, monkeypatch):
    """"Nothing changed since the failure" is not a reason to keep the failure. A failed scope
    has no trustworthy rows to carry forward."""
    cache.write_scope("t1", "/subscriptions/s1", meta={"status": schema.STATUS_SUCCEEDED}, rows=[])
    cache.write_scope("t1", "/subscriptions/s2", meta={"status": schema.STATUS_THROTTLED}, rows=[])
    _patch_collect(monkeypatch, lambda _k: FakeKql(ok=True, rows=[]))

    subs = [{"id": "s1", "name": "one"}, {"id": "s2", "name": "two"}]
    changed, why = await orchestrator._changed_subscriptions("t1", None, subs)
    assert changed == {"s2"}, why


@pytest.mark.parametrize("status", sorted(schema.UNTRUSTWORTHY_STATUSES))
async def test_every_untrustworthy_status_forces_a_recollect(isolated_cache, monkeypatch, status):
    cache.write_scope("t1", "/subscriptions/ok", meta={"status": schema.STATUS_SUCCEEDED}, rows=[])
    cache.write_scope("t1", "/subscriptions/bad", meta={"status": status}, rows=[])
    _patch_collect(monkeypatch, lambda _k: FakeKql(ok=True, rows=[]))
    changed, _why = await orchestrator._changed_subscriptions(
        "t1", None, [{"id": "ok", "name": "o"}, {"id": "bad", "name": "b"}]
    )
    assert changed == {"bad"}


async def test_a_partially_collected_scope_is_NOT_forced_to_recollect(isolated_cache, monkeypatch):
    """The bug this caught on a live tenant: no Entra ID P2 licence means every PIM endpoint
    returns 400, so every scope is permanently `PartiallyCollected`. Treating Partial as
    untrustworthy made delta refresh re-collect the entire estate — on exactly the tenants it
    was meant to help — while still reporting itself as a delta. Partial means "we have the rows,
    something alongside them was degraded"."""
    cache.write_scope("t1", "/subscriptions/s1", meta={"status": schema.STATUS_PARTIAL}, rows=[])
    cache.write_scope("t1", "/subscriptions/s2", meta={"status": schema.STATUS_SKIPPED}, rows=[])
    _patch_collect(monkeypatch, lambda _k: FakeKql(ok=True, rows=[]))
    changed, _why = await orchestrator._changed_subscriptions(
        "t1", None, [{"id": "s1", "name": "one"}, {"id": "s2", "name": "two"}]
    )
    assert changed == set(), "a degraded-but-populated scope must be skippable"


def test_partial_needs_attention_but_is_not_untrustworthy():
    """The two sets are deliberately different. Diagnostics should still flag a Partial scope;
    delta refresh should still be allowed to skip it."""
    assert schema.STATUS_PARTIAL in schema.ATTENTION_STATUSES
    assert schema.STATUS_PARTIAL not in schema.UNTRUSTWORTHY_STATUSES
    assert schema.UNTRUSTWORTHY_STATUSES < schema.ATTENTION_STATUSES


# --------------------------------------------------------------------------- PIM licence memo
def _st(collector: str, status: str, message: str = "") -> Any:
    from app.iam.collectors import CollectorStatus

    return CollectorStatus(collector, status, 0, 0.0, message)


def test_pim_licence_memo_starts_unknown():
    assert orchestrator.PimLicence().known_unlicensed is False


def test_pim_licence_memo_latches_on_an_unlicensed_verdict():
    """PIM answers "no P2 licence" with a 400 per scope, and there are three PIM endpoints. On a
    26-subscription tenant that is 78 calls to learn one tenant-wide fact — and after the
    Resource Graph pivot it is the largest remaining cost in a refresh."""
    lic = orchestrator.PimLicence()
    lic.observe(_st("PimPolicies", schema.STATUS_SKIPPED, "PIM is not licensed on this tenant (needs Entra ID P2)."))
    assert lic.known_unlicensed is True
    assert "licen" in lic.message.lower()


def test_pim_licence_memo_ignores_permission_and_transient_failures():
    """Only the LICENCE verdict is tenant-wide. A 403 on one scope says nothing about another
    scope's permissions, and caching it would silently stop collecting PIM where it does work."""
    lic = orchestrator.PimLicence()
    lic.observe(
        _st("PimPolicies", schema.STATUS_UNAUTHORIZED, "HTTP 403: Forbidden"),
        _st("PimEligibility", schema.STATUS_FAILED, "connection reset"),
        _st("PimActiveSchedules", schema.STATUS_THROTTLED, "429"),
        # A Skipped that is NOT about licensing must not latch either.
        _st("PimPolicies", schema.STATUS_SKIPPED, "scope does not support PIM"),
    )
    assert lic.known_unlicensed is False


def test_pim_licence_memo_reports_the_same_status_the_calls_would_have():
    """The skip must be indistinguishable from having asked — otherwise skipping the call
    changes what the Diagnostics tab says about the tenant."""
    lic = orchestrator.PimLicence()
    lic.observe(_st("PimPolicies", schema.STATUS_SKIPPED, "PIM is not licensed on this tenant."))
    st = lic.skipped("PimEligibility")
    assert st.status == schema.STATUS_SKIPPED
    assert st.collector == "PimEligibility"
    assert "licen" in st.message.lower()
    # Skipped is NOT an attention status: an unlicensed tenant genuinely has no PIM data, and
    # flagging it as a problem to investigate would be wrong.
    assert st.status not in schema.ATTENTION_STATUSES


async def test_delta_refreshes_a_scope_that_was_never_collected(isolated_cache, monkeypatch):
    cache.write_scope("t1", "/subscriptions/s1", meta={"status": schema.STATUS_SUCCEEDED}, rows=[])
    _patch_collect(monkeypatch, lambda _k: FakeKql(ok=True, rows=[]))

    subs = [{"id": "s1", "name": "one"}, {"id": "new", "name": "brand new"}]
    changed, _why = await orchestrator._changed_subscriptions("t1", None, subs)
    assert changed == {"new"}


async def test_delta_declines_when_nothing_has_ever_been_collected(isolated_cache, monkeypatch):
    _patch_collect(monkeypatch, lambda _k: FakeKql(ok=True, rows=[]))
    changed, why = await orchestrator._changed_subscriptions("t1", None, [{"id": "s1", "name": "one"}])
    assert changed is None
    assert why


async def test_delta_declines_when_the_change_feed_cannot_answer(isolated_cache, monkeypatch):
    cache.write_scope("t1", "/subscriptions/s1", meta={"status": schema.STATUS_SUCCEEDED}, rows=[])
    _patch_collect(monkeypatch, lambda _k: FakeKql(ok=False, error="service unavailable"))
    changed, why = await orchestrator._changed_subscriptions("t1", None, [{"id": "s1", "name": "one"}])
    assert changed is None
    assert why


async def test_delta_compares_against_the_oldest_collection_not_the_newest(isolated_cache, monkeypatch):
    """Comparing against the NEWEST scope's timestamp would miss every change that happened to
    an older scope in between — the exact rows a delta refresh exists to catch."""
    cache.write_scope(
        "t1", "/subscriptions/old",
        meta={"status": schema.STATUS_SUCCEEDED, "generated_at": "2026-01-01T00:00:00+00:00"}, rows=[],
    )
    cache.write_scope(
        "t1", "/subscriptions/new",
        meta={"status": schema.STATUS_SUCCEEDED, "generated_at": "2026-07-01T00:00:00+00:00"}, rows=[],
    )
    seen: list[str] = []

    async def _spy(_connection, since_iso):  # noqa: ANN001
        seen.append(since_iso)
        return set(), ""

    monkeypatch.setattr(arg, "subscriptions_changed_since", _spy)
    await orchestrator._changed_subscriptions(
        "t1", None, [{"id": "old", "name": "o"}, {"id": "new", "name": "n"}]
    )
    assert seen == ["2026-01-01T00:00:00+00:00"]
