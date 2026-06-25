"""Register all built-in quota collectors into the shared registry.

Importing this package wires every collector. The scan service imports it (``import
app.quota.collectors``) before reading ``registry``. Add a new collector by creating its module
and appending a ``registry.register(...)`` line here — nothing else in the orchestrator changes."""
from __future__ import annotations

from app.quota.base import registry
from app.quota.collectors.ai import AiQuotaCollector
from app.quota.collectors.appservice import AppServiceQuotaCollector
from app.quota.collectors.compute import ComputeQuotaCollector
from app.quota.collectors.container_instance import ContainerInstanceQuotaCollector
from app.quota.collectors.governance import GovernanceLimitCollector
from app.quota.collectors.keyvault import KeyVaultLimitCollector
from app.quota.collectors.monitor import AzureMonitorLimitCollector
from app.quota.collectors.network import NetworkQuotaCollector
from app.quota.collectors.network_counts import NetworkCountCollector
from app.quota.collectors.sql import SqlQuotaCollector
from app.quota.collectors.static import (
    StaticRegionLimitsCollector,
    StaticSubscriptionLimitsCollector,
)
from app.quota.collectors.storage import StorageQuotaCollector

# Order here is the order collectors run within a scope (dynamic first, static last).
_BUILTINS = [
    ComputeQuotaCollector(),
    NetworkQuotaCollector(),
    NetworkCountCollector(),
    StorageQuotaCollector(),
    AppServiceQuotaCollector(),
    SqlQuotaCollector(),
    ContainerInstanceQuotaCollector(),
    KeyVaultLimitCollector(),
    AzureMonitorLimitCollector(),
    AiQuotaCollector(),
    GovernanceLimitCollector(),
    StaticSubscriptionLimitsCollector(),
    StaticRegionLimitsCollector(),
]

# Idempotent registration (the registry is a module singleton; guard against double-import).
if not registry.all():
    for _c in _BUILTINS:
        registry.register(_c)

__all__ = ["registry"]
