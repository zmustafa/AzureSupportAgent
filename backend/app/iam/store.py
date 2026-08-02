"""IAM scan-run history + drift (IamScanRun).

Each completed refresh records a compact summary so the dashboard can chart movement and answer
"what privileged access is NEW since the last scan?". The heavy rows stay in the file cache; here
we persist KPIs, per-scope summaries and the set of privileged-access keys used to diff runs."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select, update

from app.core.db import SessionLocal
from app.iam import cache, compose, diff as diff_mod, schema
from app.models import IamScanRun

log = logging.getLogger("app.iam.store")

#: How many classified changes are kept on the run row itself. The full list lives in the drift
#: cache slice for the latest run only; this is the bounded copy that survives in history.
RUN_DIFF_CHANGES = 500


def _privileged_keys(rows: list[dict[str, Any]]) -> list[str]:
    """Stable identity of each privileged grant — effective principal | role | scope."""
    keys = set()
    for r in rows:
        if not r.get("roleIsPrivileged"):
            continue
        who = r.get("effectivePrincipalId") or r.get("effectivePrincipalName") or r.get("principalId")
        keys.add(f"{who}|{r.get('roleName','')}|{r.get('scope','')}")
    return sorted(keys)


def _public(run: Any) -> dict[str, Any]:
    # `rows_retained` comes pre-computed when the caller selected summary columns only (see
    # `list_runs`), and is derived from the column itself when a whole entity was loaded.
    retained = getattr(run, "rows_retained", None)
    if retained is None:
        retained = run.rows_json is not None
    return {
        "id": run.id,
        "tenant_id": run.tenant_id,
        "scope": run.scope,
        "trigger": run.trigger,
        "status": run.status,
        "total_rows": run.total_rows,
        "privileged_count": run.privileged_count,
        "unique_principals": run.unique_principals,
        "kpis": run.kpis_json or {},
        "scopes": run.scopes_json or [],
        "diff": run.diff_json or None,
        "demo": run.demo,
        "pinned": bool(run.pinned),
        "pin_reason": run.pin_reason or "",
        # Whether the full rows are still retained. A run without them cannot be diffed against
        # or used as a campaign baseline, and the UI has to be able to say so.
        "rows_retained": bool(retained),
        "triggered_by": run.triggered_by,
        "started_at": run.started_at.isoformat() if run.started_at else "",
        "ended_at": run.ended_at.isoformat() if run.ended_at else "",
        "duration_ms": run.duration_ms,
    }


async def save_run(
    tenant_id: str,
    *,
    connection_id: str | None = None,
    scope: str = "__all__",
    trigger: str = "manual",
    triggered_by: str = "",
    demo: bool = False,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    """Snapshot the current composed access for ``tenant_id`` into a history row + diff vs prev."""
    overview = compose.compute_overview(tenant_id)
    master = compose.build_master_rows(tenant_id)
    keys = _privileged_keys(master)

    async with SessionLocal() as db:
        prev = (
            await db.execute(
                select(IamScanRun).where(IamScanRun.tenant_id == tenant_id).order_by(desc(IamScanRun.started_at)).limit(1)
            )
        ).scalar_one_or_none()
        prev_keys = set(prev.privileged_keys_json or []) if prev else set()
        cur_keys = set(keys)
        diff = None
        classified: dict[str, Any] = {"available": False, "changes": [], "counts_by_class": {}, "total": 0}
        if prev:
            added = sorted(cur_keys - prev_keys)
            removed = sorted(prev_keys - cur_keys)
            # The classified diff needs the PREVIOUS run's rows. They are only retained for runs
            # that were pinned, so an unpinned predecessor yields the privileged key diff and an
            # explicit `available: False` rather than a silently empty change list.
            prev_rows = list(prev.rows_json or []) if getattr(prev, "rows_json", None) else []
            if prev_rows:
                classified = diff_mod.compute(prev_rows, master)
                classified["available"] = True
            classified["baseline_run_id"] = prev.id
            diff = {
                "baseline_run_id": prev.id,
                "added_privileged": added,
                "removed_privileged": removed,
                "added_count": len(added),
                "removed_count": len(removed),
                "key_set_hash": diff_mod.key_set_hash(master),
                "counts_by_class": classified.get("counts_by_class", {}),
                "classified_total": classified.get("total", 0),
                "classified_available": classified.get("available", False),
                # Bounded on purpose: thirty runs of a full change list is not a history feature.
                "changes": classified.get("changes", [])[:RUN_DIFF_CHANGES],
            }
        cache.write_drift(tenant_id, classified)

        run = IamScanRun(
            tenant_id=tenant_id,
            connection_id=connection_id,
            scope=scope,
            trigger=trigger,
            status="succeeded",
            total_rows=len(master),
            privileged_count=overview["kpis"].get("privileged", 0),
            unique_principals=overview["kpis"].get("unique_principals", 0),
            kpis_json=overview["kpis"],
            scopes_json=[
                {"scope": s["scope"], "displayName": s["displayName"], "row_count": s["row_count"], "status": s["status"]}
                for s in overview["scopes"]
            ],
            privileged_keys_json=keys,
            rows_json=master,
            diff_json=diff,
            demo=demo,
            triggered_by=triggered_by,
            ended_at=datetime.now(timezone.utc),
            duration_ms=duration_ms,
        )
        db.add(run)
        # Flush FIRST. The primary key is a Python-side default assigned at flush time, so
        # before this `run.id` is None — and SQLAlchemy renders `id != None` as `IS NOT NULL`,
        # which matches every row including the one just written. The "keep the newest run"
        # clause then silently cleared the rows it existed to protect, and the classified diff
        # could never find a baseline.
        await db.flush()
        assert run.id, "the run id must exist before pruning older snapshots"

        # Drop the rolling buffer from every older unpinned run. Retention is exactly one
        # unpinned snapshot plus whatever a human pinned, which is what keeps this bounded.
        await db.execute(
            update(IamScanRun)
            .where(
                IamScanRun.tenant_id == tenant_id,
                IamScanRun.pinned.is_(False),
                IamScanRun.id != run.id,
            )
            .values(rows_json=None)
        )
        await db.commit()
        await db.refresh(run)
        return _public(run)


async def pin_run(tenant_id: str, run_id: str, *, reason: str = "") -> dict[str, Any] | None:
    """Keep a run's full rows indefinitely so it can serve as a campaign baseline or evidence.

    A pinned run is the only thing that makes "show me who had privileged access on 1 April, and
    show me it has not been edited since" answerable later."""
    async with SessionLocal() as db:
        run = (
            await db.execute(select(IamScanRun).where(IamScanRun.tenant_id == tenant_id, IamScanRun.id == run_id))
        ).scalar_one_or_none()
        if not run:
            return None
        if run.rows_json is None:
            # The buffer already rolled past this run. Pinning it now would claim a fidelity the
            # record does not have, so say so instead of pinning an empty snapshot.
            return {**_public(run), "pinned": False, "error": "this run's rows were already discarded; pin a newer run"}
        run.pinned = True
        run.pin_reason = reason
        await db.commit()
        await db.refresh(run)
        return _public(run)


