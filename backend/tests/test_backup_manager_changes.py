"""Backup Manager change ledger: refusals, apply dispatch, concurrency, LRO, rollback."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from app.backup_manager import changes, gaps, service
from app.backup_manager.builtin_seed import PORTAL_ONLY_OPERATIONS
from app.models import BackupManagerChange

VAULT = "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.RecoveryServices/vaults/rsv"
VM = "/subscriptions/s1/resourceGroups/rg-app/providers/Microsoft.Compute/virtualMachines/vm1"
ITEM = gaps.rsv_protected_item_id(VAULT, "rg-app", "vm1")
CONNECTION: dict[str, Any] = {"id": "c1", "tenant_id": "t", "read_only": False, "auth_method": "service_principal"}


def _change(**overrides) -> BackupManagerChange:
    payload = {
        "tenant_id": "t1", "connection_id": "c1", "target_type": "protection", "target_id": ITEM,
        "operation": "create", "requested_by": "op@example.test",
        "desired": {"body": gaps.build_vm_protection_body(vm_id=VM, policy_id=f"{VAULT}/backupPolicies/DefaultPolicy")},
        "summary": {"mechanism": "rsv_vm", "api_version": service.RSV_BACKUP_API},
    }
    payload.update(overrides)
    return changes.build_change(**payload)


# --------------------------------------------------------------------------- refusals
def test_restore_has_no_target_type_at_all() -> None:
    """Restores are refused structurally: there is nothing in the registry to create."""
    assert "restore" not in changes.TARGET_SPECS
    assert not any("restore" in key for key in changes.TARGET_SPECS)


def test_unknown_target_types_cannot_be_created() -> None:
    with pytest.raises(ValueError):
        changes.build_change(
            tenant_id="t", connection_id="c", target_type="delete_backup_data", target_id="x",
            operation="delete", requested_by="op", desired={},
        )


def test_documented_refusals_are_explicit() -> None:
    ids = {item["id"] for item in PORTAL_ONLY_OPERATIONS}
    assert {"restore", "delete_backup_data", "lock_immutability", "disable_soft_delete"}.issubset(ids)
    for item in PORTAL_ONLY_OPERATIONS:
        assert item["reason"]
        with pytest.raises(changes.RefusedOperation):
            changes.refuse(item["id"])


def test_operation_validation_rejects_unsupported_verbs() -> None:
    with pytest.raises(ValueError):
        changes.validate_operation("adhoc_backup", "delete")
    with pytest.raises(ValueError):
        changes.validate_operation("vault", "delete")


# --------------------------------------------------------------------------- construction
def test_build_change_encrypts_payloads_and_hashes_before_state() -> None:
    before = {"location": "eastus", "properties": {"policyId": "old"}, "sku": {}}
    change = _change(operation="update", before=before)
    assert change.desired_encrypted and "policyId" not in change.desired_encrypted
    assert change.expected_state_hash == service.canonical_hash(before)
    assert service.decrypted_json(change.before_encrypted) == before


def test_public_change_never_leaks_the_payload() -> None:
    change = _change()
    change.id = "abc"
    public = changes.public_change(change)
    assert "desired_encrypted" not in public
    assert "body" not in public
    assert public["target_label"] == "Protected item"


def test_test_failover_requires_two_approvers_by_default() -> None:
    change = changes.build_change(
        tenant_id="t", connection_id="c", target_type="asr_test_failover", target_id="/x",
        operation="invoke", requested_by="op", desired={"body": {}},
    )
    assert change.requires_dual_approval is True
    assert change.risk == "high"


# --------------------------------------------------------------------------- apply
class _Submission:
    """Records the ARM call the dispatcher made."""

    calls: list[dict[str, Any]] = []


@pytest.fixture(autouse=True)
def _reset_calls():
    _Submission.calls = []
    yield
    _Submission.calls = []


@pytest.fixture
def stub_arm(monkeypatch):
    async def token_for(_connection):
        return "token"

    async def arm_submit(token, method, path, *, body=None, api_version="", query=None):  # noqa: ARG001
        _Submission.calls.append({"method": method, "path": path, "body": body, "api_version": api_version})
        return service.ArmSubmission(status=202, body={"id": "op-1"}, error="",
                                     async_operation_url="https://management.azure.com/op/1", retry_after=5)

    monkeypatch.setattr(service, "token_for", token_for)
    monkeypatch.setattr(service, "arm_submit", arm_submit)
    return _Submission


@pytest.mark.asyncio
async def test_enable_protection_puts_the_protected_item(stub_arm) -> None:
    change = _change()
    submission, _context = await changes.apply_change(CONNECTION, change)
    assert submission.ok and submission.is_async
    call = stub_arm.calls[0]
    assert call["method"] == "PUT"
    assert call["path"] == ITEM
    assert call["body"]["properties"]["sourceResourceId"] == VM


@pytest.mark.asyncio
async def test_stop_protection_only_ever_retains_data(stub_arm) -> None:
    change = _change(
        operation="delete",
        desired={"body": {"properties": {"protectedItemType": "Microsoft.Compute/virtualMachines",
                                         "sourceResourceId": VM, "protectionState": "ProtectionStopped"}}},
        summary={"mechanism": "rsv_vm", "api_version": service.RSV_BACKUP_API,
                 "stop_mode": changes.STOP_PROTECTION_RETAIN},
    )
    submission, _context = await changes.apply_change(CONNECTION, change)
    assert submission.ok
    assert stub_arm.calls[0]["body"]["properties"]["protectionState"] == "ProtectionStopped"


@pytest.mark.asyncio
async def test_any_other_stop_mode_is_refused_at_apply_time(stub_arm) -> None:
    """Defense in depth: even a hand-crafted row cannot reach a data-deleting call."""
    change = _change(operation="delete", desired={"body": {}},
                     summary={"mechanism": "rsv_vm", "stop_mode": "delete_data"})
    submission, _context = await changes.apply_change(CONNECTION, change)
    assert not submission.ok
    assert "Azure portal" in submission.error
    assert stub_arm.calls == []


@pytest.mark.asyncio
async def test_dataprotection_stop_uses_the_stop_protection_endpoint(stub_arm) -> None:
    instance = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.DataProtection/backupVaults/bv/backupInstances/i"
    change = _change(target_id=instance, operation="delete", desired={"body": {}},
                     summary={"mechanism": "dataprotection", "stop_mode": changes.STOP_PROTECTION_RETAIN})
    await changes.apply_change(CONNECTION, change)
    assert stub_arm.calls[0]["path"].endswith("/stopProtection")
    assert stub_arm.calls[0]["method"] == "POST"


@pytest.mark.asyncio
async def test_vault_security_uses_the_declared_sub_resource_path(stub_arm) -> None:
    change = changes.build_change(
        tenant_id="t", connection_id="c", target_type="vault_security", target_id=VAULT,
        operation="update", requested_by="op",
        desired={"body": {"properties": {"softDeleteFeatureState": "Enabled"}}},
        summary={"arm_path": f"{VAULT}/backupconfig/vaultconfig", "arm_method": "PUT",
                 "api_version": service.RSV_VAULT_CONFIG_API},
    )
    await changes.apply_change(CONNECTION, change)
    assert stub_arm.calls[0]["path"].endswith("/backupconfig/vaultconfig")
    assert stub_arm.calls[0]["method"] == "PUT"


@pytest.mark.asyncio
async def test_update_enforces_optimistic_concurrency(monkeypatch) -> None:
    change = _change(operation="update", before={"location": "eastus", "properties": {"policyId": "a"}, "sku": {}})

    async def token_for(_connection):
        return "token"

    async def arm_get(_connection, _path, _api, **_kwargs):
        # Someone else changed the policy after the request was reviewed.
        return {"location": "eastus", "properties": {"policyId": "b"}, "sku": {}}, 200, ""

    monkeypatch.setattr(service, "token_for", token_for)
    monkeypatch.setattr(service, "arm_get", arm_get)
    submission, _context = await changes.apply_change(CONNECTION, change)
    assert submission.status == 409
    assert "state changed" in submission.error


@pytest.mark.asyncio
async def test_apply_refuses_a_read_only_connection() -> None:
    with pytest.raises(PermissionError):
        await changes.apply_change({**CONNECTION, "read_only": True}, _change())


# --------------------------------------------------------------------------- LRO transitions
def test_async_submission_parks_the_change_for_polling() -> None:
    change = _change()
    submission = service.ArmSubmission(status=202, body={"id": "job-9"}, error="",
                                       async_operation_url="https://management.azure.com/op/1", retry_after=20)
    changes.mark_submitted(change, submission, "approver@example.test")
    assert change.status == "applying"
    assert change.operation_url.endswith("/op/1")
    assert change.azure_job_id == "job-9"
    assert change.poll_after is not None and change.poll_deadline is not None
    assert change.poll_deadline > change.poll_after


def test_synchronous_success_short_circuits_to_applied() -> None:
    change = _change()
    changes.mark_submitted(change, service.ArmSubmission(status=200, body={"ok": True}, error=""), "a@b.test")
    assert change.status == "applied"
    assert service.decrypted_json(change.after_encrypted) == {"ok": True}


def test_failed_submission_records_the_error() -> None:
    change = _change()
    changes.mark_submitted(change, service.ArmSubmission(status=403, body=None, error="ARM 403: denied"), "a@b.test")
    assert change.status == "failed"
    assert change.error_message == "ARM 403: denied"


def test_poll_transitions() -> None:
    change = _change()
    change.status = "applying"
    changes.mark_polled(change, "running", {}, "", 30.0)
    assert change.status == "applying" and change.poll_attempts == 1
    changes.mark_polled(change, "succeeded", {"final": True}, "", 0.0)
    assert change.status == "applied"
    assert change.poll_after is None

    failing = _change()
    failing.status = "applying"
    changes.mark_polled(failing, "failed", {}, "Snapshot failed", 0.0)
    assert failing.status == "failed"
    assert failing.error_code == "AzureOperationFailed"


def test_timeout_is_terminal_and_explains_itself() -> None:
    change = _change()
    change.status = "applying"
    changes.mark_timed_out(change)
    assert change.status == "failed"
    assert change.error_code == "OperationTimeout"
    assert "Azure" in (change.error_message or "")


# --------------------------------------------------------------------------- rollback
def test_enable_protection_rolls_back_to_stop_with_data_retained() -> None:
    change = _change()
    change.id = "orig"
    change.status = "applied"
    change.after_encrypted = service.encrypted_json({"properties": {"protectedItemType": "Microsoft.Compute/virtualMachines",
                                                                    "sourceResourceId": VM}})
    rollback = changes.build_rollback(change, requested_by="op@example.test")
    assert rollback.target_type == "protection"
    assert rollback.operation == "delete"
    assert rollback.rollback_of == "orig"
    body = service.decrypted_json(rollback.desired_encrypted)["body"]
    assert body["properties"]["protectionState"] == "ProtectionStopped"


def test_unreversible_changes_refuse_automatic_rollback() -> None:
    change = changes.build_change(
        tenant_id="t", connection_id="c", target_type="adhoc_backup", target_id=ITEM,
        operation="invoke", requested_by="op", desired={"body": {}},
    )
    change.status = "applied"
    with pytest.raises(ValueError) as excinfo:
        changes.build_rollback(change, requested_by="op")
    assert "Azure portal" in str(excinfo.value)


# --------------------------------------------------------------------------- ordering
def test_changes_apply_in_prerequisite_order() -> None:
    vault = _change(target_type="vault", target_id=VAULT, operation="create", desired={"body": {}}, summary={})
    vault.id = "v"
    policy = _change(target_type="backup_policy", target_id=f"{VAULT}/backupPolicies/p",
                     operation="create", desired={"body": {}}, summary={})
    policy.id = "p"
    policy.depends_on = ["v"]
    protection = _change()
    protection.id = "i"
    protection.depends_on = ["p"]
    ordered = changes.order_changes([protection, policy, vault])
    assert [c.id for c in ordered] == ["v", "p", "i"]


def test_ordering_survives_a_dependency_cycle() -> None:
    a, b = _change(), _change()
    a.id, b.id = "a", "b"
    a.depends_on, b.depends_on = ["b"], ["a"]
    ordered = changes.order_changes([a, b])
    assert {c.id for c in ordered} == {"a", "b"}


# --------------------------------------------------------------------------- protection bodies
def test_protected_item_id_matches_the_azure_naming_contract() -> None:
    assert ITEM.endswith("/protectionContainers/IaasVMContainer;iaasvmcontainerv2;rg-app;vm1"
                         "/protectedItems/vm;iaasvmcontainerv2;rg-app;vm1")


def test_plan_item_blocks_a_cross_subscription_target() -> None:
    gap = {"gap_id": "g", "resource_id": VM, "resource_name": "vm1",
           "resource_type": "microsoft.compute/virtualmachines", "display_type": "Virtual machine",
           "resource_group": "rg-app", "subscription_id": "s1", "location": "eastus"}
    vault = {"id": VAULT, "name": "rsv", "kind": "recovery_services", "subscription_id": "OTHER", "location": "eastus"}
    policy = {"id": f"{VAULT}/backuppolicies/defaultpolicy", "arm_id": f"{VAULT}/backupPolicies/DefaultPolicy",
              "name": "DefaultPolicy", "vault_id": VAULT, "backup_management_type": "AzureIaasVM"}
    item = gaps.plan_item(gap, vault, policy)
    assert item["status"] == "blocked"
    assert "subscription" in item["reason"].lower()


def test_plan_item_blocks_a_mismatched_vault_kind() -> None:
    gap = {"gap_id": "g", "resource_id": VM, "resource_name": "vm1",
           "resource_type": "microsoft.compute/virtualmachines", "display_type": "Virtual machine",
           "resource_group": "rg-app", "subscription_id": "s1", "location": "eastus"}
    vault = {"id": "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.DataProtection/backupVaults/bv",
             "name": "bv", "kind": "backup", "subscription_id": "s1", "location": "eastus"}
    policy = {"id": "p", "arm_id": "P", "name": "p", "vault_id": vault["id"], "backup_management_type": "DataProtection"}
    item = gaps.plan_item(gap, vault, policy)
    assert item["status"] == "blocked"
    assert "Recovery Services vault" in item["reason"]


def test_plan_item_builds_a_ready_dataprotection_instance() -> None:
    storage = "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/sa1"
    gap = {"gap_id": "g", "resource_id": storage, "resource_name": "sa1",
           "resource_type": "microsoft.storage/storageaccounts", "display_type": "Storage account (blobs)",
           "resource_group": "rg", "subscription_id": "s1", "location": "eastus"}
    vault = {"id": "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.DataProtection/backupVaults/bv",
             "name": "bv", "kind": "backup", "subscription_id": "s1", "location": "eastus"}
    policy = {"id": "p", "arm_id": f"{vault['id']}/backupPolicies/p", "name": "p", "vault_id": vault["id"],
              "backup_management_type": "DataProtection"}
    item = gaps.plan_item(gap, vault, policy)
    assert item["status"] == "ready"
    assert item["requires_validation"] is True
    datasource = item["body"]["properties"]["dataSourceInfo"]
    assert datasource["resourceID"].endswith("/blobServices/default")
    assert datasource["datasourceType"] == "Microsoft.Storage/storageAccounts/blobServices"


def test_instance_names_are_deterministic_and_arm_safe() -> None:
    first = gaps.instance_name_for("my storage!", "/subscriptions/s/x")
    second = gaps.instance_name_for("my storage!", "/subscriptions/s/x")
    assert first == second
    assert all(ch.isalnum() or ch in "-_" for ch in first)
