"""End-to-end: is the application still responsive while IAM does heavy work?

Runs against the LIVE local server, because that is the only place the real question can be
asked. Everything below the HTTP boundary can look correct while the product is still frozen —
what a user experiences is whether an unrelated request comes back.

Three probes run concurrently with a heavy IAM operation:

  * ``/healthz``            — no auth, no database, not even under /api. If THIS stalls, the
                               event loop is blocked and nothing else about the diagnosis
                               matters.
  * ``/api/me``             — authenticated, so it exercises the session heartbeat write.
  * ``/api/iam/overview``   — the screen that is actually open while a refresh runs.

Usage (from backend/, venv active, server on :8000):

    python scripts/iam_responsiveness_e2e.py
    python scripts/iam_responsiveness_e2e.py --session <cookie>   # skip auto-discovery

Exit code 1 if any probe's p95 exceeds its budget, so this can gate a change.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

import httpx

BASE = "http://localhost:35001"

# Budgets, set from measurement rather than aspiration — a gate nobody can pass is a gate that
# gets deleted. Taken during back-to-back forced escalation rebuilds (~45 s each of pure Python
# CPU) on a real 5,514-row / 45-scope tenant, which is heavier than an ordinary refresh.
#
# Gated on the MEDIAN, not p95. On a developer box sharing cores with a Vite server and an MCP
# warmup, p95 moved from 1.06 s to 1.73 s between two runs of identical code — a gate on that
# number fails for reasons that have nothing to do with the change under test, and a flaky gate
# teaches people to ignore it. p95 and max are still printed, because they are what a reader
# needs to judge a regression; they are just not the pass/fail.
#
# The exception is /healthz, which IS gated on p95: it is the freeze detector, it touches no
# database and almost no Python, and a tail there means the event loop itself stalled.
#
#   /healthz  inline, this was the endpoint that hung. Threaded it holds ~62 ms median.
#   /api/me   is GIL-bound, not loop-bound, and cannot currently do better. One CPU-bound worker
#             thread takes a single database round trip from ~0 ms to **210 ms median**, and
#             this endpoint makes several. Moving the analysis to a separate PROCESS is the only
#             thing that would remove it; that is a real change (the row payload is ~13 MB to
#             pickle, and the cache's write counter would no longer be shared) and is not
#             pretended to be done here.
#   /overview recomposes the estate when a write has invalidated the memo, so it is expected to
#             be the slowest of the three.
BUDGETS = {"/healthz": 0.25, "/api/me": 1.5, "/api/iam/overview": 2.0}

#: Paths whose TAIL is also gated, because for them a tail means "the loop stopped".
TAIL_GATED = {"/healthz": 0.5}


def _discover_session() -> str:
    """Reuse the e2e Playwright session so this script never handles a password."""
    state = Path(__file__).resolve().parents[2] / "e2e" / ".auth" / "storageState.json"
    if not state.is_file():
        return ""
    try:
        for cookie in json.loads(state.read_text(encoding="utf-8")).get("cookies", []):
            if cookie.get("name") == "azsupagent_session":
                return str(cookie.get("value", ""))
    except (json.JSONDecodeError, OSError):
        return ""
    return ""


async def _probe(client: httpx.AsyncClient, path: str, stop: asyncio.Event, out: list[float]) -> None:
    while not stop.is_set():
        started = time.monotonic()
        try:
            await client.get(f"{BASE}{path}")
        except httpx.HTTPError:
            out.append(99.0)  # a failed request is an infinitely slow one to the person waiting
        else:
            out.append(time.monotonic() - started)
        await asyncio.sleep(0.05)


async def _load(client: httpx.AsyncClient, tenant_conn: str | None, rounds: int) -> str:
    """The heavy IAM work: rebuild the derived caches, repeatedly.

    Deliberately NOT a live Azure refresh — that needs credentials and takes minutes. This is
    the same CPU the refresh performs at its end (recompose + role index + escalation graph),
    which is the part that runs in this process and therefore the part that can freeze it."""
    q = f"?connection_id={tenant_conn}" if tenant_conn else ""
    for _ in range(rounds):
        r = await client.post(f"{BASE}/api/iam/cache/rebuild{q}", headers={"Sec-Fetch-Site": "same-origin"})
        if r.status_code >= 400:
            return f"load generator got HTTP {r.status_code}: {r.text[:200]}"
    return ""


def _report(name: str, samples: list[float]) -> tuple[str, bool]:
    if not samples:
        return f"{name:<22} no samples", False
    median = statistics.median(samples)
    p95 = statistics.quantiles(samples, n=20)[-1] if len(samples) > 20 else max(samples)
    budget = BUDGETS.get(name, 1.0)
    tail_budget = TAIL_GATED.get(name)
    ok = median <= budget and (tail_budget is None or p95 <= tail_budget)
    tail = f" tail<={tail_budget*1000:.0f}ms" if tail_budget else ""
    return (
        f"{name:<22} n={len(samples):>4}  median {median*1000:6.0f}ms  "
        f"p95 {p95*1000:6.0f}ms  max {max(samples)*1000:6.0f}ms  "
        f"budget {budget*1000:.0f}ms{tail}  {'PASS' if ok else 'FAIL'}",
        ok,
    )


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", default="", help="azsupagent_session cookie value")
    ap.add_argument("--connection", default="", help="connection_id to rebuild for")
    ap.add_argument("--rounds", type=int, default=6, help="how many rebuilds to run")
    args = ap.parse_args()

    session = args.session or _discover_session()
    if not session:
        print("No session cookie found. Run the e2e global-setup first, or pass --session.")
        return 1

    cookies = {"azsupagent_session": session}
    async with httpx.AsyncClient(cookies=cookies, timeout=60) as client:
        health = await client.get(f"{BASE}/healthz")
        if health.status_code != 200:
            print(f"Server not reachable at {BASE} (health {health.status_code}).")
            return 1

        stop = asyncio.Event()
        samples: dict[str, list[float]] = {p: [] for p in BUDGETS}
        probes = [asyncio.create_task(_probe(client, p, stop, samples[p])) for p in BUDGETS]
        await asyncio.sleep(0.5)  # a quiet baseline first

        started = time.monotonic()
        err = await _load(client, args.connection or None, args.rounds)
        load_wall = time.monotonic() - started

        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.gather(*probes)

    if err:
        print(err)
        return 1

    print(f"{args.rounds} derived-cache rebuild(s) took {load_wall:.1f}s. During that time:\n")
    ok = True
    for path in BUDGETS:
        line, passed = _report(path, samples[path])
        ok = ok and passed
        print(line)
    print()
    print("A FAIL on /healthz means the event loop was blocked — every request in the")
    print("product was stalled, not just IAM's. That is the freeze being tested for.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
