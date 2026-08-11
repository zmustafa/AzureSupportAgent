"""Deterministic, provider-neutral tool routing and deferred catalog expansion."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Iterable

from app.agent.provider import ToolSpec
from app.agent.tool_catalog import ToolCatalog, ToolEntry

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "get",
    "access", "add", "apply", "assign", "change", "create", "delete", "deploy",
    "disable", "enabled", "enable", "in", "is", "it", "list", "modify", "my", "name",
    "names", "of", "on", "only", "or", "policies", "policy", "read", "remove", "report",
    "reset", "revoke", "rotate", "set", "show", "start", "state", "states", "stop",
    "tenant", "that", "the", "this", "to", "trigger", "update",
    "what", "which", "with",
})
_GREETING_RE = re.compile(
    r"^\s*(hi|hello|hey|good\s+(morning|afternoon|evening)|thanks|thank\s+you)[.!?\s]*$",
    re.IGNORECASE,
)
_WRITE_INTENT_RE = re.compile(
    r"\b(create|update|delete|remove|add|assign|revoke|reset|rotate|enable|disable|"
    r"modify|change|set|send|post|trigger|acknowledge|resolve|apply|deploy|restart|stop|start)\b",
    re.IGNORECASE,
)

# User vocabulary that should activate domains even when the underlying tool uses a
# provider-specific name. Values are routing metadata only, never authorization.
_BUNDLE_ALIASES: dict[str, tuple[str, ...]] = {
    "entra.users": ("user", "users", "guest", "account", "password"),
    "entra.groups": ("group", "groups", "membership", "member", "owner"),
    "entra.applications": (
        "application", "applications", "app", "service principal", "enterprise app",
        "credential", "secret", "certificate", "consent", "permission",
    ),
    "entra.authentication": ("mfa", "authentication", "auth method", "passwordless"),
    "entra.conditional_access": ("conditional access", "ca policy", "sign in policy"),
    "entra.audit": ("sign in", "signin", "audit log", "activity"),
    "entra.roles": ("directory role", "global admin", "privileged", "pim"),
    "entra.devices": ("intune", "managed device", "device compliance"),
    "azure.compute": ("vm", "virtual machine", "compute", "aks", "container", "app service"),
    "azure.networking": (
        "network", "dns", "firewall", "nsg", "route", "vnet", "subnet", "connectivity",
        "private endpoint", "load balancer", "application gateway",
    ),
    "azure.storage": ("storage", "blob", "file share", "managed disk"),
    "azure.data": ("sql", "database", "postgres", "mysql", "cosmos", "redis"),
    "azure.monitoring": (
        "monitor", "metric", "chart", "latency", "performance", "health", "alert", "log",
    ),
    "azure.identity": (
        "rbac", "effective access", "who can access", "role assignment", "managed identity",
        "key vault",
    ),
    "azure.governance": ("policy", "resource graph", "subscription", "management group", "cost"),
    "ownership": ("owner", "ownership", "unowned", "accountable"),
    "access_review": ("who can", "effective access", "privileged access", "revoke", "escalation"),
    "diagnostics.network": ("ping", "traceroute", "port", "http", "endpoint", "resolve"),
    "diagnostics.sandbox": ("sandbox", "inside vnet", "ssh", "run command"),
    "performance": ("performance", "bottleneck", "saturation", "throughput", "latency"),
    "connectors": ("email", "teams", "slack", "jira", "servicenow", "ticket", "notify"),
}


@dataclass(frozen=True)
class RouteDecision:
    name: str
    score: int
    reason: str


@dataclass
class RoutedToolSurface:
    catalog: ToolCatalog
    initial_budget: int = 24
    max_per_turn: int = 32
    search_page_size: int = 8
    active_names: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)
    loaded_bundles: set[str] = field(default_factory=set)

    def specs(self) -> list[ToolSpec]:
        return self.catalog.specs(self.active_names)

    def all_specs_for_native_search(self) -> list[ToolSpec]:
        active = set(self.active_names)
        return [replace(e.spec, defer_loading=e.name not in active) for e in self.catalog.entries()]

    def remaining_slots(self) -> int:
        return max(0, self.max_per_turn - len(self.active_names))

    def add(self, names: Iterable[str], *, reason: str, limit: int | None = None) -> list[str]:
        active = set(self.active_names)
        slots = self.remaining_slots()
        if limit is not None:
            slots = min(slots, max(0, limit))
        added: list[str] = []
        for name in names:
            if slots <= 0:
                break
            if name in active or self.catalog.get(name) is None:
                continue
            self.active_names.append(name)
            self.reasons[name] = reason
            active.add(name)
            added.append(name)
            slots -= 1
        return added

    def search(
        self,
        query: str,
        *,
        source: str = "",
        domain: str = "",
        bundles: Iterable[str] = (),
        limit: int | None = None,
        include_active: bool = False,
        allow_writes: bool | None = None,
    ) -> list[RouteDecision]:
        wanted_bundles = {str(v) for v in bundles if str(v)}
        active = set(self.active_names)
        writes_ok = allows_writes(query, wanted_bundles) if allow_writes is None else allow_writes
        ranked = rank_entries(query, self.catalog.entries(), preferred_bundles=wanted_bundles)
        out: list[RouteDecision] = []
        for decision in ranked:
            if decision.score < 6:
                continue
            entry = self.catalog.get(decision.name)
            if entry is None:
                continue
            if not include_active and entry.name in active:
                continue
            if source and entry.source != source:
                continue
            if domain and entry.domain != domain:
                continue
            if wanted_bundles and not wanted_bundles.intersection(entry.bundles):
                continue
            if entry.kind == "write" and entry.source != "azure_mcp" and not writes_ok:
                continue
            out.append(decision)
            if len(out) >= (limit or self.search_page_size):
                break
        return out

    def load_search_results(self, decisions: Iterable[RouteDecision]) -> list[str]:
        return self.add(
            [d.name for d in decisions],
            reason="deferred catalog search",
            limit=self.search_page_size,
        )

    def load_bundle(self, bundles: Iterable[str], *, query: str = "") -> list[str]:
        wanted = {str(v) for v in bundles if str(v)}
        self.loaded_bundles.update(wanted)
        decisions = rank_entries(query, self.catalog.entries(), preferred_bundles=wanted)
        names = [
            d.name
            for d in decisions
            if (entry := self.catalog.get(d.name)) is not None
            and wanted.intersection(entry.bundles)
            and (
                entry.kind != "write"
                or entry.source == "azure_mcp"
                or allows_writes(query, wanted)
            )
        ]
        return self.add(names, reason=f"bundle: {', '.join(sorted(wanted))}")

    def diagnostics(self) -> dict[str, object]:
        active = set(self.active_names)
        return {
            "available": len(self.catalog.names()),
            "selected": len(self.active_names),
            "withheld": max(0, len(self.catalog.names()) - len(self.active_names)),
            "initial_budget": self.initial_budget,
            "max_per_turn": self.max_per_turn,
            "schema_bytes_available": self.catalog.schema_bytes(),
            "schema_bytes_selected": self.catalog.schema_bytes(self.active_names),
            "by_source": self.catalog.counts_by_source(),
            "by_domain": self.catalog.counts_by_domain(),
            "selected_by_source": _count_selected(self.catalog, active, "source"),
            "selected_by_domain": _count_selected(self.catalog, active, "domain"),
            "selected_tools": list(self.active_names),
            "selection_reasons": dict(self.reasons),
        }


def _count_selected(catalog: ToolCatalog, active: set[str], attr: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in catalog.entries():
        if entry.name not in active:
            continue
        value = str(getattr(entry, attr))
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _tokens(text: str) -> set[str]:
    return {
        t
        for t in _TOKEN_RE.findall((text or "").lower())
        if len(t) > 1 and t not in _STOPWORDS
    }


def detect_bundles(query: str) -> set[str]:
    lowered = (query or "").lower()
    return {
        bundle
        for bundle, phrases in _BUNDLE_ALIASES.items()
        if any(
            re.search(
                rf"(?<![a-z0-9]){re.escape(phrase).replace(r'\ ', r'\s+')}(?![a-z0-9])",
                lowered,
            )
            for phrase in phrases
        )
    }


def allows_writes(query: str, bundles: set[str] | None = None) -> bool:
    bundles = bundles or set()
    return any(name.endswith(".writes") for name in bundles) or bool(
        _WRITE_INTENT_RE.search(query or "")
    )


def _score_entry(query: str, query_tokens: set[str], bundles: set[str], entry: ToolEntry) -> RouteDecision:
    name_tokens = _tokens(entry.name.replace("_", " "))
    description_tokens = _tokens(entry.spec.description)
    keyword_tokens = set(entry.keywords)
    name_hits = query_tokens.intersection(name_tokens)
    description_hits = query_tokens.intersection(description_tokens)
    keyword_hits = query_tokens.intersection(keyword_tokens)
    bundle_hits = bundles.intersection(entry.bundles)
    score = int(entry.spec.priority)
    score += len(name_hits) * 12
    score += len(description_hits) * 3
    score += len(keyword_hits)
    score += len(bundle_hits) * 18
    lowered = (query or "").lower().strip()
    if lowered and entry.name.lower() in lowered:
        score += 40
    if entry.spec.always_available:
        score += 1000
    reason_bits: list[str] = []
    if entry.spec.always_available:
        reason_bits.append("core")
    if bundle_hits:
        reason_bits.append("bundle " + ", ".join(sorted(bundle_hits)))
    if name_hits:
        reason_bits.append("name " + ", ".join(sorted(name_hits)))
    elif description_hits:
        reason_bits.append("description " + ", ".join(sorted(description_hits)[:4]))
    return RouteDecision(entry.name, score, "; ".join(reason_bits) or "catalog fallback")


def rank_entries(
    query: str,
    entries: Iterable[ToolEntry],
    *,
    preferred_bundles: set[str] | None = None,
) -> list[RouteDecision]:
    query_tokens = _tokens(query)
    bundles = detect_bundles(query).union(preferred_bundles or set())
    decisions = [_score_entry(query, query_tokens, bundles, entry) for entry in entries]
    return sorted(decisions, key=lambda d: (-d.score, d.name))


def route_initial(
    query: str,
    catalog: ToolCatalog,
    *,
    initial_budget: int = 24,
    max_per_turn: int = 32,
    search_page_size: int = 8,
    explicit_names: Iterable[str] = (),
    explicit_bundles: Iterable[str] = (),
    fill_to_budget: bool = False,
) -> RoutedToolSurface:
    initial_budget = max(4, min(int(initial_budget), int(max_per_turn)))
    max_per_turn = max(initial_budget, int(max_per_turn))
    surface = RoutedToolSurface(catalog, initial_budget, max_per_turn, search_page_size)
    explicit = {str(v) for v in explicit_names if str(v)}
    preferred = {str(v) for v in explicit_bundles if str(v)}

    always = sorted(e.name for e in catalog.entries() if e.spec.always_available)
    surface.add(always, reason="core routing capability")
    surface.add(sorted(explicit), reason="explicit agent selection")

    if preferred:
        surface.loaded_bundles.update(preferred)
    # Greetings should not pay for unrelated Azure/Graph schemas. The model can answer
    # directly or use search/load if the message carries hidden context in later turns.
    if _GREETING_RE.match(query or ""):
        return surface

    writes_ok = allows_writes(query, preferred)
    for decision in rank_entries(query, catalog.entries(), preferred_bundles=preferred):
        if len(surface.active_names) >= initial_budget:
            break
        if decision.score < 6 and not fill_to_budget:
            continue
        entry = catalog.get(decision.name)
        if (
            entry is not None
            and entry.kind == "write"
            and entry.source != "azure_mcp"
            and entry.name not in explicit
            and not writes_ok
        ):
            continue
        surface.add([decision.name], reason=decision.reason)
    return surface


def internal_tool_specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="search_tools",
            description=(
                "Search the permitted tool catalog when the tools currently visible are not enough. "
                "Returns compact matches and loads the best matches for the next model round. Use a "
                "specific capability query, not the user's entire conversation."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Capability to find."},
                    "source": {"type": "string", "description": "Optional exact source filter."},
                    "domain": {"type": "string", "description": "Optional exact domain filter."},
                    "bundles": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 12},
                    "load": {"type": "boolean", "description": "Load matches for the next round (default true)."},
                },
                "required": ["query"],
            },
            source="internal",
            domain="routing",
            bundles=("internal",),
            always_available=True,
            priority=100,
        ),
        ToolSpec(
            name="load_tool_bundle",
            description=(
                "Load one or more permitted Azure, Entra, connector, or diagnostic bundles into "
                "the next model round after catalog search or skill selection."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "bundles": {"type": "array", "items": {"type": "string"}},
                    "query": {"type": "string", "description": "Optional ranking hint."},
                },
                "required": ["bundles"],
            },
            source="internal",
            domain="routing",
            bundles=("internal",),
            always_available=True,
            priority=100,
        ),
        ToolSpec(
            name="load_skill",
            description=(
                "Load the full procedure for a listed support skill and activate its permitted tool "
                "bundles. Skills are guidance only and never grant authorization."
            ),
            parameters={
                "type": "object",
                "properties": {"skill_id": {"type": "string"}},
                "required": ["skill_id"],
            },
            source="internal",
            domain="routing",
            bundles=("internal",),
            always_available=True,
            priority=100,
        ),
        ToolSpec(
            name="read_tool_artifact",
            description=(
                "Read the next bounded page of a large tool result using the artifact id and offset "
                "returned by that tool."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 500, "maximum": 20000},
                },
                "required": ["artifact_id"],
            },
            source="internal",
            domain="routing",
            bundles=("internal",),
            always_available=True,
            priority=100,
        ),
    ]
