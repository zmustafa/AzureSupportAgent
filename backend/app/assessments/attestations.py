"""Manual-attestation registry for assessment controls (JSON, no secrets).

Many Well-Architected / APRL recommendations can't be verified from Resource Graph — they
require a reviewer to confirm (e.g. "a DR failover has been tested in the last 6 months").
A ``manual`` control surfaces as *pending* until a human records an attestation here; once
recorded, the runner scores it like any deterministic control.

Attestations are scoped per (tenant, workload, check) so the same control can have a
different verdict for each workload. Persisted under backend/.data/assessment_attestations.json.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "assessment_attestations.json"

_VALID = ("pass", "fail", "not_applicable")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {})
    return data if isinstance(data, dict) else {}


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
    if status not in _VALID:
        status = "fail" if status else ""
    entry: dict[str, Any] | None = None
    if status:
        entry = {
            "status": status,
            "note": str(note or "")[:2000],
            "by": str(by or "")[:128],
            "at": _now(),
        }

    def _mutate(data: dict[str, Any]) -> None:
        key = _key(tenant_id, workload_id)
        bucket = data.setdefault(key, {})
        if not isinstance(bucket, dict):
            bucket = {}
            data[key] = bucket
        if entry is None:
            bucket.pop(check_id, None)
        else:
            bucket[check_id] = entry

    jsonstore.mutate_json(_PATH, {}, _mutate)
    return entry
