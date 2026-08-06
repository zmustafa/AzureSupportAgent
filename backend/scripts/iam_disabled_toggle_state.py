"""Temporarily strip (and restore) the cached account state for the ACTIVE IAM tenant.

Used to browser-check the one state that matters most and that no happy-path screenshot can
show: what the screen renders when account state was never collected. It must be a WALL, not an
empty table — "no disabled account holds access" is the most reassuring sentence this feature
can produce and it must never come from having failed to ask.

    python scripts/iam_disabled_toggle_state.py strip     # writes a backup, clears the map
    python scripts/iam_disabled_toggle_state.py restore   # puts it back

Local only. Refuses to run if a backup already exists (strip) or is missing (restore), so it
cannot destroy the real cache by being run twice.
"""
from __future__ import annotations

import json
import pathlib
import sys

from app.core.azure_connections import resolve_connection
from app.iam import cache, schema

BACKUP = pathlib.Path(__file__).resolve().parents[1] / ".data" / "_principal_state_backup.json"


def _tenant() -> str:
    conn = resolve_connection(None)
    return (conn or {}).get("tenant_id") or "default"


def _rewrite(tenant_id: str, state: dict | None) -> None:
    d = cache.read_directory(tenant_id)
    meta = cache.read_directory_meta(tenant_id)
    cache.write_directory(
        tenant_id,
        meta={
            "status": meta.get("status") or schema.STATUS_SUCCEEDED,
            "demo": meta.get("demo", False),
            "collectors": meta.get("collectors") or [],
        },
        rows=d["rows"], role_defs=d["role_defs"], principals=d["principals"],
        groups=d["groups"], management_groups=d["management_groups"],
        identities=d["identities"], federated=d["federated"], principal_state=state,
    )


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    tenant_id = _tenant()
    if action == "strip":
        if BACKUP.exists():
            print(f"Backup already exists at {BACKUP} — restore first.")
            return 1
        state = cache.read_directory(tenant_id).get("principal_state") or {}
        BACKUP.parent.mkdir(parents=True, exist_ok=True)
        BACKUP.write_text(json.dumps({"tenant": tenant_id, "state": state}), encoding="utf-8")
        _rewrite(tenant_id, None)
        print(f"stripped {len(state)} entries for {tenant_id} (backed up to {BACKUP})")
        return 0
    if action == "restore":
        if not BACKUP.exists():
            print("No backup to restore.")
            return 1
        payload = json.loads(BACKUP.read_text(encoding="utf-8"))
        _rewrite(payload["tenant"], payload["state"])
        BACKUP.unlink()
        print(f"restored {len(payload['state'])} entries for {payload['tenant']}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
