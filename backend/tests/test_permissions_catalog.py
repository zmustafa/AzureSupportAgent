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
    PERMISSION_GROUPS,
    PERMISSIONS,
    READ_PERMISSIONS,
    SYSTEM_ROLES,
)
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
        "rbac.read",
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
    admin_only = {"settings.write", "users.manage", "audit.read", "backup.manage", "demo.manage"}
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
    # No write/run/manage capabilities leak into the auditor role.
    for p in auditor:
        assert p.endswith(".read") or p in {"chat.use", "monitor.view", "audit.read"}, p


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
