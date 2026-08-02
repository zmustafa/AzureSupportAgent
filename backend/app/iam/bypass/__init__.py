"""Shadow access: the doors that are not RBAC.

Every other screen in this product answers *"who has a role?"*. That question assumes RBAC is
the door. For a large share of Azure services it is not even the main one:

    Storage account          shared keys      -> full data-plane, no RBAC involved
    Cosmos DB                primary keys     -> bypasses Cosmos RBAC entirely
    AKS                      admin kubeconfig -> cluster-admin, bypasses Azure RBAC for K8s
    SQL / Synapse            SQL logins       -> no Entra principal exists at all
    ACR                      admin user       -> a username and password for the registry

A perfect RBAC report on an estate where `allowSharedKeyAccess` is true everywhere is a report
about a door standing next to an open window.

**A bypass row is not an access row.** It has no principal — it is a property of a *resource*.
What makes this an access feature rather than a config checklist is ``reachableBy``: the set of
principals who hold the action that yields the credential (``listKeys``,
``listClusterAdminCredential``, …), computed by the effective-permission engine from P4.

**Scope, stated plainly and repeated in the UI:** this reports *the door, not the room*.
``listClusterAdminCredential`` is an Azure control-plane action and is in scope; the
``ClusterRoleBinding``s behind it are not. A reader must never infer from this tab that a
cluster's internal authorization has been assessed.
"""
from __future__ import annotations

from app.iam.bypass.specs import (
    BYPASS_SPECS,
    FAMILIES,
    KIND_ADMIN_USER,
    KIND_BASIC_PUBLISHING,
    KIND_CLUSTER_ADMIN,
    KIND_LOCAL_AUTH,
    KIND_PUBLIC_ACCESS,
    KIND_SHARED_KEY,
    KIND_SQL_AUTH,
    BypassSpec,
)
from app.iam.bypass.service import (
    assess,
    collect,
    compute_reachability,
    summarize,
)

__all__ = [
    "BYPASS_SPECS",
    "FAMILIES",
    "BypassSpec",
    "KIND_ADMIN_USER",
    "KIND_BASIC_PUBLISHING",
    "KIND_CLUSTER_ADMIN",
    "KIND_LOCAL_AUTH",
    "KIND_PUBLIC_ACCESS",
    "KIND_SHARED_KEY",
    "KIND_SQL_AUTH",
    "assess",
    "collect",
    "compute_reachability",
    "summarize",
]
