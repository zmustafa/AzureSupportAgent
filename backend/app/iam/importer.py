"""Import a standalone all-azure-access scanner run into the IAM cache.

Why this exists: the app runs with a read-only service principal that often lacks Microsoft
Graph directory permissions, billing reader, or management-group reader. The scanner
(github.com/zmustafa/AzureEntraIDIAMScanner) runs interactively under ``az login`` as a human
who *does* hold them. Import lets that human produce the data and this product analyse it —
**without granting the app those permissions.** Because ``schema.SCANNER_COLUMNS`` is the
scanner's header verbatim, a row is interchangeable and every tab, pivot and export works over
imported data unchanged.

Safety: the upload is untrusted input. It is size- and row-capped, parsed without ``eval`` or
dynamic attribute assignment, and every value is coerced per column type by ``schema.make_row``.
Unknown columns are **reported, never silently dropped** — a silent drop would let a newer
scanner version lose data with no signal.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import zipfile
from datetime import datetime, timezone
from typing import Any

from app.iam import cache, schema

log = logging.getLogger("app.iam.importer")

# Bounds. A scanner run on a large tenant is big but not unbounded; these keep a hostile or
# corrupt upload from exhausting memory.
MAX_BYTES = 200 * 1024 * 1024      # 200 MB of JSON/CSV
MAX_ROWS = 2_000_000
# The path the scanner writes inside results.zip. Compared lower-cased against the archive's
# entries, because zip member names are case-sensitive and the casing varies by producer.
_ZIP_MEMBER_CANDIDATES = ("output/allazureaccess.json", "output/allazureaccess.csv")

# Columns that identify a file as an ACCESS export rather than one of the scanner's other
# artifacts. `collector` alone is not enough — collectorStatus.csv has that column too, so a
# single incidental overlap would let the wrong file through and silently blank the grid.
_IDENTIFYING_COLUMNS = frozenset({
    "principalId", "roleName", "scope", "roleDefinitionId", "assignmentId", "effectivePrincipalId",
})
_MIN_IDENTIFYING = 2

# Scope key prefix for imported slices, so they are listable and purgeable independently of
# live scans and demo data.
IMPORT_SCOPE_PREFIX = "__imported__"


class ImportError_(ValueError):
    """Raised for a malformed or oversized upload. Surfaces as HTTP 400."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rows_from_json(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ImportError_(f"Not valid JSON: {exc}") from exc
    # The scanner writes a bare array; tolerate {"rows": [...]} / {"value": [...]} wrappers.
    if isinstance(data, dict):
        for key in ("rows", "value", "allAzureAccess"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        raise ImportError_("Expected a JSON array of access rows.")
    out = [r for r in data if isinstance(r, dict)]
    if not out:
        raise ImportError_("No access rows found in the file.")
    return out


def _rows_from_csv(text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ImportError_("CSV has no header row.")
    out = [dict(r) for r in reader]
    if not out:
        raise ImportError_("No access rows found in the file.")
    return out


def _extract_zip(payload: bytes) -> tuple[str, str]:
    """Pull the access rows out of the scanner's results.zip. Returns (text, filename)."""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = {n.lower(): n for n in zf.namelist()}
            for candidate in _ZIP_MEMBER_CANDIDATES:
                real = names.get(candidate)
                if real is None:
                    # Tolerate a zip rooted at the run folder rather than at output/.
                    real = next((n for low, n in names.items() if low.endswith(candidate)), None)
                if real is None:
                    continue
                info = zf.getinfo(real)
                if info.file_size > MAX_BYTES:
                    raise ImportError_(f"{real} is {info.file_size} bytes, over the {MAX_BYTES} limit.")
                return zf.read(real).decode("utf-8-sig", errors="replace"), real
    except zipfile.BadZipFile as exc:
        raise ImportError_(f"Not a readable zip archive: {exc}") from exc
    raise ImportError_("Archive contains no output/allAzureAccess.json or .csv.")


def parse_upload(payload: bytes, filename: str) -> tuple[list[dict[str, Any]], str]:
    """Decode an uploaded scanner artifact into raw row dicts. Returns (rows, source_name)."""
    if not payload:
        raise ImportError_("The uploaded file is empty.")
    if len(payload) > MAX_BYTES:
        raise ImportError_(f"File is {len(payload)} bytes, over the {MAX_BYTES} limit.")

    name = (filename or "").strip() or "upload"
    lower = name.lower()
    if lower.endswith(".zip") or payload[:2] == b"PK":
        text, member = _extract_zip(payload)
        return (_rows_from_json(text) if member.lower().endswith(".json") else _rows_from_csv(text)), f"{name}:{member}"

    text = payload.decode("utf-8-sig", errors="replace")
    if lower.endswith(".csv"):
        return _rows_from_csv(text), name
    if lower.endswith(".json"):
        return _rows_from_json(text), name
    # No usable extension — sniff. JSON arrays start with '[' (or '{' for a wrapper).
    head = text.lstrip()[:1]
    return (_rows_from_json(text) if head in "[{" else _rows_from_csv(text)), name


_TRUTHY = {"true", "1", "yes", "y"}


def _coerce(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize one raw row into a schema row.

    CSV gives everything as strings, so the boolean columns arrive as ``"True"`` / ``"False"``
    and would otherwise all read as truthy. ``make_row`` fills any column the source omitted."""
    values: dict[str, Any] = {}
    for col in schema.COLUMNS:
        if col not in raw:
            continue
        val = raw[col]
        if col in schema._BOOL_COLUMNS:
            values[col] = val if isinstance(val, bool) else str(val).strip().lower() in _TRUTHY
        else:
            values[col] = "" if val is None else str(val)
    values["imported"] = True
    values["collectionStatus"] = values.get("collectionStatus") or schema.STATUS_SUCCEEDED
    return schema.make_row(**values)


def import_rows(
    tenant_id: str,
    payload: bytes,
    filename: str,
    *,
    label: str = "",
) -> dict[str, Any]:
    """Parse, validate and cache an uploaded scanner run.

    Rows are written as ONE cache slice flagged ``imported`` so freshness, Diagnostics and the
    purge path can all tell imported data from a live scan. The UI must never present it as
    live — provenance is carried on the slice meta and on every row's ``imported`` column."""
    raw_rows, source = parse_upload(payload, filename)
    if len(raw_rows) > MAX_ROWS:
        raise ImportError_(f"{len(raw_rows)} rows exceeds the {MAX_ROWS} row limit.")

    known = set(schema.COLUMNS)
    seen_columns: set[str] = set()
    for r in raw_rows[:1000]:  # sampling the head is enough to characterise the header
        seen_columns.update(r.keys())
    unknown_columns = sorted(seen_columns - known)
    missing_columns = sorted(set(schema.SCANNER_COLUMNS) - seen_columns)

    rows = [_coerce(r) for r in raw_rows]
    # A file that parsed but carries too few identifying columns is almost certainly the wrong
    # artifact (collectorStatus.csv, coverageSummary.json, errorsWarnings.csv). Fail loudly
    # rather than cache an empty grid over the operator's real data.
    if len(seen_columns & _IDENTIFYING_COLUMNS) < _MIN_IDENTIFYING:
        raise ImportError_(
            "This does not look like an access export. Expected the scanner's allAzureAccess "
            f"file (columns such as {', '.join(sorted(_IDENTIFYING_COLUMNS)[:3])}…), "
            f"got: {', '.join(sorted(seen_columns)[:6]) or 'no columns'}."
        )

    scope_key = f"{IMPORT_SCOPE_PREFIX}/{source}"
    display = label.strip() or source
    subscriptions = sorted({r["subscriptionId"] for r in rows if r.get("subscriptionId")})
    meta = {
        "scopeType": schema.SCOPE_TENANT,
        "displayName": f"Imported: {display}",
        "subscriptionId": "",
        "managementGroupId": "",
        "status": schema.STATUS_SUCCEEDED,
        "collectors": [
            {
                "collector": "ScannerImport",
                "status": schema.STATUS_SUCCEEDED,
                "rowsAdded": len(rows),
                "durationSeconds": 0.0,
                "message": f"Imported from {source}",
            }
        ],
        "coverage": {"roleAssignments": len(rows), "subscriptions": len(subscriptions)},
        "demo": False,
        "imported": True,
        "importSource": source,
        "importedAt": _now_iso(),
    }
    cache.write_scope(tenant_id, scope_key, meta=meta, rows=rows)
    log.info("iam import: %s rows from %s for tenant %s", len(rows), source, tenant_id)
    return {
        "rows_imported": len(rows),
        "scope": scope_key,
        "source": source,
        "subscriptions": len(subscriptions),
        "unknown_columns": unknown_columns,
        "missing_columns": missing_columns,
    }


def purge_imported(tenant_id: str) -> int:
    """Drop every imported slice for a tenant. Live scans and demo data are untouched."""
    removed = 0
    for meta in cache.list_scope_meta(tenant_id):
        scope = meta.get("scope", "")
        if meta.get("imported") or scope.startswith(IMPORT_SCOPE_PREFIX):
            if cache.delete_scope(tenant_id, scope):
                removed += 1
    return removed
