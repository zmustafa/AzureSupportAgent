"""Built-in utility tools for the agent (network diagnostics + web fetch).

These are first-party, in-process tools — NOT external connectors — exposed to the LLM
through the same ``ConnectorTool`` shape so the orchestrator's existing tool-call loop
dispatches them uniformly. All are READ-ONLY (no approval pause).

Security is the focus: every tool that reaches out to the network goes through
``_resolve_safe_target`` which blocks SSRF to loopback, private/link-local ranges, and
— critically for a service running in Azure — the cloud metadata endpoint
(169.254.169.254). Shell tools (ping/traceroute) never use a shell: arguments are passed
as an argv list and the target is strictly validated, so command injection is impossible.

An admin kill-switch (``builtin_tools_enabled``) and optional egress allow/deny lists
live in app_settings; this module reads them at call time.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import platform
import re
import socket
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from app.connectors.base import ConnectorTool, err, ok

# --- limits (defensive caps; some overridable via settings) ---------------------------
_DEFAULT_TIMEOUT_S = 10
_MAX_TIMEOUT_S = 30
_MAX_BODY_CHARS = 20000
_MAX_PING_COUNT = 10
_MAX_TRACE_HOPS = 30

# A hostname (RFC-1123 labels) or a bare IP literal. Anything else is rejected so a
# target can never start with "-" (option injection) or contain shell metacharacters.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$"
)


def _settings() -> dict[str, Any]:
    try:
        from app.core.app_settings import load_settings

        return load_settings()
    except Exception:  # noqa: BLE001 - never let settings break a tool
        return {}


def _timeout(args: dict[str, Any]) -> float:
    raw = args.get("timeout_seconds")
    try:
        t = float(raw) if raw is not None else float(_settings().get("network_tool_timeout_seconds", _DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        t = _DEFAULT_TIMEOUT_S
    return max(1.0, min(_MAX_TIMEOUT_S, t))


def _ip_is_blocked(ip: str) -> bool:
    """True if an IP must never be contacted (SSRF / metadata / private ranges)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # unparseable → block
    # Cloud instance-metadata endpoints (Azure/AWS/GCP all use this link-local IP).
    if str(addr) in ("169.254.169.254", "fd00:ec2::254"):
        return True
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _egress_check(host: str) -> str | None:
    """Apply admin allow/deny lists. Returns an error string if blocked, else None."""
    s = _settings()
    h = host.lower().strip()
    deny = [str(d).lower().strip() for d in (s.get("network_egress_denylist") or []) if str(d).strip()]
    allow = [str(a).lower().strip() for a in (s.get("network_egress_allowlist") or []) if str(a).strip()]
    for d in deny:
        if h == d or h.endswith("." + d):
            return f"Host '{host}' is blocked by the egress denylist."
    if allow:
        if not any(h == a or h.endswith("." + a) for a in allow):
            return f"Host '{host}' is not on the egress allowlist."
    return None


def _resolve_safe_target(host: str) -> tuple[list[str], str | None]:
    """Validate + DNS-resolve a host, blocking SSRF targets. Returns (ips, error).

    Note: there's a residual DNS-rebind window (we validate the resolved IPs, but the
    eventual connection re-resolves). Acceptable for an admin-gated internal tool with a
    kill-switch; the metadata IP and private ranges are still blocked at resolution time.
    """
    host = (host or "").strip()
    if not host:
        return [], "No host provided."
    # Accept a bare IP literal directly.
    try:
        ipaddress.ip_address(host)
        ips = [host]
    except ValueError:
        if not _HOSTNAME_RE.match(host):
            return [], f"'{host}' is not a valid hostname or IP address."
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            return [], f"DNS resolution failed for '{host}': {exc}"
        ips = sorted({i[4][0] for i in infos})
        if not ips:
            return [], f"Could not resolve '{host}'."
    blocked = [ip for ip in ips if _ip_is_blocked(ip)]
    if blocked:
        return [], f"Refusing to contact '{host}' — resolves to a private/blocked address ({blocked[0]})."
    egress = _egress_check(host)
    if egress:
        return [], egress
    return ips, None


def _host_of_url(url: str) -> tuple[str, str | None]:
    try:
        p = urlparse(url)
    except ValueError:
        return "", "Malformed URL."
    if p.scheme not in ("http", "https"):
        return "", "Only http and https URLs are allowed."
    if not p.hostname:
        return "", "URL has no host."
    return p.hostname, None


