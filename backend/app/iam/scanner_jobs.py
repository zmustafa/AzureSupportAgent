"""Scanner delivery: run the due scanners and turn their deltas into notifications.

Split from :mod:`app.iam.scanners` deliberately. That module is pure apart from its own state
file and can be reasoned about (and tested) without a database, an event loop or a connection.
This one is where the async, the DB and the fan-out live.

Delivery policy, in one place because it is the whole product decision:

* a finding in :data:`app.iam.scanners.ALWAYS_IMMEDIATE` is published on its own, at its own
  severity, as soon as it appears;
* everything else new is published as ONE digest per scanner;
* resolutions are published as a single informational line, never one per fingerprint;
* nothing is published when a scanner is blocked — except the fact that it is blocked, once.

A scanner that reports "0 new" every day is invisible on purpose. A scanner that could not run
is NOT invisible: silence from a broken check is indistinguishable from silence from a clean
tenant, and only one of those is good news.
"""
from __future__ import annotations

import logging
from typing import Any

from app.iam import findings as findings_mod
from app.iam import scanners
from app.notifications import engine as notify

log = logging.getLogger("app.iam.scanner_jobs")

SOURCE = "iam"

# Event types, so a notification rule can route "a new critical escalation path" differently
# from "your weekly hygiene digest".
TYPE_IMMEDIATE = "iam.finding_immediate"
TYPE_DIGEST = "iam.scanner_digest"
TYPE_RESOLVED = "iam.findings_resolved"
TYPE_BLOCKED = "iam.scanner_blocked"

# A digest that lists 400 findings is a wall nobody reads. The count is always exact; only the
# examples are capped, and the digest says so.
MAX_DIGEST_EXAMPLES = 10


def _link_for(finding: dict[str, Any]) -> dict[str, Any]:
    """Deep link back to the finding, so a notification is actionable rather than a headline."""
    return {
        "iam": "/iam/findings",
        "signal": f"/iam/findings?signal_id={finding.get('signal_id', '')}",
    }


async def _publish_immediate(tenant_id: str, scanner: scanners.ScannerSpec,
                             finding: dict[str, Any]) -> None:
    await notify.publish(
        tenant_id=tenant_id,
        type=TYPE_IMMEDIATE,
        source=SOURCE,
        severity=str(finding.get("severity") or "warning"),
        title=str(finding.get("title") or "IAM finding"),
        body=(
            f"{finding.get('subject_label') or finding.get('subject') or ''}\n\n"
            f"{finding.get('detail') or ''}\n\n"
            f"Reported by the '{scanner.name}' scanner. This finding bypasses the digest "
            f"because it is a change where an hour matters."
        ).strip(),
        facts={
            "signal_id": finding.get("signal_id", ""),
            "pillar": finding.get("pillar", ""),
            "subject": finding.get("subject", ""),
            "count": finding.get("count", 1),
            "scanner_id": scanner.id,
        },
        links=_link_for(finding),
        # The finding's own fingerprint: the same finding reappearing after a resolution is the
        # same notification, and the notification centre can collapse it.
        fingerprint=str(finding.get("id") or ""),
    )


async def _publish_digest(tenant_id: str, scanner: scanners.ScannerSpec,
                          result: dict[str, Any], digest: list[dict[str, Any]]) -> None:
    worst = min(
        (str(f.get("severity") or "info") for f in digest),
        key=lambda s: scanners._sev_rank(s),
        default="info",
    )
    examples = digest[:MAX_DIGEST_EXAMPLES]
    lines = [
        f"- [{f.get('severity')}] {f.get('title')} — {f.get('subject_label') or f.get('subject')}"
        for f in examples
    ]
    if len(digest) > len(examples):
        lines.append(f"- …and {len(digest) - len(examples)} more.")
    counts = result.get("counts") or {}
    await notify.publish(
        tenant_id=tenant_id,
        type=TYPE_DIGEST,
        source=SOURCE,
        severity=worst,
        title=f"{scanner.name}: {len(digest)} new finding(s)",
        body="\n".join([
            *lines,
            "",
            f"{counts.get('total', 0)} total reported by this scanner "
            f"({counts.get('persisting', 0)} already known).",
        ]),
        facts={
            "scanner_id": scanner.id,
            "new": len(digest),
            "total": counts.get("total", 0),
            "persisting": counts.get("persisting", 0),
        },
        links={"iam": "/iam/findings"},
        # Deliberately NOT stable across runs: two different digests are two different events.
        fingerprint=f"{scanner.id}|digest|{result.get('at', '')}",
    )


