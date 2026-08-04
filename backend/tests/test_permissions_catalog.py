"""The fine-grained permission catalog and the built-in system roles derived from it.

Covers the redesigned catalog (grouped capabilities mirroring the product nav), the role
membership rules (admin ⊇ operator, auditor = read-only oversight, user = minimal), and the
``require_permission`` guard (admins always pass; everyone else needs the exact capability).
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.auth.permissions import (
    ALL_PERMISSIONS,
    PERMISSION_ALIASES,
    PERMISSION_GROUPS,
    PERMISSIONS,
    READ_PERMISSIONS,
    SYSTEM_ROLES,
)
from app.auth.service import migrate_legacy_permissions
from app.core.security import Principal, require_permission


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _roles() -> dict[str, list[str]]:
    return {name: perms for name, _desc, perms in SYSTEM_ROLES}


def _principal(role: str, perms: list[str]) -> Principal:
    return Principal(
        subject="u",
        email="u@local",
        tenant_id="default",
        role=role,
        permissions=frozenset(perms),
    )


# --------------------------------------------------------------------- catalog shape
def test_catalog_is_derived_from_groups_without_duplicates():
    flat = [key for _g, items in PERMISSION_GROUPS for key, _label in items]
    assert flat == ALL_PERMISSIONS  # order preserved, derived 1:1
    assert len(flat) == len(set(flat))  # no duplicate keys
    assert set(PERMISSIONS) == set(flat)


def test_new_feature_permissions_are_present():
    # A representative spread of the capabilities added when the product grew.
    for key in (
        "inventory.read",
        "graph.read",
        "iam.read",
        "identity.read",
        "tagintel.read",
        "tagintel.write",
        "policy.read",
        "policy.write",
        "coverage.read",
        "coverage.manage",
        "missions.read",
        "missions.run",
        "workbooks.read",
        "workbooks.write",
        "playbooks.read",
        "playbooks.write",
        "workloads.read",
        "workloads.write",
        "architectures.read",
        "architectures.write",
        "netdiag.run",
        "backup.manage",
        "demo.manage",
        "notifications.manage",
    ):
        assert key in PERMISSIONS, key


def test_every_permission_has_a_nonempty_label():
    assert all(isinstance(v, str) and v.strip() for v in PERMISSIONS.values())


# ------------------------------------------------------------------- role membership
def test_admin_has_every_permission():
    roles = _roles()
    assert set(roles["admin"]) == set(ALL_PERMISSIONS)


def test_operator_is_admin_minus_the_admin_only_surface():
    roles = _roles()
    # Spelled out rather than imported so that ADDING a capability to the admin-only set is a
    # deliberate, reviewed act: this test failing is the intended signal.
    admin_only = {
        "settings.write",
        "users.manage",
        "audit.read",
        "firewall.manage",
        "backup.manage",
        "demo.manage",
    }
    operator = set(roles["operator"])
    # Operator is a strict subset of admin (nothing operator has that admin lacks).
    assert operator <= set(roles["admin"])
    # Operator is exactly admin minus the reserved admin-only capabilities.
    assert operator == set(ALL_PERMISSIONS) - admin_only
    assert operator.isdisjoint(admin_only)


def test_auditor_is_read_only_oversight():
    roles = _roles()
    auditor = set(roles["auditor"])
    assert set(READ_PERMISSIONS) <= auditor  # can view every read surface
    assert {"chat.use", "monitor.view", "audit.read"} <= auditor
    # No write/run/manage capabilities leak into the auditor role. The allowlist is for reads
    # that cannot be spelled ".read": `investigate.activity` reads a named person's sign-in and
    # audit history, held apart from the structural reads precisely BECAUSE it is sensitive —
    # but proving who held privileged access in a period and what they did with it is the
    # auditor's job, so withholding it would defeat the role.
    non_read_reads = {"chat.use", "monitor.view", "audit.read", "investigate.activity"}
    for p in auditor:
        assert p.endswith(".read") or p in non_read_reads, p
    # The guard that actually matters: nothing that mutates, runs or approves.
    for p in auditor:
        assert not any(p.endswith(suffix) for suffix in
                       (".write", ".manage", ".approve", ".delete", ".exec", ".run")), p


def test_user_role_is_minimal_self_service():
    roles = _roles()
    assert set(roles["user"]) == {
        "chat.use",
        "ownership.read",
        "workloads.read",
        "architectures.read",
    }


def test_noaccess_has_zero_permissions():
    assert _roles()["noaccess"] == []


# ------------------------------------------------------------------- guard behaviour
def test_require_permission_allows_holder_and_admin_denies_others():
    dep = require_permission("inventory.read")

    holder = _principal("auditor", ["inventory.read"])
    assert _run(dep(principal=holder)) is holder

    admin = _principal("admin", [])  # role=admin ⇒ is_admin ⇒ always passes
    assert _run(dep(principal=admin)) is admin

    # users.manage also marks a principal as admin (see Principal.is_admin).
    super_perm = _principal("custom", ["users.manage"])
    assert _run(dep(principal=super_perm)) is super_perm

    denied = _principal("user", ["chat.use"])
    with pytest.raises(HTTPException) as exc:
        _run(dep(principal=denied))
    assert exc.value.status_code == 403


# ------------------------------------------------------- renamed capabilities (aliases)
def test_legacy_permission_key_still_satisfies_the_renamed_capability():
    """A CUSTOM role holding only the pre-rename key must not lose access.

    ``seed_system_roles`` rewrites the built-in roles from code on every startup, so they heal
    themselves — custom roles keep whatever was stored when they were created. Renaming a
    capability without this alias silently 403s every custom role that legitimately holds it,
    and the symptom looks like a bug in the renamed feature."""
    dep = require_permission("iam.read")

    legacy = _principal("custom", ["rbac.read"])  # /rbac screen renamed to /iam
    assert _run(dep(principal=legacy)) is legacy

    current = _principal("custom", ["iam.read"])
    assert _run(dep(principal=current)) is current

    # The alias is one-way: it does not grant unrelated capabilities.
    with pytest.raises(HTTPException):
        _run(require_permission("policy.read")(principal=legacy))


def test_permission_aliases_point_at_live_catalog_keys():
    """Every alias must resolve to a key that actually exists, or it silently grants nothing."""
    for old, new in PERMISSION_ALIASES.items():
        assert new in PERMISSIONS, f"alias {old} -> {new} targets a key not in the catalog"
        assert old not in PERMISSIONS, f"legacy key {old} is still in the catalog"


def test_legacy_permission_migration_rewrites_custom_roles_only():
    class _Role:
        def __init__(self, perms):
            self.permissions_json = list(perms)

    custom = _Role(["rbac.read", "inventory.read"])
    already = _Role(["iam.read", "inventory.read"])
    unrelated = _Role(["chat.use"])

    changed = migrate_legacy_permissions([custom, already, unrelated])
    assert changed == 1
    assert custom.permissions_json == ["iam.read", "inventory.read"]  # order preserved
    assert already.permissions_json == ["iam.read", "inventory.read"]
    assert unrelated.permissions_json == ["chat.use"]

    # Idempotent: a second pass changes nothing.
    assert migrate_legacy_permissions([custom, already, unrelated]) == 0


def test_legacy_permission_migration_dedupes_when_both_keys_are_held():
    class _Role:
        def __init__(self, perms):
            self.permissions_json = list(perms)

    both = _Role(["rbac.read", "iam.read"])
    assert migrate_legacy_permissions([both]) == 1
    assert both.permissions_json == ["iam.read"]
