"""Azure Policy local registry (JSON).

Persists three things the toolkit needs across requests (no Azure secrets, so plaintext
JSON like the other registries):

* ``snapshots`` — point-in-time captures of inventory + compliance counts, used for
  posture-over-time trends and drift-since-last-scan.
* ``drafts`` — AI-authored or hand-edited candidate policy definitions/assignments the
  user is iterating on before exporting to IaC.
* ``iac_sources`` — a stored "source of truth" (e.g. an EPAC/Bicep export) to diff live
  assignments against for policy-as-code drift.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "policy.json"

# --- Server-side inventory cache (separate file so the big payloads don't bloat the
# registry). In-memory for instant hits + file-persisted so a backend restart stays fast.
_CACHE_PATH = Path(__file__).resolve().parents[2] / ".data" / "policy_cache.json"
_mem_cache: dict[str, Any] | None = None

_MAX_SNAPSHOTS = 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("snapshots", {})
    data.setdefault("drafts", {})
    data.setdefault("iac_sources", {})
    data.setdefault("enforcement_links", {})
    return data


def _store(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        data = {}
    data.setdefault("snapshots", {})
    data.setdefault("drafts", {})
    data.setdefault("iac_sources", {})
    data.setdefault("enforcement_links", {})
    return data


# --------------------------------------------------------------------------- snapshots
def save_snapshot(tenant_id: str, connection_id: str, summary: dict[str, Any], actor: str = "") -> dict[str, Any]:
    """Persist a compact compliance/inventory snapshot for trend + drift analysis."""
    sid = uuid.uuid4().hex[:12]
    snap = {
        "id": sid,
        "tenant_id": tenant_id or "",
        "connection_id": connection_id or "",
        "created_at": _now(),
        "created_by": actor,
        "summary": summary,  # {counts, compliance:{...}, by_effect, by_enforcement}
    }
    def _mutate(raw: Any) -> dict[str, Any]:
        data = _store(raw)
        data["snapshots"][sid] = snap
        # Trim oldest beyond the cap.
        snapshots = sorted(
            data["snapshots"].values(), key=lambda stored: stored["created_at"], reverse=True
        )
        if len(snapshots) > _MAX_SNAPSHOTS:
            for old in snapshots[_MAX_SNAPSHOTS:]:
                data["snapshots"].pop(old["id"], None)
        return data

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return snap


def list_snapshots(tenant_id: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
    data = _read()
    out = list(data.get("snapshots", {}).values())
    if tenant_id is not None:
        out = [s for s in out if (s.get("tenant_id") or "") in ("", tenant_id)]
    out.sort(key=lambda s: s.get("created_at", ""), reverse=True)
    return out[:limit]


def latest_snapshot(tenant_id: str | None = None) -> dict[str, Any] | None:
    snaps = list_snapshots(tenant_id, limit=1)
    return snaps[0] if snaps else None


# --------------------------------------------------------------------------- saved simulations
# Completed Safe-Rollout Planner runs, persisted so the user can reopen a previous simulation
# and review its impact / staged plan. Kept in a dedicated file (results are bulky) so the main
# registry stays lean. Listing returns compact summaries; the full result is fetched on open.
_SIMS_PATH = Path(__file__).resolve().parents[2] / ".data" / "policy_simulations.json"
_MAX_SIMS = 100


def _sims_read() -> dict[str, Any]:
    data = jsonstore.read_json(_SIMS_PATH, {})
    return data if isinstance(data, dict) else {}


def _sim_summary(rec: dict[str, Any]) -> dict[str, Any]:
    """A saved simulation without the heavy ``result`` payload (for list views)."""
    return {k: v for k, v in rec.items() if k != "result"}


def save_simulation(tenant_id: str, rec: dict[str, Any], actor: str = "") -> dict[str, Any]:
    """Persist a completed simulation. Derives the display metadata from the result so the
    client only has to post the raw result + workload context. Returns the summary."""
    sid = uuid.uuid4().hex[:12]
    result = rec.get("result") or {}
    target = result.get("target_state") or {}
    impact = result.get("impact") or {}
    blast = result.get("blast") or {}
    plan = result.get("plan") or {}
    saved = {
        "id": sid,
        "tenant_id": tenant_id or "",
        "workload_id": rec.get("workload_id", "") or "",
        "workload_name": rec.get("workload_name", "") or "",
        "connection_id": rec.get("connection_id", "") or "",
        "mode": result.get("mode", ""),
        "title": result.get("display_name") or "Simulation",
        "scope": target.get("scope", ""),
        "scope_label": target.get("scope_label", ""),
        "target_effect": target.get("effect", ""),
        "target_enforcement": target.get("enforcement", ""),
        "impact_count": impact.get("count", 0),
        "impact_supported": bool(impact.get("supported", False)),
        "risk_level": (blast or {}).get("risk_level", "") if isinstance(blast, dict) else "",
        "go_no_go": (plan or {}).get("go_no_go", "") if isinstance(plan, dict) else "",
        "check_id": result.get("check_id", ""),
        "result": result,
        "created_at": _now(),
        "created_by": actor,
    }
    def _mutate(store: dict[str, Any]) -> None:
        store[sid] = saved
        # Trim oldest beyond the cap.
        records = sorted(
            store.values(), key=lambda stored: stored.get("created_at", ""), reverse=True
        )
        if len(records) > _MAX_SIMS:
            for old in records[_MAX_SIMS:]:
                store.pop(old["id"], None)

    jsonstore.mutate_json(_SIMS_PATH, {}, _mutate, indent=None)
    return _sim_summary(saved)


def list_simulations(tenant_id: str, workload_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    store = _sims_read()
    out = [r for r in store.values() if (r.get("tenant_id") or "") in ("", tenant_id)]
    if workload_id:
        out = [r for r in out if (r.get("workload_id") or "") == workload_id]
    out.sort(key=lambda s: s.get("created_at", ""), reverse=True)
    return [_sim_summary(r) for r in out[:limit]]


def get_simulation(tenant_id: str, sim_id: str) -> dict[str, Any] | None:
    rec = _sims_read().get(sim_id)
    if not rec or (rec.get("tenant_id") or "") not in ("", tenant_id):
        return None
    return rec


def delete_simulation(tenant_id: str, sim_id: str) -> bool:
    deleted = False

    def _mutate(store: dict[str, Any]) -> None:
        nonlocal deleted
        rec = store.get(sim_id)
        if rec and (rec.get("tenant_id") or "") in ("", tenant_id):
            store.pop(sim_id, None)
            deleted = True

    jsonstore.mutate_json(_SIMS_PATH, {}, _mutate, indent=None)
    return deleted


# ----------------------------------------------------------------- coverage-gap analyses
# History of Coverage-gap analyses (baseline comparison + AI built-in proposals), persisted so
# the user can reopen a previous run and review which controls were missing / proposed. Kept in
# a dedicated file (the proposal payload is bulky) so the main registry stays lean. Listing
# returns compact summaries; the full result is fetched on open.
_COV_PATH = Path(__file__).resolve().parents[2] / ".data" / "policy_coverage_runs.json"
_MAX_COV = 100


def _cov_read() -> dict[str, Any]:
    data = jsonstore.read_json(_COV_PATH, {})
    return data if isinstance(data, dict) else {}


def _cov_summary(rec: dict[str, Any]) -> dict[str, Any]:
    """A saved coverage analysis without the heavy ``result`` payload (for list views)."""
    return {k: v for k, v in rec.items() if k != "result"}


def save_coverage_run(tenant_id: str, rec: dict[str, Any], actor: str = "") -> dict[str, Any]:
    """Persist a completed Coverage-gap analysis. Derives display metadata from the result so
    the client only posts the raw result + workload context. Returns the summary."""
    rid = uuid.uuid4().hex[:12]
    result = rec.get("result") or {}
    proposals = result.get("proposals") or []
    saved = {
        "id": rid,
        "tenant_id": tenant_id or "",
        "workload_id": rec.get("workload_id", "") or "",
        "workload_name": rec.get("workload_name", "") or "",
        "connection_id": rec.get("connection_id", "") or "",
        "baseline_id": result.get("baseline_id", ""),
        "baseline_label": result.get("baseline_label", ""),
        "total": result.get("total", 0),
        "covered_count": result.get("covered_count", 0),
        "missing_count": result.get("missing_count", 0),
        "coverage_pct": result.get("coverage_pct", 0),
        "proposals_count": len(proposals) if isinstance(proposals, list) else 0,
        "result": result,
        "created_at": _now(),
        "created_by": actor,
    }
    def _mutate(store: dict[str, Any]) -> None:
        store[rid] = saved
        # Trim oldest beyond the cap.
        records = sorted(
            store.values(), key=lambda stored: stored.get("created_at", ""), reverse=True
        )
        if len(records) > _MAX_COV:
            for old in records[_MAX_COV:]:
                store.pop(old["id"], None)

    jsonstore.mutate_json(_COV_PATH, {}, _mutate, indent=None)
    return _cov_summary(saved)


def list_coverage_runs(tenant_id: str, workload_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    store = _cov_read()
    out = [r for r in store.values() if (r.get("tenant_id") or "") in ("", tenant_id)]
    if workload_id:
        out = [r for r in out if (r.get("workload_id") or "") == workload_id]
    out.sort(key=lambda s: s.get("created_at", ""), reverse=True)
    return [_cov_summary(r) for r in out[:limit]]


def get_coverage_run(tenant_id: str, run_id: str) -> dict[str, Any] | None:
    rec = _cov_read().get(run_id)
    if not rec or (rec.get("tenant_id") or "") not in ("", tenant_id):
        return None
    return rec


def delete_coverage_run(tenant_id: str, run_id: str) -> bool:
    deleted = False

    def _mutate(store: dict[str, Any]) -> None:
        nonlocal deleted
        rec = store.get(run_id)
        if rec and (rec.get("tenant_id") or "") in ("", tenant_id):
            store.pop(run_id, None)
            deleted = True

    jsonstore.mutate_json(_COV_PATH, {}, _mutate, indent=None)
    return deleted


# --------------------------------------------------------------------------- drafts
def list_drafts(tenant_id: str | None = None) -> list[dict[str, Any]]:
    data = _read()
    out = list(data.get("drafts", {}).values())
    if tenant_id is not None:
        out = [d for d in out if (d.get("tenant_id") or "") in ("", tenant_id)]
    out.sort(key=lambda d: d.get("updated_at", ""), reverse=True)
    return out


def save_draft(draft: dict[str, Any], actor: str = "") -> dict[str, Any]:
    did = draft.get("id") or uuid.uuid4().hex[:12]
    rec: dict[str, Any] = {}

    def _mutate(raw: Any) -> dict[str, Any]:
        data = _store(raw)
        existing = data["drafts"].get(did, {})
        rec.update({
            "id": did,
            "tenant_id": draft.get("tenant_id", existing.get("tenant_id", "")),
            "title": draft.get("title", existing.get("title", "Untitled policy")),
            "kind": draft.get("kind", existing.get("kind", "definition")),
            "intent": draft.get("intent", existing.get("intent", "")),
            "policy_json": draft.get("policy_json", existing.get("policy_json", {})),
            "notes": draft.get("notes", existing.get("notes", "")),
            "created_at": existing.get("created_at") or _now(),
            "created_by": existing.get("created_by") or actor,
            "updated_at": _now(),
            "updated_by": actor,
        })
        data["drafts"][did] = rec
        return data

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return rec


def delete_draft(draft_id: str) -> bool:
    deleted = False

    def _mutate(raw: Any) -> dict[str, Any]:
        nonlocal deleted
        data = _store(raw)
        if draft_id in data["drafts"]:
            data["drafts"].pop(draft_id, None)
            deleted = True
        return data

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return deleted


# --------------------------------------------------------------------------- iac sources
def set_iac_source(tenant_id: str, content: str, fmt: str, actor: str = "") -> dict[str, Any]:
    sid = tenant_id or "default"
    rec = {
        "id": sid,
        "tenant_id": tenant_id or "",
        "format": fmt,  # epac | bicep | terraform | json
        "content": content[:200_000],
        "updated_at": _now(),
        "updated_by": actor,
    }
    def _mutate(raw: Any) -> dict[str, Any]:
        data = _store(raw)
        data["iac_sources"][sid] = rec
        return data

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return rec


def get_iac_source(tenant_id: str) -> dict[str, Any] | None:
    data = _read()
    return data.get("iac_sources", {}).get(tenant_id or "default")


# --------------------------------------------------------------------------- enforcement links
# Bridge between an assessment finding and the Azure Policy guardrail planned to enforce it.
# Powers the "✅ Guardrail planned" badge in the assessment report and reverse links in Policy.
def _link_key(tenant_id: str, workload_id: str, check_id: str) -> str:
    return f"{tenant_id or ''}|{workload_id or ''}|{check_id or ''}"


def save_enforcement_link(
    tenant_id: str, workload_id: str, check_id: str, data: dict[str, Any], actor: str = ""
) -> dict[str, Any]:
    rec = {
        "tenant_id": tenant_id or "",
        "workload_id": workload_id or "",
        "check_id": check_id,
        "title": data.get("title", ""),
        "definition_id": data.get("definition_id", ""),
        "builtin_name": data.get("builtin_name", ""),
        "target_effect": data.get("target_effect", ""),
        "target_scope": data.get("target_scope", ""),
        "go_no_go": data.get("go_no_go", ""),
        "plan_summary": data.get("plan_summary", ""),
        "impact_count": data.get("impact_count", 0),
        "frameworks": data.get("frameworks", {}),
        "planned_by": actor,
        "planned_at": _now(),
    }
    def _mutate(raw: Any) -> dict[str, Any]:
        store = _store(raw)
        store["enforcement_links"][_link_key(tenant_id, workload_id, check_id)] = rec
        return store

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return rec


def list_enforcement_links(tenant_id: str, workload_id: str | None = None) -> list[dict[str, Any]]:
    store = _read()
    out = list(store.get("enforcement_links", {}).values())
    out = [r for r in out if (r.get("tenant_id") or "") in ("", tenant_id)]
    if workload_id is not None:
        out = [r for r in out if (r.get("workload_id") or "") == workload_id]
    out.sort(key=lambda r: r.get("planned_at", ""), reverse=True)
    return out



# --------------------------------------------------------------------------- inventory cache
def _cache_load() -> dict[str, Any]:
    """Lazy-load the on-disk cache into the module-level dict (once per process)."""
    global _mem_cache
    loaded = jsonstore.read_json(_CACHE_PATH, {})
    _mem_cache = loaded if isinstance(loaded, dict) else {}
    return _mem_cache


def _cache_key(tenant_id: str, connection_id: str, with_compliance: bool, workload_id: str = "") -> str:
    return f"{tenant_id or ''}|{connection_id or ''}|{workload_id or ''}|{int(bool(with_compliance))}"


def get_inventory_cache(
    tenant_id: str, connection_id: str, with_compliance: bool, workload_id: str = "", ttl: int | None = None
) -> dict[str, Any] | None:
    """Return the cached inventory payload, or None if missing. By default the cache is
    PERMANENT (``ttl=None``): a stored payload is reused indefinitely until an explicit refresh
    (``force``) overwrites it, so the slow Azure Policy collection runs only when asked. Pass a
    positive ``ttl`` (seconds) to treat older entries as a miss."""
    cache = _cache_load()
    entry = cache.get(_cache_key(tenant_id, connection_id, with_compliance, workload_id))
    if not entry:
        return None
    age = time.time() - float(entry.get("ts", 0))
    if ttl is not None and age > ttl:
        return None
    return {"payload": entry.get("payload", {}), "fetched_at": entry.get("fetched_at", ""), "age_seconds": int(age)}


def set_inventory_cache(
    tenant_id: str, connection_id: str, with_compliance: bool, payload: dict[str, Any], workload_id: str = ""
) -> str:
    """Store an inventory payload and return the stored ``fetched_at`` ISO timestamp."""
    fetched = _now()
    entry = {
        "ts": time.time(),
        "fetched_at": fetched,
        "payload": payload,
    }
    global _mem_cache

    def _mutate(cache: dict[str, Any]) -> None:
        cache[_cache_key(tenant_id, connection_id, with_compliance, workload_id)] = entry

    try:
        _mem_cache = jsonstore.mutate_json(_CACHE_PATH, {}, _mutate, indent=None)
    except OSError:
        pass
    return fetched


def clear_inventory_cache(tenant_id: str | None = None) -> None:
    """Drop cached inventory (all, or just a tenant's keys)."""
    global _mem_cache

    def _mutate(cache: dict[str, Any]) -> None:
        if tenant_id is None:
            cache.clear()
        else:
            for key in [key for key in cache if key.startswith(f"{tenant_id}|")]:
                cache.pop(key, None)

    try:
        _mem_cache = jsonstore.mutate_json(_CACHE_PATH, {}, _mutate, indent=None)
    except OSError:
        pass

