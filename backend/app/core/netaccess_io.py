"""Bulk import preview and safe export helpers for network access rules.

Imports are deliberately two-phase: this module parses and previews but never persists. The
frontend applies an approved result to its existing draft, and the ordinary firewall save path
remains the only writer. That preserves self-lockout protection, typed ENFORCE confirmation,
audit, and the commit-confirm timer.
"""
from __future__ import annotations

import csv
import io
from typing import Any

from app.core import netaccess

MAX_IMPORT_BYTES = 1 * 1024 * 1024
MAX_LINE_CHARS = 1_024
MAX_OVERLAP_DETAILS = 100
IMPORT_FORMATS = ("auto", "txt", "csv")
IMPORT_STRATEGIES = ("merge", "replace")

_TRUE_VALUES = {"1", "true", "yes", "y", "enabled", "active", "on"}
_FALSE_VALUES = {"0", "false", "no", "n", "disabled", "inactive", "off"}
_FORMULA_TRIGGERS = ("=", "+", "-", "@")


class NetAccessImportError(ValueError):
    """A structurally invalid or oversized import request."""


def _source_name(value: str) -> str:
    # Browsers supply a basename, but normalize both separator styles so a direct API caller
    # cannot put a local path into audit metadata or a preview response.
    return (value or "pasted-ranges.txt").replace("\\", "/").rsplit("/", 1)[-1][:255]


