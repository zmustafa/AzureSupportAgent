"""Shadow access / RBAC bypass (P6).

The premise: every other screen answers "who has a role?", which assumes RBAC is the door. The
ways to be confidently wrong here are all about defaults and denominators:

* an absent property read as "disabled"      -> a wide-open estate reported as locked down
* a failed sweep read as "nothing found"     -> a service nobody could read looks clean
* an empty `reachableBy` read as "nobody"    -> "no one can get the key" when we never checked
* a percentage over an unknown denominator   -> a number that gets quoted and cannot be defended
* remediation without its `breaks if`        -> a one-line fix that takes production down
"""
from __future__ import annotations

import pytest

from app.iam import cache, demo, effective, schema
from app.iam.bypass import service
from app.iam.bypass import specs as sp
from app.iam.collectors import CollectorStatus

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


SUB = "11111111-1111-1111-1111-111111111111"


def _res(name: str, rtype: str, **props):
    base = {
        "id": f"/subscriptions/{SUB}/resourceGroups/rg1/providers/{rtype}/{name}",
        "name": name,
        "type": rtype.lower(),
        "subscriptionId": SUB,
        "resourceGroup": "rg1",
    }
    base.update(props)
    return base


def _spec(key: str) -> sp.BypassSpec:
    return next(s for s in sp.BYPASS_SPECS if s.key == key)


def _keys(rows) -> set[str]:
    return {r["key"] for r in rows}


# --------------------------------------------------------------------------- the table
def test_every_spec_is_uniquely_keyed_and_family_namespaced():
    keys = [s.key for s in sp.BYPASS_SPECS]
    assert len(keys) == len(set(keys))
    for s in sp.BYPASS_SPECS:
        assert s.key.startswith(f"{s.family}."), f"{s.key} is not namespaced to family {s.family}"


def test_every_spec_declares_what_breaks_if_you_apply_the_remediation():
    """A `--allow-shared-key-access false` that silently breaks every connection-string client in
    production is worse than the finding it closes. Remediation is never published alone."""
    for s in sp.BYPASS_SPECS:
        assert s.remediation.strip(), f"{s.key} has no remediation"
        assert s.breaks_if.strip(), f"{s.key} publishes a remediation with no 'breaks if'"


def test_every_spec_has_a_severity_and_a_resource_type():
    for s in sp.BYPASS_SPECS:
        assert s.severity in ("critical", "error", "warning", "info")
        assert s.resource_type == s.resource_type.lower(), "ARM types are compared lower-cased"


def test_credential_actions_are_well_formed_when_present():
    """`reachableBy` is resolved through the effective-permission engine, so a malformed action
    silently resolves to nobody — which reads as "no one can get this key"."""
    for s in sp.BYPASS_SPECS:
        if s.credential_action:
            assert "/" in s.credential_action
            assert s.credential_action.startswith("Microsoft.")


# --------------------------------------------------------------------------- defaults
@pytest.mark.parametrize(
    ("key", "resource", "expected"),
    [
        # ABSENT MEANS ENABLED. `allowSharedKeyAccess` postdates storage accounts by years;
        # reading a missing value as "disabled" reports every legacy account as safe.
        ("storage.shared_key", {}, True),
        ("storage.shared_key", {"allowSharedKeyAccess": "true"}, True),
        ("storage.shared_key", {"allowSharedKeyAccess": "false"}, False),
        # Public blob access is the other way round: absent means NOT enabled.
        ("storage.public_blob", {}, False),
        ("storage.public_blob", {"allowBlobPublicAccess": "true"}, True),
        # disableLocalAuth absent means local auth is ON.
        ("cosmos.local_auth", {}, True),
        ("cosmos.local_auth", {"disableLocalAuth": "false"}, True),
        ("cosmos.local_auth", {"disableLocalAuth": "true"}, False),
        ("aks.local_accounts", {}, True),
        ("aks.local_accounts", {"disableLocalAccounts": "true"}, False),
        ("aks.no_azure_rbac", {"enableAzureRBAC": "true"}, False),
        ("aks.no_azure_rbac", {}, True),
        ("acr.admin_user", {}, False),
        ("acr.admin_user", {"adminUserEnabled": "true"}, True),
        ("sql.entra_only_off", {}, True),
        ("sql.entra_only_off", {"azureADOnlyAuthentication": "true"}, False),
        ("keyvault.rbac_off", {}, True),
        ("keyvault.rbac_off", {"enableRbacAuthorization": "true"}, False),
        ("batch.shared_key", {"allowedAuthenticationModes": "AAD,SharedKey"}, True),
        ("batch.shared_key", {"allowedAuthenticationModes": "AAD"}, False),
        ("redis.access_keys", {}, True),
        ("redis.access_keys", {"disableAccessKeyAuthentication": "true"}, False),
    ],
)
def test_detector_defaults(key, resource, expected):
    assert _spec(key).detect(resource) is expected


