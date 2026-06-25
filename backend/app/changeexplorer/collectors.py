"""Change collectors — query Azure for raw change rows. Read-only and best-effort: a source
that can't be reached returns ``(rows, note)`` with an explanatory note rather than failing the
run. Both collectors emit the common *raw change* shape the normalizer consumes.

Sources (MVP):
  * ResourceGraphChangeCollector — ARG ``resourcechanges`` (before/after property diffs).
  * AzureActivityLogCollector    — ``az monitor activity-log list`` (operation, caller, status).

Future collectors (Entra audit/sign-in, PIM, Policy events, diagnostic logs, DevOps/ServiceNow)
implement the same ``collect`` contract and append their rows.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger("app.changeexplorer.collectors")


# --------------------------------------------------------------------------- concurrency + 429 policy
# Change history is gathered with bounded concurrency (>= 5 parallel Azure calls) so a tenant-wide
# scope spanning many subscriptions finishes quickly, paired with smart exponential backoff +
# full jitter and automatic retry whenever Azure answers 429 / throttling.
_COLLECT_CONCURRENCY = 8          # max parallel Azure calls (>= 5 as required)
_MAX_RETRIES = 5                  # retry attempts after the first try, on throttling only
_BACKOFF_BASE_SECONDS = 1.0       # first backoff window (grows 2**attempt)
_BACKOFF_CAP_SECONDS = 30.0       # never wait longer than this between attempts

# ``resourcechanges`` rows carry full before/after property values, so 1000 rows of diffs can
# far exceed the default 256 KB capture cap — which truncates the JSON and (pre-fix) silently
# yielded ZERO changes. Capture the change query with a much larger cap so the data comes
# through, then trim individual values to keep the persisted run + browser payload bounded.
_CHANGE_CAPTURE_BYTES = 6_000_000   # ~6 MB capture cap for the change query

# The Activity Log CLI (`az monitor activity-log list -o json`) can likewise exceed the default
# 256 KB cap for a busy subscription / wide window — truncation breaks the JSON and (pre-fix)
# dropped EVERY event with a misleading "query failed" note. Capture with the same large cap; on
# the rare further overflow we salvage the complete objects received before the cut.
_ACTIVITY_CAPTURE_BYTES = 6_000_000  # ~6 MB capture cap for the Activity Log CLI query

# Max change rows the ARG ``resourcechanges`` query returns per scan (one ARG page). When a scan
# hits this, the run is flagged so the UI can tell the user the result was capped at the limit.
# This is the DEFAULT/fallback; the live value is admin-tunable via ``change_limit()``.
RG_CHANGE_LIMIT = 5000
_MAX_CHANGES_PER_RESOURCE = 40      # cap changed-property entries kept per resource row
_MAX_VALUE_CHARS = 1500             # cap each before/after value's length


def change_limit() -> int:
    """The admin-tunable per-scan change limit (default ``RG_CHANGE_LIMIT``, clamped 100..50000).

    Applied to BOTH sources: the ARG ``resourcechanges`` ``take`` and the Activity Log
    ``--max-events`` / REST ``max_events``. Falls back to the default if settings can't be read."""
    try:
        from app.core.app_settings import load_settings

        v = int(load_settings().get("changeexplorer_change_limit", RG_CHANGE_LIMIT) or RG_CHANGE_LIMIT)
        return max(100, min(50000, v))
    except Exception:  # noqa: BLE001
        return RG_CHANGE_LIMIT

# Substrings that mark an Azure throttling / rate-limit response (CLI + ARM + ARG variants).
_THROTTLE_SIGNALS = (
    "429", "toomanyrequests", "too many requests", "rate limit", "ratelimit",
    "throttl", "request limit exceeded", "requestlimitexceeded",
)
_RETRY_AFTER_RE = re.compile(r"retry[\s\-]?after[\"']?\s*[:=]\s*[\"']?(\d+)", re.IGNORECASE)


def _is_throttled(cap: Any) -> bool:
    """True when a failed CaptureResult looks like an Azure 429 / throttling response."""
    if getattr(cap, "ok", False):
        return False
    blob = f"{getattr(cap, 'error', '') or ''}\n{getattr(cap, 'stderr', '') or ''}".lower()
    return any(sig in blob for sig in _THROTTLE_SIGNALS)


