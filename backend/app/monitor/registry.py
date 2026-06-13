"""Monitor dashboards registry (JSON).

An Azure-Dashboard-style customizable Monitor view (Monitor 2.0). A *dashboard* is a
named, saved arrangement of **widgets** on a 12-column grid. Each widget is a typed,
data-bound visualization::

    {
      "id": "w_xxx", "title": "CPU", "type": "chart",
      "layout": {"x":0,"y":0,"w":6,"h":4},
      "dataSource": {"kind":"azure_metrics", ...},
      "transform": {...}, "viz": {...},
      "refresh": {"mode":"live","intervalSec":60},
      "links": {...}, "conditional": [...]
    }

Back-compat: the original ``tiles`` list (``{tileId, x, y, w, h}`` builtin tiles) is
preserved AND surfaced as ``type: "builtin"`` widgets, so dashboards saved by Monitor 1.0
keep working. Dashboards also carry lightweight ``version`` + ``revisions`` history
(mirroring the architectures/memory registries). Persisted under
backend/.data/monitor_dashboards.json, tenant-scoped. No secrets, so no encryption.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "monitor_dashboards.json"

MAX_REVISIONS = 30

DEFAULTS: dict[str, Any] = {
    "name": "Untitled dashboard",
    "description": "",
    "tenant_id": "",
    "is_default": False,
    # Legacy builtin tile placements: [{tileId, x, y, w, h}] on a 12-column grid.
    "tiles": [],
    # Monitor 2.0 data-bound widgets (see module docstring).
    "widgets": [],
    # Dashboard-level parameters (e.g. subscription / timespan) cascaded into widgets.
    "params": [],
    # Optional workload this dashboard was generated for (AI build-from-workload).
    "workload_id": "",
    # AI generation metadata: design brief, context digest, critic notes, dry-run summary.
    "ai_design": {},
    "version": 1,
    "revisions": [],
    "created_by": "",
    "updated_by": "",
    "created_at": "",
    "updated_at": "",
}

# Copied wholesale on upsert (everything except server-managed timestamps/id/history).
_FIELDS = [
    k for k in DEFAULTS if k not in ("created_at", "updated_at", "version", "revisions")
]

# ---- widget validation -------------------------------------------------------------

WIDGET_TYPES = (
    "stat", "chart", "table", "list", "gauge", "map", "markdown", "clock",
    "availability", "builtin", "iframe",
)
DATASOURCE_KINDS = (
    "app_telemetry", "resource_graph", "log_analytics", "azure_metrics",
    "web_ping", "tcp_ping", "workbook_ref", "resource_health", "azure_cost",
    "static", "none",
)
_REFRESH_MODES = ("live", "manual")


def _as_int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _clean_layout(raw: Any) -> dict[str, int]:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "x": _as_int(raw.get("x", 0), 0, 0, 12),
        "y": _as_int(raw.get("y", 0), 0, 0, 10_000),
        "w": _as_int(raw.get("w", 4), 4, 1, 12),
        "h": _as_int(raw.get("h", 3), 3, 1, 40),
    }


def _clean_datasource(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    kind = str(raw.get("kind") or "none").strip()
    if kind not in DATASOURCE_KINDS:
        kind = "none"
    out: dict[str, Any] = {"kind": kind}
    # Pass-through known config keys (kept permissive — resolvers validate their own).
    for key in (
        "connection_id", "query", "subscription_id", "resource_group",
        "resource_id", "resource_ids", "metric", "metrics", "aggregation", "interval",
        "timespan", "workspace_id", "url", "method", "host", "port",
        "assert_status", "assert_body", "workbook_id", "metric_key",
        "series", "rows", "columns", "telemetry_key", "params", "top",
        "sample_every_s",
    ):
        if key in raw and raw[key] is not None:
            out[key] = raw[key]
    return out


def _clean_refresh(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    mode = str(raw.get("mode") or "manual").strip()
    if mode not in _REFRESH_MODES:
        mode = "manual"
    return {"mode": mode, "intervalSec": _as_int(raw.get("intervalSec", 60), 60, 5, 86_400)}


def _clean_widget(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    wtype = str(raw.get("type") or "").strip()
    if wtype not in WIDGET_TYPES:
        return None
    wid = str(raw.get("id") or "").strip() or f"w_{uuid.uuid4().hex[:10]}"
    widget: dict[str, Any] = {
        "id": wid,
        "title": str(raw.get("title") or "")[:200],
        "type": wtype,
        "layout": _clean_layout(raw.get("layout")),
        "dataSource": _clean_datasource(raw.get("dataSource")),
        "transform": raw.get("transform") if isinstance(raw.get("transform"), dict) else {},
        "viz": raw.get("viz") if isinstance(raw.get("viz"), dict) else {},
        "refresh": _clean_refresh(raw.get("refresh")),
        "links": raw.get("links") if isinstance(raw.get("links"), dict) else {},
        "conditional": raw.get("conditional") if isinstance(raw.get("conditional"), list) else [],
    }
    # Builtin widgets carry the legacy tileId they render.
    if wtype == "builtin":
        widget["tileId"] = str(raw.get("tileId") or raw.get("dataSource", {}).get("tileId") or "").strip()
    return widget


def _clean_widgets(widgets: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for w in widgets or []:
        cleaned = _clean_widget(w)
        if cleaned is None:
            continue
        if cleaned["id"] in seen:
            cleaned["id"] = f"w_{uuid.uuid4().hex[:10]}"
        seen.add(cleaned["id"])
        out.append(cleaned)
    return out


def _clean_params(params: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in params or []:
        if not isinstance(p, dict):
            continue
        key = str(p.get("key") or "").strip()
        if not key:
            continue
        out.append({
            "key": key,
            "label": str(p.get("label") or key)[:120],
            "type": str(p.get("type") or "text")[:40],
            "default": p.get("default", ""),
            "options": p.get("options") if isinstance(p.get("options"), list) else [],
        })
    return out


def _migrate_tiles_to_widgets(tiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Surface legacy ``{tileId,x,y,w,h}`` placements as ``type:"builtin"`` widgets."""
    out: list[dict[str, Any]] = []
    for t in tiles or []:
        tid = str(t.get("tileId") or "").strip()
        if not tid:
            continue
        out.append({
            "id": f"builtin_{tid}",
            "title": "",
            "type": "builtin",
            "tileId": tid,
            "layout": {"x": t.get("x", 0), "y": t.get("y", 0), "w": t.get("w", 4), "h": t.get("h", 3)},
            "dataSource": {"kind": "none"},
            "transform": {},
            "viz": {},
            "refresh": {"mode": "manual", "intervalSec": 60},
            "links": {},
            "conditional": [],
        })
    return out


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
    return {"dashboards": {}}


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _merge(did: str, raw: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(DEFAULTS))
    merged.update(raw)
    merged["id"] = did
    # Ensure widgets exist: migrate legacy tiles for dashboards saved by Monitor 1.0.
    if not merged.get("widgets") and merged.get("tiles"):
        merged["widgets"] = _migrate_tiles_to_widgets(merged["tiles"])
    return merged