def test_key_expiry_is_only_a_finding_when_shared_key_is_actually_enabled():
    """An account with shared key disabled has no key to expire; reporting one is noise."""
    s = _spec("storage.key_never_expires")
    assert s.detect({"allowSharedKeyAccess": "true"}) is True
    assert s.detect({"allowSharedKeyAccess": "true", "keyExpirationPeriodInDays": "90"}) is False
    assert s.detect({"allowSharedKeyAccess": "false"}) is False


# --------------------------------------------------------------------------- assessment
def test_only_enabled_bypasses_become_rows():
    """A resource where every door is closed contributes to the DENOMINATOR, not to the list."""
    resources = [
        _res("open", "Microsoft.Storage/storageAccounts", allowSharedKeyAccess="true"),
        _res("closed", "Microsoft.Storage/storageAccounts",
             allowSharedKeyAccess="false", keyExpirationPeriodInDays="90",
             allowBlobPublicAccess="false", allowCrossTenantReplication="false"),
    ]
    rows = service.assess(resources)
    assert {r["resourceName"] for r in rows} == {"open"}


def test_a_row_carries_its_remediation_and_what_breaks():
    rows = service.assess([_res("st1", "Microsoft.Storage/storageAccounts", allowSharedKeyAccess="true")])
    row = next(r for r in rows if r["key"] == "storage.shared_key")
    assert "st1" in row["remediation"] and "rg1" in row["remediation"]
    assert row["breaksIf"]


def test_a_malformed_resource_does_not_lose_the_whole_sweep():
    """One resource with an unexpected shape must not cost every other finding."""
    class Exploding(dict):
        def get(self, key, default=None):  # noqa: ANN001
            if key == "allowSharedKeyAccess":
                raise ValueError("boom")
            return super().get(key, default)

    bad = Exploding(_res("bad", "Microsoft.Storage/storageAccounts"))
    good = _res("good", "Microsoft.ContainerRegistry/registries", adminUserEnabled="true")
    rows = service.assess([bad, good])
    assert "acr.admin_user" in _keys(rows)


# --------------------------------------------------------------------------- reachability
def test_reachability_is_computed_per_scope_not_per_resource():
    """A principal can call listKeys on a resource if they hold the action at or above it, and
    assignments only exist at a handful of scopes. Per-resource evaluation is the difference
    between instant and unusable on a real estate."""
    role_defs = [{"roleDefinitionId": "/rd/op", "roleName": "Key Lister",
                  "actions": ["Microsoft.Storage/storageAccounts/listKeys/action"]}]
    idx = effective.build_role_index(role_defs)
    access = [
        schema.make_row(
            surface=schema.SURFACE_AZURE_RBAC, effect=schema.EFFECT_ALLOW,
            assignmentState=schema.STATE_ACTIVE, principalId="alice",
            effectivePrincipalId="alice", effectivePrincipalName="Alice",
            roleDefinitionId="/rd/op", roleName="Key Lister",
            scope=f"/subscriptions/{SUB}", scopeDisplayName="sub", assignmentId="a1",
        )
    ]
    reach = service.compute_reachability(access, idx, ["Microsoft.Storage/storageAccounts/listKeys/action"])
    assert [h["principalId"] for h in reach["Microsoft.Storage/storageAccounts/listKeys/action"]] == ["alice"]