async def unpin_run(tenant_id: str, run_id: str) -> bool:
    async with SessionLocal() as db:
        run = (
            await db.execute(select(IamScanRun).where(IamScanRun.tenant_id == tenant_id, IamScanRun.id == run_id))
        ).scalar_one_or_none()
        if not run:
            return False
        run.pinned = False
        await db.commit()
        return True


async def run_rows(tenant_id: str, run_id: str) -> list[dict[str, Any]] | None:
    """Full rows for a run, or None when the rolling buffer has moved past it."""
    async with SessionLocal() as db:
        run = (
            await db.execute(select(IamScanRun).where(IamScanRun.tenant_id == tenant_id, IamScanRun.id == run_id))
        ).scalar_one_or_none()
        return list(run.rows_json) if run and run.rows_json else None


#: Everything `_public` needs EXCEPT the two bulk columns. `rows_json` holds a full access
#: snapshot (megabytes on a real tenant) and `privileged_keys_json` thousands of keys; selecting
#: the whole entity made listing 30 runs read and JSON-decode both for every one of them, to
#: display a summary table that shows neither.
_SUMMARY_COLUMNS = (
    IamScanRun.id, IamScanRun.tenant_id, IamScanRun.scope, IamScanRun.trigger,
    IamScanRun.status, IamScanRun.total_rows, IamScanRun.privileged_count,
    IamScanRun.unique_principals, IamScanRun.kpis_json, IamScanRun.scopes_json,
    IamScanRun.diff_json, IamScanRun.demo, IamScanRun.pinned, IamScanRun.pin_reason,
    IamScanRun.triggered_by, IamScanRun.started_at, IamScanRun.ended_at,
    IamScanRun.duration_ms,
)


async def list_runs(tenant_id: str, *, limit: int = 30) -> list[dict[str, Any]]:
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(
                    *_SUMMARY_COLUMNS,
                    IamScanRun.rows_json.is_not(None).label("rows_retained"),
                )
                .where(IamScanRun.tenant_id == tenant_id)
                .order_by(desc(IamScanRun.started_at))
                .limit(limit)
            )
        ).all()
        return [_public(r) for r in rows]


async def get_run(tenant_id: str, run_id: str) -> dict[str, Any] | None:
    async with SessionLocal() as db:
        run = (
            await db.execute(select(IamScanRun).where(IamScanRun.tenant_id == tenant_id, IamScanRun.id == run_id))
        ).scalar_one_or_none()
        return _public(run) if run else None