def _retry_after_seconds(cap: Any) -> float | None:
    """Honor a server ``Retry-After: N`` hint when Azure provides one (capped)."""
    blob = f"{getattr(cap, 'error', '') or ''}\n{getattr(cap, 'stderr', '') or ''}"
    m = _RETRY_AFTER_RE.search(blob)
    if not m:
        return None
    try:
        return min(float(m.group(1)), _BACKOFF_CAP_SECONDS)
    except ValueError:
        return None


def _backoff_delay(attempt: int, cap: Any) -> float:
    """Exponential backoff with full jitter; defers to a server Retry-After hint when present.

    ``attempt`` is 0-based (0 = first retry). Full jitter (``uniform(0, expo)``) spreads
    concurrent retries so N throttled workers don't all wake at the same instant."""
    server = _retry_after_seconds(cap)
    if server is not None:
        return server + random.uniform(0, 0.5)
    expo = min(_BACKOFF_BASE_SECONDS * (2 ** attempt), _BACKOFF_CAP_SECONDS)
    return random.uniform(0, expo)


async def _capture_with_retry(run: Callable[[], Awaitable[Any]], *, label: str) -> Any:
    """Await a callable returning a CaptureResult, retrying ONLY on 429 / throttling with smart
    exponential backoff + jitter. Non-throttle failures return immediately (best-effort). Returns
    the final CaptureResult."""
    cap = await run()
    attempt = 0
    while _is_throttled(cap) and attempt < _MAX_RETRIES:
        delay = _backoff_delay(attempt, cap)
        log.warning("changeexplorer: %s throttled (429) — retry %d/%d in %.1fs",
                    label, attempt + 1, _MAX_RETRIES, delay)
        await asyncio.sleep(delay)
        cap = await run()
        attempt += 1
    return cap