def test_reachable_by_is_filtered_to_resources_the_scope_actually_covers():
    reach = {
        "Microsoft.Storage/storageAccounts/listKeys/action": [
            {"principalId": "alice", "principalName": "Alice", "scope": f"/subscriptions/{SUB}"},
            {"principalId": "bob", "principalName": "Bob", "scope": "/subscriptions/other"},
        ]
    }
    rows = service.assess(
        [_res("st1", "Microsoft.Storage/storageAccounts", allowSharedKeyAccess="true")],
        reachability=reach,
    )
    row = next(r for r in rows if r["key"] == "storage.shared_key")
    assert [h["principalId"] for h in row["reachableBy"]] == ["alice"]


def test_a_principal_is_recorded_at_every_scope_they_hold_the_action_not_just_the_first():
    """Regression, found on the live tenant and not by any earlier test.

    The scan used to ``break`` at the first scope where a principal was allowed. The caller then
    filters those scopes against each resource, so a principal whose first hit happened to be a
    scope that does not cover the resource in question vanished. Measured effect: 9 of 18 storage
    accounts reported "nobody can fetch the key" while two principals could."""
    role_defs = [{"roleDefinitionId": "/rd/op", "roleName": "Key Lister",
                  "actions": ["Microsoft.Storage/storageAccounts/listKeys/action"]}]
    idx = effective.build_role_index(role_defs)
    # Alice is granted at TWO subscriptions. The first one iterated must not hide the second.
    access = [
        schema.make_row(
            surface=schema.SURFACE_AZURE_RBAC, effect=schema.EFFECT_ALLOW,
            assignmentState=schema.STATE_ACTIVE, principalId="alice",
            effectivePrincipalId="alice", effectivePrincipalName="Alice",
            roleDefinitionId="/rd/op", roleName="Key Lister",
            scope=scope, scopeDisplayName=scope, assignmentId=f"a-{i}",
        )
        for i, scope in enumerate((f"/subscriptions/{SUB}", "/subscriptions/other"))
    ]
    action = "Microsoft.Storage/storageAccounts/listKeys/action"
    holders = service.compute_reachability(access, idx, [action])[action]
    assert sorted(h["scope"] for h in holders) == sorted(
        (f"/subscriptions/{SUB}", "/subscriptions/other")
    )

    # …and the resource in the *second* subscription still finds her.
    rows = service.assess(
        [_res("st1", "Microsoft.Storage/storageAccounts", allowSharedKeyAccess="true",
              subscriptionId="other", id="/subscriptions/other/resourceGroups/rg/providers/"
              "Microsoft.Storage/storageAccounts/st1")],
        reachability={action: holders},
    )
    row = next(r for r in rows if r["key"] == "storage.shared_key")
    assert [h["principalId"] for h in row["reachableBy"]] == ["alice"]


def test_reachable_count_is_people_not_principal_scope_pairs():
    """One principal recorded at several covering scopes must count once. "12 principals can
    fetch this key" has to mean 12 people."""
    action = "Microsoft.Storage/storageAccounts/listKeys/action"
    reach = {
        action: [
            {"principalId": "alice", "principalName": "Alice", "scope": f"/subscriptions/{SUB}"},
            {"principalId": "alice", "principalName": "Alice",
             "scope": f"/subscriptions/{SUB}/resourceGroups/rg1"},
            {"principalId": "bob", "principalName": "Bob", "scope": f"/subscriptions/{SUB}"},
        ]
    }
    rows = service.assess(
        [_res("st1", "Microsoft.Storage/storageAccounts", allowSharedKeyAccess="true")],
        reachability=reach,
    )
    row = next(r for r in rows if r["key"] == "storage.shared_key")
    assert row["reachableCount"] == 2
    assert sorted(h["principalId"] for h in row["reachableBy"]) == ["alice", "bob"]


