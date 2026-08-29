"""Normalized metadata for every tool source used by chat and automations."""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, replace
from typing import Iterable

from app.agent.provider import ToolSpec

_WORD_RE = re.compile(r"[a-z0-9]+")

# First-party names are stable and let us classify ConnectorToolset entries that do not
# otherwise carry their origin. Unknown entries remain ordinary external connectors.
_IAM_TOOLS = frozenset({
    "who_can_access", "privileged_access_review", "effective_access_for_principal",
    "can_principal_do", "why_does_principal_have_access", "escalation_paths_to",
    "unused_permissions_for", "simulate_revoke", "access_changed_since",
    "who_can_reach_resource",
})
_OWNERSHIP_TOOLS = frozenset({"who_owns", "what_does_owner_own", "find_unowned"})
_ENTRA_IDENTITY_TOOLS = frozenset({
    "identity_investigate", "ca_evaluate", "identity_group_members",
    "ca_policies_for_app", "identity_findings",
})
_RECOVERY_TOOLS = frozenset({"recovery_posture", "recovery_gaps", "recovery_breaches"})
_COST_TOOLS = frozenset({"azure_cost_query"})
_ADVISOR_TOOLS = frozenset({"azure_advisor_recommendations"})
_INVENTORY_TOOLS = frozenset({"azure_resource_inventory"})
_PUBLIC_EXPOSURE_TOOLS = frozenset({"azure_public_exposure_inventory"})
_BUILTIN_TOOLS = frozenset({
    "net_web_fetch", "net_http_request", "net_dns_lookup", "net_port_check",
    "net_ping", "net_traceroute", "azure_metrics",
})
_VM_TOOLS = frozenset({"vm_exec", "vm_list", "vm_read_file"})


@dataclass(frozen=True)
class ToolEntry:
    spec: ToolSpec
    source: str
    domain: str
    bundles: tuple[str, ...]
    kind: str
    keywords: tuple[str, ...]

    @property
    def name(self) -> str:
        return self.spec.name

    def public(self, *, selected: bool = False, reason: str = "") -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.spec.description,
            "source": self.source,
            "domain": self.domain,
            "bundles": list(self.bundles),
            "kind": self.kind,
            "selected": selected,
            **({"reason": reason} if reason else {}),
        }


class ToolNameCollisionError(ValueError):
    def __init__(self, name: str, first_source: str, second_source: str) -> None:
        super().__init__(
            f"Tool name collision for '{name}' between {first_source} and {second_source}."
        )
        self.name = name
        self.first_source = first_source
        self.second_source = second_source


class ToolCatalog:
    def __init__(self, entries: Iterable[ToolEntry]) -> None:
        self._entries: dict[str, ToolEntry] = {}
        for entry in entries:
            previous = self._entries.get(entry.name)
            if previous is not None:
                raise ToolNameCollisionError(entry.name, previous.source, entry.source)
            self._entries[entry.name] = entry

    def get(self, name: str) -> ToolEntry | None:
        return self._entries.get(name)

    def entries(self) -> list[ToolEntry]:
        return list(self._entries.values())

    def names(self) -> list[str]:
        return list(self._entries)

    def specs(self, names: Iterable[str] | None = None) -> list[ToolSpec]:
        if names is None:
            return [e.spec for e in self._entries.values()]
        wanted = set(names)
        return [e.spec for e in self._entries.values() if e.name in wanted]

    def counts_by_source(self) -> dict[str, int]:
        return dict(sorted(Counter(e.source for e in self._entries.values()).items()))

    def counts_by_domain(self) -> dict[str, int]:
        return dict(sorted(Counter(e.domain for e in self._entries.values()).items()))

    def schema_bytes(self, names: Iterable[str] | None = None) -> int:
        specs = self.specs(names)
        payload = [
            {"name": s.name, "description": s.description, "parameters": s.parameters}
            for s in specs
        ]
        return len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


_ENTRA_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("entra.users", ("user", "users", "password")),
    ("entra.groups", ("group", "member", "owner")),
    ("entra.applications", ("application", "service_principal", "credential", "permission")),
    ("entra.authentication", ("mfa", "authentication", "password_method")),
    ("entra.conditional_access", ("conditional_access", "ca_policy")),
    ("entra.audit", ("sign_in", "audit_log")),
    ("entra.roles", ("role", "privileged")),
    ("entra.devices", ("device", "managed_device")),
)

