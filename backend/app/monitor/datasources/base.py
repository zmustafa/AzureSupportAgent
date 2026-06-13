"""Normalized data-source result + shared helpers for Monitor widgets.

Every Monitor data source — Azure metrics, Resource Graph, Log Analytics, web/TCP ping,
app telemetry, a saved workbook, etc. — resolves to ONE tabular shape, ``TableResult``,
so the frontend's generic chart/table/stat renderers never need to know where the data
came from. A result is ``{columns, rows, meta}`` where ``columns`` describe the schema
and ``rows`` are row-major arrays aligned to those columns.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# Logical column types the frontend uses for formatting/axis selection.
COL_TYPES = ("string", "number", "datetime", "bool", "category")


@dataclass
class Column:
    name: str
    type: str = "string"

    def to_dict(self) -> dict[str, str]:
        t = self.type if self.type in COL_TYPES else "string"
        return {"name": self.name, "type": t}


@dataclass
class TableResult:
    """A normalized, row-major tabular result fed to widget renderers."""

    columns: list[Column] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "columns": [c.to_dict() for c in self.columns],
            "rows": self.rows,
            "meta": self.meta,
            "error": self.error,
        }

    @classmethod
    def from_error(cls, message: str, meta: dict[str, Any] | None = None) -> "TableResult":
        return cls(columns=[], rows=[], meta=meta or {}, error=message)


def _infer_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        # Heuristic: ISO-ish timestamp.
        if len(value) >= 10 and value[4:5] == "-" and value[7:8] == "-":
            return "datetime"
        return "string"
    return "string"


def table_from_records(records: list[dict[str, Any]], *, max_rows: int = 1000) -> TableResult:
    """Build a TableResult from a list of flat dicts (e.g. Resource Graph / LA rows).

    Column order is the union of keys in first-seen order; types are inferred from the
    first non-null value seen per column.
    """
    if not isinstance(records, list):
        return TableResult.from_error("Expected a list of rows.")
    col_order: list[str] = []
    col_types: dict[str, str] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        for k in rec.keys():
            if k not in col_types:
                col_order.append(k)
                col_types[k] = ""
            if not col_types[k] and rec.get(k) is not None:
                col_types[k] = _infer_type(rec.get(k))
    columns = [Column(name=k, type=(col_types[k] or "string")) for k in col_order]
    rows: list[list[Any]] = []
    for rec in records[:max_rows]:
        if not isinstance(rec, dict):
            continue
        rows.append([_jsonable(rec.get(k)) for k in col_order])
    return TableResult(columns=columns, rows=rows, meta={"row_count": len(rows), "total": len(records)})


def _jsonable(value: Any) -> Any:
    """Coerce nested dict/list cells into compact JSON strings so the grid stays flat."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)[:500]
    except (TypeError, ValueError):
        return str(value)[:500]


def parse_json_output(stdout: str) -> tuple[Any, str]:
    """Parse az/KQL JSON stdout. Returns (data, error). Tolerates leading warnings."""
    text = (stdout or "").strip()
    if not text:
        return None, "Empty output."
    try:
        return json.loads(text), ""
    except json.JSONDecodeError:
        pass
    # Some CLI paths prepend warnings before the JSON; find the first { or [.
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1]), ""
            except json.JSONDecodeError:
                continue
    return None, "Output was not valid JSON."
