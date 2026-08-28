"""Deletion blast radius: caveats, locks, and the ARM-sourced storage facts.

Every Azure behaviour asserted here was verified against a live tenant while this was built,
not recalled. Where a test encodes a Microsoft-documented limit, the docstring says which one,
so a future reader can re-check it rather than trusting the assertion.
"""
from __future__ import annotations

import pytest

from app.resiliency import collect, derive, join, model

DELETE = model.SCENARIO_ACCIDENTAL_DELETE
CORRUPT = model.SCENARIO_DATA_CORRUPTION


def _cfg(rtype: str, **over):
    base = {
        "id": f"/subscriptions/s/resourcegroups/rg/providers/{rtype}/thing",
        "name": "thing", "type": rtype, "location": "eastus",
        "resource_group": "rg", "subscription_id": "s",
        "zones": [], "zone_redundant": None, "replication": "", "sku": "",
        "native_backup": {"kind": "unknown"}, "size_gb": None,
        "soft_delete": None, "holds_data": None,
    }
    base.update(over)
    return base


def _delete(config, **kw):
    return derive.verdicts_for(config, **kw)[DELETE]


# --------------------------------------------------------------------------- schema
def test_caveats_round_trip_through_as_dict():
    v = model.with_caveats(
        model.verdict(DELETE, rpo_minutes=10, rpo_state=model.RPO_KNOWN,
                      rto_class=model.RTO_HOURS,
                      basis=(model.Evidence("native_backup", "x"),)),
        (model.Caveat(model.CAVEAT_BLAST_RADIUS, model.CAVEAT_CRITICAL, "gone", "http://d"),),
    )
    d = v.as_dict()
    assert d["caveats"] == [{"kind": "blast_radius", "severity": "critical",
                             "detail": "gone", "doc_url": "http://d"}]
    assert model.Caveat(**d["caveats"][0]) == v.caveats[0]


def test_a_caveat_is_not_a_basis():
    """A caveat must never license a confident answer out of nothing measured."""
    v = model.with_caveats(
        model.verdict(DELETE),  # no basis -> forced to unknown
        (model.Caveat(model.CAVEAT_BLAST_RADIUS, model.CAVEAT_CRITICAL, "bad"),),
    )
    assert v.rto_class == model.RTO_UNKNOWN
    assert v.confidence == model.CONFIDENCE_LOW
    assert v.caveats  # still recorded, just not load-bearing


def test_with_caveats_does_not_mutate_and_skips_not_applicable():
    original = model.verdict(DELETE, rpo_minutes=1, rpo_state=model.RPO_KNOWN,
                             rto_class=model.RTO_HOURS,
                             basis=(model.Evidence("native_backup", "x"),))
    out = model.with_caveats(original, (model.Caveat("k", "info", "d"),))
    assert original.caveats == ()
    assert out is not original

    na = model.not_applicable(DELETE, "stateless")
    assert model.with_caveats(na, (model.Caveat("k", "info", "d"),)) is na


def test_only_deletion_carries_caveats():
    cfg = _cfg("microsoft.sql/servers/databases",
               native_backup={"kind": "sql_pitr", "interval_minutes": 10})
    verdicts = derive.verdicts_for(cfg)
    assert verdicts[DELETE].caveats
    for scenario, v in verdicts.items():
        if scenario != DELETE:
            assert v.caveats == (), f"{scenario} must not carry caveats"


# --------------------------------------------------------------------------- per type
def test_sql_database_warns_about_the_parent_server_without_changing_the_verdict():
    """Deleting the DB is a routine restore; deleting the server is unrecoverable without LTR."""
    cfg = _cfg("microsoft.sql/servers/databases",
               native_backup={"kind": "sql_pitr", "interval_minutes": 10})
    v = _delete(cfg)
    assert v.rto_class == model.RTO_HOURS
    assert v.rpo_minutes == 10
    critical = [c for c in v.caveats if c.severity == model.CAVEAT_CRITICAL]
    assert len(critical) == 1
    assert "parent logical server" in critical[0].detail
    assert "long-term retention" in critical[0].detail.lower()