def test_an_unavailable_reachability_join_is_distinguishable_from_nobody():
    """An empty list and an uncomputed join look identical to a reader, so the flag is carried
    on every row — otherwise "we never checked" renders as "no one can get the key"."""
    rows = service.assess(
        [_res("st1", "Microsoft.Storage/storageAccounts", allowSharedKeyAccess="true")],
        reachability={}, reachability_available=False,
    )
    row = next(r for r in rows if r["key"] == "storage.shared_key")
    assert row["reachableBy"] == []
    assert row["reachabilityAvailable"] is False

    rows2 = service.assess(
        [_res("st1", "Microsoft.Storage/storageAccounts", allowSharedKeyAccess="true")],
        reachability={"Microsoft.Storage/storageAccounts/listKeys/action": []},
        reachability_available=True,
    )
    assert rows2[0]["reachabilityAvailable"] is True


def test_a_bypass_with_no_credential_action_never_claims_reachability():
    """Anonymous blob access needs no credential at all, so there is nothing to be reachable by
    and claiming the join applies would be meaningless."""
    rows = service.assess(
        [_res("st1", "Microsoft.Storage/storageAccounts", allowBlobPublicAccess="true")],
        reachability={}, reachability_available=True,
    )
    row = next(r for r in rows if r["key"] == "storage.public_blob")
    assert row["reachabilityAvailable"] is False


# --------------------------------------------------------------------------- severity
def test_environment_modulates_severity():
    """A dev sandbox with shared keys is not the same finding as a production payments account,
    and treating them alike is how a findings list stops being read."""
    res = [_res("st1", "Microsoft.Storage/storageAccounts", allowSharedKeyAccess="true")]
    rid = res[0]["id"].lower()

    prod = service.assess(res, workload_env={rid: "prod"})
    dev = service.assess(res, workload_env={rid: "dev"})
    none = service.assess(res)

    sev = {r["key"]: r["severity"] for r in prod}["storage.shared_key"]
    assert sev == "critical"
    assert {r["key"]: r["severity"] for r in dev}["storage.shared_key"] == "warning"
    assert {r["key"]: r["severity"] for r in none}["storage.shared_key"] == "error"


def test_a_widely_reachable_credential_raises_severity():
    reach = {
        "Microsoft.Storage/storageAccounts/listKeys/action": [
            {"principalId": f"p{i}", "principalName": f"P{i}", "scope": f"/subscriptions/{SUB}"}
            for i in range(12)
        ]
    }
    rows = service.assess(
        [_res("st1", "Microsoft.Storage/storageAccounts", allowSharedKeyAccess="true")],
        reachability=reach,
    )
    assert {r["key"]: r["severity"] for r in rows}["storage.shared_key"] == "critical"


# --------------------------------------------------------------------------- summary
def _ok_statuses() -> dict[str, CollectorStatus]:
    return {f: CollectorStatus(f"Bypass{f.title()}", schema.STATUS_SUCCEEDED, 1, 0.1, "") for f in sp.FAMILIES}


def test_the_denominator_is_published_alongside_the_percentage():
    """A ratio whose denominator is unknown gets quoted in a board pack and cannot be defended."""
    resources = [
        _res("open", "Microsoft.Storage/storageAccounts", allowSharedKeyAccess="true"),
        _res("closed", "Microsoft.Storage/storageAccounts",
             allowSharedKeyAccess="false", keyExpirationPeriodInDays="90",
             allowBlobPublicAccess="false", allowCrossTenantReplication="false"),
    ]
    rows = service.assess(resources)
    s = service.summarize(resources, rows, _ok_statuses())
    assert s["assessed"] == 2
    assert s["bypassed"] == 1
    assert s["rbac_only"] == 1
    assert s["rbac_only_pct"] == 50


def test_the_percentage_is_none_rather_than_zero_when_nothing_was_assessed():
    """0% reads as "no resource is RBAC-only", which is the opposite of "we looked at none"."""
    s = service.summarize([], [], _ok_statuses())
    assert s["assessed"] == 0
    assert s["rbac_only_pct"] is None


def test_the_scope_limitation_is_always_published():
    """A tab that shows AKS but not Kubernetes RBAC must not let a reader infer that the
    cluster's internal authorization has been assessed."""
    s = service.summarize([], [], _ok_statuses())
    assert any("door, not the room" in lim for lim in s["limitations"])


def test_an_unreadable_family_is_named_in_the_limitations():
    statuses = _ok_statuses()
    statuses["cosmos"] = CollectorStatus("BypassCosmos", schema.STATUS_UNAUTHORIZED, 0, 0.1, "403")
    s = service.summarize([], [], statuses)
    assert any("cosmos" in lim for lim in s["limitations"])