_AZURE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("azure.compute", ("compute", "virtual_machine", "vm", "container", "aks", "appservice")),
    ("azure.networking", ("network", "dns", "firewall", "load_balancer", "traffic", "private")),
    ("azure.storage", ("storage", "blob", "file", "disk")),
    ("azure.data", ("sql", "postgres", "mysql", "cosmos", "redis", "database")),
    ("azure.monitoring", ("monitor", "metric", "log", "health", "resourcehealth", "advisor")),
    ("azure.identity", ("role", "authorization", "keyvault", "managedidentity")),
    # Broad words such as "subscription", "governance", and "compliance" classify almost every
    # namespace; known guidance/scan tools are mapped explicitly below instead.
    ("azure.governance", ("policy", "resourcegraph", "managementgroup")),
    ("azure.pricing", ("pricing", "retail_price", "sku_price")),
)

_AZURE_EXPLICIT_BUNDLES: dict[str, tuple[str, ...]] = {
    "arm": ("azure.inventory", "azure.governance"),
    "advisor": ("azure.advisor", "azure.governance"),
    "azurebackup": ("azure.backup",),
    "extension_azqr": ("azure.governance",),
    "wellarchitectedframework": ("azure.governance",),
}


def _matches(text: str, candidates: tuple[str, ...]) -> bool:
    normalized = text.replace("-", "_").replace(" ", "_")
    return any(value in normalized for value in candidates)


def infer_metadata(
    name: str,
    description: str,
    *,
    source_hint: str = "connector",
    kind: str = "read",
) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
    lowered = f"{name} {description}".lower()
    normalized_name = name.lower().replace("-", "_")
    source = source_hint
    domain = "general"
    bundles: list[str] = []

    if source_hint == "entra_mcp":
        domain = "identity"
        for bundle, terms in _ENTRA_RULES:
            if _matches(normalized_name, terms):
                bundles.append(bundle)
        if kind == "write":
            bundles.append("entra.writes")
        else:
            bundles.append("entra.reads")
    elif source_hint == "azure_mcp":
        domain = "azure"
        for bundle, terms in _AZURE_RULES:
            if _matches(normalized_name, terms) or _matches(lowered, terms):
                bundles.append(bundle)
        bundles.extend(_AZURE_EXPLICIT_BUNDLES.get(normalized_name, ()))
        bundles.append("azure.reads" if kind == "read" else "azure.writes")
    elif name in _BUILTIN_TOOLS:
        source = "builtin"
        domain = "monitoring" if name == "azure_metrics" else "networking"
        bundles.append("azure.monitoring" if name == "azure_metrics" else "diagnostics.network")
    elif name in _IAM_TOOLS:
        source = "iam"
        domain = "identity"
        bundles.extend(("azure.identity", "access_review"))
    elif name in _OWNERSHIP_TOOLS:
        source = "ownership"
        domain = "governance"
        bundles.append("ownership")
    elif name in _ENTRA_IDENTITY_TOOLS:
        source = "entra_identity"
        domain = "identity"
        bundles.append("entra.conditional_access" if name.startswith("ca_") else "entra.users")
    elif name in _RECOVERY_TOOLS:
        source = "resiliency"
        domain = "reliability"
        bundles.extend(("azure.backup", "recovery"))
    elif name in _COST_TOOLS:
        source = "cost"
        domain = "cost"
        bundles.append("azure.cost")
    elif name in _ADVISOR_TOOLS:
        source = "advisor"
        domain = "governance"
        bundles.append("azure.advisor")
    elif name in _INVENTORY_TOOLS:
        source = "inventory"
        domain = "azure"
        bundles.extend(("azure.inventory", "azure.networking"))
    elif name in _PUBLIC_EXPOSURE_TOOLS:
        source = "inventory"
        domain = "azure"
        bundles.extend(("azure.inventory", "azure.networking", "azure.public_exposure"))
    elif name in _VM_TOOLS:
        source = "sandbox"
        domain = "networking"
        bundles.append("diagnostics.sandbox")
    elif name == "run_performance_profile":
        source = "workload"
        domain = "performance"
        bundles.extend(("azure.monitoring", "performance"))
    elif source_hint == "internal":
        source = "internal"
        domain = "routing"
        bundles.append("internal")
    else:
        source = source_hint or "connector"
        domain = "connector"
        bundles.append("connectors")

    if not bundles:
        bundles.append(f"{domain}.general")
    keywords = tuple(sorted(set(_WORD_RE.findall(lowered))))
    return source, domain, tuple(dict.fromkeys(bundles)), keywords


def make_entry(
    spec: ToolSpec,
    *,
    source_hint: str | None = None,
    kind: str | None = None,
) -> ToolEntry:
    actual_kind = kind or spec.kind or "read"
    source, domain, bundles, keywords = infer_metadata(
        spec.name,
        spec.description,
        source_hint=source_hint or spec.source or "connector",
        kind=actual_kind,
    )
    enriched = replace(
        spec,
        kind=actual_kind,
        source=source,
        domain=domain,
        bundles=bundles,
        keywords=keywords,
    )
    return ToolEntry(enriched, source, domain, bundles, actual_kind, keywords)