def test_managed_instance_gets_its_own_wording():
    v = _delete(_cfg("microsoft.sql/managedinstances",
                     native_backup={"kind": "sql_pitr", "interval_minutes": 10}))
    assert any("managed instance" in c.detail for c in v.caveats)


@pytest.mark.parametrize("rtype,kind", [
    ("microsoft.dbforpostgresql/flexibleservers", "pg_backup"),
    ("microsoft.dbformysql/flexibleservers", "mysql_backup"),
])
def test_flexible_servers_warn_about_the_five_day_window_and_drop_confidence(rtype, kind):
    """Five days, off-portal API, not guaranteed. 'Hours' badly understates that."""
    cfg = _cfg(rtype, native_backup={"kind": kind, "interval_minutes": 10})
    v = _delete(cfg)
    assert any("five" in c.detail.lower() for c in v.caveats)
    assert v.confidence == model.CONFIDENCE_LOW
    # Corruption is a routine PITR and keeps its confidence.
    assert derive.verdicts_for(cfg)[CORRUPT].confidence == model.CONFIDENCE_MEDIUM


# --------------------------------------------------------------------------- vault awareness
# A vault backup stores data OUTSIDE the resource, so it changes the deletion answer. Azure
# Backup's own words: vaulted blob backup protects against "any accidental or malicious
# deletion of blobs or storage account"; vaulted PostgreSQL backup lives "outside of customer
# tenant and subscription". Asserting the resource is gone over the top of that is the same
# false confidence this module exists to prevent, pointed the other way.
_VAULT = {"schedule_raw": {"scheduleRunFrequency": "Daily",
                           "dailySchedule": {"scheduleRunTimes": ["2026-01-01T02:00:00Z"]}},
          "recovery_point_age_hours": 3.0, "vault_redundancy": "GeoRedundant"}


def test_storage_account_with_a_vault_backup_does_not_claim_it_is_unrecoverable():
    cfg = _cfg("microsoft.storage/storageaccounts", soft_delete=True,
               native_backup={"kind": "storage_pitr", "interval_minutes": 5})
    bare = _delete(cfg)
    vaulted = _delete(cfg, backup=_VAULT)

    assert any("best-effort basis within 14 days" in c.detail for c in bare.caveats)
    assert not any("best-effort basis within 14 days" in c.detail for c in vaulted.caveats)
    # The reader still needs to know the tier decides it — but it is no longer a flat "gone".
    assert any("Vaulted backup" in c.detail for c in vaulted.caveats)
    assert model.worst_caveat_severity(bare.caveats) == model.CAVEAT_CRITICAL
    assert model.worst_caveat_severity(vaulted.caveats) == model.CAVEAT_WARNING


@pytest.mark.parametrize("rtype,kind", [
    ("microsoft.dbforpostgresql/flexibleservers", "pg_backup"),
    ("microsoft.dbformysql/flexibleservers", "mysql_backup"),
])
def test_flexible_server_with_a_vault_backup_is_not_capped_at_five_days(rtype, kind):
    cfg = _cfg(rtype, native_backup={"kind": kind, "interval_minutes": 10})
    vaulted = _delete(cfg, backup=_VAULT)
    assert not any("five days only" in c.detail for c in vaulted.caveats)
    assert any("survives deletion of the server" in c.detail for c in vaulted.caveats)
    # The five-day deadline was the whole reason confidence dropped; it no longer applies.
    assert vaulted.confidence != model.CONFIDENCE_LOW


def test_container_caveat_survives_a_vault_backup():
    """Vaulted backup answers the ACCOUNT radius; the container trap is still true."""
    v = _delete(_cfg("microsoft.storage/storageaccounts", soft_delete=True,
                     native_backup={"kind": "storage_pitr", "interval_minutes": 5}),
                backup=_VAULT)
    assert any("deleted container" in c.detail for c in v.caveats)


