"""Tests for Backup & DR Coverage SQL handling: master-db exclusion + DB-level evaluation."""
from app.backupdr.collector import (
    _is_sql_system_db,
    _state_from_arg,
    compute_coverage,
)


def test_is_sql_system_db_excludes_master():
    assert _is_sql_system_db({"type": "microsoft.sql/servers/databases", "name": "iblogger/master"}) is True
    assert _is_sql_system_db({"type": "microsoft.sql/servers/databases", "name": "master"}) is True
    assert _is_sql_system_db({"type": "microsoft.sql/servers/databases", "name": "iblogger/appdb"}) is False
    # Managed-instance master too.
    assert _is_sql_system_db({"type": "microsoft.sql/managedinstances/databases", "name": "mi/master"}) is True
    # Non-SQL types are never system DBs.
    assert _is_sql_system_db({"type": "microsoft.storage/storageaccounts", "name": "master"}) is False


def test_sql_database_state_from_arg():
    geo = _state_from_arg(
        {
            "type": "microsoft.sql/servers/databases",
            "properties": {"requestedBackupStorageRedundancy": "Geo"},
        }
    )
    assert geo["backup_enabled"] is True  # PITR always on
    assert geo["geo_redundant"] is True

    local = _state_from_arg(
        {
            "type": "microsoft.sql/servers/databases",
            "properties": {"requestedBackupStorageRedundancy": "Local"},
        }
    )
    assert local["backup_enabled"] is True
    assert local["geo_redundant"] is False


def test_sql_databases_appear_in_coverage_groups():
    """A SQL database resource is evaluated and grouped (the core of the user's report)."""
    resources = [
        {
            "id": "/sub/s/rg/r/providers/microsoft.sql/servers/iblogger/databases/appdb",
            "name": "iblogger/appdb",
            "type": "microsoft.sql/servers/databases",
            "location": "southcentralus",
            "properties": {"requestedBackupStorageRedundancy": "Local"},
        }
    ]
    state = {resources[0]["id"].lower(): _state_from_arg(resources[0])}
    snap = compute_coverage(resources, state, [])
    groups = {g["resource_type"]: g for g in snap["groups"]}
    assert "microsoft.sql/servers/databases" in groups
    assert len(groups["microsoft.sql/servers/databases"]["rows"]) == 1
    assert groups["microsoft.sql/servers/databases"]["display"] == "SQL Database"