async def _publish_resolved(tenant_id: str, scanner: scanners.ScannerSpec,
                            result: dict[str, Any]) -> None:
    n = len(result.get("resolved_fingerprints") or [])
    await notify.publish(
        tenant_id=tenant_id,
        type=TYPE_RESOLVED,
        source=SOURCE,
        severity="info",
        title=f"{scanner.name}: {n} finding(s) resolved",
        body=(
            f"{n} finding(s) this scanner reported previously no longer appear. Resolution is "
            f"computed from the snapshot — nobody marked these closed."
        ),
        facts={"scanner_id": scanner.id, "resolved": n},
        links={"iam": "/iam/findings"},
        fingerprint=f"{scanner.id}|resolved|{result.get('at', '')}",
    )


async def _publish_blocked(tenant_id: str, scanner: scanners.ScannerSpec,
                           result: dict[str, Any]) -> None:
    reasons = result.get("blocked") or []
    await notify.publish(
        tenant_id=tenant_id,
        type=TYPE_BLOCKED,
        source=SOURCE,
        severity="warning",
        title=f"{scanner.name} could not run",
        body=(
            "This scanner reported nothing because it could not look, not because the tenant is "
            "clean:\n\n" + "\n".join(f"- {r}" for r in reasons)
        ),
        facts={"scanner_id": scanner.id, "reasons": reasons},
        links={"iam": "/iam/diagnostics"},
        # One notification per blocked scanner per reason set — a permanently blocked scanner
        # must not produce a daily alarm forever.
        fingerprint=f"{scanner.id}|blocked|{'|'.join(sorted(reasons))}",
    )


async def run_scanner(
    tenant_id: str,
    scanner: scanners.ScannerSpec,
    *,
    findings: list[dict[str, Any]] | None = None,
    results: list[Any] | None = None,
    notify_enabled: bool = True,
) -> dict[str, Any]:
    """Run ONE scanner and deliver its delta. Returns the scanner result."""
    if results is None:
        results = findings_mod.evaluate(tenant_id)
    if findings is None:
        findings = [f.public() for r in results for f in r.findings]

    result = scanners.run(scanner, tenant_id, findings, results)
    if not notify_enabled:
        return result

    if result["blocked"]:
        await _publish_blocked(tenant_id, scanner, result)
        return result

    # A first run has no baseline, so EVERY finding is "new". Publishing 400 notifications the
    # first time a scanner is enabled is how a user turns notifications off permanently.
    if result["first_run"]:
        return result

    immediate_ids = {str(f.get("id")) for f in result["immediate"]}
    for finding in result["immediate"]:
        await _publish_immediate(tenant_id, scanner, finding)

    digest = [f for f in result["new"] if str(f.get("id")) not in immediate_ids]
    if digest:
        await _publish_digest(tenant_id, scanner, result, digest)

    if result["resolved_fingerprints"]:
        await _publish_resolved(tenant_id, scanner, result)
    return result


async def run_due(tenant_id: str, *, force: bool = False,
                  notify_enabled: bool = True) -> list[dict[str, Any]]:
    """Run every scanner whose cadence has elapsed. One evaluation shared by all of them.

    Evaluating the registry once and handing the same finding list to nine scanners is not an
    optimisation detail — it is what stops two scanners in the same sweep disagreeing because
    the cache was refreshed between them."""
    results = findings_mod.evaluate(tenant_id)
    findings = [f.public() for r in results for f in r.findings]
    scanners.update_ledger(tenant_id, findings)

    out: list[dict[str, Any]] = []
    for scanner in scanners.registry():
        if not scanner.enabled:
            continue
        if not force and not scanners.due(scanner, tenant_id):
            continue
        try:
            out.append(await run_scanner(
                tenant_id, scanner, findings=findings, results=results,
                notify_enabled=notify_enabled,
            ))
        except Exception:  # noqa: BLE001 - one bad scanner must not stop the sweep
            log.warning("iam scanner %s failed", scanner.id, exc_info=True)
    return out