def _parse_rows(stdout: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(stdout or "[]")
    except (json.JSONDecodeError, TypeError):
        # The capture layer may have truncated a large result into invalid JSON. Rather than
        # silently dropping EVERY row (which turns a big result into a misleading "0 changes"),
        # salvage the complete top-level objects that arrived before the truncation point.
        salvaged = _salvage_json_array(stdout)
        return salvaged
    if isinstance(data, dict):
        data = data.get("data") or data.get("value") or []
    return data if isinstance(data, list) else []


def _salvage_json_array(text: str) -> list[dict[str, Any]]:
    """Recover the complete objects from a truncated JSON array string (``[{...},{...},{...``).

    Uses ``raw_decode`` to pull one object at a time and stops at the first incomplete tail, so
    a result truncated by the output cap still yields every fully-received row instead of zero."""
    if not text:
        return []
    s = text.lstrip()
    if not s.startswith("["):
        return []
    decoder = json.JSONDecoder()
    out: list[dict[str, Any]] = []
    i = 1  # skip the opening '['
    n = len(s)
    while i < n:
        while i < n and s[i] in " \t\r\n,":
            i += 1
        if i >= n or s[i] == "]":
            break
        try:
            obj, end = decoder.raw_decode(s, i)
        except json.JSONDecodeError:
            break  # the truncated final element — stop here
        if isinstance(obj, dict):
            out.append(obj)
        i = end
    return out


def _trim_value(v: Any) -> Any:
    """Bound a single before/after change value so a verbose diff (e.g. a whole template or rule
    set) can't bloat the persisted run + browser payload. Non-strings pass through unchanged."""
    if isinstance(v, str) and len(v) > _MAX_VALUE_CHARS:
        return v[:_MAX_VALUE_CHARS] + "…(truncated)"
    return v


# --------------------------------------------------------------------------- ARG resourcechanges


async def collect_resource_graph_changes(predicate: str, start_iso: str, end_iso: str,
                                         connection: dict[str, Any] | None) -> tuple[list[dict[str, Any]], str]:
    """Query the ARG ``resourcechanges`` table for changes within the window, scoped by joining
    to resources matching ``predicate``. Returns (raw_rows, note)."""
    from app.exec.command_runner import run_kql_capture

    if not predicate:
        return [], "No scope predicate for Resource Graph changes."
    limit = change_limit()
    # Join resourcechanges to resources so we can scope by the workload predicate and enrich
    # each change with its resource type / group / subscription.
    kql = (
        "resourcechanges "
        "| extend ts=todatetime(properties.changeAttributes.timestamp), "
        "ct=tostring(properties.changeType), targetId=tolower(tostring(properties.targetResourceId)) "
        f"| where ts >= datetime('{start_iso}') and ts <= datetime('{end_iso}') "
        "| join kind=inner (Resources "
        f"| where {predicate} "
        "| project rid=tolower(id), name, type, resourceGroup, subscriptionId, location) on $left.targetId == $right.rid "
        "| project ts, ct, targetId, name, type, resourceGroup, subscriptionId, location, "
        "changes=properties.changes, correlationId=tostring(properties.changeAttributes.correlationId) "
        f"| order by ts desc | take {limit}"
    )
    cap = await _capture_with_retry(
        lambda: run_kql_capture(kql, connection, output="json", max_bytes=_CHANGE_CAPTURE_BYTES),
        label="resourcechanges")
    if not cap.ok:
        err = (cap.error or cap.stderr or "").strip()
        el = err.lower()
        if "forbidden" in el or "authoriz" in el or "403" in el:
            return [], "Resource Graph: access denied reading change history (the connection lacks read permission)."
        return [], f"Resource Graph change history unavailable: {err[:140]}"
    rows = _parse_rows(cap.stdout)
    # Detect a result that overran even the enlarged cap (salvage recovered partial rows). Surface
    # it as a note so an apparent shortfall isn't mistaken for "no more changes".
    truncated = len(cap.stdout or "") >= _CHANGE_CAPTURE_BYTES
    out: list[dict[str, Any]] = []
    for r in rows:
        changes = []
        raw_changes = r.get("changes") or {}
        if isinstance(raw_changes, dict):
            for path, ch in list(raw_changes.items())[:_MAX_CHANGES_PER_RESOURCE]:
                if isinstance(ch, dict):
                    changes.append({"propertyPath": path, "before": _trim_value(ch.get("previousValue")),
                                    "after": _trim_value(ch.get("newValue")), "changeType": ch.get("changeType", "Update")})
        out.append({
            "source": "ResourceGraph", "resourceId": r.get("targetId", ""), "resourceName": r.get("name", ""),
            "resourceType": r.get("type", ""), "resourceGroup": r.get("resourceGroup", ""),
            "subscriptionId": r.get("subscriptionId", ""), "location": r.get("location", ""),
            "eventTime": r.get("ts", ""), "operation": r.get("ct", "Update"), "changeType": r.get("ct", "Update"),
            "actor": "", "actorType": "Unknown", "correlationId": r.get("correlationId", ""),
            "changes": changes, "raw": {k: v for k, v in r.items() if k != "changes"},
        })
    note = ""
    if truncated or len(out) >= limit:
        note = (f"Change history was capped at the {limit:,} most recent changes for this "
                "window. There may be more — narrow the time range or scope to see all changes.")
    return out, note


# --------------------------------------------------------------------------- Activity Log


def _activity_note(sub: str, raw_err: str) -> str:
    """A short, actionable note for a failed Activity Log subscription query. Recognizes the
    common 'subscription not recognized' / access errors and explains the likely cause (the
    selected Azure connection can't reach that subscription) instead of echoing a raw CLI dump."""
    e = (raw_err or "").lower()
    short = sub[:8] + "…"
    if "not recognized" in e or "was not found" in e or "couldn't find" in e or "couldn't be found" in e:
        return (f"Activity Log: subscription {short} isn't reachable by the selected Azure connection "
                "(wrong tenant or no access). Pick the connection that owns this subscription.")
    if "forbidden" in e or "authoriz" in e or "does not have authorization" in e or "403" in e:
        return f"Activity Log: access denied for subscription {short} (the connection lacks read permission)."
    if "az login" in e or "please run" in e or "credential" in e:
        return f"Activity Log: the selected connection isn't signed in for subscription {short}."
    return f"Activity Log: subscription {short} query failed: {(raw_err or '').strip()[:80]}"


async def collect_activity_log(subscriptions: list[str], start_iso: str, end_iso: str,
                               connection: dict[str, Any] | None,
                               resource_ids: list[str] | None = None) -> tuple[list[dict[str, Any]], str]:
    """Query the Azure Activity Log per subscription within the window. Returns (raw_rows, note).
    ``resource_ids`` (lowercased) optionally restricts to the workload's resources.

    Subscriptions are queried CONCURRENTLY (bounded to ``_COLLECT_CONCURRENCY`` parallel calls,
    >= 5) with per-call 429 backoff/retry, so a tenant-wide scope spanning many subscriptions
    completes in a fraction of the sequential time.

    Two execution paths: a service-principal connection signs ``az`` in and runs
    ``az monitor activity-log list`` (CLI); a non-SP connection (pasted ARM token / managed
    identity) has no ambient ``az`` login for its tenant, so it reads the Activity Log over ARM
    REST with the connection's own token instead — mirroring how Resource Graph already falls
    back to REST. Without this, pasted-token connections fail with "subscription not recognized"
    even when the subscription IS in the connection's tenant."""
    if not subscriptions:
        return [], "No subscriptions for Activity Log."
    wanted = {r.lower() for r in (resource_ids or [])}
    subs = subscriptions[:25]
    limit = change_limit()

    # Non-service-principal connections can't use the `az monitor activity-log list` CLI (no
    # ambient login for their tenant). Use the connection's ARM token over REST when available.
    from app.exec.command_runner import _is_service_principal

    if not _is_service_principal(connection):
        from app.azure.credentials import get_arm_token

        token, terr = await get_arm_token(connection)
        if token:
            return await _collect_activity_log_rest(subs, start_iso, end_iso, token, wanted, limit)
        # No token: a pasted-token / managed-identity connection has no ambient `az` fallback, so
        # surface the auth error rather than silently returning zero. Pure local dev (ambient
        # `az login`, no managed identity) falls through to the CLI path below.
        method = (connection or {}).get("auth_method", "")
        if method == "az_cli_token" or os.environ.get("IDENTITY_ENDPOINT") or os.environ.get("MSI_ENDPOINT"):
            return [], f"Activity Log: {terr or 'could not acquire an Azure token for this connection.'}"

    sem = asyncio.Semaphore(_COLLECT_CONCURRENCY)

    async def _one(sub: str) -> tuple[list[dict[str, Any]], str]:
        from app.exec.command_runner import run_command_capture

        cmd = (
            f"az monitor activity-log list --subscription {sub} "
            f"--start-time {start_iso} --end-time {end_iso} --max-events {limit} "
            # Project ONLY the fields the normalizer/identity layer consumes. The full Activity Log
            # event carries a huge ``properties`` blob (embedded ARM templates / response bodies)
            # that can blow past the capture cap — projecting it out keeps each event tiny so a
            # busy subscription / wide window fits without truncation.
            "--query \"[?operationName.value && (status.value=='Succeeded' || status.value=='Accepted')]"
            ".{operationName:operationName, status:status, caller:caller, claims:claims, "
            "correlationId:correlationId, resourceId:resourceId, resourceType:resourceType, "
            "resourceGroupName:resourceGroupName, eventTimestamp:eventTimestamp, "
            "subscriptionId:subscriptionId}\" -o json"
        )
        async with sem:
            cap = await _capture_with_retry(
                lambda: run_command_capture(cmd, connection, read_only=True, max_bytes=_ACTIVITY_CAPTURE_BYTES),
                label=f"activity-log {sub[:8]}")
        note = ""
        if not cap.ok:
            # A truncation/timeout on a huge result still leaves complete objects in stdout — salvage
            # them (best-effort) rather than dropping the whole subscription's changes. Only a genuine
            # failure (auth/tenant/denied with no usable stdout) surfaces as a hard error note.
            salvaged = _parse_rows(cap.stdout) if cap.stdout else []
            if not salvaged:
                return [], _activity_note(sub, cap.error or cap.stderr)
            note = (f"Activity Log for subscription {sub[:8]}… returned more than "
                    f"{_ACTIVITY_CAPTURE_BYTES // 1_000_000} MB and was truncated; showing the "
                    f"{len(salvaged)} event(s) received. Narrow the time range for full fidelity.")
            rows_iter = salvaged
        else:
            rows_iter = _parse_rows(cap.stdout)
        rows_out: list[dict[str, Any]] = []
        for r in rows_iter:
            rid = (r.get("resourceId") or "").lower()
            if wanted and rid not in wanted and not any(rid.startswith(w) for w in wanted):
                continue
            op = ((r.get("operationName") or {}) or {}).get("value", "") if isinstance(r.get("operationName"), dict) else r.get("operationName", "")
            rows_out.append(_activity_row(r, sub, op))
        return rows_out, note

    results = await asyncio.gather(*(_one(s) for s in subs))
    out: list[dict[str, Any]] = []
    notes: list[str] = []
    for rows_out, note in results:
        out.extend(rows_out)
        if note:
            notes.append(note)
    return out, ("; ".join(notes) if notes else "")


def _activity_row(r: dict[str, Any], sub: str, op: str) -> dict[str, Any]:
    """Project one raw Activity Log event (CLI or REST — identical shape) into the common raw
    change row the normalizer consumes."""
    from app.changeexplorer.identity import extract_actor_meta

    caller = r.get("caller", "")
    claims = r.get("claims") or {}
    corr = r.get("correlationId", "")
    meta = extract_actor_meta(caller, claims, corr)
    return {
        "source": "ActivityLog", "resourceId": r.get("resourceId", ""),
        "resourceName": (r.get("resourceId", "") or "").rsplit("/", 1)[-1],
        "resourceType": (r.get("resourceType") or {}).get("value", "") if isinstance(r.get("resourceType"), dict) else r.get("resourceType", ""),
        "resourceGroup": r.get("resourceGroupName", ""), "subscriptionId": r.get("subscriptionId", sub),
        "location": "", "eventTime": r.get("eventTimestamp", ""), "operation": op, "changeType": "Update",
        "actor": caller, "actorType": meta["kind"],
        "actorObjectId": meta["object_id"], "actorKind": meta["kind"],
        "actorIp": meta["ip"], "actorOnBehalfOf": meta["on_behalf_of"], "actorAppId": meta["app_id"],
        "isPlatformActor": meta["is_platform"],
        "correlationId": corr, "changes": [], "raw": r,
    }


async def _collect_activity_log_rest(subs: list[str], start_iso: str, end_iso: str,
                                     token: str, wanted: set[str], max_events: int = RG_CHANGE_LIMIT) -> tuple[list[dict[str, Any]], str]:
    """Activity Log via ARM REST (for pasted-token / managed-identity connections). Queries each
    subscription concurrently with the connection's token, applies the same status/operation +
    resource-id filtering the CLI ``--query`` does, and returns the same row shape."""
    from app.azure.arm import list_activity_log_events

    sem = asyncio.Semaphore(_COLLECT_CONCURRENCY)

    async def _one(sub: str) -> tuple[list[dict[str, Any]], str]:
        async with sem:
            events, err = await list_activity_log_events(token, sub, start_iso, end_iso, max_events=max_events)
        if err:
            return [], _activity_note(sub, err)
        rows_out: list[dict[str, Any]] = []
        for r in events:
            status = (r.get("status") or {}).get("value", "") if isinstance(r.get("status"), dict) else r.get("status", "")
            op = (r.get("operationName") or {}).get("value", "") if isinstance(r.get("operationName"), dict) else r.get("operationName", "")
            if not op or status not in ("Succeeded", "Accepted"):
                continue
            rid = (r.get("resourceId") or "").lower()
            if wanted and rid not in wanted and not any(rid.startswith(w) for w in wanted):
                continue
            rows_out.append(_activity_row(r, sub, op))
        return rows_out, ""

    results = await asyncio.gather(*(_one(s) for s in subs))
    out: list[dict[str, Any]] = []
    notes: list[str] = []
    for rows_out, note in results:
        out.extend(rows_out)
        if note:
            notes.append(note)
    return out, ("; ".join(notes) if notes else "")
