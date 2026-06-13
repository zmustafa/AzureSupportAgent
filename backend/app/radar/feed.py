"""Optional public Azure Updates "Retirements" feed fetcher.

This is the ONLY net-new external network fetch in the Radar (everything else comes from
Resource Graph / Advisor / MCP the app already uses). It is OFF by default and gated behind
an admin app-setting. The public Azure Updates RSS/Atom feed surfaces announcements before
they appear in a tenant's Service Health, but per the official template the workbook
visibility can lag an announcement by up to ~2 weeks — so items here are advisory.

Parsed defensively with the stdlib XML parser (no new dependency); network is via the
already-present httpx. SSRF-guarded to https Azure/Microsoft hosts."""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

log = logging.getLogger("app.radar.feed")

DEFAULT_FEED_URL = "https://www.microsoft.com/releasecommunications/api/v2/azure/rss"
_ALLOWED_HOST_SUFFIXES = (".microsoft.com", ".azure.com", "microsoft.com", "azure.com")
_RETIRE_HINTS = ("retire", "retirement", "deprecat", "end of support", "end-of-support", "breaking change")


def _host_allowed(url: str) -> bool:
    try:
        p = urlparse(url)
    except ValueError:
        return False
    if p.scheme != "https" or not p.hostname:
        return False
    host = p.hostname.lower()
    return any(host == s or host.endswith(s) for s in _ALLOWED_HOST_SUFFIXES)


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _parse_rss(xml_text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_text)  # noqa: S314 - trusted Microsoft feed, no entity expansion used
    except ET.ParseError:
        return out
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        desc = _strip_html(item.findtext("description") or "")
        link = (item.findtext("link") or "").strip()
        combined = f"{title} {desc}".lower()
        if not any(h in combined for h in _RETIRE_HINTS):
            continue
        out.append(
            {
                "source": "azure_updates",
                "tracking_id": "",  # synthesized downstream from title
                "title": title,
                "summary": desc[:500],
                "migration_url": link,
                "impacted_resources": [],
            }
        )
    return out


async def fetch_azure_updates(url: str = "") -> list[dict[str, Any]]:
    """Fetch + filter the public Azure Updates feed to retirement/breaking-change items.

    Returns [] on any error (never raises into the collector)."""
    target = url.strip() or DEFAULT_FEED_URL
    if not _host_allowed(target):
        log.warning("Azure Updates feed URL rejected (not an allowed Microsoft host): %s", target)
        return []
    try:
        import httpx

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(target, headers={"User-Agent": "azsupagent-radar/1.0"})
            resp.raise_for_status()
            return _parse_rss(resp.text)[:100]
    except Exception as exc:  # noqa: BLE001
        log.warning("Azure Updates feed fetch failed: %s", exc)
        return []
