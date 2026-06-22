"""Canonical tag catalog registry (F2).

Per-tenant JSON store (``.data/tagintel_catalog.json``) of canonical tag definitions the
customer is standardizing on: canonical name, aliases, business purpose, allowed values,
whether it's required/inherited, the scope it applies at, and the standard's owner. No Azure
secrets, so plaintext JSON like the other local registries.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.tagintel.analysis import classify_key

_PATH = Path(__file__).resolve().parents[2] / ".data" / "tagintel_catalog.json"

_FIELDS = ("canonical", "aliases", "category", "purpose", "required", "inherited", "scope",
           "allowed_values", "example_values", "owner", "description")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    if _PATH.exists():
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write(data: dict[str, Any]) -> None:
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def _norm(entry: dict[str, Any]) -> dict[str, Any]:
    out = {
        "id": entry.get("id") or uuid.uuid4().hex,
        "canonical": str(entry.get("canonical", "")).strip(),
        "aliases": [str(a).strip() for a in (entry.get("aliases") or []) if str(a).strip()],
        "category": entry.get("category") or classify_key(entry.get("canonical", "")),
        "purpose": str(entry.get("purpose", "")),
        "required": bool(entry.get("required", False)),
        "inherited": bool(entry.get("inherited", False)),
        "scope": entry.get("scope") or "resource",
        "allowed_values": [str(v) for v in (entry.get("allowed_values") or [])],
        "example_values": [str(v) for v in (entry.get("example_values") or [])],
        "owner": str(entry.get("owner", "")),
        "description": str(entry.get("description", "")),
        "created_at": entry.get("created_at") or _now(),
        "updated_at": _now(),
    }
    return out


def list_catalog(tenant_id: str) -> list[dict[str, Any]]:
    bucket = _read().get(tenant_id or "default", {})
    rows = list(bucket.values()) if isinstance(bucket, dict) else []
    rows.sort(key=lambda e: (not e.get("required"), e.get("category", ""), e.get("canonical", "").lower()))
    return rows


def upsert(tenant_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    if not str(entry.get("canonical", "")).strip():
        raise ValueError("canonical name is required")
    data = _read()
    bucket = data.setdefault(tenant_id or "default", {})
    norm = _norm(entry)
    # Preserve created_at on update.
    if norm["id"] in bucket:
        norm["created_at"] = bucket[norm["id"]].get("created_at", norm["created_at"])
    bucket[norm["id"]] = norm
    _write(data)
    return norm


def delete(tenant_id: str, entry_id: str) -> bool:
    data = _read()
    bucket = data.get(tenant_id or "default", {})
    if entry_id in bucket:
        del bucket[entry_id]
        _write(data)
        return True
    return False


def required_keys(tenant_id: str) -> list[str]:
    return [e["canonical"] for e in list_catalog(tenant_id) if e.get("required")]


def seed_from_census(tenant_id: str, census_keys: list[dict[str, Any]], key_clusters: list[dict[str, Any]],
                     limit: int = 12) -> list[dict[str, Any]]:
    """Create draft catalog entries from the most-used discovered keys (skipping any already
    cataloged). Aliases are pulled from the hygiene key-clusters so casing variants fold in."""
    existing = {a.lower() for e in list_catalog(tenant_id) for a in [e["canonical"], *e["aliases"]]}
    alias_map: dict[str, list[str]] = {}
    for c in key_clusters:
        alias_map[c["canonical"].lower()] = [m for m in c["members"] if m != c["canonical"]]

    created = []
    for k in census_keys[:limit]:
        canon = k["key"]
        if canon.lower() in existing:
            continue
        cat = k.get("category", "other")
        entry = {
            "canonical": canon,
            "aliases": alias_map.get(canon.lower(), k.get("casing_variants", [])),
            "category": cat,
            "purpose": _PURPOSE_HINTS.get(cat, ""),
            "required": cat in ("billing", "ownership", "environment"),
            "inherited": cat in ("billing", "organization"),
            "scope": "subscription,resource_group,resource" if cat in ("billing", "organization") else "resource",
            "allowed_values": [v["value"] for v in k.get("top_values", [])][:10] if k.get("distinct_values", 0) <= 12 else [],
            "example_values": [v["value"] for v in k.get("top_values", [])][:3],
            "owner": "",
            "description": "",
        }
        created.append(upsert(tenant_id, entry))
        existing.add(canon.lower())
    return created


_PURPOSE_HINTS = {
    "billing": "Billing, chargeback and cost allocation.",
    "ownership": "Identifies the owning team or person for support and accountability.",
    "environment": "Deployment environment (production / staging / development / etc.).",
    "application": "Logical application or workload the resource belongs to.",
    "organization": "Owning business unit / department for reporting.",
    "security": "Data classification / security handling requirement.",
    "lifecycle": "Lifecycle / expiration / decommission metadata.",
    "operations": "Operational metadata (backup, DR, patching, criticality).",
    "other": "",
}
