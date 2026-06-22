"""Assessment runner — the hybrid engine.

For a selected workload + pillars:
  1. Resolve the workload's scope (subscriptions / resource-group / resource ids) and the
     set of ARM resource types it actually contains.
  2. For each applicable check, run its deterministic Resource Graph (KQL) control scoped
     to the workload; flagged resources ⇒ fail, none ⇒ pass, no matching types ⇒ N/A.
  3. Compute 0-100 per-pillar scores (weighted by severity) and an overall score.
  4. AI layer (hybrid): one LLM call produces an executive summary + a short rationale
     per failed finding. Degrades gracefully when the LLM is unavailable.
  5. Persist an AssessmentRun (history) and diff against the previous run (drift).

Streams progress as events for the SSE endpoint.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import httpx
from sqlalchemy import desc, select

from app.assessments import catalog
from app.core.db import SessionLocal
from app.exec.command_runner import KQL_RESOURCE_CAPTURE_BYTES, parse_kql_rows, run_kql_capture, run_kql_collect
from app.models import AssessmentRun
from app.workloads import discovery
from app.workloads.registry import get_workload

logger = logging.getLogger("app.assessments.runner")

_SEV_RANK = {"info": 0, "warning": 1, "error": 2, "critical": 3}
_RANK_SEV = {v: k for k, v in _SEV_RANK.items()}
_FLAGGED_SAMPLE = 25  # max flagged resources stored per finding
_SCAN_SAMPLE = 1000  # max in-scope resources captured for the report's Resources tab
_METRIC_RESOURCE_CAP = 40  # max resources probed per metric-backed check (bounds az calls)


def _real_remediation(template: str, resource: dict[str, Any]) -> str:
    """Fill a remediation-command template's placeholders with a flagged resource's real
    values, so the operator gets a ready-to-run command instead of <name>/<rg> stubs."""
    if not template:
        return ""
    name = str(resource.get("name", "") or "")
    rg = str(resource.get("resource_group", "") or "")
    sub = str(resource.get("subscription_id", "") or "")
    repl = {
        "<name>": name,
        "<n>": name,
        "<rg>": rg,
        "<resource-group>": rg,
        "<resource_group>": rg,
        "<resourcegroup>": rg,
        "<subscription>": sub,
        "<subscription-id>": sub,
        "<subscription_id>": sub,
        "<sub>": sub,
    }
    out = template
    for ph, val in repl.items():
        if val:
            # Case-insensitive placeholder replacement.
            out = re.sub(re.escape(ph), val, out, flags=re.IGNORECASE)
    # Prepend a subscription scope so the command targets the right subscription even
    # when the operator's default differs, but only if the command doesn't already set it.
    if sub and out.startswith("az ") and "--subscription" not in out:
        out = f"{out} --subscription {sub}"
    return out


# Run ids for which a cancellation has been requested (cooperative cancellation: the
# runner checks this between checks and stops gracefully, marking the run 'cancelled').
_CANCEL_REQUESTS: set[str] = set()


def request_cancel(run_id: str) -> None:
    """Flag a queued/running assessment run for cooperative cancellation."""
    _CANCEL_REQUESTS.add(run_id)


def _is_cancelled(run_id: str | None) -> bool:
    return bool(run_id) and run_id in _CANCEL_REQUESTS


def _clear_cancel(run_id: str | None) -> None:
    if run_id:
        _CANCEL_REQUESTS.discard(run_id)



async def create_queued_run(
    *,
    workload_id: str,
    workload_name: str,
    pillars: list[str],
    tenant_id: str,
    connection_id: str | None,
    actor: str,
    trigger: str,
) -> str:
    """Insert a placeholder AssessmentRun (status='queued') and return its id.

    Used by background/batch enqueue so the run is immediately visible in history
    with a live status while the work happens asynchronously."""
    run = AssessmentRun(
        workload_id=workload_id,
        workload_name=workload_name,
        tenant_id=tenant_id,
        connection_id=connection_id or None,
        pillars=[p for p in pillars if p in catalog.PILLARS] or list(catalog.PILLARS),
        status="queued",
        triggered_by=actor,
        trigger=trigger,
    )
    async with SessionLocal() as db:
        db.add(run)
        await db.commit()
        await db.refresh(run)
        return run.id


async def _set_run_status(run_id: str | None, status: str, *, error: str | None = None) -> None:
    """Update just the status (and optional error) of an existing run row."""
    if not run_id:
        return
    async with SessionLocal() as db:
        run = await db.get(AssessmentRun, run_id)
        if run is None:
            return
        run.status = status
        if error is not None:
            run.error = error[:2000]
        if status in ("succeeded", "failed"):
            run.ended_at = _now()
        await db.commit()


async def reap_orphaned_runs() -> int:
    """Fail any assessment runs left ``queued``/``running`` by a previous process.

    A run only executes inside a live in-process task; once the process exits, an
    in-flight run can never resume, so a row stuck at ``running``/``queued`` is an orphan.
    Called once at startup so history never shows a mission-critical assessment as
    perpetually "in progress" and schedulers don't treat a dead run as still working.
    Returns the number of runs reaped."""
    from sqlalchemy import update

    async with SessionLocal() as db:
        result = await db.execute(
            update(AssessmentRun)
            .where(AssessmentRun.status.in_(("queued", "running")))
            .values(
                status="failed",
                error="Interrupted by a server restart before completion.",
                ended_at=_now(),
            )
        )
        await db.commit()
        return int(getattr(result, "rowcount", 0) or 0)



async def run_assessment_to_completion(
    *,
    run_id: str,
    workload_id: str,
    pillars: list[str],
    tenant_id: str,
    connection_id: str | None = None,
    actor: str = "",
    trigger: str = "manual",
    use_ai: bool = True,
) -> None:
    """Drive an enqueued run to completion, updating its existing row's status.

    Drains the streaming runner; on an ``error`` event or unexpected exception the
    run row is marked ``failed`` so the failure is visible in history."""
    try:
        async for ev in run_assessment(
            existing_run_id=run_id,
            workload_id=workload_id,
            pillars=pillars,
            tenant_id=tenant_id,
            connection_id=connection_id,
            actor=actor,
            trigger=trigger,
            use_ai=use_ai,
        ):
            if ev.get("type") == "cancelled":
                return  # status already set to 'cancelled' by the runner
            if ev.get("type") == "error":
                await _set_run_status(run_id, "failed", error=str(ev.get("message", "Assessment failed.")))
                return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Background assessment %s failed", run_id)
        await _set_run_status(run_id, "failed", error=str(exc))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event(type_: str, **data: Any) -> dict[str, Any]:
    return {"type": type_, **data}


def _sub_guid(value: str) -> str:
    """Extract the bare subscription GUID from an ARM id or pass through a GUID."""
    if not value:
        return ""
    m = re.search(r"/subscriptions/([0-9a-fA-F-]{36})", value)
    if m:
        return m.group(1)
    return value


def _esc(val: str) -> str:
    return (val or "").replace("'", "''")


async def _resolve_scope(workload: dict[str, Any], connection: dict[str, Any] | None) -> dict[str, Any]:
    """Turn a workload's nodes into a KQL scope predicate.

    Returns {predicate, subscriptions, rg_pairs, resource_ids, error}. mg nodes are
    expanded to their subscriptions; subscription nodes scope the whole subscription;
    resource-group and resource nodes scope precisely."""
    subs: set[str] = set()
    rg_pairs: set[tuple[str, str]] = set()
    resource_ids: set[str] = set()

    for node in workload.get("nodes", []):
        kind = node.get("kind")
        if kind == "subscription":
            guid = _sub_guid(node.get("id", "")) or _sub_guid(node.get("subscription_id", ""))
            if guid:
                subs.add(guid)
        elif kind == "mg":
            mg_id = node.get("id", "")
            for s in await discovery.subscriptions_under_mg(connection, mg_id):
                subs.add(_sub_guid(s))
        elif kind == "resource_group":
            guid = _sub_guid(node.get("subscription_id", "")) or _sub_guid(node.get("id", ""))
            rg = node.get("resource_group") or node.get("name", "")
            if guid and rg:
                rg_pairs.add((guid, rg))
        elif kind == "resource":
            rid = node.get("id", "")
            if rid:
                resource_ids.add(rid)

    clauses: list[str] = []
    if subs:
        joined = ", ".join(f"'{_esc(s)}'" for s in sorted(subs))
        clauses.append(f"subscriptionId in~ ({joined})")
    for guid, rg in sorted(rg_pairs):
        clauses.append(f"(subscriptionId =~ '{_esc(guid)}' and resourceGroup =~ '{_esc(rg)}')")
    if resource_ids:
        joined = ", ".join(f"'{_esc(r)}'" for r in sorted(resource_ids))
        clauses.append(f"id in~ ({joined})")

    predicate = " or ".join(clauses) if clauses else ""
    # Effective subscription set: the distinct subscriptions the workload touches across ALL
    # node kinds (direct subscriptions, the parent sub of each RG, and the parent sub of each
    # resource id). Subscription-scoped CIS controls (Defender plans, activity-log alerts,
    # RBAC owners, …) and existence checks evaluate against these, since they govern the whole
    # subscription, not an individual resource.
    eff_subs: set[str] = set(subs)
    for guid, _rg in rg_pairs:
        if guid:
            eff_subs.add(guid)
    for rid in resource_ids:
        g = _sub_guid(rid)
        if g:
            eff_subs.add(g)
    effective = sorted(eff_subs)
    _sub_list = ", ".join("'" + _esc(s) + "'" for s in effective)
    sub_predicate = f"subscriptionId in~ ({_sub_list})" if effective else ""
    return {
        "predicate": predicate,
        "subscriptions": sorted(subs),
        "effective_subscriptions": effective,
        "sub_predicate": sub_predicate,
        "rg_pairs": sorted(rg_pairs),
        "resource_ids": sorted(resource_ids),
        "error": "" if predicate else "Workload has no resolvable scope (empty membership).",
    }


# Resource Graph rejects queries beyond ~8000 chars (see command_runner._normalize_kql). A
# workload made of many individual resource nodes produces an ``id in~ (...)`` clause carrying
# one full ARM id each (~130-200 chars), so a single predicate blows past that limit at roughly
# ~40 resources — surfacing as the opaque "Query is too long." error. We instead split the
# scope into several predicates, each kept under this character budget, and run + merge them.
# 6000 leaves comfortable headroom for the query wrapper (project/order/take ≈ 150 chars) plus
# escaping growth, while keeping the batch count low (e.g. ~230 resources → ~6 queries).
_PREDICATE_BUDGET = 6000


def scope_predicate_batches(scope: dict[str, Any], *, budget: int = _PREDICATE_BUDGET) -> list[str]:
    """Split a resolved scope into one or more KQL ``where`` predicates, each under ``budget``
    characters, so a workload with many resources never produces an over-length query.

    The (short, bounded) subscription + resource-group clauses are packed first; the resource-id
    list is then chunked into ``id in~ (...)`` predicates by accumulated length (robust to the
    wide variance in ARM id lengths). Callers run each predicate and merge + de-duplicate the
    rows by id (an id inside an in-scope subscription would otherwise appear in both queries)."""
    subs = scope.get("subscriptions") or []
    rg_pairs = scope.get("rg_pairs") or []
    resource_ids = scope.get("resource_ids") or []

    preds: list[str] = []

    # Subscription + resource-group clauses, greedily packed under the budget.
    base_clauses: list[str] = []
    if subs:
        base_clauses.append("subscriptionId in~ (" + ", ".join(f"'{_esc(s)}'" for s in sorted(subs)) + ")")
    for guid, rg in sorted(rg_pairs):
        base_clauses.append(f"(subscriptionId =~ '{_esc(guid)}' and resourceGroup =~ '{_esc(rg)}')")
    cur: list[str] = []
    cur_len = 0
    for clause in base_clauses:
        add = len(clause) + 4  # " or "
        if cur and cur_len + add > budget:
            preds.append(" or ".join(cur))
            cur, cur_len = [], 0
        cur.append(clause)
        cur_len += add
    if cur:
        preds.append(" or ".join(cur))

    # Resource-id list, chunked into id-only predicates under the budget.
    cur, cur_len = [], 0
    for rid in sorted(resource_ids):
        tok = f"'{_esc(rid)}'"
        add = len(tok) + 2  # ", "
        if cur and cur_len + add > budget:
            preds.append("id in~ (" + ", ".join(cur) + ")")
            cur, cur_len = [], 0
        cur.append(tok)
        cur_len += add
    if cur:
        preds.append("id in~ (" + ", ".join(cur) + ")")

    return preds


async def query_resources_batched(
    predicates: list[str],
    connection: dict[str, Any] | None,
    *,
    projection: str,
    session_dir: str | None = None,
    take: int = 10_000,
) -> list[dict[str, Any]]:
    """Run one ARG ``Resources`` query per predicate (each pre-sized under the query-length
    limit by ``scope_predicate_batches``) and return the merged rows, de-duplicated by id.

    A single ARG query returns at most 1000 rows per page, so a subscription with thousands of
    resources is collected by PAGING (``run_kql_collect``) up to ``take`` — not a single
    ``take 1000`` query that would silently cap the result at 1000. This applies to both light
    and heavy (full-``properties``) projections; the heavy path falls back to id-enumeration +
    adaptive bisection only if a page can't be captured (a 1000-row properties page exceeding the
    capture cap on the CLI path).

    Workload-scope id-lists (already chunked under the query-length budget) pull properties in
    ADAPTIVE id-batches: start with a moderate chunk and, on an output-truncation, bisect the id
    list and retry each half. This converges on a safe size for any property distribution.

    Raises ``RuntimeError`` on the first genuine query failure (fail-closed, matching the
    per-collector ``_query_resources`` it replaces). An output truncation is NOT a genuine failure
    — it's handled by bisection; a single resource whose properties alone exceed the cap is skipped
    (logged) rather than sinking the whole scan."""
    heavy = "properties" in projection.lower()
    seen: set[str] = set()
    out: list[dict[str, Any]] = []

    async def _run(pred: str):
        # Single-page query — used only for the small heavy id-batches in ``_run_ids_adaptive``
        # (≤ _HEAVY_PROJECTION_CHUNK ids), which never exceed one page.
        kql = (
            f"Resources | where {pred} | project {projection} "
            f"| order by type asc, name asc | take {take}"
        )
        return await run_kql_capture(kql, connection, output="json", session_config_dir=session_dir,
                                     max_bytes=KQL_RESOURCE_CAPTURE_BYTES)

    async def _run_paged(pred: str):
        # Light-projection predicate (a whole subscription / RG): page through up to ``take`` rows
        # so a >1000-resource subscription returns its real set instead of a single 1000-row page.
        kql = f"Resources | where {pred} | project {projection} | order by type asc, name asc"
        return await run_kql_collect(kql, connection, session_config_dir=session_dir, max_rows=take)

    async def _enumerate_ids(pred: str) -> list[str]:
        # Light id-only projection of the SAME predicate, PAGED to ``take`` — ids are tiny, so this
        # discovers every matching id even when the full-``properties`` version blows the cap.
        # Returns QUOTED tokens ("'<id>'") to match what ``_run_ids_adaptive`` expects.
        kql = f"Resources | where {pred} | project id | order by id asc"
        kr = await run_kql_collect(kql, connection, session_config_dir=session_dir, max_rows=take)
        if not kr.ok:
            raise RuntimeError(kr.error or "Resource id enumeration failed.")
        return [f"'{_esc(str(r.get('id', '')))}'" for r in kr.rows if r.get("id")]

    def _merge(rows: list[dict[str, Any]]) -> None:
        for r in rows:
            rid = str(r.get("id", "")).lower()
            if rid:
                if rid in seen:
                    continue
                seen.add(rid)
            out.append(r)

    async def _run_ids_adaptive(toks: list[str]) -> None:
        if not toks:
            return
        cap = await _run("id in~ (" + ", ".join(toks) + ")")
        if cap.ok:
            _merge(_parse_rows(cap.stdout))
            return
        truncated = "truncat" in (cap.error or "").lower()
        if truncated and len(toks) > 1:
            mid = len(toks) // 2
            await _run_ids_adaptive(toks[:mid])
            await _run_ids_adaptive(toks[mid:])
            return
        if truncated:
            # A single resource's properties alone exceed the capture cap — skip it instead of
            # failing the whole coverage scan (it simply won't appear in the resource list).
            logger.warning("query_resources_batched: skipping a resource whose properties exceed the capture cap")
            return
        raise RuntimeError(cap.error or "Resource query failed.")

    for pred in predicates:
        if not pred:
            continue
        if heavy and pred.startswith("id in~ ("):
            # Workload-scope id list (already chunked under the query-length budget): pull
            # properties in adaptive id-batches that bisect on any output truncation.
            toks = [t.strip() for t in pred[len("id in~ ("):-1].split(",") if t.strip()]
            for i in range(0, len(toks), _HEAVY_PROJECTION_CHUNK):
                await _run_ids_adaptive(toks[i : i + _HEAVY_PROJECTION_CHUNK])
        else:
            # A subscription / RG predicate (light OR heavy): PAGE through up to ``take`` rows.
            # This collects a >1000-resource subscription in a handful of pages instead of one
            # capped 1000-row query (or thousands of tiny id-batches).
            kr = await _run_paged(pred)
            if kr.ok:
                _merge(kr.rows)
                continue
            if heavy:
                # A heavy page couldn't be captured (a 1000-row full-``properties`` page exceeded
                # the cap on the CLI path). Fall back to enumerating the ids with a light, paged
                # projection, then pull properties in adaptive id-batches (which bisect further).
                ids = await _enumerate_ids(pred)
                for i in range(0, len(ids), _HEAVY_PROJECTION_CHUNK):
                    await _run_ids_adaptive(ids[i : i + _HEAVY_PROJECTION_CHUNK])
                continue
            raise RuntimeError(kr.error or "Resource query failed.")
    return out


# Starting id-count per query for heavy (full-``properties``) projections. Adaptive bisection in
# ``query_resources_batched`` shrinks this further whenever a batch truncates, so it only needs to
# be "usually safe"; 20 keeps the query count low while rarely needing to split.
_HEAVY_PROJECTION_CHUNK = 20



def _parse_rows(stdout: str) -> list[dict[str, Any]]:
    # Salvage a cap-truncated array so a big result isn't silently turned into zero rows
    # (notably on REST connections whose output is sliced rather than erroring).
    return parse_kql_rows(stdout)


def _parse_metric_points(stdout: str, aggregation: str) -> list[float]:
    """Extract the numeric values from an `az monitor metrics list` JSON blob, preferring the
    requested aggregation column and falling back to whatever column is populated."""
    try:
        data = json.loads(stdout or "{}")
    except (json.JSONDecodeError, TypeError):
        return []
    agg = (aggregation or "average").lower()
    out: list[float] = []
    for m in data.get("value", []) or []:
        for ts in m.get("timeseries") or []:
            for pt in ts.get("data") or []:
                val = pt.get(agg)
                if val is None:
                    val = (
                        pt.get("average")
                        or pt.get("maximum")
                        or pt.get("total")
                        or pt.get("count")
                        or pt.get("minimum")
                    )
                if val is not None:
                    out.append(float(val))
    return out


def _reduce_series(values: list[float], how: str) -> float | None:
    """Reduce a metric series to a single comparable number (avg|max|min)."""
    if not values:
        return None
    how = (how or "avg").lower()
    if how == "max":
        return max(values)
    if how == "min":
        return min(values)
    return sum(values) / len(values)


def _metric_violates(value: float, comparison: str, threshold: float) -> bool:
    """True when ``value <comparison> threshold`` holds (lt|le|gt|ge)."""
    c = (comparison or "lt").lower()
    if c in ("lt", "less", "lessthan"):
        return value < threshold
    if c in ("le", "lessorequal"):
        return value <= threshold
    if c in ("gt", "greater", "greaterthan"):
        return value > threshold
    if c in ("ge", "greaterorequal"):
        return value >= threshold
    return False


async def _evaluate_metric_check(
    check: dict[str, Any],
    predicate: str,
    present: set[str],
    connection: dict[str, Any] | None,
    session_config_dir: str | None = None,
) -> dict[str, Any]:
    """Evaluate a metric-backed check: enumerate in-scope resources of the check's type(s),
    pull each one's Azure Monitor metric over the lookback window, and flag resources whose
    reduced value violates the threshold.

    Returns {status, rows[, error]}. Degrades gracefully: resources with no readable metric
    data are skipped; if NO resource yielded data the check is 'error' (excluded from the
    score) rather than a misleading pass."""
    from app.exec.command_runner import run_metrics_capture

    mc = check["metric"]
    types = [t for t in check["resource_types"] if t in present]
    if not types:
        return {"status": "not_applicable", "rows": []}
    type_filter = " or ".join(f"type =~ '{_esc(t)}'" for t in types)
    cap = await run_kql_capture(
        f"Resources | where {predicate} | where {type_filter} "
        f"| project id, name, type, resourceGroup, subscriptionId | take {_METRIC_RESOURCE_CAP}",
        connection,
        output="json",
        session_config_dir=session_config_dir,
    )
    if not cap.ok:
        return {"status": "error", "rows": [], "error": cap.error or "Resource query failed."}
    resources = _parse_rows(cap.stdout)
    start = (datetime.now(timezone.utc) - timedelta(days=mc["lookback_days"])).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    flagged: list[dict[str, Any]] = []
    probed = 0
    for r in resources:
        rid = r.get("id", "")
        if not rid:
            continue
        can = await run_metrics_capture(
            rid,
            [mc["metric"]],
            connection,
            aggregation=mc["aggregation"],
            interval=mc["interval"],
            timespan=start,
            session_config_dir=session_config_dir,
        )
        if not can.ok:
            continue
        values = _parse_metric_points(can.stdout, mc["aggregation"])
        if not values:
            continue
        probed += 1
        value = _reduce_series(values, mc["evaluate"])
        if value is None:
            continue
        if _metric_violates(value, mc["comparison"], mc["threshold"]):
            row = dict(r)
            row["metric_value"] = round(value, 2)
            flagged.append(row)
    if probed == 0:
        return {
            "status": "error",
            "rows": [],
            "error": "No metric data available for any in-scope resource.",
        }
    return {"status": "fail" if flagged else "pass", "rows": flagged}


async def _scan_scope(
    predicate: str, connection: dict[str, Any] | None, session_config_dir: str | None = None
) -> dict[str, Any]:
    """Inventory the workload scope in one pass: the ARM types present (drives per-check
    applicability), the exact total resource count, and a capped sample of resources
    (id/name/type/rg/subscription/location) for the report's Resources tab."""
    types: set[str] = set()
    total = 0
    by_type = await run_kql_capture(
        f"Resources | where {predicate} | summarize n=count() by type",
        connection,
        output="json",
        session_config_dir=session_config_dir,
    )
    if by_type.ok:
        for r in _parse_rows(by_type.stdout):
            t = str(r.get("type", "")).lower()
            if t:
                types.add(t)
            # ARG names count() as the alias we gave ("n"); fall back defensively.
            total += int(r.get("n", r.get("count_", r.get("count", 0))) or 0)

    resources: list[dict[str, Any]] = []
    proj = await run_kql_capture(
        f"Resources | where {predicate} "
        "| project name, type, location, id, resourceGroup, subscriptionId "
        f"| order by type asc, name asc | take {_SCAN_SAMPLE}",
        connection,
        output="json",
        session_config_dir=session_config_dir,
    )
    if proj.ok:
        resources = [
            {
                "id": r.get("id", ""),
                "name": r.get("name", ""),
                "type": r.get("type", ""),
                "resource_group": r.get("resourceGroup", ""),
                "subscription_id": r.get("subscriptionId", ""),
                "location": r.get("location", ""),
            }
            for r in _parse_rows(proj.stdout)
        ]
    return {"types": types, "count": total, "resources": resources}


