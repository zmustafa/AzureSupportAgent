"""Extract the canonical feature inventory from application source.

The public documentation claims, in /technical/documentation-regeneration/, that every
current route, navigation item, permission area and connector maps to documentation. That
claim was previously unenforceable: pages carry `feature_ids:` frontmatter, but nothing
compared those ids against the source.

This module is the source half of that check. It reads the implementation - never a
documentation page - and emits a sorted inventory of stable ids in the form
`NAMESPACE:id`. `_validate_public_docs.py` consumes the emitted JSON and fails when an id
is undocumented, or when a page claims an id that no longer exists.

Run from the docs/ directory:

    python _feature_inventory.py            # write _feature_inventory.json
    python _feature_inventory.py --print    # print the inventory to stdout

Every registry below names the exact file and symbol it came from. When a registry moves
or is renamed in the frontend, extraction returns nothing and this script exits non-zero
rather than silently shrinking the inventory - a quiet shrink would make undocumented
features look documented.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent
ROOT = DOCS.parent
FRONTEND = ROOT / "frontend" / "src"
COMPONENTS = FRONTEND / "components"
BACKEND = ROOT / "backend" / "app"

# Namespaces whose ids come from an `export const NAME = [ ... ]` array in navConfig.ts,
# where every element carries an `id:` field.
NAVCONFIG_ARRAYS = [
    "SECURITY_NAV",
    "ACCESS_NAV",
    "ADMIN_NAV",
    "PROACTIVE_NAV",
    "AUTOMATIONS_NAV",
    "POLICY_NAV",
    "INVENTORY_NAV",
    "TAGINTEL_NAV",
    "CHANGEEXPLORER_NAV",
    "IAM_NAV",
    "OWNERSHIP_NAV",
    "ENTRA_NAV",
    "ENTRA_CA_NAV",
]

# Namespaces whose ids live in a component-local constant rather than in navConfig.ts.
# `anchor` is a literal substring that must appear on the line that opens the collection;
# ids are the string literals that follow it, up to `terminator`.
COMPONENT_REGISTRIES = [
    {
        "namespace": "ALERTS_MANAGER_NAV",
        "path": COMPONENTS / "AlertsManagerView.tsx",
        "anchor": "const VALID_TABS = new Set<Tab>([",
        "terminator": "]",
    },
    {
        "namespace": "BACKUP_MANAGER_NAV",
        "path": COMPONENTS / "BackupManagerView.tsx",
        "anchor": "const TABS: { id: Tab; label: string; icon: string }[] = [",
        "terminator": "];",
        "id_field": True,
    },
    {
        "namespace": "RESILIENCY_NAV",
        "path": COMPONENTS / "ResiliencyView.tsx",
        "anchor": "const TABS: { id: Tab; label: string; icon: string }[] = [",
        "terminator": "];",
        "id_field": True,
    },
    {
        "namespace": "EVIDENCE_CONTENT_TABS",
        "path": COMPONENTS / "EvidenceLockerView.tsx",
        "anchor": "const CONTENT_TABS = [",
        "terminator": "]",
    },
    {
        "namespace": "INSIGHTS_NAV",
        "path": COMPONENTS / "InsightPacksView.tsx",
        "anchor": '(["today", "library", "runs", "schedule"] as const)',
        "terminator": ")",
    },
    {
        "namespace": "MONITORING_COVERAGE_LOCAL_TABS",
        "path": COMPONENTS / "MonitoringCoverageView.tsx",
        "anchor": 'useState<"coverage" | "all">',
        "anchor_only": True,
    },
    {
        "namespace": "TELEMETRY_COVERAGE_LOCAL_TABS",
        "path": COMPONENTS / "TelemetryCoverageView.tsx",
        "anchor": 'useState<"coverage" | "all">',
        "anchor_only": True,
    },
]

# Five screens carry a second, higher strip above their tabs: the single-scope view, a
# whole-estate `fleet` sweep, and a `cleanup` purge. Fleet and cleanup are separate
# capabilities with their own permissions and destructive behaviour, so they are inventoried
# as ids of the owning feature. The base member is the screen the tab strip already covers.
MAIN_VIEW_REGISTRIES = [
    ("ASSESSMENTS_NAV", COMPONENTS / "AssessmentsView.tsx", "AssessmentMainView", "assessments"),
    ("BACKUP_MANAGER_NAV", COMPONENTS / "BackupManagerView.tsx", "MainView", "manager"),
    ("BACKUPDR_NAV", COMPONENTS / "BackupDrCoverageView.tsx", "CoverageMainView", "coverage"),
    ("MONITORING_COVERAGE_LOCAL_TABS", COMPONENTS / "MonitoringCoverageView.tsx", "CoverageMainView", "coverage"),
    ("TELEMETRY_COVERAGE_LOCAL_TABS", COMPONENTS / "TelemetryCoverageView.tsx", "CoverageMainView", "coverage"),
]

# Shell areas that own a route and a landing page but are not themselves a nav ITEM.
SHELL_AREAS = ["automations", "admin", "proactive", "chat", "workloads"]

STRING_LITERAL = re.compile(r'"([a-z0-9][a-z0-9_-]*)"')
ID_FIELD = re.compile(r'id:\s*"([a-z0-9][a-z0-9_-]*)"')


class ExtractionError(RuntimeError):
    """Raised when a registry anchor no longer matches the source."""


def _read(path: Path) -> str:
    if not path.exists():
        raise ExtractionError(f"source file is missing: {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8", errors="replace")


def _block(text: str, opener: str, terminator: str, *, where: str) -> str:
    """Return the text between `opener` and the first following `terminator`."""
    start = text.find(opener)
    if start < 0:
        raise ExtractionError(f"anchor not found in {where}: {opener!r}")
    rest = text[start + len(opener):]
    end = rest.find(terminator)
    if end < 0:
        raise ExtractionError(f"terminator {terminator!r} not found after anchor in {where}")
    return rest[:end]


def navconfig_ids() -> dict[str, list[str]]:
    text = _read(COMPONENTS / "navConfig.ts")
    out: dict[str, list[str]] = {}
    for name in NAVCONFIG_ARRAYS:
        match = re.search(rf"export const {name}\b[^=]*=\s*\[", text)
        if not match:
            raise ExtractionError(f"navConfig.ts no longer exports an array named {name}")
        depth = 1
        i = match.end()
        while i < len(text) and depth:
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
            i += 1
        body = text[match.end(): i - 1]
        ids = ID_FIELD.findall(body)
        if not ids:
            raise ExtractionError(f"{name} in navConfig.ts yielded no id: fields")
        out[name] = ids

    # IAM keeps routable-but-unlisted tabs in a separate set. They are real URLs and every
    # existing bookmark and doc link depends on them, so they belong in the inventory.
    alias = _block(
        text,
        "export const IAM_ALIAS_TAB_IDS = new Set<IamTab>([",
        "]",
        where="navConfig.ts",
    )
    alias_ids = STRING_LITERAL.findall(alias)
    if not alias_ids:
        raise ExtractionError("IAM_ALIAS_TAB_IDS yielded no ids")
    out["IAM_NAV"] = sorted(set(out["IAM_NAV"]) | set(alias_ids))
    return out


def component_ids() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for reg in COMPONENT_REGISTRIES:
        path: Path = reg["path"]
        text = _read(path)
        if reg.get("anchor_only"):
            # The whole collection is spelled out in the anchor (a narrowed useState type).
            # Reading past it would sweep up unrelated literals from the same line.
            if reg["anchor"] not in text:
                raise ExtractionError(
                    f"anchor not found in {path.relative_to(ROOT)}: {reg['anchor']!r}"
                )
            ids = STRING_LITERAL.findall(reg["anchor"])
        else:
            body = _block(
                text,
                reg["anchor"],
                reg["terminator"],
                where=str(path.relative_to(ROOT)),
            )
            if reg.get("id_field"):
                ids = ID_FIELD.findall(body)
            else:
                ids = STRING_LITERAL.findall(reg["anchor"] + body)
        ids = [i for i in ids if i]
        if not ids:
            raise ExtractionError(
                f"{reg['namespace']} yielded no ids from {path.relative_to(ROOT)}"
            )
        out[reg["namespace"]] = sorted(set(ids))
    return out


def main_view_ids() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for namespace, path, type_name, base in MAIN_VIEW_REGISTRIES:
        text = _read(path)
        match = re.search(rf"^type {type_name} = ([^;]+);", text, re.M)
        if not match:
            raise ExtractionError(
                f"{path.relative_to(ROOT)} no longer declares `type {type_name} = ...`"
            )
        members = STRING_LITERAL.findall(match.group(1))
        if base not in members:
            raise ExtractionError(
                f"{type_name} in {path.relative_to(ROOT)} no longer has base view {base!r}"
            )
        extra = [m for m in members if m != base]
        if not extra:
            raise ExtractionError(f"{type_name} declares no view beyond the base {base!r}")
        out.setdefault(namespace, []).extend(extra)
    return {ns: sorted(set(ids)) for ns, ids in out.items()}


def route_ids() -> list[str]:
    """Top-level route segments declared in App.tsx, excluding redirect-only routes."""
    text = _read(FRONTEND / "App.tsx")
    routes = re.findall(r'<Route\s+path="([^"]+)"([^>]*)>', text)
    segments: set[str] = set()
    for path, attrs in routes:
        if "Navigate" in attrs or "Redirect" in attrs:
            continue
        head = path.strip("/").split("/")[0]
        if not head or head.startswith(":") or head == "*":
            continue
        segments.add(head)
    if not segments:
        raise ExtractionError("App.tsx yielded no routes")
    return sorted(segments)


def permission_ids() -> list[str]:
    text = _read(BACKEND / "auth" / "permissions.py")
    keys = re.findall(r'\(\s*"([a-z_]+\.[a-z_]+)"\s*,', text)
    if not keys:
        raise ExtractionError("permissions.py yielded no permission keys")
    return sorted(set(keys))


def connector_ids() -> list[str]:
    registry = _read(BACKEND / "connectors" / "registry.py")
    modules = re.findall(r"^\s*(\w+)\.CONNECTOR\.id:", registry, re.M)
    if not modules:
        raise ExtractionError("registry.py yielded no CONNECTOR_TYPES entries")
    ids: list[str] = []
    for module in modules:
        source = _read(BACKEND / "connectors" / f"{module}.py")
        match = re.search(r'^\s*id="([a-z0-9_]+)",', source, re.M)
        if not match:
            raise ExtractionError(f"connector module {module}.py declares no id=")
        ids.append(match.group(1))
    return sorted(set(ids))


def build() -> dict[str, object]:
    namespaces: dict[str, list[str]] = {}
    namespaces.update(navconfig_ids())
    namespaces.update(component_ids())
    for namespace, ids in main_view_ids().items():
        namespaces[namespace] = sorted(set(namespaces.get(namespace, [])) | set(ids))
    namespaces["SHELL_NAV"] = sorted(SHELL_AREAS)
    namespaces["ROUTE"] = route_ids()
    namespaces["PERMISSION"] = permission_ids()
    namespaces["CONNECTOR"] = connector_ids()

    feature_ids = sorted(
        f"{ns}:{fid}" for ns, ids in namespaces.items() for fid in ids
    )
    release = (ROOT / "RELEASE").read_text(encoding="utf-8").strip()
    return {
        "app_release": int(release),
        "namespaces": {ns: sorted(ids) for ns, ids in sorted(namespaces.items())},
        "feature_ids": feature_ids,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print", action="store_true", help="print instead of writing")
    args = parser.parse_args()
    try:
        inventory = build()
    except ExtractionError as exc:
        print(f"feature inventory extraction FAILED: {exc}", file=sys.stderr)
        print(
            "A registry was renamed or moved. Fix the anchor in _feature_inventory.py "
            "rather than deleting it: a silently smaller inventory reports undocumented "
            "features as documented.",
            file=sys.stderr,
        )
        return 2

    if args.print:
        json.dump(inventory, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        out = DOCS / "_feature_inventory.json"
        out.write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out.relative_to(ROOT)}")

    counts = {ns: len(ids) for ns, ids in inventory["namespaces"].items()}
    print(f"release {inventory['app_release']}, {len(inventory['feature_ids'])} feature ids")
    for ns, count in sorted(counts.items()):
        print(f"  {ns}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