def test_mysql_and_postgres_are_independent():
    """They shared one `kind` before, so a change to either silently moved the other."""
    pg = _delete(_cfg("microsoft.dbforpostgresql/flexibleservers",
                      native_backup={"kind": "pg_backup"}))
    my = _delete(_cfg("microsoft.dbformysql/flexibleservers",
                      native_backup={"kind": "mysql_backup"}))
    assert {c.doc_url for c in pg.caveats} != {c.doc_url for c in my.caveats}
    assert my.rpo_minutes == 5 and pg.rpo_minutes == 10


def test_cosmos_periodic_is_day_plus_not_hours():
    """A support-request restore is not an hours-class recovery under any reading."""
    cfg = _cfg("microsoft.documentdb/databaseaccounts",
               native_backup={"kind": "cosmos_periodic", "interval_minutes": 240})
    v = _delete(cfg)
    assert v.rto_class == model.RTO_DAY_PLUS
    assert v.confidence == model.CONFIDENCE_LOW
    assert any(c.kind == model.CAVEAT_NOT_SELF_SERVICE for c in v.caveats)
    # The correction is scoped to deletion AND corruption for periodic, but not to continuous.
    assert derive.verdicts_for(cfg)[CORRUPT].rto_class == model.RTO_DAY_PLUS


def test_cosmos_periodic_override_is_not_vacuous():
    """With a sub-hour interval the generic rule would say 'hours'. The override must win.

    Without this, the assertion above passes only because 240 > 60 and would keep passing
    if the override were deleted."""
    cfg = _cfg("microsoft.documentdb/databaseaccounts",
               native_backup={"kind": "cosmos_periodic", "interval_minutes": 30})
    assert _delete(cfg).rto_class == model.RTO_DAY_PLUS


def test_cosmos_continuous_keeps_hours_and_only_informs():
    v = _delete(_cfg("microsoft.documentdb/databaseaccounts",
                     native_backup={"kind": "cosmos_continuous", "interval_minutes": 1}))
    assert v.rto_class == model.RTO_HOURS
    assert [c.severity for c in v.caveats] == [model.CAVEAT_INFO]


def test_cosmos_with_an_unread_backup_policy_gets_no_caveat():
    """Guessing 'periodic' would manufacture a critical warning out of missing data."""
    v = _delete(_cfg("microsoft.documentdb/databaseaccounts",
                     native_backup={"kind": "unknown"}), backup={"schedule_raw": ""})
    assert v.caveats == ()


def test_keyvault_without_purge_protection_warns():
    v = _delete(_cfg("microsoft.keyvault/vaults", soft_delete=True,
                     native_backup={"kind": "keyvault_soft_delete", "purge_protection": False}))
    assert any("Purge protection is not enabled" in c.detail for c in v.caveats)


def test_keyvault_with_purge_protection_still_warns_about_lost_links():
    """Recovering a vault does not restore its role assignments or Event Grid subscriptions."""
    v = _delete(_cfg("microsoft.keyvault/vaults", soft_delete=True,
                     native_backup={"kind": "keyvault_soft_delete", "purge_protection": True}))
    assert not any("Purge protection is not enabled" in c.detail for c in v.caveats)
    assert any("role assignments" in c.detail for c in v.caveats)


def test_storage_account_warns_that_soft_delete_does_not_cover_the_account():
    v = _delete(_cfg("microsoft.storage/storageaccounts", soft_delete=True,
                     native_backup={"kind": "storage_pitr", "interval_minutes": 5}))
    details = " ".join(c.detail for c in v.caveats)
    assert "Neither protects the account itself" in details
    assert "deleted container" in details


def test_unprotected_resource_still_gets_its_caveats():
    """The RTO_NONE branch is exactly where knowing the available control earns its keep."""
    v = _delete(_cfg("microsoft.storage/storageaccounts", soft_delete=False,
                     native_backup={"kind": "none"}))
    assert v.rto_class == model.RTO_NONE
    assert v.caveats


def test_every_supported_type_resolves_caveats_without_error():
    """Adding a type must force a deliberate decision about its deletion story."""
    for rtype in collect.SUPPORTED_TYPES:
        assert isinstance(model.deletion_caveats(rtype, _cfg(rtype)), tuple)


