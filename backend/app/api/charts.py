"""Chat metric-chart artifacts: serve the time-series behind a ```azchart block by id.

The agent's ``azure_metrics`` tool stores a fetched series server-side and only the
``chart_id`` rides into the assistant reply. The chat UI fetches the data back here to
render an interactive chart. Read-only; any authenticated user may read a chart by its
(unguessable) id.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.core.security import Principal, get_principal
from app.monitor.chart_store import get_chart

router = APIRouter(prefix="/charts", tags=["charts"])


@router.get("/{chart_id}")
async def read_chart(chart_id: str, _principal: Principal = Depends(get_principal)) -> dict:
    """Return ``{spec, result}`` for a stored chart, or 404 if missing/evicted."""
    art = get_chart(chart_id)
    if not art:
        raise HTTPException(status_code=404, detail="Chart not found or expired.")
    return {"spec": art.get("spec") or {}, "result": art.get("result") or {}}
