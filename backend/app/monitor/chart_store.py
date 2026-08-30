"""Server-side store for chat metric-chart artifacts (the data behind a ```azchart block).

When the agent's ``azure_metrics`` tool fetches a time-series, it stores the normalized
table here keyed by a random ``chart_id`` and only the id travels into the assistant's
reply (inside a fenced ``azchart`` block). The frontend then fetches the series back by
id to render an interactive chart. Keeping the data server-side (rather than echoing it
into the message) keeps messages small and stops the model from fabricating data points.

Artifacts are persisted to the ``backend/.data`` volume (surviving restarts) and bounded
with a simple oldest-first eviction so the file can't grow without limit.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import jsonstore

_PATH = Path(__file__).resolve().parents[2] / ".data" / "chart_artifacts.json"

# Keep the most recent N charts; older ones are evicted (the chat still renders, but an
# evicted chart's fence shows a friendly "expired" message instead of a graph).
_MAX_ARTIFACTS = 300


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    data = jsonstore.read_json(_PATH, {"artifacts": {}})
    if isinstance(data, dict) and isinstance(data.get("artifacts"), dict):
        return data
    return {"artifacts": {}}


def _prune(artifacts: dict[str, Any]) -> dict[str, Any]:
    """Evict oldest artifacts (by created_at) once over the cap."""
    if len(artifacts) <= _MAX_ARTIFACTS:
        return artifacts
    ordered = sorted(artifacts.items(), key=lambda kv: kv[1].get("created_at", ""))
    keep = ordered[-_MAX_ARTIFACTS:]
    return dict(keep)


def save_chart(spec: dict[str, Any], result: dict[str, Any]) -> str:
    """Persist a chart artifact and return its id.

    ``spec`` is the chart descriptor (title, type, unit, metrics, …) and ``result`` is the
    normalized ``{columns, rows, meta}`` table that drives the rendering.
    """
    chart_id = uuid.uuid4().hex
    artifact = {
        "id": chart_id,
        "created_at": _now(),
        "spec": spec or {},
        "result": result or {},
    }

    def _mutate(data: dict[str, Any]) -> None:
        artifacts = data.get("artifacts") or {}
        artifacts[chart_id] = artifact
        data["artifacts"] = _prune(artifacts)

    jsonstore.mutate_json(
        _PATH, {"artifacts": {}}, _mutate, indent=None, separators=(",", ":")
    )
    return chart_id


def get_chart(chart_id: str) -> dict[str, Any] | None:
    """Return a stored chart artifact ``{id, created_at, spec, result}`` or None."""
    if not chart_id:
        return None
    art = _read().get("artifacts", {}).get(chart_id)
    return art if isinstance(art, dict) else None