def test_family_counts_distinguish_none_exist_from_none_assessed():
    resources = [_res("st1", "Microsoft.Storage/storageAccounts", allowSharedKeyAccess="true")]
    s = service.summarize(resources, service.assess(resources), _ok_statuses())
    by_family = {f["family"]: f for f in s["by_family"]}
    assert by_family["storage"]["assessed"] == 1
    assert by_family["cosmos"]["assessed"] == 0


# --------------------------------------------------------------------------- collection
class _FakeKql:
    def __init__(self, ok=True, rows=None, error="", complete=True):
        self.ok, self.rows, self.error, self.complete = ok, rows or [], error, complete


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("429 TooManyRequests", schema.STATUS_THROTTLED),
        ("AuthorizationFailed", schema.STATUS_UNAUTHORIZED),
        ("connection reset", schema.STATUS_FAILED),
    ],
)
async def test_a_failed_sweep_makes_EVERY_family_report_the_failure(monkeypatch, error, expected):
    """Not zero findings. A service the connection cannot read must never render as a service
    with nothing wrong — that is an all-clear the product did not earn."""
    async def _fake(_kql, _conn, **kw):
        return _FakeKql(ok=False, error=error)

    monkeypatch.setattr("app.exec.command_runner.run_kql_collect", _fake)
    resources, statuses = await service.collect(None)
    assert resources == []
    assert set(statuses) == set(sp.FAMILIES)
    assert all(s.status == expected for s in statuses.values())


async def test_a_family_with_no_resources_is_skipped_not_failed(monkeypatch):
    """A tenant with no Cosmos accounts genuinely has no Cosmos bypass. Flagging that as a
    problem to investigate would be wrong, and would train the reader to ignore the statuses."""
    async def _fake(_kql, _conn, **kw):
        return _FakeKql(rows=[_res("st1", "Microsoft.Storage/storageAccounts")])

    monkeypatch.setattr("app.exec.command_runner.run_kql_collect", _fake)
    _resources, statuses = await service.collect(None)
    assert statuses["storage"].status == schema.STATUS_SUCCEEDED
    assert statuses["cosmos"].status == schema.STATUS_SKIPPED
    assert statuses["cosmos"].status not in schema.ATTENTION_STATUSES


async def test_a_capped_sweep_is_partial(monkeypatch):
    async def _fake(_kql, _conn, **kw):
        return _FakeKql(rows=[_res("st1", "Microsoft.Storage/storageAccounts")], complete=False)

    monkeypatch.setattr("app.exec.command_runner.run_kql_collect", _fake)
    _resources, statuses = await service.collect(None)
    assert statuses["storage"].status == schema.STATUS_PARTIAL


async def test_the_sweep_is_one_query_not_one_per_service(monkeypatch):
    """Resource Graph allows 15 queries per 5s per principal tenant-wide and each PAGE costs a
    unit. Twenty per-service queries would spend the whole budget before the rest of a refresh
    started."""
    calls: list[str] = []

    async def _fake(kql, _conn, **kw):
        calls.append(kql)
        return _FakeKql(rows=[])

    monkeypatch.setattr("app.exec.command_runner.run_kql_collect", _fake)
    await service.collect(None)
    assert len(calls) == 1
    # …and it must be ordered, or $skipToken paging drops rows non-reproducibly.
    assert "order by id asc" in calls[0]
    for rtype in sp.RESOURCE_TYPES:
        assert rtype in calls[0], f"{rtype} is not covered by the batched query"


# --------------------------------------------------------------------------- demo estate
def test_the_demo_estate_exercises_the_bypass_tab_with_no_connection(isolated_cache):
    demo.seed_demo("t1")
    payload = cache.read_bypass("t1")
    assert payload["rows"], "the demo estate must produce bypass findings"
    assert payload["summary"]["assessed"] > 0
    assert payload["summary"]["rbac_only_pct"] is not None