def _scored(checks: list[dict[str, Any]], findings: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute 0-100 per-pillar scores + totals + completeness/confidence from findings.

    Trust model: a control that could NOT be evaluated (status ``error``) is excluded from
    the optimistic score but counted against *completeness*. A separate ``worst_case`` score
    assumes every errored control would have FAILED, so reviewers see the risk envelope, not
    just the optimistic number. A run whose evaluated coverage is low is flagged low/medium
    confidence so the headline score can be shown as provisional."""
    from app.core.app_settings import assessment_execution, assessment_weights

    weights = assessment_weights()
    by_pillar: dict[str, dict[str, Any]] = {}
    by_severity: dict[str, int] = {"critical": 0, "error": 0, "warning": 0, "info": 0}
    passed = failed = na = waived = errored = manual = 0

    for f in findings:
        pillar = f["pillar"]
        p = by_pillar.setdefault(
            pillar,
            {"weight_total": 0, "weight_passed": 0, "weight_errored": 0,
             "passed": 0, "failed": 0, "na": 0, "waived": 0, "errored": 0, "manual": 0},
        )
        status = f["status"]
        w = weights.get(f.get("severity", "warning"), f.get("weight", 3))
        if status == "not_applicable":
            p["na"] += 1
            na += 1
            continue
        if status == "waived":
            # Suppressed by a risk acceptance — excluded from scoring entirely.
            p["waived"] += 1
            waived += 1
            continue
        if status == "manual":
            # Manual attestation pending — excluded from the auto-score until answered.
            p["manual"] += 1
            manual += 1
            continue
        if status == "error":
            # Could not be evaluated — excluded from the optimistic score, counted against
            # completeness, and treated as a failure in the worst-case score.
            p["errored"] += 1
            p["weight_errored"] += w
            errored += 1
            continue
        # Score weight is derived from severity at run time so admin-tuned weights apply.
        p["weight_total"] += w
        if status == "pass":
            p["weight_passed"] += w
            p["passed"] += 1
            passed += 1
        elif status == "fail":
            p["failed"] += 1
            failed += 1
            by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1

    scores: dict[str, Any] = {}
    pillar_score_values: list[int] = []
    pillar_worst_values: list[int] = []
    for pillar, p in by_pillar.items():
        wt = p["weight_total"]
        score = round(100 * p["weight_passed"] / wt) if wt > 0 else None
        # Worst-case denominator includes errored controls as if they had failed.
        wt_worst = wt + p["weight_errored"]
        worst_score = round(100 * p["weight_passed"] / wt_worst) if wt_worst > 0 else None
        if score is not None:
            pillar_score_values.append(score)
        if worst_score is not None:
            pillar_worst_values.append(worst_score)
        scores[pillar] = {
            "score": score,
            "worst_case_score": worst_score,
            "passed": p["passed"],
            "failed": p["failed"],
            "na": p["na"],
            "waived": p["waived"],
            "errored": p["errored"],
            "manual": p["manual"],
            "total": p["passed"] + p["failed"],
        }
    overall = round(sum(pillar_score_values) / len(pillar_score_values)) if pillar_score_values else None
    worst_overall = round(sum(pillar_worst_values) / len(pillar_worst_values)) if pillar_worst_values else None

    # Completeness: of the controls that SHOULD yield a deterministic verdict (evaluated +
    # errored), how many actually were evaluated. 100% when there was nothing to evaluate.
    evaluatable = passed + failed + errored
    completeness_pct = round(100 * (passed + failed) / evaluatable) if evaluatable else 100
    cfg = assessment_execution()
    high = cfg["confidence_high_pct"]
    if completeness_pct >= high:
        confidence = "high"
    elif completeness_pct >= max(0, high - 15):
        confidence = "medium"
    else:
        confidence = "low"

    # Worst failing severity for the run-level badge.
    worst = "info"
    for sev in ("critical", "error", "warning"):
        if by_severity.get(sev, 0) > 0:
            worst = sev
            break

    return {
        "overall_score": overall,
        "worst_case_score": worst_overall,
        "completeness_pct": completeness_pct,
        "confidence": confidence,
        "scores": scores,
        "totals": {
            "passed": passed, "failed": failed, "na": na, "waived": waived,
            "errored": errored, "manual": manual,
            "evaluated": passed + failed, "evaluatable": evaluatable,
            "completeness_pct": completeness_pct, "confidence": confidence,
            "by_severity": by_severity,
        },
        "severity": worst,
    }


async def _ai_enrich(workload_name: str, findings: list[dict[str, Any]], scores: dict[str, Any]) -> tuple[str, bool]:
    """One LLM call: executive summary + per-failed-finding rationale (hybrid layer).

    Mutates ``findings`` in place to add ``ai_rationale`` to failed checks. Returns
    (summary_markdown, used_ai)."""
    failed = [f for f in findings if f["status"] == "fail"]
    if not failed:
        # Nothing failed — produce a short deterministic summary, skip the LLM.
        return ("No failed controls. All applicable checks passed for this workload.", False)

    from app.agent.factory import build_provider_for

    lines = []
    for f in failed:
        lines.append(
            f"- [{f['check_id']}] ({f['pillar']}/{f['severity']}) {f['title']} — "
            f"{f['flagged_count']} flagged resource(s)"
        )
    score_line = ", ".join(
        f"{p}: {v.get('score')}" for p, v in (scores or {}).items()
    )
    sys = (
        "You are an Azure Well-Architected reviewer. You are given the FAILED controls from a "
        "workload assessment (already evaluated deterministically). Write a concise executive "
        "summary and a one-sentence business-impact rationale for each failed control. Be "
        "specific and prioritize by severity. Reply with ONLY JSON (no code fence): "
        '{"summary": "<2-4 sentence markdown exec summary>", '
        '"rationales": {"<check_id>": "<one sentence impact/why it matters>"}}'
    )
    user = (
        f"Workload: {workload_name}\nPillar scores (0-100): {score_line}\n\n"
        f"Failed controls:\n" + "\n".join(lines)
    )

    provider = build_provider_for(None, None)
    parts: list[str] = []
    try:
        async for ev in provider.stream(
            [{"role": "system", "content": sys}, {"role": "user", "content": user}], None
        ):
            if ev.type == "token":
                parts.append(ev.text)
    except Exception as exc:  # noqa: BLE001 - AI is best-effort
        logger.warning("Assessment AI enrichment failed: %s", exc)
        return ("", False)
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass

    text = "".join(parts).strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return (text[:1200], bool(text))
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return (text[:1200], bool(text))
    rationales = data.get("rationales") or {}
    if isinstance(rationales, dict):
        for f in failed:
            r = rationales.get(f["check_id"])
            if isinstance(r, str) and r.strip():
                f["ai_rationale"] = r.strip()
    return (str(data.get("summary", "")).strip(), True)


async def _active_waivers(workload_id: str, tenant_id: str) -> dict[str, list[dict[str, Any]]]:
    """Active (non-revoked, non-expired) waivers for a workload, keyed by check id."""
    from app.models import AssessmentWaiver

    now = _now()
    out: dict[str, list[dict[str, Any]]] = {}
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(AssessmentWaiver).where(
                    AssessmentWaiver.tenant_id == tenant_id,
                    AssessmentWaiver.workload_id == workload_id,
                    AssessmentWaiver.status == "active",
                )
            )
        ).scalars().all()
    for w in rows:
        if w.expires_at is not None:
            exp = w.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp <= now:
                continue  # expired
        out.setdefault(w.check_id, []).append(
            {"resource_id": w.resource_id, "justification": w.justification, "approver": w.approver, "id": w.id}
        )
    return out


def _apply_waivers(finding: dict[str, Any], waivers: list[dict[str, Any]]) -> None:
    """Mutate a FAILED finding per its waivers: whole-check waiver ⇒ status 'waived';
    per-resource waivers remove those resources (and pass if all are cleared)."""
    if not waivers or finding["status"] != "fail":
        return
    whole = [w for w in waivers if not w.get("resource_id")]
    if whole:
        finding["status"] = "waived"
        finding["waiver"] = {"justification": whole[0]["justification"], "approver": whole[0]["approver"]}
        return
    waived_ids = {w["resource_id"].lower() for w in waivers if w.get("resource_id")}
    if waived_ids:
        kept = [r for r in finding["flagged_resources"] if r.get("id", "").lower() not in waived_ids]
        removed = len(finding["flagged_resources"]) - len(kept)
        finding["flagged_resources"] = kept
        finding["flagged_count"] = max(0, finding["flagged_count"] - removed)
        if finding["flagged_count"] == 0:
            finding["status"] = "waived"
            finding["waiver"] = {"justification": "All flagged resources waived", "approver": ""}


async def _previous_run(tenant_id: str, workload_id: str, pillars: list[str]) -> AssessmentRun | None:
    async with SessionLocal() as db:
        # Prefer an admin-pinned baseline run; else the most recent succeeded run.
        baseline = (
            await db.execute(
                select(AssessmentRun)
                .where(
                    AssessmentRun.tenant_id == tenant_id,
                    AssessmentRun.workload_id == workload_id,
                    AssessmentRun.status == "succeeded",
                    AssessmentRun.is_baseline.is_(True),
                    AssessmentRun.deleted_at.is_(None),
                )
                .order_by(desc(AssessmentRun.started_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if baseline is not None:
            return baseline
        return (
            await db.execute(
                select(AssessmentRun)
                .where(
                    AssessmentRun.tenant_id == tenant_id,
                    AssessmentRun.workload_id == workload_id,
                    AssessmentRun.status == "succeeded",
                    AssessmentRun.deleted_at.is_(None),
                )
                .order_by(desc(AssessmentRun.started_at))
                .limit(1)
            )
        ).scalar_one_or_none()


def _compute_diff(prev: AssessmentRun | None, findings: list[dict[str, Any]], scores: dict[str, Any]) -> dict[str, Any] | None:
    if prev is None:
        return None
    prev_by_id = {f.get("check_id"): f for f in (prev.findings_json or [])}
    new_failures: list[dict[str, Any]] = []
    resolved: list[str] = []
    new_criticals = 0
    for f in findings:
        cid = f["check_id"]
        before = (prev_by_id.get(cid) or {}).get("status")
        if f["status"] == "fail" and before in ("pass", "waived", None):
            new_failures.append({"title": f["title"], "severity": f["severity"]})
            if f["severity"] == "critical":
                new_criticals += 1
        if f["status"] in ("pass", "waived") and before == "fail":
            resolved.append(f["title"])
    score_delta: dict[str, Any] = {}
    prev_scores = (prev.scores_json or {})
    for pillar, v in (scores or {}).items():
        before = (prev_scores.get(pillar) or {}).get("score")
        score_delta[pillar] = {"before": before, "after": v.get("score")}
    return {
        "baseline_run_id": prev.id,
        "baseline_is_pinned": bool(getattr(prev, "is_baseline", False)),
        "new_failures": new_failures,
        "new_criticals": new_criticals,
        "resolved": resolved,
        "score_delta": score_delta,
    }


def _new_finding_base(check: dict[str, Any]) -> dict[str, Any]:
    """The per-check result skeleton shared by every control kind."""
    return {
        "check_id": check["id"],
        "pillar": check["pillar"],
        "title": check["title"],
        "description": check["description"],
        "severity": check["severity"],
        "weight": check["weight"],
        "frameworks": check["frameworks"],
        "remediation": check["remediation"],
        "remediation_command": check["remediation_command"],
        "resource_types": check["resource_types"],
        "kind": check.get("kind") or ("metric" if check.get("metric") else "graph"),
        "impact": check.get("impact", ""),
        "effort": check.get("effort", ""),
        "sub_category": check.get("sub_category", ""),
        "source": check.get("source", "built-in"),
        "profile": check.get("profile", ""),
        "learn_more": check.get("learn_more", []),
        "flagged_count": 0,
        "flagged_resources": [],
        "partial": False,
        "ai_rationale": "",
    }


def _subscription_subject(guid: str, template: str, name: str = "") -> dict[str, Any]:
    """Synthesize a flagged-resource entry whose subject is a whole subscription (used by
    subscription-scoped CIS controls and existence/absence checks, where the failing subject
    is the subscription itself, not an individual resource)."""
    label = name or guid
    return {
        "id": f"/subscriptions/{guid}",
        "name": label,
        "type": "microsoft.resources/subscriptions",
        "resource_group": "",
        "subscription_id": guid,
        "remediation_command": _real_remediation(template, {"name": label, "subscription_id": guid}),
    }


def _tenant_subject(tenant_id: str, template: str, name: str = "") -> dict[str, Any]:
    """Synthesize a flagged-resource entry whose subject is the whole Entra tenant (used by
    the identity-policy controls that live in Microsoft Graph, where the failing subject is
    the directory itself, not an Azure resource)."""
    label = name or tenant_id or "tenant"
    return {
        "id": f"/tenants/{tenant_id}" if tenant_id else "/tenants/current",
        "name": label,
        "type": "microsoft.aad/tenant",
        "resource_group": "",
        "subscription_id": "",
        "remediation_command": _real_remediation(template, {"name": label}),
    }


# --- Microsoft Graph (Entra tenant identity policy) access. ---------------------------------
# Cache GET responses per (tenant, path) for a short TTL and serialize fetches so the handful
# of identity controls that share a Graph object (e.g. /policies/authorizationPolicy) cause a
# single network fetch per object per run instead of one per control.
_GRAPH_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_GRAPH_CACHE_TTL = 60.0
_GRAPH_LOCK = asyncio.Lock()


async def _graph_get(connection: dict[str, Any] | None, path: str) -> tuple[bool, dict[str, Any], str]:
    """GET a Microsoft Graph v1.0 object, cached + deduped per (tenant, path).

    Returns (ok, json, error). FAIL-CLOSED: any token/HTTP/parse failure returns ok=False so
    the calling control is marked ``error`` (excluded from the score) rather than a false pass.
    """
    from app.azure.credentials import get_graph_token

    tenant = (connection or {}).get("tenant_id", "") or "_"
    key = (tenant, path)
    async with _GRAPH_LOCK:
        now = _time.monotonic()
        hit = _GRAPH_CACHE.get(key)
        if hit and (now - hit[0]) < _GRAPH_CACHE_TTL:
            return True, hit[1], ""
        token, terr = await get_graph_token(connection or {})
        if not token:
            return False, {}, terr or "Could not acquire a Microsoft Graph token."
        url = f"https://graph.microsoft.com/v1.0{path}"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:  # noqa: BLE001
            return False, {}, f"Graph request error: {e}"
        if resp.status_code != 200:
            return False, {}, f"Graph GET {path} failed ({resp.status_code}): {resp.text[:200]}"
        try:
            body = resp.json()
        except ValueError:
            return False, {}, "Graph returned a non-JSON response."
        _GRAPH_CACHE[key] = (now, body)
        return True, body, ""


def _drill(obj: Any, dotted: str) -> Any:
    """Walk a dotted path (``a.b.c``) into nested dicts; return None if any hop is missing."""
    cur = obj
    for part in (dotted or "").split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _policy_satisfied(val: Any, op: str, expected: Any) -> bool:
    """Compare a Graph policy value against the control's expectation."""
    if op == "is_true":
        return val is True
    if op == "is_false":
        return val is False
    if op == "equals":
        return val == expected
    if op == "not_equals":
        return val != expected
    if op == "in":
        return val in (expected or [])
    return False


async def _arm_get_token(token: str, url: str) -> tuple[bool, dict[str, Any], str]:
    """GET an ARM resource with an already-acquired token. Returns (ok, json, error)."""
    try:
        async with httpx.AsyncClient(timeout=40) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as e:  # noqa: BLE001
        return False, {}, f"ARM request error: {e}"
    if resp.status_code != 200:
        return False, {}, f"ARM GET failed ({resp.status_code}): {resp.text[:200]}"
    try:
        return True, resp.json(), ""
    except ValueError:
        return False, {}, "ARM returned a non-JSON response."


def _diag_covers(settings: list[dict[str, Any]], required: set[str]) -> bool:
    """True when some diagnostic setting enables the ``allLogs`` category group, or enables
    every required individual log category (Activity-Log category check for CIS 6.1.1.2)."""
    for s in settings or []:
        logs = ((s.get("properties") or {}).get("logs")) or []
        enabled_groups = {str(l.get("categoryGroup", "")).lower() for l in logs if l.get("enabled")}
        if "alllogs" in enabled_groups:
            return True
        enabled_cats = {str(l.get("category", "")) for l in logs if l.get("enabled")}
        if required and required.issubset(enabled_cats):
            return True
    return False


def _diag_resource_ok(settings: list[dict[str, Any]], required: set[str]) -> bool:
    """True when a per-resource diagnostic setting enables the relevant logs. With ``required``
    categories given (e.g. {AuditEvent} for CIS 6.1.1.4) it matches the ``allLogs``/``audit``
    group or any required category; with no required categories (existence mode, e.g. CIS
    2.1.7 Databricks log delivery) any enabled log entry suffices."""
    for s in settings or []:
        logs = ((s.get("properties") or {}).get("logs")) or []
        for l in logs:
            if not l.get("enabled"):
                continue
            if not required:
                return True
            if str(l.get("categoryGroup", "")).lower() in ("alllogs", "audit"):
                return True
            if str(l.get("category", "")) in required:
                return True
    return False


def _flagged_resource(r: dict[str, Any], template: str, *, metric: bool = False) -> dict[str, Any]:
    """Project an ARG/metric row to the stored flagged-resource shape, with a ready-to-run
    remediation command (placeholders filled from the resource's real values)."""
    out = {
        "id": r.get("id", ""),
        "name": r.get("name", ""),
        "type": r.get("type", ""),
        "resource_group": r.get("resourceGroup", ""),
        "subscription_id": r.get("subscriptionId", ""),
        "remediation_command": _real_remediation(
            template,
            {
                "name": r.get("name", ""),
                "resource_group": r.get("resourceGroup", ""),
                "subscription_id": r.get("subscriptionId", ""),
            },
        ),
    }
    if metric:
        out["metric_value"] = r.get("metric_value")
    return out


async def _execute_check(
    check: dict[str, Any],
    predicate: str,
    present: set[str],
    connection: dict[str, Any] | None,
    session_dir: str | None,
    attestation: dict[str, Any] | None = None,
    sub_predicate: str = "",
    in_scope_subs: list[str] | None = None,
) -> dict[str, Any]:
    """Evaluate ONE control and return its finished finding dict.

    No waivers applied and no events yielded here — the caller does both, so this helper is
    safe to run sequentially OR concurrently. FAIL-CLOSED: any query/auth/throttle error sets
    status ``error`` (excluded from the score), never a misleading ``pass``. A control whose
    violating set was capped by paging is marked ``partial`` so the count is treated as a
    lower bound.

    Control kinds:
    - ``graph``  Resource Graph (KQL) predicate (the default).
    - ``metric`` Azure Monitor threshold per in-scope resource.
    - ``signal`` live platform signal (Azure Advisor recommendation) joined to in-scope resources.
    - ``manual`` human attestation — ``manual`` (pending) until an attestation is recorded.

    Graph controls additionally support:
    - ``arg_table``   the ARG table to query (``Resources`` default; ``securityresources`` /
                      ``authorizationresources`` for subscription-scoped CIS controls). Non-
                      ``Resources`` tables are scoped by ``sub_predicate`` (subscription-level).
    - ``expectation`` ``"present"`` flips to existence/absence mode: the KQL returns the
                      subscriptions that HAVE the desired config; any in-scope subscription
                      MISSING it fails, with the subscription as the flagged subject.
    """
    base = _new_finding_base(check)
    template = check.get("remediation_command", "")
    kind = base["kind"]
    in_scope_subs = in_scope_subs or []

    # Applicability: a control with resource_types applies only when the scope contains one of
    # those types; a control with NO resource_types (e.g. workload-level signal/manual) always
    # applies. (Empty resource_types must NOT collapse to "not applicable".)
    rtypes = check.get("resource_types") or []
    applicable = (not rtypes) or any(t in present for t in rtypes)

    # --- Manual attestation control -----------------------------------------------------
    if kind == "manual":
        if not applicable:
            base["status"] = "not_applicable"
            return base
        st = (attestation or {}).get("status")
        if st in ("pass", "fail", "not_applicable"):
            base["status"] = st
            base["attestation"] = {
                "status": st,
                "note": (attestation or {}).get("note", ""),
                "by": (attestation or {}).get("by", ""),
                "at": (attestation or {}).get("at", ""),
            }
        else:
            base["status"] = "manual"  # pending human review — excluded from the auto-score
        return base

    if not applicable:
        base["status"] = "not_applicable"
        return base

    # --- Metric-backed control (Azure Monitor threshold per in-scope resource). ----------
    if check.get("metric"):
        res = await _evaluate_metric_check(check, predicate, present, connection, session_dir)
        if res["status"] == "error":
            base["status"] = "error"
            base["error"] = (res.get("error") or "Metric evaluation failed.")[:300]
            return base
        rows = res["rows"]
        if rows:
            base["status"] = "fail"
            base["flagged_count"] = len(rows)
            base["flagged_resources"] = [_flagged_resource(r, template, metric=True) for r in rows[:_FLAGGED_SAMPLE]]
        else:
            base["status"] = "pass"
        return base

    # --- Live-signal control (Azure Advisor) — joined to in-scope resources. -------------
    if kind == "signal":
        kql = _signal_kql(check, predicate)
        if not kql:
            base["status"] = "error"
            base["error"] = "Signal control is misconfigured (no query)."
            return base
        res = await run_kql_collect(kql, connection, session_config_dir=session_dir)
        if not res.ok:
            base["status"] = "error"
            base["error"] = (res.error or "Advisor query failed.")[:300]
            return base
        rows = res.rows
        if rows:
            base["status"] = "fail"
            base["flagged_count"] = res.total if res.total is not None else len(rows)
            base["partial"] = not res.complete
            base["flagged_resources"] = [_flagged_resource(r, template) for r in rows[:_FLAGGED_SAMPLE]]
        else:
            base["status"] = "pass"
        return base

    # --- Microsoft Graph (Entra tenant identity policy) control. -------------------------
    # The failing subject is the tenant itself; a single GET (cached/deduped) drives the check.
    if kind == "graph_api":
        spec = check.get("graph_check") or {}
        path = str(spec.get("path", ""))
        if not path:
            base["status"] = "error"
            base["error"] = "Graph control is misconfigured (no path)."
            return base
        ok, body, err = await _graph_get(connection, path)
        if not ok:
            base["status"] = "error"
            base["error"] = err[:300]
            return base
        val = _drill(body, str(spec.get("field", "")))
        if _policy_satisfied(val, str(spec.get("op", "equals")), spec.get("expected")):
            base["status"] = "pass"
        else:
            tenant = (connection or {}).get("tenant_id", "")
            base["status"] = "fail"
            base["flagged_count"] = 1
            base["flagged_resources"] = [
                _tenant_subject(tenant, template, (connection or {}).get("name", ""))
            ]
        return base

    # --- Control-plane ARM REST control (diagnostic settings / App Service logs). ---------
    # ARG does not surface diagnostic settings, so these fan out over ARM REST: subscription
    # Activity-Log diag settings (one call per in-scope subscription) or App Service HTTP logs
    # (one call per in-scope site, bounded). FAIL-CLOSED on any token/HTTP error.
    if kind == "arm_rest":
        from app.azure.credentials import get_arm_token

        spec = check.get("rest_check") or {}
        mode = str(spec.get("mode", ""))
        token, terr = await get_arm_token(connection or {})
        if not token:
            base["status"] = "error"
            base["error"] = (terr or "Could not acquire an ARM token.")[:300]
            return base

        if mode in ("diag_exists", "diag_categories"):
            if not in_scope_subs:
                base["status"] = "not_applicable"
                return base
            required = {str(c) for c in (spec.get("categories") or [])}
            failed: list[str] = []
            for sub in in_scope_subs:
                url = (
                    f"https://management.azure.com/subscriptions/{sub}"
                    "/providers/microsoft.insights/diagnosticSettings?api-version=2021-05-01-preview"
                )
                ok, body, err = await _arm_get_token(token, url)
                if not ok:
                    base["status"] = "error"
                    base["error"] = err[:300]
                    return base
                settings = body.get("value", []) or []
                good = (len(settings) > 0) if mode == "diag_exists" else _diag_covers(settings, required)
                if not good:
                    failed.append(sub)
            if failed:
                base["status"] = "fail"
                base["flagged_count"] = len(failed)
                base["flagged_resources"] = [_subscription_subject(s, template) for s in failed[:_FLAGGED_SAMPLE]]
            else:
                base["status"] = "pass"
            return base

        if mode == "diag_resource":
            rtypes = check.get("resource_types") or []
            if not predicate or not rtypes:
                base["status"] = "not_applicable"
                return base
            type_filter = " or ".join(f"type =~ '{_esc(t)}'" for t in rtypes)
            kql = (
                f"Resources | where {predicate}\n"
                f"| where {type_filter} "
                "| project id, name, type, resourceGroup, subscriptionId"
            )
            res = await run_kql_collect(
                kql, connection, session_config_dir=session_dir, max_rows=_METRIC_RESOURCE_CAP
            )
            if not res.ok:
                base["status"] = "error"
                base["error"] = (res.error or "Resource query failed.")[:300]
                return base
            targets = res.rows
            if not targets:
                base["status"] = "not_applicable"
                return base
            required = {str(c) for c in (spec.get("categories") or [])}
            failed_res: list[dict[str, Any]] = []
            for target in targets[:_METRIC_RESOURCE_CAP]:
                url = (
                    f"https://management.azure.com{target.get('id', '')}"
                    "/providers/microsoft.insights/diagnosticSettings?api-version=2021-05-01-preview"
                )
                ok, body, err = await _arm_get_token(token, url)
                if not ok:
                    base["status"] = "error"
                    base["error"] = err[:300]
                    return base
                if not _diag_resource_ok(body.get("value", []) or [], required):
                    failed_res.append(target)
            if failed_res:
                base["status"] = "fail"
                base["flagged_count"] = len(failed_res)
                base["partial"] = not res.complete
                base["flagged_resources"] = [_flagged_resource(r, template) for r in failed_res[:_FLAGGED_SAMPLE]]
            else:
                base["status"] = "pass"
            return base

        if mode == "app_httplogs":
            if not predicate:
                base["status"] = "not_applicable"
                return base
            kql = (
                f"Resources | where {predicate}\n"
                "| where type =~ 'microsoft.web/sites' "
                "| project id, name, type, resourceGroup, subscriptionId"
            )
            res = await run_kql_collect(
                kql, connection, session_config_dir=session_dir, max_rows=_METRIC_RESOURCE_CAP
            )
            if not res.ok:
                base["status"] = "error"
                base["error"] = (res.error or "Site query failed.")[:300]
                return base
            sites = res.rows
            if not sites:
                base["status"] = "not_applicable"
                return base
            failed_sites: list[dict[str, Any]] = []
            for site in sites[:_METRIC_RESOURCE_CAP]:
                url = f"https://management.azure.com{site.get('id', '')}/config/logs?api-version=2022-03-01"
                ok, body, err = await _arm_get_token(token, url)
                if not ok:
                    base["status"] = "error"
                    base["error"] = err[:300]
                    return base
                http = (body.get("properties", {}) or {}).get("httpLogs", {}) or {}
                enabled = bool((http.get("fileSystem", {}) or {}).get("enabled")) or bool(
                    (http.get("azureBlobStorage", {}) or {}).get("enabled")
                )
                if not enabled:
                    failed_sites.append(site)
            if failed_sites:
                base["status"] = "fail"
                base["flagged_count"] = len(failed_sites)
                base["partial"] = not res.complete
                base["flagged_resources"] = [_flagged_resource(r, template) for r in failed_sites[:_FLAGGED_SAMPLE]]
            else:
                base["status"] = "pass"
            return base

        base["status"] = "error"
        base["error"] = f"Unknown arm_rest mode '{mode}'."
        return base

    # --- Resource Graph (KQL) control — paged + fail-closed. -----------------------------
    # Subscription-scoped CIS controls query a different ARG table (securityresources /
    # authorizationresources) and are scoped by subscription, not the resource predicate.
    arg_table = (check.get("arg_table") or "Resources").strip() or "Resources"
    expectation = (check.get("expectation") or "").strip().lower()
    scope_mode = (check.get("scope_mode") or "").strip().lower()
    # Existence/absence checks are inherently subscription-scoped (a control "exists somewhere
    # in the subscription"), so they always use the subscription predicate. Likewise any
    # non-Resources ARG table is subscription-scoped. Plain violation checks on Resources use
    # the full resource predicate (subscription / RG / resource-id granularity).
    # ``scope_mode="tenant"`` opts out entirely (tenant-wide governance controls, e.g. custom
    # role definitions which carry no subscriptionId) — the check's KQL scopes itself.
    if scope_mode == "tenant":
        scope_pred = "1 == 1"
    elif expectation == "present" or arg_table.lower() != "resources":
        scope_pred = sub_predicate
    else:
        scope_pred = predicate
    if not scope_pred:
        # A subscription-scoped control with no resolvable subscription scope can't be evaluated.
        base["status"] = "not_applicable"
        return base

    kql = f"{arg_table} | where {scope_pred}\n{check['kql']}"
    res = await run_kql_collect(kql, connection, session_config_dir=session_dir)
    if not res.ok:
        base["status"] = "error"
        base["error"] = (res.error or "Query failed.")[:300]
        return base
    rows = res.rows

    # --- Existence / absence mode: KQL returns subscriptions that HAVE the desired config;
    # any in-scope subscription MISSING it is the violation (subject = the subscription). -----
    if expectation == "present":
        if not in_scope_subs:
            base["status"] = "not_applicable"
            return base
        have = {
            _sub_guid(str(r.get("subscriptionId", "")))
            for r in rows
            if r.get("subscriptionId")
        }
        missing = [s for s in in_scope_subs if s not in have]
        if missing:
            base["status"] = "fail"
            base["flagged_count"] = len(missing)
            base["flagged_resources"] = [_subscription_subject(s, template) for s in missing[:_FLAGGED_SAMPLE]]
        else:
            base["status"] = "pass"
        return base

    # --- Violation mode: rows ARE the violating resources/subscriptions. -----------------
    if rows:
        base["status"] = "fail"
        # Prefer ARG's reported total (accurate even when the row sample is capped); fall
        # back to the fetched row count. The stored flagged_resources remain a capped sample.
        base["flagged_count"] = res.total if res.total is not None else len(rows)
        base["partial"] = not res.complete  # sample capped — more violating resources exist
        base["flagged_resources"] = [_flagged_resource(r, template) for r in rows[:_FLAGGED_SAMPLE]]
    else:
        base["status"] = "pass"
    return base


def _signal_kql(check: dict[str, Any], predicate: str) -> str:
    """Build the Resource Graph query for an Azure Advisor signal control: Advisor
    recommendations in the configured category, JOINED to the in-scope resources (so the
    workload scope is honored exactly like every other control)."""
    sig = check.get("signal") or {}
    category = str(sig.get("category", "")).strip()
    if (sig.get("provider", "advisor") != "advisor") or not category:
        return ""
    cat = _esc(category.lower())
    return (
        "advisorresources "
        "| where type =~ 'microsoft.advisor/recommendations' "
        f"| where tolower(tostring(properties.category)) == '{cat}' "
        "| extend rid = tolower(tostring(properties.resourceMetadata.resourceId)) "
        "| where isnotempty(rid) "
        "| join kind=inner (resources "
        f"| where {predicate} "
        "| project rid = tolower(id), rname = name, rtype = type, resourceGroup, subscriptionId) on rid "
        "| project id = rid, name = rname, type = rtype, resourceGroup, subscriptionId "
        "| distinct id, name, type, resourceGroup, subscriptionId"
    )


async def run_assessment(
    *,
    workload_id: str,
    pillars: list[str],
    tenant_id: str,
    connection_id: str | None = None,
    actor: str = "",
    trigger: str = "manual",
    use_ai: bool = True,
    existing_run_id: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run an assessment end-to-end, yielding progress events and a final ``done`` event
    carrying the persisted run id.

    When ``existing_run_id`` is given, that pre-created (queued) run row is updated in
    place instead of inserting a new one — enabling background/batch execution with a
    live status visible in history."""
    started = _time.perf_counter()
    if existing_run_id:
        await _set_run_status(existing_run_id, "running")
    if _is_cancelled(existing_run_id):
        _clear_cancel(existing_run_id)
        await _set_run_status(existing_run_id, "cancelled")
        yield _event("cancelled", run_id=existing_run_id or "")
        return
    workload = get_workload(workload_id)
    if workload is None:
        yield _event("error", message="Workload not found.")
        return

    pillars = [p for p in pillars if p in catalog.PILLARS] or list(catalog.PILLARS)
    from app.core.azure_connections import resolve_connection
    from app.exec.command_runner import close_sp_session, open_sp_session

    conn_id = connection_id or workload.get("connection_id") or ""
    connection = resolve_connection(conn_id or None)
    wl_name = workload.get("name", "workload")

    # Open ONE service-principal session for the whole run (a no-op for managed-identity /
    # pasted-token / ambient connections). Without this, every one of the 60+ controls would
    # pay a fresh `az login`, which is slow and turns one auth hiccup into many failed checks.
    session_dir, sess_err = await open_sp_session(connection)
    if sess_err:
        msg = f"Could not authenticate the workload's connection: {sess_err}"
        if existing_run_id:
            await _set_run_status(existing_run_id, "failed", error=msg[:2000])
        yield _event("error", message=msg)
        return

    # Pre-flight auth probe: confirm the connection can obtain an ARM token BEFORE running
    # the 100+ controls. ``open_sp_session`` only validates service-principal logins; a pasted
    # ARM token (az_cli_token) that has expired is a no-op there, so without this every control
    # would fail its Resource Graph query with the same auth error — surfacing 100+ identical
    # "error" rows instead of one clear, actionable message. The probe is cheap (a local expiry
    # check for pasted tokens; a token mint for SP/MI) and fails the run fast with the real cause.
    if connection is not None:
        from app.azure.credentials import get_arm_token

        _tok, _tok_err = await get_arm_token(connection)
        if _tok_err:
            msg = f"Connection '{connection.get('display_name', wl_name)}' can't authenticate to Azure: {_tok_err}"
            close_sp_session(session_dir)
            if existing_run_id:
                await _set_run_status(existing_run_id, "failed", error=msg[:2000])
            yield _event("error", message=msg)
            return

    yield _event("status", phase="scope", message=f"Resolving scope for '{wl_name}'…")
    scope = await _resolve_scope(workload, connection)
    if scope["error"]:
        close_sp_session(session_dir)
        yield _event("error", message=scope["error"])
        return
    predicate = scope["predicate"]

    yield _event("status", phase="inventory", message="Enumerating resources in scope…")
    scan = await _scan_scope(predicate, connection, session_dir)
    present = scan["types"]
    resource_count = scan["count"]
    scanned_resources = scan["resources"]
    yield _event(
        "status",
        phase="inventory",
        message=f"{resource_count} resource(s) across {len(present)} type(s) in scope.",
        subscriptions=len(scope["subscriptions"]),
        resources=resource_count,
    )

    checks = catalog.checks_for(pillars)
    findings: list[dict[str, Any]] = []
    total = len(checks)
    waivers = await _active_waivers(workload_id, tenant_id)
    try:
        from app.assessments.attestations import get_attestations

        attestations = get_attestations(tenant_id, workload_id)
    except Exception:  # noqa: BLE001 - attestations optional
        attestations = {}

    from app.core.app_settings import assessment_execution

    cfg = assessment_execution()
    sem = asyncio.Semaphore(cfg["concurrency"])
    check_timeout = cfg["check_timeout_s"]
    run_deadline = _time.monotonic() + cfg["run_budget_s"]
    results_q: asyncio.Queue = asyncio.Queue()

    async def _worker(chk: dict[str, Any]) -> None:
        """Evaluate one control under the concurrency gate + a per-control timeout, and push
        the finished finding onto the queue. Never raises — failures become 'error' findings
        so one bad control can't sink the whole run."""
        async with sem:
            base = _new_finding_base(chk)
            if _is_cancelled(existing_run_id):
                base["status"] = "error"
                base["error"] = "Run cancelled."
                await results_q.put((chk, base, True))
                return
            remaining = max(1.0, run_deadline - _time.monotonic())
            try:
                base = await asyncio.wait_for(
                    _execute_check(
                        chk, predicate, present, connection, session_dir,
                        attestation=attestations.get(chk["id"]),
                        sub_predicate=scope.get("sub_predicate", ""),
                        in_scope_subs=scope.get("effective_subscriptions", []),
                    ),
                    timeout=min(check_timeout, remaining),
                )
            except asyncio.TimeoutError:
                base["status"] = "error"
                base["error"] = f"Control exceeded the {check_timeout}s timeout."
            except Exception as exc:  # noqa: BLE001 - isolate a bad control
                base["status"] = "error"
                base["error"] = f"Control failed: {exc}"[:300]
            await results_q.put((chk, base, False))

    tasks = [asyncio.create_task(_worker(c)) for c in checks]
    yield _event(
        "status", phase="checks",
        message=f"Evaluating {total} control(s) (up to {cfg['concurrency']} at a time)…",
        total=total,
    )

    async def _abort_tasks() -> None:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    completed = 0
    cancelled = False
    budget_exhausted = False
    for _ in range(total):
        chk, base, _was_cancel = await results_q.get()
        completed += 1
        if _is_cancelled(existing_run_id):
            cancelled = True
            break
        _apply_waivers(base, waivers.get(chk["id"], []))
        findings.append(base)
        yield _event(
            "check_result",
            index=completed,
            total=total,
            check_id=chk["id"],
            title=chk["title"],
            pillar=chk["pillar"],
            severity=chk["severity"],
            status=base["status"],
            flagged_count=base["flagged_count"],
        )
        # Enforce the overall run budget: stop accepting new results once it's blown and
        # mark every control that hadn't finished as 'error' (excluded from the score).
        if not budget_exhausted and _time.monotonic() > run_deadline and completed < total:
            budget_exhausted = True
            break

    if cancelled:
        await _abort_tasks()
        _clear_cancel(existing_run_id)
        close_sp_session(session_dir)
        await _set_run_status(existing_run_id, "cancelled")
        yield _event("cancelled", run_id=existing_run_id or "")
        return

    if budget_exhausted:
        await _abort_tasks()
        done_ids = {f["check_id"] for f in findings}
        for chk in checks:
            if chk["id"] in done_ids:
                continue
            base = _new_finding_base(chk)
            base["status"] = "error"
            base["error"] = "Run time budget exhausted before this control was evaluated."
            findings.append(base)
        yield _event(
            "status", phase="checks",
            message="Run time budget reached — remaining controls marked not evaluated.",
        )

    # Session is no longer needed once every control has run (scoring + AI use no Azure calls).
    close_sp_session(session_dir)

    # --- Score ---------------------------------------------------------------
    yield _event("status", phase="scoring", message="Scoring results…")
    sc = _scored(checks, findings)

    # --- AI hybrid layer -----------------------------------------------------
    summary = ""
    used_ai = False
    if use_ai:
        yield _event("status", phase="ai", message="Generating executive summary…")
        try:
            summary, used_ai = await _ai_enrich(wl_name, findings, sc["scores"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI enrich error: %s", exc)
    if not summary:
        t = sc["totals"]
        summary = (
            f"Assessed {t['passed'] + t['failed']} applicable controls: "
            f"{t['passed']} passed, {t['failed']} failed. Overall score {sc['overall_score']}/100."
        )

    # --- Diff vs previous ----------------------------------------------------
    prev = await _previous_run(tenant_id, workload_id, pillars)
    diff = _compute_diff(prev, findings, sc["scores"])

    # --- Persist -------------------------------------------------------------
    duration_ms = int((_time.perf_counter() - started) * 1000)
    fields = dict(
        workload_id=workload_id,
        workload_name=wl_name,
        tenant_id=tenant_id,
        connection_id=conn_id or None,
        pillars=pillars,
        status="succeeded",
        overall_score=sc["overall_score"],
        scores_json=sc["scores"],
        totals_json=sc["totals"],
        severity=sc["severity"],
        findings_json=findings,
        resource_count=resource_count,
        resources_json=scanned_resources,
        summary=summary,
        used_ai=used_ai,
        catalog_version=catalog.CATALOG_VERSION,
        schema_version=catalog.FINDING_SCHEMA_VERSION,
        completeness_pct=sc.get("completeness_pct"),
        confidence=sc.get("confidence"),
        baseline_run_id=(prev.id if prev else None),
        diff_json=diff,
        triggered_by=actor,
        trigger=trigger,
        ended_at=_now(),
        duration_ms=duration_ms,
    )
    async with SessionLocal() as db:
        run = None
        if existing_run_id:
            run = await db.get(AssessmentRun, existing_run_id)
        if run is not None:
            for k, v in fields.items():
                setattr(run, k, v)
        else:
            run = AssessmentRun(started_at=_now(), **fields)
            db.add(run)
        await db.commit()
        await db.refresh(run)
        run_id = run.id

    # Publish a notification event (best-effort) for routing / alerts.
    try:
        from app.notifications.engine import publish

        await publish(
            tenant_id=tenant_id,
            type="assessment.completed",
            source="assessment",
            severity=sc["severity"] if sc["totals"]["failed"] else "info",
            title=f"Assessment: {wl_name} scored {sc['overall_score']}/100",
            body=summary[:1000],
            facts={
                "workload_id": workload_id,
                "overall_score": sc["overall_score"],
                "failed": sc["totals"]["failed"],
            },
            links={"run_id": run_id},
            fingerprint=f"assessment:{workload_id}",
        )
    except Exception:  # noqa: BLE001 - notifications optional
        pass

    yield _event(
        "done",
        run_id=run_id,
        overall_score=sc["overall_score"],
        worst_case_score=sc.get("worst_case_score"),
        completeness_pct=sc.get("completeness_pct"),
        confidence=sc.get("confidence"),
        scores=sc["scores"],
        totals=sc["totals"],
        severity=sc["severity"],
        used_ai=used_ai,
        summary=summary,
        diff=diff,
        duration_ms=duration_ms,
    )