# --------------------------------------------------------------------------- locks
def test_lock_caveat_never_changes_the_verdict():
    """A lock is prevention, not recovery. Letting it green a cell repeats the redundancy error."""
    cfg = _cfg("microsoft.storage/storageaccounts", native_backup={"kind": "none"})
    lock = [{"scope": "/subscriptions/s", "scope_kind": "subscription", "level": "CanNotDelete"}]
    without = _delete(cfg)
    with_lock = _delete(cfg, locks=lock)
    for field in ("rto_class", "rpo_state", "rpo_minutes", "confidence"):
        assert getattr(without, field) == getattr(with_lock, field)
    assert len(with_lock.caveats) == len(without.caveats) + 1


def test_lock_caveat_states_its_documented_limits():
    v = _delete(_cfg("microsoft.storage/storageaccounts", native_backup={"kind": "none"}),
                locks=[{"scope": "/subscriptions/s/resourcegroups/rg",
                        "scope_kind": "resource_group", "level": "CanNotDelete"}])
    text = next(c.detail for c in v.caveats if c.kind == model.CAVEAT_MITIGATION)
    assert "resource group" in text
    assert "data plane" in text            # locks are control-plane only
    assert "subscription cancellation" in text  # a lock does not block it


def test_locks_only_attach_to_deletion():
    lock = [{"scope": "/subscriptions/s", "scope_kind": "subscription", "level": "CanNotDelete"}]
    verdicts = derive.verdicts_for(
        _cfg("microsoft.storage/storageaccounts", native_backup={"kind": "none"}), locks=lock)
    assert verdicts[CORRUPT].caveats == ()


@pytest.mark.parametrize("scope,rid,covered", [
    ("/subscriptions/s", "/subscriptions/s/resourcegroups/rg/providers/x/y", True),
    ("/subscriptions/s/resourcegroups/rg", "/subscriptions/s/resourcegroups/rg/providers/x/y", True),
    ("/subscriptions/s/resourcegroups/rg/providers/x/y",
     "/subscriptions/s/resourcegroups/rg/providers/x/y", True),
    # The boundary that a naive startswith gets wrong.
    ("/subscriptions/s/resourcegroups/rg-prod",
     "/subscriptions/s/resourcegroups/rg-production/providers/x/y", False),
    ("/subscriptions/other", "/subscriptions/s/resourcegroups/rg/providers/x/y", False),
])
def test_lock_inheritance_is_prefix_matched_on_a_separator(scope, rid, covered):
    got = join._locks_for(rid, [{"scope": scope, "scope_kind": "resource", "level": "CanNotDelete"}])
    assert bool(got) is covered


def test_lock_scope_kind_classification():
    assert collect._lock_scope_kind("/subscriptions/s") == "subscription"
    assert collect._lock_scope_kind("/subscriptions/s/resourcegroups/rg") == "resource_group"
    assert collect._lock_scope_kind(
        "/subscriptions/s/resourcegroups/rg/providers/microsoft.storage/storageaccounts/a"
    ) == "resource"


# --------------------------------------------------------------------------- storage shape
def _storage_row(name="sa"):
    return {"id": f"/subscriptions/s/resourcegroups/rg/providers/microsoft.storage/storageaccounts/{name}",
            "name": name, "type": "microsoft.storage/storageaccounts", "location": "eastus",
            "resourceGroup": "rg", "subscriptionId": "s", "zones": [],
            "skuName": "Standard_LRS", "props": {}}


def test_storage_pitr_is_read_from_the_blob_service_child():
    """restorePolicy lives on blobServices/default; reading it off the account found nothing."""
    row = _storage_row()
    shaped = collect.shape(row, {row["id"]: {
        "restorePolicy": {"enabled": True, "days": 10},
        "deleteRetentionPolicy": {"enabled": True, "days": 14},
        "containerDeleteRetentionPolicy": {"enabled": True, "days": 21},
    }})
    assert shaped["native_backup"]["kind"] == "storage_pitr"
    assert shaped["native_backup"]["retention_days"] == 10
    assert shaped["soft_delete"] is True