def _clean_tiles(tiles: Any) -> list[dict[str, Any]]:
    """Validate + normalize incoming tile placements (drop malformed entries)."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for t in tiles or []:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("tileId") or "").strip()
        if not tid or tid in seen:
            continue
        seen.add(tid)
        try:
            out.append(
                {
                    "tileId": tid,
                    "x": max(0, min(12, int(t.get("x", 0)))),
                    "y": max(0, int(t.get("y", 0))),
                    "w": max(1, min(12, int(t.get("w", 4)))),
                    "h": max(1, min(40, int(t.get("h", 3)))),
                }
            )
        except (TypeError, ValueError):
            continue
    return out


def list_dashboards(tenant_id: str | None = None) -> list[dict[str, Any]]:
    data = _read()
    out = [_merge(did, d) for did, d in data.get("dashboards", {}).items()]
    if tenant_id is not None:
        out = [d for d in out if (d.get("tenant_id") or "") in ("", tenant_id)]
    # Default first, then newest-updated.
    out.sort(key=lambda d: (not d.get("is_default"), d.get("updated_at", "")), reverse=False)
    out.sort(key=lambda d: d.get("updated_at", ""), reverse=True)
    out.sort(key=lambda d: not d.get("is_default"))
    return out


def get_dashboard(dashboard_id: str) -> dict[str, Any] | None:
    data = _read()
    raw = data.get("dashboards", {}).get(dashboard_id)
    return _merge(dashboard_id, raw) if raw is not None else None


def upsert_dashboard(dashboard: dict[str, Any], *, actor: str = "") -> dict[str, Any]:
    data = _read()
    dashboards = data.setdefault("dashboards", {})
    did = dashboard.get("id") or str(uuid.uuid4())
    existing = dashboards.get(did, {})
    merged = dict(existing)
    for key in _FIELDS:
        if key in dashboard and dashboard[key] is not None:
            merged[key] = dashboard[key]
    merged["tiles"] = _clean_tiles(dashboard.get("tiles", existing.get("tiles", [])))
    if "widgets" in dashboard:
        merged["widgets"] = _clean_widgets(dashboard.get("widgets"))
    else:
        merged["widgets"] = _clean_widgets(existing.get("widgets", []))
    if "params" in dashboard:
        merged["params"] = _clean_params(dashboard.get("params"))
    else:
        merged.setdefault("params", _clean_params(existing.get("params", [])))
    # Version + revision snapshot (keep a bounded history of prior widget arrangements).
    prev_version = int(existing.get("version", 1) or 1)
    if existing:
        revisions = list(existing.get("revisions", []))
        revisions.append({
            "version": prev_version,
            "at": _now(),
            "by": existing.get("updated_by", ""),
            "widgets": existing.get("widgets", []),
            "tiles": existing.get("tiles", []),
            "params": existing.get("params", []),
            "name": existing.get("name", ""),
        })
        merged["revisions"] = revisions[-MAX_REVISIONS:]
        merged["version"] = prev_version + 1
    else:
        merged["revisions"] = []
        merged["version"] = 1
    merged["created_at"] = existing.get("created_at") or _now()
    merged["updated_at"] = _now()
    if actor:
        merged["updated_by"] = actor
        if not existing:
            merged.setdefault("created_by", actor)
    merged.pop("id", None)
    dashboards[did] = merged
    # A single default per tenant: if this one is default, clear the flag on its siblings.
    if merged.get("is_default"):
        tid = merged.get("tenant_id", "")
        for other_id, other in dashboards.items():
            if other_id != did and (other.get("tenant_id") or "") == tid:
                other["is_default"] = False
    _write(data)
    result = get_dashboard(did)
    assert result is not None
    return result


def list_revisions(dashboard_id: str) -> list[dict[str, Any]]:
    """Prior saved versions of a dashboard (newest first), without the current one."""
    dash = get_dashboard(dashboard_id)
    if dash is None:
        return []
    revs = list(dash.get("revisions", []))
    revs.sort(key=lambda r: r.get("version", 0), reverse=True)
    return revs


def restore_revision(dashboard_id: str, version: int, *, actor: str = "") -> dict[str, Any] | None:
    """Restore a dashboard's widgets/params from a prior revision (creates a new version)."""
    dash = get_dashboard(dashboard_id)
    if dash is None:
        return None
    target = next((r for r in dash.get("revisions", []) if int(r.get("version", -1)) == int(version)), None)
    if target is None:
        return None
    payload = dict(dash)
    payload["id"] = dashboard_id
    payload["widgets"] = target.get("widgets", [])
    payload["tiles"] = target.get("tiles", [])
    payload["params"] = target.get("params", [])
    return upsert_dashboard(payload, actor=actor)


def delete_dashboard(dashboard_id: str) -> bool:
    data = _read()
    if dashboard_id in data.get("dashboards", {}):
        del data["dashboards"][dashboard_id]
        _write(data)
        return True
    return False


def set_default(dashboard_id: str, tenant_id: str, actor: str = "") -> dict[str, Any] | None:
    """Make one dashboard the tenant's default, clearing the flag on the others."""
    data = _read()
    dashboards = data.get("dashboards", {})
    if dashboard_id not in dashboards:
        return None
    for did, d in dashboards.items():
        if (d.get("tenant_id") or "") == tenant_id:
            d["is_default"] = did == dashboard_id
    dashboards[dashboard_id]["updated_at"] = _now()
    if actor:
        dashboards[dashboard_id]["updated_by"] = actor
    _write(data)
    return get_dashboard(dashboard_id)