def test_the_demo_estate_contains_a_resource_where_rbac_IS_the_only_door(isolated_cache):
    """A demo where everything is broken teaches nothing. The locked-down account is what makes
    the percentage meaningful and the contrast visible."""
    demo.seed_demo("t1")
    payload = cache.read_bypass("t1")
    assert payload["summary"]["rbac_only"] >= 1
    affected = {r["resourceName"] for r in payload["rows"]}
    assert "stprodlogs" not in affected


def test_the_demo_estate_shows_the_same_setting_scored_differently_by_environment(isolated_cache):
    """stprodpayments and stdevscratch both have shared keys enabled. Same configuration, and
    deliberately NOT the same finding."""
    demo.seed_demo("t1")
    rows = cache.read_bypass("t1")["rows"]
    by_name = {r["resourceName"]: r for r in rows if r["key"] == "storage.shared_key"}
    assert by_name["stprodpayments"]["severity"] == "critical"
    assert by_name["stdevscratch"]["severity"] == "warning"


def test_the_demo_bypass_slice_is_separate_from_the_access_rows(isolated_cache):
    """A bypass row has NO principal. Mixing it into the access rows would corrupt every
    per-principal pivot and every KPI in the product."""
    from app.iam import compose

    demo.seed_demo("t1")
    master = compose.build_master_rows("t1")
    bypass_ids = {r["resourceId"] for r in cache.read_bypass("t1")["rows"]}
    assert bypass_ids
    assert not any(r.get("assignmentId") in bypass_ids for r in master)
    assert all(set(r.keys()) == set(schema.COLUMNS) for r in master)


# --------------------------------------------------------------------------- signals
def test_the_bypass_pillar_is_registered_and_measured(isolated_cache):
    from app.iam import findings, signals

    demo.seed_demo("t1")
    ids = {s.id for s in signals.all_signals()}
    assert any(i.startswith("byp.") for i in ids)

    score = findings.compute_score("t1")
    byp = next(p for p in score["pillars"] if p["key"] == "byp")
    assert byp["state"] != "not_implemented"
    assert byp["score"] is not None


def test_bypass_signals_report_not_measured_when_the_sweep_never_ran(isolated_cache):
    """The whole point: no shared-key findings because the sweep failed is indistinguishable
    from every account being locked down, and the reader will assume the second."""
    from app.iam import signals
    from app.iam.signal_defs import byp

    ctx = signals.SignalContext(tenant_id="t1", rows=[], kpis={}, scopes=[])
    spec = next(s for s in byp.SIGNALS if s.id == "byp.shared_key")
    with pytest.raises(signals.SignalUnavailable):
        spec.evaluate(ctx)


def test_a_never_run_sweep_reads_back_a_well_formed_summary_not_an_empty_dict(isolated_cache):
    """Regression. ``read_bypass`` used to return ``summary: {}`` for a tenant that had never
    been swept. Every consumer reads the keys, and a MISSING ``rbac_only_pct`` is not ``None``
    in JavaScript — it is ``undefined``, which sailed past the UI's "nothing assessed" branch and
    rendered the reassuring "…% of 0 assessed resources have RBAC as the only door" headline."""
    payload = cache.read_bypass("never-swept")
    s = payload["summary"]
    assert payload["rows"] == [] and payload["resources"] == []
    for key in ("assessed", "rbac_only", "bypassed", "rbac_only_pct", "findings",
                "by_family", "by_severity", "limitations"):
        assert key in s, f"a never-run sweep omits {key}, so the reader gets undefined"
    # Explicitly null, never 0 — 0% would read as "no resource is RBAC-only".
    assert s["rbac_only_pct"] is None
    assert s["assessed"] == 0
    assert s["limitations"], "a never-run sweep must say so rather than render blank"


def test_every_bypass_finding_carries_its_breaks_if(isolated_cache):
    from app.iam import findings

    demo.seed_demo("t1")
    results = findings.evaluate("t1")
    byp_findings = [f for r in results if r.spec.pillar == "byp" for f in r.findings]
    assert byp_findings
    for f in byp_findings:
        if f.evidence.get("remediation"):
            assert f.evidence.get("breaksIf"), f"{f.signal_id} publishes a command with no warning"
            assert "WARNING" in f.remediation