# --- tool handlers --------------------------------------------------------------------
async def _web_fetch(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    url = str(args.get("url", "")).strip()
    host, herr = _host_of_url(url)
    if herr:
        return err(herr)
    _, serr = _resolve_safe_target(host)
    if serr:
        return err(serr)
    strip_html = bool(args.get("strip_html", True))
    timeout = _timeout(args)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.get(url, headers={"User-Agent": "AzureSupportAgent/1.0"})
            hops = 0
            while resp.is_redirect and hops < 5:
                loc = resp.headers.get("location", "")
                nxt = str(httpx.URL(resp.url).join(loc))
                nh, nerr = _host_of_url(nxt)
                if nerr:
                    return err(f"Blocked redirect: {nerr}")
                _, nserr = _resolve_safe_target(nh)
                if nserr:
                    return err(f"Blocked redirect to '{nh}': {nserr}")
                resp = await client.get(nxt, headers={"User-Agent": "AzureSupportAgent/1.0"})
                hops += 1
    except httpx.HTTPError as exc:
        return err(f"Fetch failed: {exc}")
    text = resp.text
    if strip_html and "html" in resp.headers.get("content-type", "").lower():
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
    truncated = len(text) > _MAX_BODY_CHARS
    body = text[:_MAX_BODY_CHARS] + ("\n…[truncated]" if truncated else "")
    return ok(
        f"GET {url}\nStatus: {resp.status_code} {resp.reason_phrase}\n"
        f"Content-Type: {resp.headers.get('content-type', '')}\n"
        f"Length: {len(text)} chars{' (truncated)' if truncated else ''}\n\n{body}"
    )


async def _http_request(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    url = str(args.get("url", "")).strip()
    method = str(args.get("method", "GET")).upper()
    if method not in ("GET", "HEAD", "OPTIONS"):
        return err("Only read methods (GET, HEAD, OPTIONS) are allowed.")
    host, herr = _host_of_url(url)
    if herr:
        return err(herr)
    _, serr = _resolve_safe_target(host)
    if serr:
        return err(serr)
    timeout = _timeout(args)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.request(method, url, headers={"User-Agent": "AzureSupportAgent/1.0"})
    except httpx.HTTPError as exc:
        return err(f"Request failed: {exc}")
    headers = "\n".join(f"{k}: {v}" for k, v in resp.headers.items())
    body = "" if method == "HEAD" else resp.text[:4000]
    return ok(
        f"{method} {url}\nStatus: {resp.status_code} {resp.reason_phrase}\n\n"
        f"Headers:\n{headers}\n\nBody (first 4000 chars):\n{body}"
    )


async def _dns_lookup(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    host = str(args.get("host", "")).strip()
    if not host:
        return err("No host provided.")
    if not _HOSTNAME_RE.match(host):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return err(f"'{host}' is not a valid hostname or IP.")
    # Reverse lookup for an IP, forward lookup for a name.
    try:
        ipaddress.ip_address(host)
        try:
            name, aliases, _ = socket.gethostbyaddr(host)
            return ok(f"Reverse DNS for {host}:\n  {name}" + (f"\n  aliases: {', '.join(aliases)}" if aliases else ""))
        except socket.herror as exc:
            return err(f"No reverse DNS for {host}: {exc}")
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        return err(f"DNS resolution failed for '{host}': {exc}")
    v4 = sorted({i[4][0] for i in infos if i[0] == socket.AF_INET})
    v6 = sorted({i[4][0] for i in infos if i[0] == socket.AF_INET6})
    lines = [f"DNS for {host}:"]
    if v4:
        lines.append("  A:    " + ", ".join(v4))
    if v6:
        lines.append("  AAAA: " + ", ".join(v6))
    return ok("\n".join(lines))


async def _port_check(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    host = str(args.get("host", "")).strip()
    try:
        port = int(args.get("port"))
    except (TypeError, ValueError):
        return err("A numeric 'port' is required.")
    if not (1 <= port <= 65535):
        return err("Port must be between 1 and 65535.")
    ips, serr = _resolve_safe_target(host)
    if serr:
        return err(serr)
    target = ips[0]
    timeout = _timeout(args)
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        fut = asyncio.open_connection(target, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        ms = int((loop.time() - started) * 1000)
        return ok(f"TCP {host}:{port} ({target}) is OPEN — connected in {ms} ms.")
    except asyncio.TimeoutError:
        return ok(f"TCP {host}:{port} ({target}) is CLOSED/filtered — connection timed out after {int(timeout)}s.")
    except OSError as exc:
        return ok(f"TCP {host}:{port} ({target}) is CLOSED — {exc.strerror or exc}.")


async def _run_argv(argv: list[str], timeout: float) -> tuple[int | None, str]:
    """Run a command (NO shell) and return (exit_code, combined_output), capped + timed."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return None, "(timed out)"
    text = (out or b"").decode("utf-8", errors="replace")
    return proc.returncode, text[:_MAX_BODY_CHARS]


async def _ping(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    host = str(args.get("host", "")).strip()
    _, serr = _resolve_safe_target(host)
    if serr:
        return err(serr)
    try:
        count = int(args.get("count", 4))
    except (TypeError, ValueError):
        count = 4
    count = max(1, min(_MAX_PING_COUNT, count))
    is_win = platform.system().lower().startswith("win")
    timeout = _timeout(args)
    if is_win:
        argv = ["ping", "-n", str(count), "-w", str(int(timeout * 1000)), host]
    else:
        argv = ["ping", "-c", str(count), "-W", str(int(max(1, timeout))), host]
    code, out = await _run_argv(argv, timeout=timeout * count + 5)
    return ok(f"$ {' '.join(argv)}\n(exit {code})\n\n{out.strip()}")


async def _traceroute(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    host = str(args.get("host", "")).strip()
    _, serr = _resolve_safe_target(host)
    if serr:
        return err(serr)
    try:
        max_hops = int(args.get("max_hops", 30))
    except (TypeError, ValueError):
        max_hops = 30
    max_hops = max(1, min(_MAX_TRACE_HOPS, max_hops))
    is_win = platform.system().lower().startswith("win")
    # Traceroute is slow; give it a generous (but bounded) wall-clock budget.
    budget = min(90.0, max_hops * 3.0)
    if is_win:
        argv = ["tracert", "-h", str(max_hops), "-w", "2000", host]
    else:
        argv = ["traceroute", "-m", str(max_hops), "-w", "2", host]
    code, out = await _run_argv(argv, timeout=budget)
    if code is None and not out.strip():
        return err("traceroute is not available on this host or timed out.")
    return ok(f"$ {' '.join(argv)}\n(exit {code})\n\n{out.strip()}")


# --- Azure Monitor metrics → interactive chart ----------------------------------------
# Common friendly lookbacks → timedelta. The Azure datasource wants a START datetime
# (passed to `az ... --start-time`), so we convert a duration into `now - duration`.
def _parse_lookback(value: str) -> timedelta:
    """Parse a lookback like 'PT1H', 'P7D', 'P30D', '24h', '90m', '7d' → timedelta.

    Falls back to 1 day for anything unrecognized. Capped at 93 days (the Azure Monitor
    metrics retention ceiling) so a runaway request can't ask for years of data.
    """
    s = (value or "").strip().upper()
    days = hours = minutes = 0.0
    m = re.fullmatch(r"P(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?)?", s)
    if m and any(m.groups()):
        days = float(m.group(1) or 0)
        hours = float(m.group(2) or 0)
        minutes = float(m.group(3) or 0)
    else:
        m2 = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([DHM])", s)
        if m2:
            n = float(m2.group(1))
            unit = m2.group(2)
            days, hours, minutes = (n, 0, 0) if unit == "D" else (0, n, 0) if unit == "H" else (0, 0, n)
    delta = timedelta(days=days, hours=hours, minutes=minutes)
    if delta <= timedelta(0):
        delta = timedelta(days=1)
    return min(delta, timedelta(days=93))


def _auto_interval(delta: timedelta) -> str:
    """Pick a metrics grain that yields a readable number of points (~30-350).

    Only uses grains that Azure Monitor supports for virtually ALL metrics — PT5M, PT1H,
    P1D. (Finer/odd grains like PT1M, PT15M or PT6H are rejected for some metric sets,
    e.g. App Service ``Http5xx``/``HttpResponseTime`` only allow 5-min/1-hour/1-day.)
    A request can still pass an explicit ``interval`` to override this.
    """
    secs = delta.total_seconds()
    if secs <= 12 * 3600:  # <= 12h
        return "PT5M"
    if secs <= 14 * 86400:  # <= 14d
        return "PT1H"
    return "P1D"


def _iso_from_azure_grain(s: str) -> str:
    """Convert an Azure time-grain string to ISO8601.

    '00:05:00' -> 'PT5M', '01:00:00' -> 'PT1H', '1.00:00:00' -> 'P1D', '00:05:30' -> 'PT330S'.
    Returns '' for an unparseable or zero grain. The seconds component is honored (an
    earlier version dropped it, undersampling sub-minute grains).
    """
    s = (s or "").strip()
    days = 0
    head = s.split(":", 1)[0]
    if "." in head:
        dstr, s = s.split(".", 1)
        try:
            days = int(dstr)
        except ValueError:
            days = 0
    parts = s.split(":")
    if len(parts) not in (2, 3):
        return ""
    try:
        h = int(parts[0])
        m = int(parts[1])
        sec = int(parts[2]) if len(parts) == 3 else 0
    except (ValueError, IndexError):
        return ""
    total_sec = days * 86400 + h * 3600 + m * 60 + sec
    if total_sec <= 0:
        return ""
    if total_sec % 86400 == 0:
        return f"P{total_sec // 86400}D"
    if total_sec % 3600 == 0:
        return f"PT{total_sec // 3600}H"
    if total_sec % 60 == 0:
        return f"PT{total_sec // 60}M"
    return f"PT{total_sec}S"


def _supported_grains_from_error(msg: str) -> list[str]:
    """Parse the allowed grains out of an Azure 'time grains: 00:05:00,01:00:00,…' error."""
    m = re.search(r"time grains?[:\s]+([0-9.,:\s]+)", msg or "", re.IGNORECASE)
    if not m:
        return []
    out: list[str] = []
    for tok in m.group(1).split(","):
        iso = _iso_from_azure_grain(tok.strip())
        if iso and iso not in out:
            out.append(iso)
    return out


def _pick_grain(delta: timedelta, allowed: list[str]) -> str:
    """From Azure's allowed grains, choose the finest that keeps the chart readable (<=500 pts)."""
    ordered = sorted(allowed, key=lambda g: _parse_lookback(g).total_seconds())
    for g in ordered:
        gsecs = _parse_lookback(g).total_seconds()
        if gsecs and delta.total_seconds() / gsecs <= 500:
            return g
    return ordered[-1] if ordered else "PT1H"


def _arm_type_of(resource_id: str) -> str:
    """Extract the lowercased ARM type (``provider/type``) from a resource id."""
    parts = [p for p in (resource_id or "").split("/") if p]
    low = [p.lower() for p in parts]
    if "providers" in low:
        i = low.index("providers")
        if i + 2 < len(parts):
            return f"{parts[i + 1]}/{parts[i + 2]}".lower()
    return ""


def _amba_metrics_for(arm_type: str) -> list[str]:
    """Recommended metric names for a resource type, from the AMBA reference set."""
    if not arm_type:
        return []
    try:
        from app.amba.reference import reference_for_type

        spec = reference_for_type(arm_type) or {}
    except Exception:  # noqa: BLE001 - never let the catalog break the tool
        return []
    out: list[str] = []
    for a in spec.get("alerts", []) or []:
        if (a.get("signal") or "metric") == "metric":
            name = str(a.get("metric") or "").strip()
            if name and name not in out:
                out.append(name)
    return out


# Azure Monitor unit strings -> short display labels for the chart axis/tooltip.
_UNIT_LABELS = {
    "percent": "%",
    "bytes": "bytes",
    "bytespersecond": "B/s",
    "milliseconds": "ms",
    "seconds": "s",
    "count": "",
    "countpersecond": "/s",
    "cores": "cores",
    "millicores": "mcores",
    "nanocores": "ncores",
    "bitspersecond": "bps",
}


def _unit_label(azure_unit: str | None) -> str:
    return _UNIT_LABELS.get(str(azure_unit or "").strip().lower(), "")


# Keywords that make a metric an interesting default to chart (health/perf signals).
_INTERESTING_RE = re.compile(
    r"(cpu|memory|percent|latency|response|availab|requests?|errors?|failed|"
    r"throttl|messages?|transactions?|duration|connections?|tokens?|pull|push|hit)",
    re.IGNORECASE,
)


def _default_metrics_from_defs(defs: list[dict[str, Any]], limit: int = 4) -> list[str]:
    """Pick sensible default metrics to chart from a resource's live metric catalog.

    Used for resource types not covered by the AMBA reference set (Cosmos, Service Bus, ACR,
    Azure OpenAI, …). Prefers recognizable health/performance signals, gauges before counts.
    """
    if not defs:
        return []
    gauges, counts, rest = [], [], []
    for d in defs:
        name = d.get("name") or ""
        prim = str(d.get("primary") or "Average").lower()
        if not _INTERESTING_RE.search(name):
            rest.append(name)
        elif prim in ("average", "maximum", "minimum"):
            gauges.append(name)
        else:
            counts.append(name)
    ordered = gauges + counts + rest
    seen: list[str] = []
    for n in ordered:
        if n and n not in seen:
            seen.append(n)
        if len(seen) >= limit:
            break
    return seen


def _summarize_series(result: dict[str, Any], unit: str) -> str:
    """A compact per-series summary (min/max/avg/last + peak time) for the model."""
    cols = result.get("columns") or []
    rows = result.get("rows") or []
    if len(cols) < 2 or not rows:
        return "No datapoints."
    u = f" {unit}" if unit else ""
    lines: list[str] = []
    for ci in range(1, len(cols)):
        name = cols[ci].get("name") if isinstance(cols[ci], dict) else str(cols[ci])
        vals = [
            (r[0], r[ci])
            for r in rows
            if ci < len(r) and isinstance(r[ci], (int, float)) and math.isfinite(r[ci])
        ]
        if not vals:
            continue
        nums = [v for _, v in vals]
        peak_ts, peak = max(vals, key=lambda tv: tv[1])
        lines.append(
            f"- {name}: avg {sum(nums) / len(nums):.2f}{u}, min {min(nums):.2f}{u}, "
            f"max {peak:.2f}{u} (peak {peak_ts}), last {nums[-1]:.2f}{u}"
        )
    return "\n".join(lines) if lines else "No numeric datapoints."


# Metric names that represent discrete COUNTS / totals — these read far better as BARS
# than as a continuous line (e.g. one Http5xx spike should be a visible bar, not a dot).
_COUNT_METRIC_RE = re.compile(
    r"(http[2-5]xx|\b[2-5]xx\b|requests?|restart|errors?|failures?|throttl|"
    r"connections?|messages?|\bhits?\b|deadlock|transactions?|count\b|executions?)",
    re.IGNORECASE,
)


def _auto_chart_type(metrics: list[str], aggregation: str, result: dict[str, Any]) -> str:
    """Pick a sensible chart type from the data shape when the caller didn't specify one.

    Gives genuine variety instead of always drawing a line:
    * Sparse series (<= 3 points) → ``bar`` (a line/area with 1-2 points is nearly invisible).
    * Count / total metrics (Http5xx, Requests, RestartCount, …) → ``bar``.
    * A single continuous series → ``area`` (filled, reads nicely on its own).
    * Multiple continuous series → ``line`` (classic overlay comparison).
    """
    rows = result.get("rows") or []
    cols = result.get("columns") or []
    series_count = max(0, len(cols) - 1)
    if len(rows) <= 3:
        return "bar"
    count_like = (aggregation or "").lower() in ("total", "count") or any(
        _COUNT_METRIC_RE.search(m or "") for m in (metrics or [])
    )
    if count_like:
        return "bar"
    return "area" if series_count <= 1 else "line"


async def _azure_metrics(_config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    resource_id = str(args.get("resource_id") or "").strip()
    resource_ids = [str(r).strip() for r in (args.get("resource_ids") or []) if str(r or "").strip()]
    if resource_id and resource_id not in resource_ids:
        resource_ids.insert(0, resource_id)
    if not resource_ids:
        return err("Provide resource_id — the full ARM resource id (/subscriptions/…/providers/…).")

    metrics = args.get("metrics") or ([args.get("metric")] if args.get("metric") else [])
    metrics = [str(m).strip() for m in metrics if str(m or "").strip()]
    arm_type = _arm_type_of(resource_ids[0])

    lookback = str(args.get("timespan") or args.get("lookback") or "P1D")
    delta = _parse_lookback(lookback)
    start = (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    interval = str(args.get("interval") or "").strip() or _auto_interval(delta)
    explicit_agg = str(args.get("aggregation") or "").strip()
    # Only honor an EXPLICIT chart_type; otherwise auto-pick from the data shape after the
    # fetch (so counts/sparse data become bars, single series an area, etc.).
    explicit_chart = str(args.get("chart_type") or "").strip().lower()
    chart_type = explicit_chart if explicit_chart in ("line", "area", "bar", "pie", "donut") else ""
    unit = str(args.get("unit") or "").strip()
    title = str(args.get("title") or "").strip()

    try:
        from app.core.azure_connections import resolve_connection
        from app.monitor.chart_store import save_chart
        from app.monitor.datasources.azure import resolve_azure_metrics
        from app.monitor.metric_defs import get_metric_definitions, index_by_name

        conn = resolve_connection(args.get("connection_id") or None)

        # The resource's live metric catalog (cached per type) — the same list the Azure
        # portal shows under Resource → Metrics. Lets us validate names, pick each metric's
        # correct aggregation, choose good defaults, and label the unit. Best-effort.
        try:
            defs = await get_metric_definitions(resource_ids[0], conn)
        except Exception:  # noqa: BLE001
            defs = []
        defidx = index_by_name(defs)

        # Default metrics when none were given: AMBA reference first, else the live catalog.
        if not metrics:
            metrics = _amba_metrics_for(arm_type)[:4] or _default_metrics_from_defs(defs)
            if not metrics:
                return err(
                    "No metrics given and none could be discovered for this resource. "
                    "Pass `metrics`, e.g. [\"Percentage CPU\"]."
                )

        # Validate/canonicalize requested names against the live catalog (case-insensitive).
        if defidx:
            canon: list[str] = []
            for m in metrics:
                d = defidx.get(m.lower())
                if d:
                    canon.append(d["name"])
            if not canon:
                avail = ", ".join(d["name"] for d in defs[:40])
                return err(
                    f"None of those metrics exist for {arm_type or 'this resource'}. "
                    f"Available metrics: {avail}."
                )
            metrics = canon  # silently drop unknowns, keep the valid ones

        # Per-metric aggregation: honor an explicit override for all metrics; otherwise use
        # each metric's PRIMARY aggregation (counts→Total/Count, gauges→Average/Maximum) and
        # request the union so every column comes back populated.
        agg_by_metric: dict[str, str] = {}
        if explicit_agg:
            aggregation = explicit_agg
            request_aggs = [explicit_agg]
        else:
            aggregation = "Average"
            req: list[str] = []
            for m in metrics:
                prim = (defidx.get(m.lower()) or {}).get("primary") or "Average"
                agg_by_metric[m.lower()] = prim
                if prim not in req:
                    req.append(prim)
            request_aggs = req or ["Average"]

        # Unit label from the catalog when the caller didn't supply one — only when ALL
        # selected metrics share the same unit (a mixed %/count/bytes chart gets no label).
        if not unit and defidx:
            units = {_unit_label((defidx.get(m.lower()) or {}).get("unit")) for m in metrics}
            unit = units.pop() if len(units) == 1 else ""

        async def _run(grain: str) -> dict[str, Any]:
            cfg = {
                "resource_ids": resource_ids,
                "metrics": metrics,
                "aggregation": aggregation,
                "aggregations": request_aggs,
                "aggregation_by_metric": agg_by_metric,
                "interval": grain,
                "timespan": start,
            }
            return (await resolve_azure_metrics(cfg, conn, {})).to_dict()

        result = await _run(interval)
        # Azure rejects some metric/grain combinations and tells us the allowed grains
        # (e.g. App Service Http5xx/HttpResponseTime only allow PT5M/PT1H/P1D). Self-heal
        # by retrying once with a supported grain instead of failing the whole request.
        if result.get("error"):
            allowed = _supported_grains_from_error(result["error"])
            retry_grain = _pick_grain(delta, allowed) if allowed else ""
            if retry_grain and retry_grain != interval:
                interval = retry_grain
                result = await _run(interval)
    except Exception as exc:  # noqa: BLE001 - surface a clean tool error, never crash the turn
        from app.core.utils import format_error

        return err(f"Metrics query failed: {format_error(exc)}")

    if result.get("error"):
        suggestions = _amba_metrics_for(arm_type)
        extra = f" Known metrics for {arm_type}: {', '.join(suggestions)}." if suggestions else ""
        return err(f"Metrics query failed: {result['error']}.{extra}")
    rows = result.get("rows") or []
    if not rows:
        return err(
            "No datapoints returned. Check the resource is emitting that metric and the "
            "timeframe isn't longer than its retention."
        )

    if not chart_type:
        chart_type = _auto_chart_type(metrics, aggregation, result)

    if not title:
        name = resource_ids[0].rsplit("/", 1)[-1]
        suffix = f" +{len(resource_ids) - 1} more" if len(resource_ids) > 1 else ""
        title = f"{', '.join(metrics)} — {name}{suffix}"
    spec = {
        "title": title,
        "type": chart_type,
        "unit": unit,
        "metrics": metrics,
        "resource_ids": resource_ids,
        "timespan": lookback,
        "interval": interval,
        "aggregation": aggregation if explicit_agg else "per-metric",
    }
    chart_id = save_chart(spec, result)
    summary = _summarize_series(result, unit)
    agg_label = explicit_agg if explicit_agg else "per-metric primary"
    block = json.dumps(
        {"chart_id": chart_id, "title": title, "type": chart_type, "unit": unit},
        ensure_ascii=False,
    )
    _metric_label = ", ".join(metrics[:3]) + (f" +{len(metrics) - 3} more" if len(metrics) > 3 else "")
    _display = f"📊 Built chart: {_metric_label} ({len(rows)} datapoint{'' if len(rows) == 1 else 's'})"
    return ok(
        f"DONE — the interactive chart is built and the metrics are already fetched "
        f"({len(rows)} datapoints for {', '.join(metrics)} on {resource_ids[0].rsplit('/', 1)[-1]}; "
        f"lookback {lookback}, {agg_label} aggregation, grain {interval}).\n\n"
        f"{summary}\n\n"
        "STOP — do NOT call azure_metrics again for the same data, and do NOT call any other "
        "metrics tool (e.g. monitor / monitor_metrics_query / metrics list); that data is "
        "already captured here. Your ONLY remaining step is to write your reply and include "
        "EXACTLY this fenced block verbatim (keep chart_id unchanged) so the user sees the graph:\n"
        f"```azchart\n{block}\n```",
        display_summary=_display,
    )


# --- tool registry --------------------------------------------------------------------
def _tools() -> list[ConnectorTool]:
    return [
        ConnectorTool(
            name="net_web_fetch",
            description="Download a web page or URL over http/https and return its text "
            "(HTML stripped to readable text by default). Use for docs, status pages, APIs.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The http(s) URL to fetch."},
                    "strip_html": {"type": "boolean", "description": "Strip HTML to plain text (default true)."},
                    "timeout_seconds": {"type": "number", "description": "Request timeout (max 30)."},
                },
                "required": ["url"],
            },
            kind="read",
            handler=_web_fetch,
        ),
        ConnectorTool(
            name="net_http_request",
            description="Make a read-only HTTP request (GET/HEAD/OPTIONS) and return the "
            "status, response headers, and a body snippet. Use to probe an endpoint.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "method": {"type": "string", "enum": ["GET", "HEAD", "OPTIONS"]},
                    "timeout_seconds": {"type": "number"},
                },
                "required": ["url"],
            },
            kind="read",
            handler=_http_request,
        ),
        ConnectorTool(
            name="net_dns_lookup",
            description="Resolve a hostname to its IP addresses (A/AAAA), or reverse-resolve "
            "an IP to a hostname.",
            parameters={
                "type": "object",
                "properties": {"host": {"type": "string", "description": "Hostname or IP."}},
                "required": ["host"],
            },
            kind="read",
            handler=_dns_lookup,
        ),
        ConnectorTool(
            name="net_port_check",
            description="Check whether a TCP port is open/reachable on a host (connect test).",
            parameters={
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "port": {"type": "integer", "description": "TCP port (1-65535)."},
                    "timeout_seconds": {"type": "number"},
                },
                "required": ["host", "port"],
            },
            kind="read",
            handler=_port_check,
        ),
        ConnectorTool(
            name="net_ping",
            description="Ping a host to test reachability and round-trip latency.",
            parameters={
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "count": {"type": "integer", "description": "Echo requests (1-10, default 4)."},
                    "timeout_seconds": {"type": "number"},
                },
                "required": ["host"],
            },
            kind="read",
            handler=_ping,
        ),
        ConnectorTool(
            name="net_traceroute",
            description="Trace the network route (hops) to a host to locate where traffic "
            "slows or stops.",
            parameters={
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "max_hops": {"type": "integer", "description": "Max hops (1-30, default 30)."},
                },
                "required": ["host"],
            },
            kind="read",
            handler=_traceroute,
        ),
        ConnectorTool(
            name="azure_metrics",
            description=(
                "THE tool for visualizing Azure Monitor metrics. Fetch time-series for a resource "
                "(CPU, memory, HTTP 2xx/4xx/5xx, response time, requests, disk/network, RU/s, "
                "messages, tokens, etc.) AND render them as an INTERACTIVE chart in the chat in ONE "
                "step. READ-ONLY (runs `az monitor metrics list`). Whenever the user wants to SEE, "
                "chart, graph, plot, or visualize metrics, use THIS tool — do NOT use the generic "
                "`monitor` / `monitor_metrics_query` MCP tool for that (it returns raw numbers with "
                "no chart and wastes turns). It auto-discovers the resource's available metrics and "
                "picks each metric's correct aggregation, so you do NOT need to look up metric "
                "definitions or aggregations first. Provide the full ARM `resource_id`; pass "
                "`metrics` when the user names specific ones (else the resource's recommended metrics "
                "are used). `timespan` is a lookback such as 'PT1H', 'P1D', 'P7D', or 'P30D'. "
                "IMPORTANT — honor the user's wording: if they name a metric (e.g. 'CPU', 'memory', "
                "'5xx errors', 'response time') pass ONLY that metric in `metrics`; if they ask for a "
                "specific chart style ('line chart', 'as a bar', 'pie', 'area'), pass that exact "
                "value in `chart_type`. Only omit `chart_type` to let it auto-pick. After it returns, "
                "STOP fetching and include the provided ```azchart block verbatim in your reply."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "resource_id": {
                        "type": "string",
                        "description": "Full ARM resource id (/subscriptions/…/providers/…/<name>).",
                    },
                    "resource_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional extra resource ids to overlay as additional series.",
                    },
                    "metrics": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Metric names, e.g. ['Percentage CPU','MemoryWorkingSet','Http5xx'].",
                    },
                    "timespan": {
                        "type": "string",
                        "description": "Lookback window: PT1H, PT6H, P1D, P7D, P30D (default P1D).",
                    },
                    "interval": {
                        "type": "string",
                        "description": "Grain, e.g. PT1M/PT5M/PT1H. Omit to auto-pick from the timespan.",
                    },
                    "aggregation": {
                        "type": "string",
                        "enum": ["Average", "Total", "Maximum", "Minimum", "Count"],
                        "description": "Aggregation (default Average; use Total for counts like Http5xx).",
                    },
                    "chart_type": {
                        "type": "string",
                        "enum": ["line", "area", "bar", "pie", "donut"],
                        "description": "Visualization type. Omit to auto-pick from the data "
                        "(counts/sparse → bar, single metric → area, multiple → line); set it "
                        "to force a specific type. 'pie'/'donut' compare metrics' relative "
                        "magnitude (one slice per metric).",
                    },
                    "title": {"type": "string", "description": "Optional chart title."},
                    "unit": {"type": "string", "description": "Optional display unit (%, ms, count, bytes)."},
                    "connection_id": {
                        "type": "string",
                        "description": "Optional Azure connection id (defaults to the active connection).",
                    },
                },
                "required": ["resource_id"],
            },
            kind="read",
            handler=_azure_metrics,
        ),
    ]


def builtin_tools(allowed_tool_names: list[str] | None = None) -> list[ConnectorTool]:
    """The enabled built-in tools (empty when the admin kill-switch is off).

    Respects ``builtin_tools_enabled`` and an optional per-tool disable list, and applies
    the same name allow-list filter used to scope custom agents.
    """
    s = _settings()
    if not bool(s.get("builtin_tools_enabled", True)):
        return []
    disabled = {str(n) for n in (s.get("builtin_tools_disabled") or [])}
    allow = set(allowed_tool_names) if allowed_tool_names is not None else None
    out = []
    for t in _tools():
        if t.name in disabled:
            continue
        if allow is not None and t.name not in allow:
            continue
        out.append(t)
    return out


def builtin_tool_catalog() -> list[dict[str, str]]:
    """All built-in tools (ignoring the enable flag) for the admin Tools page."""
    return [{"name": t.name, "description": t.description, "kind": t.kind} for t in _tools()]
