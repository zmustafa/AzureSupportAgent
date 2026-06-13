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

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "policy.json"

# --- Server-side inventory cache (separate file so the big payloads don't bloat the
# registry). In-memory for instant hits + file-persisted so a backend restart stays fast.
_CACHE_PATH = Path(__file__).resolve().parents[2] / ".data" / "policy_cache.json"
_mem_cache: dict[str, Any] | None = None

_MAX_SNAPSHOTS = 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    if _PATH.exists():
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("snapshots", {})
                data.setdefault("drafts", {})
                data.setdefault("iac_sources", {})
                data.setdefault("enforcement_links", {})
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"snapshots": {}, "drafts": {}, "iac_sources": {}, "enforcement_links": {}}


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- snapshots
def save_snapshot(tenant_id: str, connection_id: str, summary: dict[str, Any], actor: str = "") -> dict[str, Any]:
    """Persist a compact compliance/inventory snapshot for trend + drift analysis."""
    data = _read()
    sid = uuid.uuid4().hex[:12]
    snap = {
        "id": sid,
        "tenant_id": tenant_id or "",
        "connection_id": connection_id or "",
        "created_at": _now(),
        "created_by": actor,
        "summary": summary,  # {counts, compliance:{...}, by_effect, by_enforcement}
    }
    data["snapshots"][sid] = snap
    # Trim oldest beyond the cap.
    snaps = sorted(data["snapshots"].values(), key=lambda s: s["created_at"], reverse=True)
    if len(snaps) > _MAX_SNAPSHOTS:
        for old in snaps[_MAX_SNAPSHOTS:]:
            data["snapshots"].pop(old["id"], None)
    _write(data)
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
    if _SIMS_PATH.exists():
        try:
            data = json.loads(_SIMS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _sims_write(data: dict[str, Any]) -> None:
    _SIMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SIMS_PATH.write_text(json.dumps(data), encoding="utf-8")


def _sim_summary(rec: dict[str, Any]) -> dict[str, Any]:
    """A saved simulation without the heavy ``result`` payload (for list views)."""
    return {k: v for k, v in rec.items() if k != "result"}


def save_simulation(tenant_id: str, rec: dict[str, Any], actor: str = "") -> dict[str, Any]:
    """Persist a completed simulation. Derives the display metadata from the result so the
    client only has to post the raw result + workload context. Returns the summary."""
    store = _sims_read()
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
    store[sid] = saved
    # Trim oldest beyond the cap.
    recs = sorted(store.values(), key=lambda s: s.get("created_at", ""), reverse=True)
    if len(recs) > _MAX_SIMS:
        for old in recs[_MAX_SIMS:]:
            store.pop(old["id"], None)
    _sims_write(store)
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
    store = _sims_read()
    rec = store.get(sim_id)
    if rec and (rec.get("tenant_id") or "") in ("", tenant_id):
        store.pop(sim_id, None)
        _sims_write(store)
        return True
    return False


# ----------------------------------------------------------------- coverage-gap analyses
# History of Coverage-gap analyses (baseline comparison + AI built-in proposals), persisted so
# the user can reopen a previous run and review which controls were missing / proposed. Kept in
# a dedicated file (the proposal payload is bulky) so the main registry stays lean. Listing
# returns compact summaries; the full result is fetched on open.
_COV_PATH = Path(__file__).resolve().parents[2] / ".data" / "policy_coverage_runs.json"
_MAX_COV = 100


def _cov_read() -> dict[str, Any]:
    if _COV_PATH.exists():
        try:
            data = json.loads(_COV_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _cov_write(data: dict[str, Any]) -> None:
    _COV_PATH.parent.mkdir(parents=True, exist_ok=True)
    _COV_PATH.write_text(json.dumps(data), encoding="utf-8")


def _cov_summary(rec: dict[str, Any]) -> dict[str, Any]:
    """A saved coverage analysis without the heavy ``result`` payload (for list views)."""
    return {k: v for k, v in rec.items() if k != "result"}


def save_coverage_run(tenant_id: str, rec: dict[str, Any], actor: str = "") -> dict[str, Any]:
    """Persist a completed Coverage-gap analysis. Derives display metadata from the result so
    the client only posts the raw result + workload context. Returns the summary."""
    store = _cov_read()
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
    store[rid] = saved
    # Trim oldest beyond the cap.
    recs = sorted(store.values(), key=lambda s: s.get("created_at", ""), reverse=True)
    if len(recs) > _MAX_COV:
        for old in recs[_MAX_COV:]:
            store.pop(old["id"], None)
    _cov_write(store)
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
    store = _cov_read()
    rec = store.get(run_id)
    if rec and (rec.get("tenant_id") or "") in ("", tenant_id):
        store.pop(run_id, None)
        _cov_write(store)
        return True
    return False


# --------------------------------------------------------------------------- drafts
def list_drafts(tenant_id: str | None = None) -> list[dict[str, Any]]:
    data = _read()
    out = list(data.get("drafts", {}).values())
    if tenant_id is not None:
        out = [d for d in out if (d.get("tenant_id") or "") in ("", tenant_id)]
    out.sort(key=lambda d: d.get("updated_at", ""), reverse=True)
    return out


def save_draft(draft: dict[str, Any], actor: str = "") -> dict[str, Any]:
    data = _read()
    did = draft.get("id") or uuid.uuid4().hex[:12]
    existing = data["drafts"].get(did, {})
    rec = {
        "id": did,
        "tenant_id": draft.get("tenant_id", existing.get("tenant_id", "")),
        "title": draft.get("title", existing.get("title", "Untitled policy")),
        "kind": draft.get("kind", existing.get("kind", "definition")),  # definition | assignment
        "intent": draft.get("intent", existing.get("intent", "")),
        "policy_json": draft.get("policy_json", existing.get("policy_json", {})),
        "notes": draft.get("notes", existing.get("notes", "")),
        "created_at": existing.get("created_at") or _now(),
        "created_by": existing.get("created_by") or actor,
        "updated_at": _now(),
        "updated_by": actor,
    }
    data["drafts"][did] = rec
    _write(data)
    return rec


def delete_draft(draft_id: str) -> bool:
    data = _read()
    if draft_id in data.get("drafts", {}):
        data["drafts"].pop(draft_id, None)
        _write(data)
        return True
    return False


# --------------------------------------------------------------------------- iac sources
def set_iac_source(tenant_id: str, content: str, fmt: str, actor: str = "") -> dict[str, Any]:
    data = _read()
    sid = tenant_id or "default"
    rec = {
        "id": sid,
        "tenant_id": tenant_id or "",
        "format": fmt,  # epac | bicep | terraform | json
        "content": content[:200_000],
        "updated_at": _now(),
        "updated_by": actor,
    }
    data["iac_sources"][sid] = rec
    _write(data)
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
    store = _read()
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
    store["enforcement_links"][_link_key(tenant_id, workload_id, check_id)] = rec
    _write(store)
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
    if _mem_cache is None:
        if _CACHE_PATH.exists():
            try:
                loaded = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
                _mem_cache = loaded if isinstance(loaded, dict) else {}
            except (json.JSONDecodeError, OSError):
                _mem_cache = {}
        else:
            _mem_cache = {}
    return _mem_cache


def _cache_persist() -> None:
    if _mem_cache is None:
        return
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(_mem_cache), encoding="utf-8")
    except OSError:
        pass


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
    cache = _cache_load()
    fetched = _now()
    cache[_cache_key(tenant_id, connection_id, with_compliance, workload_id)] = {
        "ts": time.time(),
        "fetched_at": fetched,
        "payload": payload,
    }
    _cache_persist()
    return fetched


def clear_inventory_cache(tenant_id: str | None = None) -> None:
    """Drop cached inventory (all, or just a tenant's keys)."""
    cache = _cache_load()
    if tenant_id is None:
        cache.clear()
    else:
        for k in [k for k in cache if k.startswith(f"{tenant_id}|")]:
            cache.pop(k, None)
    _cache_persist()

