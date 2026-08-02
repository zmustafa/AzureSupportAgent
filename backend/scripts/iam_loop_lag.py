"""Measure what an IAM refresh does to the rest of the application — before and after a fix.

Two hypotheses explain "the whole app freezes during an IAM refresh", and they need opposite
fixes:

  A. **Event-loop starvation.** Synchronous CPU/file work runs on the single shared loop, so
     every request in the process stops. SQLite `database is locked` then appears as a knock-on
     effect, because an awaited commit cannot resume while nothing is being scheduled.
  B. **Database write-lock contention.** The loop is fine; requests queue behind a long-held
     SQLite write transaction. Every authenticated request in this product performs a write
     (`resolve_session` slides `last_seen_at`), so a long writer stalls literally everything.

This script measures BOTH at once against the real local cache:

  * a **loop-lag probe** samples how late a fixed-interval sleep actually wakes up (hypothesis A)
  * a **writer probe** performs the same kind of tiny UPDATE the session slide does, and records
    how long each one took to commit (hypothesis B)

Whichever probe degrades is the answer. Run it, change one thing, run it again.

Usage (from backend/, venv active):

    python scripts/iam_loop_lag.py                       # largest cached tenant
    python scripts/iam_loop_lag.py --tenant <tenant-id>
    python scripts/iam_loop_lag.py --skip-db             # loop probe only (no DB writes)

Exit code is 0 always; this is a measurement tool, not a gate.
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.iam import cache, compose  # noqa: E402


class LagProbe:
    """Samples how late a fixed-interval sleep actually wakes up (event-loop health)."""

    def __init__(self, interval_s: float = 0.02) -> None:
        self.interval_s = interval_s
        self.samples: list[float] = []
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        while True:
            before = time.monotonic()
            await asyncio.sleep(self.interval_s)
            self.samples.append(time.monotonic() - before - self.interval_s)

    def start(self) -> None:
        self.samples.clear()
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> dict[str, float]:
        await _cancel(self._task)
        return _summarize(self.samples)


class WriterProbe:
    """Times the tiny DB write every authenticated request makes.

    ``auth.service.resolve_session`` slides ``Session.last_seen_at`` and commits on the request's
    critical path. If a background job holds the SQLite write lock, this is what every user is
    queued behind — so this probe IS the user-visible latency, not a proxy for it."""

    def __init__(self, interval_s: float = 0.05) -> None:
        self.interval_s = interval_s
        self.samples: list[float] = []
        self.errors = 0
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        from sqlalchemy import text

        from app.core.db import SessionLocal

        while True:
            started = time.monotonic()
            try:
                async with SessionLocal() as db:
                    await db.execute(text("UPDATE sessions SET last_seen_at = last_seen_at WHERE 1=0"))
                    await db.commit()
            except Exception:  # noqa: BLE001 - a failed write IS the symptom being measured
                self.errors += 1
            self.samples.append(time.monotonic() - started)
            await asyncio.sleep(self.interval_s)

    def start(self) -> None:
        self.samples.clear()
        self.errors = 0
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> dict[str, float]:
        await _cancel(self._task)
        out = _summarize(self.samples)
        out["errors"] = self.errors
        return out


async def _cancel(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _summarize(samples: list[float]) -> dict[str, float]:
    s = samples or [0.0]
    return {
        "max_s": max(s),
        "p95_s": statistics.quantiles(s, n=20)[-1] if len(s) > 20 else max(s),
        "mean_s": statistics.fmean(s),
        "samples": len(s),
    }


def _verdict(lag: float, write: float) -> str:
    if lag >= 0.5:
        return "LOOP BLOCKED"
    if write >= 0.5:
        return "DB WRITE STALLED"
    if lag >= 0.1 or write >= 0.1:
        return "marginal"
    return "ok"


async def measure(label: str, fn, *, with_db: bool) -> str:
    lag = LagProbe()
    writer = WriterProbe() if with_db else None
    lag.start()
    if writer:
        writer.start()
    await asyncio.sleep(0.1)  # let the probes settle
    started = time.monotonic()
    try:
        await fn()
    except Exception as exc:  # noqa: BLE001 - a failing step must not hide the other measurements
        await _cancel(lag._task)
        if writer:
            await _cancel(writer._task)
        return f"{label:<40} FAILED: {str(exc)[:90]}"
    wall = time.monotonic() - started
    await asyncio.sleep(0.1)
    lag_stats = await lag.stop()
    write_stats = await writer.stop() if writer else {"max_s": 0.0, "p95_s": 0.0, "errors": 0}
    return (
        f"{label:<40} wall {wall:6.2f}s | loop lag max {lag_stats['max_s']:6.3f}s "
        f"| db write max {write_stats['max_s']:6.3f}s err {int(write_stats.get('errors', 0)):>2} "
        f"| {_verdict(lag_stats['max_s'], write_stats['max_s'])}"
    )


def _largest_tenant() -> str:
    index = cache._read_index()
    best, best_n = "default", -1
    for tenant in index:
        try:
            n = len(compose.build_master_rows(tenant))
        except Exception:  # noqa: BLE001
            continue
        if n > best_n:
            best, best_n = tenant, n
    return best


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tenant", default="", help="tenant id (default: the largest cached one)")
    ap.add_argument("--skip-db", action="store_true", help="do not run the writer probe")
    args = ap.parse_args()

    tenant = args.tenant or _largest_tenant()
    with_db = not args.skip_db
    rows = compose.build_master_rows(tenant)
    print(f"tenant={tenant}  rows={len(rows)}  scopes={len(cache.list_scope_meta(tenant))}  db_probe={with_db}")
    if not rows:
        print("No cached rows for this tenant — run an access scan or seed demo data first.")
        return 0
    if with_db:
        from app.core.db import ensure_schema

        await ensure_schema()
    print()

    lines: list[str] = []

    async def cold_rebuild() -> None:
        compose._MASTER_CACHE.pop(tenant, None)
        compose.build_master_rows(tenant)

    lines.append(await measure("build_master_rows (cold, inline)", cold_rebuild, with_db=with_db))

    async def derived() -> None:
        from app.iam import effective, escalation, findings

        directory = cache.read_directory(tenant)
        findings._EVAL_CACHE.clear()
        escalation.graph_for_tenant(
            tenant, rows, effective.build_role_index(directory.get("role_defs", [])),
            identities=directory.get("identities", {}), federated=directory.get("federated", []),
        )
        findings.evaluate(tenant)

    lines.append(await measure("escalation graph + findings (inline)", derived, with_db=with_db))

    metas = cache.list_scope_meta(tenant)
    if metas:
        scope_rows = cache.read_scope_rows(tenant, metas[0]["scope"])

        async def writes() -> None:
            for _ in range(10):
                cache._write_blob(tenant, "__loopprobe__", {"rows": scope_rows}, bump=False)

        lines.append(await measure(f"write_blob x10 ({len(scope_rows)} rows, inline)", writes, with_db=with_db))
        cache._delete_blob(tenant, "__loopprobe__")

    if with_db:
        # THE ONE THAT MATTERS: a full run snapshot. `rows_json` is the entire composed row set
        # in one SQLite JSON column, inside one transaction that also NULLs the previous run's
        # copy. On a real tenant that is tens of megabytes of held write lock, and every request
        # in the product is queued behind it by `resolve_session`.
        from app.iam import store

        async def snapshot() -> None:
            await store.save_run(tenant, scope="__loopprobe__", trigger="probe", triggered_by="loop-lag-script")

        lines.append(await measure("store.save_run (full snapshot)", snapshot, with_db=with_db))

    print("\n".join(lines))
    print()
    print("LOOP BLOCKED     -> hypothesis A: sync work on the event loop. Fix with asyncio.to_thread.")
    print("DB WRITE STALLED -> hypothesis B: a long write transaction. Fix the transaction, not the loop.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