def test_storage_without_blob_service_data_is_unknown_not_none():
    """'We did not look' and 'there is nothing' are opposite facts. Conflating them WAS the bug."""
    shaped = collect.shape(_storage_row(), {})
    assert shaped["native_backup"]["kind"] == "unknown"
    assert shaped["soft_delete"] is None


def test_storage_with_an_empty_blob_service_body_is_none_not_unknown():
    """A 404 (e.g. FileStorage) is a real answer: nothing to restore there."""
    row = _storage_row()
    shaped = collect.shape(row, {row["id"]: {}})
    assert shaped["native_backup"]["kind"] == "none"
    assert shaped["soft_delete"] is False


def test_storage_soft_delete_true_when_only_containers_are_protected():
    row = _storage_row()
    shaped = collect.shape(row, {row["id"]: {
        "containerDeleteRetentionPolicy": {"enabled": True, "days": 7}}})
    assert shaped["soft_delete"] is True


def test_sql_system_database_is_excluded_from_the_sweep():
    """`master` cannot be deleted or restored on its own; a verdict against it is noise."""
    kql = collect.config_query()
    assert "microsoft.sql/servers/databases" in kql
    assert "name =~ 'master'" in kql


def test_anf_distinguishes_snapshots_from_vaulted_backups():
    """Snapshots die with the volume; only a vaulted backup is independent storage."""
    base = {"id": "/subscriptions/s/resourcegroups/rg/providers/microsoft.netapp/netappaccounts/a/capacitypools/p/volumes/v",
            "name": "v", "type": "microsoft.netapp/netappaccounts/capacitypools/volumes",
            "location": "eastus", "resourceGroup": "rg", "subscriptionId": "s",
            "zones": [], "skuName": "", "props": {}}
    snap = dict(base, props={"dataProtection": {"snapshot": {"snapshotPolicyId": "/p"}}})
    vault = dict(base, props={"dataProtection": {"backup": {"backupPolicyId": "/b"}}})
    assert collect.shape(snap)["native_backup"]["kind"] == "anf_snapshot"
    assert collect.shape(vault)["native_backup"]["kind"] == "anf_backup"


# --------------------------------------------------------------------------- join wiring
def test_caveats_survive_the_rto_banding_round_trip():
    """Banding rebuilt the verdict field by field and silently dropped caveats.

    This is the whole failure mode: the caveat was computed correctly, persisted as an empty
    list, and the UI showed nothing. Anything that reconstructs a Verdict must carry every
    field it does not deliberately change."""
    from app.resiliency import rto

    v = model.with_caveats(
        model.verdict(DELETE, rpo_minutes=10, rpo_state=model.RPO_KNOWN,
                      rto_class=model.RTO_HOURS, confidence=model.CONFIDENCE_MEDIUM,
                      basis=(model.Evidence(model.EV_NATIVE_BACKUP, "pitr"),)),
        (model.Caveat(model.CAVEAT_BLAST_RADIUS, model.CAVEAT_CRITICAL, "dies with the server"),),
    )
    banded = rto.apply_bands({DELETE: v}, resource_type="microsoft.sql/servers/databases",
                             size_gb=50)[DELETE]
    assert banded.rto_band_minutes is not None, "fixture must actually exercise banding"
    assert banded.caveats == v.caveats


def test_build_rows_attaches_locks_and_caveats_end_to_end():
    cfg = _cfg("microsoft.storage/storageaccounts", native_backup={"kind": "none"},
               soft_delete=False)
    rows = join.build_rows(
        [cfg],
        locks=[{"scope": "/subscriptions/s/resourcegroups/rg", "scope_kind": "resource_group",
                "level": "CanNotDelete", "name": "lk"}],
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["locks"] == [{"level": "CanNotDelete", "scope_kind": "resource_group", "name": "lk"}]
    assert row["verdicts"][DELETE]["caveats"]
    assert row["verdicts"][CORRUPT]["caveats"] == []
