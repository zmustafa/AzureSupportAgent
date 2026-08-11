"""Valid, paginated model context for oversized tool results."""
from __future__ import annotations

import json
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


@dataclass
class _Artifact:
    text: str
    created_at: float


class ToolArtifactStore:
    """Turn-local LRU store; sensitive tool output is never persisted to disk."""

    def __init__(self, *, max_items: int = 32, ttl_seconds: int = 3600) -> None:
        self._max_items = max(1, max_items)
        self._ttl = max(60, ttl_seconds)
        self._items: OrderedDict[str, _Artifact] = OrderedDict()

    def _prune(self) -> None:
        cutoff = time.monotonic() - self._ttl
        for key in list(self._items):
            if self._items[key].created_at < cutoff:
                self._items.pop(key, None)
        while len(self._items) > self._max_items:
            self._items.popitem(last=False)

    def put(self, value: Any) -> tuple[str, str]:
        text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
        artifact_id = uuid.uuid4().hex
        self._items[artifact_id] = _Artifact(text=text, created_at=time.monotonic())
        self._items.move_to_end(artifact_id)
        self._prune()
        return artifact_id, text

    def read(self, artifact_id: str, *, offset: int = 0, limit: int = 8000) -> dict[str, Any]:
        self._prune()
        item = self._items.get((artifact_id or "").strip())
        if item is None:
            return {
                "isError": True,
                "content": ["Tool-result artifact was not found or has expired."],
            }
        self._items.move_to_end(artifact_id)
        offset = max(0, int(offset or 0))
        limit = max(500, min(20000, int(limit or 8000)))
        chunk = item.text[offset : offset + limit]
        next_offset = offset + len(chunk)
        complete = next_offset >= len(item.text)
        return {
            "isError": False,
            "content": [chunk],
            "artifact_id": artifact_id,
            "offset": offset,
            "next_offset": None if complete else next_offset,
            "complete": complete,
            "total_chars": len(item.text),
        }


def prepare_tool_result(
    result: Any,
    *,
    cap: int,
    artifacts: ToolArtifactStore,
) -> tuple[Any, dict[str, Any] | None]:
    """Return a valid JSON-serializable result within ``cap`` and optional artifact metadata.

    The previous implementation sliced serialized JSON at an arbitrary character, often
    producing malformed JSON. Oversized values now retain a preview plus a resumable,
    turn-local artifact id.
    """
    cap = max(1000, int(cap))
    serialized = json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(serialized) <= cap:
        return result, None

    artifact_id, full_text = artifacts.put(result)
    preview_size = max(400, min(cap // 2, 6000))
    preview = full_text[:preview_size]
    meta = {
        "artifact_id": artifact_id,
        "total_chars": len(full_text),
        "preview_chars": len(preview),
        "next_offset": len(preview),
    }
    compact = {
        "isError": bool(result.get("isError")) if isinstance(result, dict) else False,
        "content": [preview],
        "truncated": True,
        "artifact": meta,
        "message": (
            "This result was compacted. Call read_tool_artifact with artifact_id and "
            "next_offset to read another bounded page."
        ),
    }
    return compact, meta