def _validate_text(text: str) -> str:
    if "\x00" in text:
        raise NetAccessImportError("The import contains a NUL byte and is not a text list.")
    if len(text.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise NetAccessImportError("The import is larger than 1 MiB.")
    clean = text.removeprefix("\ufeff")
    for line_number, line in enumerate(clean.splitlines(), 1):
        if len(line) > MAX_LINE_CHARS:
            raise NetAccessImportError(
                f"Line {line_number} exceeds the {MAX_LINE_CHARS:,}-character limit."
            )
    return clean


def _detect_format(text: str, source_name: str, requested: str) -> str:
    fmt = requested.strip().lower()
    if fmt not in IMPORT_FORMATS:
        raise NetAccessImportError(f"Unknown import format '{requested}'.")
    if fmt != "auto":
        return fmt
    if source_name.lower().endswith(".csv"):
        return "csv"
    first = next(
        (
            line
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ),
        "",
    )
    if first:
        try:
            fields = next(csv.reader([first]))
        except csv.Error:
            fields = []
        if "cidr" in {field.strip().lower() for field in fields}:
            return "csv"
    return "txt"


def _enabled(value: str, line: int) -> bool:
    raw = (value or "").strip().lower()
    if not raw:
        return True
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    raise NetAccessImportError(
        f"Line {line}: enabled must be true/false, yes/no, 1/0, or enabled/disabled."
    )


def _txt_rows(text: str, default_label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        value = raw_line.strip()
        if not value or value.startswith("#"):
            continue
        rows.append(
            {
                "line": line_number,
                "input": value,
                "cidr": value,
                "label": default_label,
                "enabled": True,
            }
        )
    return rows


def _csv_rows(text: str, default_label: str) -> list[dict[str, Any]]:
    try:
        reader = csv.reader(io.StringIO(text))
        header: list[str] | None = None
        rows: list[dict[str, Any]] = []
        for values in reader:
            line_number = reader.line_num
            if not values or not any(v.strip() for v in values):
                continue
            if values[0].lstrip().startswith("#"):
                continue
            if header is None:
                header = [v.strip().lower().removeprefix("\ufeff") for v in values]
                if "cidr" not in header:
                    raise NetAccessImportError("CSV header must contain a 'cidr' column.")
                if len(set(header)) != len(header):
                    raise NetAccessImportError("CSV header contains a duplicate column name.")
                continue
            if len(values) > len(header):
                rows.append(
                    {
                        "line": line_number,
                        "input": ",".join(values),
                        "cidr": "",
                        "label": "",
                        "enabled": True,
                        "parse_error": "This row has more values than the CSV header.",
                    }
                )
                continue
            record = dict(zip(header, values, strict=False))
            rows.append(
                {
                    "line": line_number,
                    "input": record.get("cidr", "").strip(),
                    "cidr": record.get("cidr", "").strip(),
                    "label": (record.get("label", "") or default_label).strip(),
                    "enabled_raw": record.get("enabled", ""),
                }
            )
        if header is None:
            raise NetAccessImportError("The CSV contains no header row.")
        return rows
    except csv.Error as exc:
        raise NetAccessImportError(f"The CSV could not be parsed: {exc}.") from exc


def _parse_rows(
    text: str,
    *,
    source_name: str,
    requested_format: str,
    default_label: str,
) -> tuple[str, list[dict[str, Any]]]:
    clean = _validate_text(text)
    fmt = _detect_format(clean, source_name, requested_format)
    rows = _csv_rows(clean, default_label) if fmt == "csv" else _txt_rows(clean, default_label)
    return fmt, rows


def _overlaps(rules: list[dict[str, Any]]) -> tuple[int, list[dict[str, str]]]:
    """Return overlap count and bounded details in O(n log n), not O(n²).

    CIDR overlaps are containment relationships. Sorting by start address and retaining the
    interval with the greatest end catches every overlapping range without comparing all pairs.
    """
    intervals: list[tuple[int, int, int, str]] = []
    for rule in rules:
        net = netaccess.parse_cidr(str(rule.get("cidr", "")))
        intervals.append(
            (net.version, int(net.network_address), int(net.broadcast_address), str(net))
        )
    intervals.sort(key=lambda item: (item[0], item[1], -item[2]))
    count = 0
    details: list[dict[str, str]] = []
    active: tuple[int, int, int, str] | None = None
    for current in intervals:
        if active is None or current[0] != active[0] or current[1] > active[2]:
            active = current
            continue
        count += 1
        if len(details) < MAX_OVERLAP_DETAILS:
            details.append(
                {
                    "cidr": current[3],
                    "overlaps": active[3],
                    "message": f"{current[3]} overlaps {active[3]}; both are retained.",
                }
            )
        if current[2] > active[2]:
            active = current
    return count, details


def _public_rule(rule: dict[str, Any]) -> dict[str, Any]:
    net = netaccess.parse_cidr(str(rule.get("cidr", "")))
    return {
        **rule,
        "cidr": str(net),
        "scope": netaccess.describe_scope(net),
        "valid": True,
    }


def preview_import(
    text: str,
    *,
    source_name: str,
    requested_format: str,
    default_label: str,
    strategy: str,
    mode: str,
    existing_rules: list[dict[str, Any]],
    caller_ip: str | None,
    actor: str,
) -> dict[str, Any]:
    """Parse an import and calculate the exact resulting draft without writing anything."""
    if strategy not in IMPORT_STRATEGIES:
        raise NetAccessImportError(f"Unknown import strategy '{strategy}'.")
    if mode not in netaccess.MODES:
        raise NetAccessImportError(f"Unknown mode '{mode}'.")

    source = _source_name(source_name)
    label = default_label.strip()
    existing = netaccess.normalize_rules(existing_rules, mode=mode)
    fmt, parsed = _parse_rows(
        text,
        source_name=source,
        requested_format=requested_format,
        default_label=label,
    )

    diagnostics: list[dict[str, Any]] = []
    incoming: list[dict[str, Any]] = []
    incoming_lines: dict[str, int] = {}
    duplicate_input = 0
    canonicalized = 0
    for row in parsed:
        item = {
            "line": int(row.get("line", 0)),
            "input": str(row.get("input", "")),
            "cidr": None,
            "label": str(row.get("label", "") or ""),
            "enabled": True,
            "status": "invalid",
            "message": "",
        }
        error = str(row.get("parse_error", "") or "")
        try:
            if error:
                raise NetAccessImportError(error)
            enabled = (
                _enabled(str(row.get("enabled_raw", "")), item["line"])
                if "enabled_raw" in row
                else bool(row.get("enabled", True))
            )
            item["enabled"] = enabled
            item["label"] = str(row.get("label", "") or "").strip()
            if not item["label"]:
                raise NetAccessImportError("A label is required; enter a default label or add one in CSV.")
            if len(item["label"]) > 128:
                raise NetAccessImportError("Label exceeds the 128-character limit.")
            net = netaccess.parse_cidr(str(row.get("cidr", "")))
            cidr = str(net)
            item["cidr"] = cidr
            if str(row.get("cidr", "")).strip() != cidr:
                canonicalized += 1
            if cidr in incoming_lines:
                duplicate_input += 1
                raise NetAccessImportError(
                    f"Duplicates line {incoming_lines[cidr]} after normalization to {cidr}."
                )
            incoming_lines[cidr] = item["line"]
            item["status"] = "valid"
            incoming.append(
                {
                    "cidr": cidr,
                    "label": item["label"],
                    "enabled": enabled,
                    "created_by": actor,
                }
            )
        except (netaccess.NetAccessError, NetAccessImportError) as exc:
            item["message"] = str(exc)
        diagnostics.append(item)

    global_errors: list[str] = []
    if not parsed:
        global_errors.append("No IP addresses or CIDR ranges were found in the import.")
    if strategy == "replace" and not incoming:
        global_errors.append("Replace requires at least one valid range; an empty import cannot erase the policy.")
    if len(incoming) > netaccess.MAX_RULES:
        global_errors.append(f"The import exceeds the {netaccess.MAX_RULES:,}-range limit.")

    existing_by = {rule["cidr"]: rule for rule in existing}
    result = list(existing) if strategy == "merge" else []
    added = 0
    skipped_existing = 0
    for entry, rule in zip((d for d in diagnostics if d["status"] == "valid"), incoming, strict=False):
        cidr = rule["cidr"]
        if strategy == "merge" and cidr in existing_by:
            entry["status"] = "existing"
            entry["message"] = "Existing rule retained; imported label and status were not applied."
            skipped_existing += 1
            continue
        entry["status"] = "retained" if cidr in existing_by else "add"
        result.append(rule)
        if cidr not in existing_by:
            added += 1

    try:
        result = netaccess.normalize_rules(result, mode=mode)
    except netaccess.NetAccessError as exc:
        global_errors.append(str(exc))

    if len(result) > netaccess.MAX_RULES:
        global_errors.append(
            f"The resulting policy has {len(result):,} ranges; the limit is {netaccess.MAX_RULES:,}."
        )

    invalid_rows = sum(1 for item in diagnostics if item["status"] == "invalid")
    if invalid_rows:
        global_errors.append(
            f"Fix {invalid_rows:,} invalid input row(s) before applying the import."
        )

    overlap_count, overlap_details = _overlaps(result) if result else (0, [])
    existing_ids = set(existing_by)
    result_ids = {rule["cidr"] for rule in result}
    retained = len(existing_ids & result_ids)
    removed = len(existing_ids - result_ids)
    enabled_total = sum(1 for rule in result if rule.get("enabled", True))
    return {
        "source_name": source,
        "format": fmt,
        "strategy": strategy,
        "can_apply": not global_errors,
        "errors": global_errors,
        "diagnostics": diagnostics,
        "overlaps": overlap_details,
        "overlap_count": overlap_count,
        "overlap_details_truncated": overlap_count > len(overlap_details),
        "result_rules": [_public_rule(rule) for rule in result],
        "your_ip": caller_ip,
        "your_ip_covered": netaccess.matches(caller_ip, result),
        "summary": {
            "input_rows": len(parsed),
            "valid_rows": len(parsed) - invalid_rows,
            "invalid_rows": invalid_rows,
            "duplicate_input": duplicate_input,
            "canonicalized": canonicalized,
            "added": added,
            "retained": retained,
            "skipped_existing": skipped_existing,
            "removed": removed,
            "result_total": len(result),
            "enabled_total": enabled_total,
        },
    }


def safe_preview_import(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
    """Run an import preview without allowing parser exceptions to cross an API boundary.

    Structural failures intentionally collapse to ``None``. Row-level diagnostics remain in a
    successful preview result, while exception objects, causes, paths, and tracebacks stay local.
    """
    try:
        return preview_import(*args, **kwargs)
    except (netaccess.NetAccessError, NetAccessImportError):
        return None


def _csv_safe(value: str) -> str:
    stripped = value.lstrip("\t\r\n ")
    return "'" + value if stripped and stripped[0] in _FORMULA_TRIGGERS else value


def export_txt(rules: list[dict[str, Any]], *, mode: str) -> str:
    """One active canonical CIDR per line, directly re-importable as text."""
    normalized = netaccess.normalize_rules(rules, mode=mode)
    values = [rule["cidr"] for rule in normalized if rule.get("enabled", True)]
    return "\n".join(values) + ("\n" if values else "")


def export_csv(rules: list[dict[str, Any]], *, mode: str) -> str:
    """All rules with labels and enabled state, protected against spreadsheet formulas."""
    normalized = netaccess.normalize_rules(rules, mode=mode)
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(("cidr", "label", "enabled"))
    for rule in normalized:
        writer.writerow(
            (rule["cidr"], _csv_safe(str(rule.get("label", ""))), str(bool(rule.get("enabled", True))).lower())
        )
    return buf.getvalue()
