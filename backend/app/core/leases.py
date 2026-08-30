"""Small shared primitives for database-backed background-work leases."""
from __future__ import annotations

import uuid


PROCESS_REPLICA_ID = uuid.uuid4().hex
LEASE_SECONDS = 60.0
HEARTBEAT_SECONDS = 20.0


def worker_id(service: str) -> str:
    """Return a stable, random identity for one service worker instance."""
    return f"{service}:{PROCESS_REPLICA_ID}:{uuid.uuid4().hex[:8]}"


def lease_token() -> str:
    """Return a unique fencing token for one acquisition of a row lease."""
    return str(uuid.uuid4())