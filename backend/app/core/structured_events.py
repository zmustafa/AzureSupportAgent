"""Small, dependency-free structured operational events.

These records intentionally contain hashes instead of tenant, connection, or principal IDs so
throttling can be correlated across replicas without putting customer identifiers in logs.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

log = logging.getLogger("app.ops.events")


def opaque_id(value: str) -> str:
    """Return a stable, non-reversible short identifier suitable for operational logs."""
    return hashlib.sha256((value or "default").encode("utf-8")).hexdigest()[:16]


def emit(event: str, **fields: Any) -> None:
    """Write one compact JSON event; telemetry failures must never affect product behavior."""
    try:
        log.info(json.dumps({"event": event, **fields}, default=str, separators=(",", ":"), sort_keys=True))
    except Exception:  # noqa: BLE001 - observability is strictly fail-open
        pass