"""Manual-attestation registry for assessment controls (JSON, no secrets).

Many Well-Architected / APRL recommendations can't be verified from Resource Graph — they
require a reviewer to confirm (e.g. "a DR failover has been tested in the last 6 months").
A ``manual`` control surfaces as *pending* until a human records an attestation here; once
recorded, the runner scores it like any deterministic control.

Attestations are scoped per (tenant, workload, check) so the same control can have a
different verdict for each workload. Persisted under backend/.data/assessment_attestations.json.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parents[2] / ".data" / "assessment_attestations.json"

_VALID = ("pass", "fail", "not_applicable")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    if _PATH.exists():
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _key(tenant_id: str, workload_id: str) -> str:
    return f"{tenant_id}|{workload_id}"


def get_attestations(tenant_id: str, workload_id: str) -> dict[str, dict[str, Any]]:
    """Return ``{check_id: attestation}`` for a workload (empty if none recorded)."""
    bucket = _read().get(_key(tenant_id, workload_id))
    return bucket if isinstance(bucket, dict) else {}


def set_attestation(
    tenant_id: str,
    workload_id: str,
    check_id: str,
    *,
    status: str,
    note: str = "",
    by: str = "",
) -> dict[str, Any] | None:
    """Record (or clear) a manual attestation for a control on a workload.

    ``status`` must be one of pass/fail/not_applicable, or the empty string to CLEAR the
    attestation (reverting the control to pending). Returns the stored entry, or None on clear."""
    data = _read()
    bucket = data.setdefault(_key(tenant_id, workload_id), {})
    if not isinstance(bucket, dict):
        bucket = {}
        data[_key(tenant_id, workload_id)] = bucket
    if not status:
        bucket.pop(check_id, None)
        _write(data)
        return None
    if status not in _VALID:
        status = "fail"
    entry = {
        "status": status,
        "note": str(note or "")[:2000],
        "by": str(by or "")[:128],
        "at": _now(),
    }
    bucket[check_id] = entry
    _write(data)
    return entry
