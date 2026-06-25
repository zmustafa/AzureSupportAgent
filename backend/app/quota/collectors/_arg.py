"""Shared Azure Resource Graph (ARG) helper for collectors that COUNT current resources where no
usage API exists (Layer 3). Wraps ``app.azure.arm.query_resource_graph_paged`` with the context's
token, scoped to the single subscription being scanned."""
from __future__ import annotations

from typing import Any

from app.quota.base import CollectorContext


async def arg_count(ctx: CollectorContext, kql: str) -> tuple[int | None, str | None]:
    """Run a KQL that projects a single ``count_`` (summarize count()) and return (count, error).

    The query is scoped to the scanned subscription. Returns (None, error) on failure so the
    caller can stamp an error/unknown result instead of a false zero."""
    from app.azure.arm import query_resource_graph_paged

    rows, err, _complete, _total = await query_resource_graph_paged(
        ctx.token, kql, [ctx.subscription_id], page_size=1, max_rows=1,
    )
    if err:
        return None, err
    if not rows:
        return 0, None
    row = rows[0]
    # summarize count() yields {"count_": N} (or a single-key row); take the first numeric value.
    for v in row.values():
        if isinstance(v, (int, float)):
            return int(v), None
    return 0, None
