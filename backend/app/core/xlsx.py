"""Shared .xlsx workbook builder.

Extracted from the IAM export when the Entra one landed, so both produce the same artefact:
same header style, same frozen top row, same auto-filter, same formula-injection neutralisation
and — most importantly — the same way of saying "this was not measured".

The formula-injection guard is the reason this is not just a thin wrapper around openpyxl.
Excel and LibreOffice interpret a cell beginning ``= + - @`` as a formula, and
``=cmd|'/c calc'!A1`` in a display name is a working remote-code-execution vector against the
reviewer's workstation. Every cell that reaches a sheet goes through :func:`cell_safe`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_FORMULA_TRIGGERS = ("=", "+", "-", "@")
_FORMULA_LEADING_WS = ("\t", "\r", "\n")

#: Excel's hard limit on a sheet title.
MAX_TITLE = 31


@dataclass(frozen=True)
class HyperlinkCell:
    """Displayed text plus a validated HTTPS target for a real OOXML hyperlink."""

    text: str
    url: str


def hyperlink(text: Any, url: Any) -> HyperlinkCell | str:
    """Create a safe hyperlink value, falling back to plain text for an invalid target."""
    label = str(text or "")
    target = str(url or "")
    if not target.lower().startswith("https://") or any(ch in target for ch in ("\r", "\n", "\t")):
        return label
    return HyperlinkCell(label, target)


def cell_safe(value: Any) -> Any:
    """Neutralise CSV / Excel formula-injection vectors in a single cell value.

    Strings beginning ``= + - @`` (or leading whitespace then one of those) are prefixed with an
    apostrophe so the spreadsheet treats them as literal text. Everything else passes through.
    """
    if not isinstance(value, str) or not value:
        return value
    stripped = value.lstrip("\t\r\n ")
    if stripped and stripped[0] in _FORMULA_TRIGGERS:
        return "'" + value
    if value[0] in _FORMULA_LEADING_WS and stripped and stripped[0] in _FORMULA_TRIGGERS:
        return "'" + value
    return value


def coerce(value: Any) -> Any:
    """A python value as a spreadsheet cell.

    ``True`` becomes "Yes" and ``False`` becomes empty rather than "No": a grid of "No" is
    unreadable, and the columns that carry a boolean are all "does this apply".
    """
    if isinstance(value, HyperlinkCell):
        return cell_safe(value.text)
    if isinstance(value, bool):
        return "Yes" if value else ""
    if value is None:
        return ""
    if isinstance(value, (int, float, str)):
        return cell_safe(value)
    if isinstance(value, (list, tuple)):
        return cell_safe(", ".join(str(v) for v in value))
    if isinstance(value, dict):
        return cell_safe("; ".join(f"{k}={v}" for k, v in value.items()))
    return cell_safe(str(value))


def safe_title(title: str, used: set[str] | None = None) -> str:
    """A legal, UNIQUE sheet title: ≤31 chars, none of ``[]:*?/\\``.

    Uniqueness matters as much as legality. openpyxl silently renames a duplicate to
    "Name1", so two sheets truncated to the same 31 characters would produce a workbook where
    one of them is not the sheet its name claims.
    """
    for ch in "[]:*?/\\":
        title = title.replace(ch, " ")
    base = title.strip()[:MAX_TITLE] or "Sheet"
    if used is None:
        return base
    candidate, n = base, 2
    while candidate.lower() in used:
        suffix = f" {n}"
        candidate = base[: MAX_TITLE - len(suffix)] + suffix
        n += 1
    used.add(candidate.lower())
    return candidate


def argb(colour: str) -> str:
    """A color as OPAQUE 8-digit ARGB.

    The alpha channel is the whole point. OOXML stores colors as aRGB, and openpyxl pads a
    6-digit value by prepending ``00`` — alpha zero, fully transparent — so Excel dutifully
    renders nothing at all. Every tab color looked correct in the file and in an openpyxl
    round-trip, and was invisible when opened.
    """
    value = (colour or "").lstrip("#").upper()
    if not value:
        return ""
    if len(value) == 6:
        return "FF" + value
    return value


class WorkbookBuilder:
    """Accumulates sheets, then serialises once.

    Deliberately tiny. The value is not the abstraction, it is that `sheet` and `blind_sheet`
    exist side by side so writing "we could not look" is exactly as easy as writing rows —
    the moment it is harder, somebody writes an empty sheet instead and it reads as a pass.
    """

    def __init__(self) -> None:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill

        self.wb = Workbook()
        # openpyxl always starts with one sheet; the caller fills it via `first_sheet`.
        self._default = self.wb.active
        self._used: set[str] = set()
        self._header_font = Font(bold=True, color="FFFFFF")
        self._header_fill = PatternFill("solid", fgColor="0F6CBD")
        #: The section subsequent sheets belong to: (label, tab color). Set with `section`.
        self._section: tuple[str, str] = ("", "")
        #: (title, section, rows, note) for every sheet written, so an index can be built after.
        self.manifest: list[tuple[str, str, int, str]] = []

    def section(self, label: str, colour: str = "") -> None:
        """Group the sheets that follow, and give their tabs a shared color.

        Carried on the builder rather than passed to every `sheet` call: a fifty-sheet workbook
        has fifty chances to pass the wrong color, and one sheet tinted like the wrong parent
        tab is worse than no color at all — it is a wrong label."""
        self._section = (label, argb(colour))

    # ---------------------------------------------------------------- sheets
    def sheet(self, title: str, headers: list[str], data: list[list[Any]], *, note: str = "") -> Any:
        from openpyxl.styles import Alignment
        from openpyxl.utils import get_column_letter

        ws = self.wb.create_sheet(safe_title(title, self._used))
        label, colour = self._section
        if colour:
            ws.sheet_properties.tabColor = colour
        ws.append(headers)
        for c in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=c)
            cell.font = self._header_font
            cell.fill = self._header_fill
            cell.alignment = Alignment(vertical="center")
        for r in data:
            ws.append([coerce(v) for v in r])
            row_number = ws.max_row
            for column, value in enumerate(r, start=1):
                if isinstance(value, HyperlinkCell) and value.url:
                    cell = ws.cell(row=row_number, column=column)
                    cell.hyperlink = value.url
                    cell.style = "Hyperlink"
        ws.freeze_panes = "A2"
        for ci, h in enumerate(headers, start=1):
            width = len(str(h))
            # Sampled, not exhaustive: measuring 40,000 rows to size a column costs more than
            # the sheet itself.
            for r in data[:200]:
                if ci - 1 < len(r):
                    width = max(width, len(str(coerce(r[ci - 1]))))
            ws.column_dimensions[get_column_letter(ci)].width = min(60, max(10, width + 2))
        if data:
            ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(data) + 1}"
        self.manifest.append((ws.title, label, len(data), note))
        return ws

    def blind_sheet(self, title: str, reason: str) -> Any:
        """A sheet for something that could NOT be measured.

        Never an empty grid. A sheet called "Risky users" with no rows reads as "no risky
        users"; that is a different statement from "we were not permitted to look", and only
        one of them is true."""
        ws = self.sheet(title, ["Status", "Why"], [["NOT MEASURED", reason]], note=reason)
        return ws

    def first_sheet(self, title: str) -> Any:
        """Take over the workbook's default sheet for the front matter."""
        self._default.title = safe_title(title, self._used)
        _label, colour = self._section
        if colour:
            self._default.sheet_properties.tabColor = colour
        return self._default

    def index_sheet(self, title: str = "Index", *, position: int = 1, colour: str = "") -> None:
        """A contents page. Written last, moved to the front.

        A fifty-sheet workbook without one is a filing cabinet with no labels. `Section` names
        the parent screen each sheet came from — the same grouping the tab colors show, spelled
        out, because color alone is not readable to everyone."""
        from openpyxl.styles import Alignment

        ws = self.wb.create_sheet(safe_title(title, self._used))
        if colour:
            ws.sheet_properties.tabColor = argb(colour)
        ws.append(["Sheet", "Section", "Rows", "Note"])
        for c in range(1, 5):
            cell = ws.cell(row=1, column=c)
            cell.font = self._header_font
            cell.fill = self._header_fill
            cell.alignment = Alignment(vertical="center")
        for name, section, count, note in self.manifest:
            ws.append([name, section, count, coerce(note)])
        ws.freeze_panes = "A2"
        ws.column_dimensions["A"].width = 34
        ws.column_dimensions["B"].width = 22
        ws.column_dimensions["C"].width = 10
        ws.column_dimensions["D"].width = 90
        self.wb.move_sheet(ws, offset=-(len(self.wb.sheetnames) - position))

    # ---------------------------------------------------------------- output
    def to_bytes(self) -> bytes:
        import io

        bio = io.BytesIO()
        self.wb.save(bio)
        return bio.getvalue()
