"""Shared Azure tag read/write plumbing for the ownership + tag-intelligence apply paths.

* READ — :func:`read_current_tags` fetches the CURRENT tag dict for a set of resource ids via
  Resource Graph (fail-closed). This is what we snapshot as the *recovery copy* BEFORE any
  apply, so a change can be reverted exactly.
* WRITE — :func:`set_resource_tags` issues an ARM ``PATCH …/tags/default`` with operation
  ``Merge`` (preserve other tags) or ``Replace`` (set the FULL tag set — used by revert to
  restore the exact prior state). Mirrors :func:`app.ownership.writeback.apply_owner_tag`.

Centralizing this means ownership owner-tag apply and tag-intelligence remediation share one
audited, revertible write path.
"""
from __future__ import annotations

from typing import Any

_TAGS_API = "2021-04-01"


async def read_current_tags(
    connection: dict[str, Any] | None,
    resource_ids: list[str],
    *,
    session_config_dir: str | None = None,
) -> tuple[dict[str, dict[str, str]], dict[str, str], str]:
    """Return ``(tags_by_id, names_by_id, error)`` for the given resource ids.

    ``tags_by_id`` maps each (lower-cased) resource id to its current tag dict ({} when
    untagged or missing). Resources not found in the graph are simply absent. Reads in
    batches via Resource Graph so a large selection stays within query limits."""
    out: dict[str, dict[str, str]] = {}
    names: dict[str, str] = {}
    ids = [r for r in {(rid or "").strip() for rid in resource_ids} if r]
    if not ids:
        return out, names, ""
    if connection is None:
        return out, names, "No Azure connection configured."

    from app.exec.command_runner import run_kql_collect

    # Chunk the id list so the `in~ (...)` clause never gets unwieldy.
    CHUNK = 200
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i : i + CHUNK]
        quoted = ", ".join("'" + rid.replace("'", "") + "'" for rid in chunk)
        kql = (
            "resources | where id in~ (" + quoted + ") "
            "| project id, name, tags"
        )
        res = await run_kql_collect(kql, connection, session_config_dir=session_config_dir, max_rows=CHUNK)
        if not res.ok:
            return out, names, (res.error or "Resource Graph query failed.")[:300]
        for row in res.rows:
            rid = (row.get("id") or "").lower()
            if not rid:
                continue
            tags = row.get("tags") or {}
            out[rid] = {str(k): str(v) for k, v in tags.items()} if isinstance(tags, dict) else {}
            names[rid] = row.get("name", "") or ""
    return out, names, ""


async def set_resource_tags(
    connection: dict[str, Any] | None,
    resource_id: str,
    tags: dict[str, str],
    *,
    operation: str = "Merge",
) -> tuple[bool, str]:
    """Write tags onto one resource via ARM REST. ``operation`` is ``Merge`` (add/overwrite the
    given keys, keep others) or ``Replace`` (set the FULL tag set — used by revert). Returns
    ``(ok, error)``."""
    if not resource_id:
        return False, "resource_id is required."
    if connection is None:
        return False, "No Azure connection configured."
    op = "Replace" if str(operation).lower() == "replace" else "Merge"

    from app.azure.arm import arm_rest, get_arm_token

    token, err = await get_arm_token(connection)
    if not token:
        return False, err or "Could not acquire an ARM token."
    url = (
        f"https://management.azure.com/{resource_id.lstrip('/')}"
        f"/providers/Microsoft.Resources/tags/default?api-version={_TAGS_API}"
    )
    body = {"operation": op, "properties": {"tags": {str(k): str(v) for k, v in tags.items()}}}
    _out, perr = await arm_rest(token, "PATCH", url, body)
    if perr:
        return False, perr
    return True, ""
