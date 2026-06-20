"""Shared, report-agnostic PDF primitives factored out of ``app.assessments.pdf_report``.

The assessment report (``app/assessments/pdf_report.py``) and the coverage reports
(``app/core/coverage_pdf.py``) render branded PDFs via ``xhtml2pdf`` (pure-Python, built
on reportlab — no system libraries, so identical output on a Windows dev box and the Linux
container). This module holds the pieces both builders share:

* the brand palette + severity colors,
* HTML/SVG escaping helpers (defense-in-depth against injection into the SVG/HTML),
* small chart primitives (progress bar, stacked bar, donut, sparkline, viz card),
* a two-pass renderer that resolves a clickable table-of-contents with real page numbers.

Each builder keeps its own page CSS + section composition; only these primitives are shared.
"""
from __future__ import annotations

import base64
import html
import io
import math
import re
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------- palette / labels

BRAND = "#4f46e5"
INK = "#111827"
MUTED = "#6b7280"
LINE = "#e5e7eb"

SEV_RANK = {"critical": 0, "error": 1, "high": 1, "warning": 2, "medium": 2, "info": 3, "low": 3}
SEV_COLOR = {
    "critical": "#b91c1c",
    "error": "#dc2626",
    "high": "#dc2626",
    "warning": "#d97706",
    "medium": "#d97706",
    "info": "#2563eb",
    "low": "#2563eb",
}
# Canonical 4-bucket severity vocabulary used by the donut + grouped tables.
SEV_ORDER = ("critical", "error", "warning", "info")


def normalize_severity(value: Any) -> str:
    """Collapse the various severity vocabularies (high/medium/low, etc.) to one of
    ``critical | error | warning | info`` so charts and groupings are consistent."""
    s = str(value or "").strip().lower()
    if s in ("critical", "crit"):
        return "critical"
    if s in ("error", "high", "fail", "failed"):
        return "error"
    if s in ("warning", "warn", "medium", "moderate"):
        return "warning"
    return "info"


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def esc_breakable(value: Any, *, width: int = 18) -> str:
    """Escape an identifier and hard-wrap it at separator boundaries (``- . / _``) so a long
    token like ``rg-002-shoppingsite-demo-eus2`` cannot overflow its narrow table column.

    xhtml2pdf/reportlab won't reliably break such tokens on their own: a zero-width space
    renders as a tofu box in the core Helvetica font, and a soft hyphen is only a
    *discretionary* break the engine often declines to use under fixed column widths. So we
    insert explicit ``<br/>`` breaks ourselves, greedily keeping each line within ``width``
    characters. Strings already short enough are returned unwrapped (no ``<br/>``)."""
    s = "" if value is None else str(value)
    if len(s) <= width:
        return esc(s)
    # Chunks that each keep a trailing separator, so breaks land *after* the separator.
    chunks = [c for c in re.findall(r"[^\-._/]*[\-._/]?", s) if c]
    lines: list[str] = []
    cur = ""
    for c in chunks:
        if cur and len(cur) + len(c) > width:
            lines.append(cur)
            cur = c
        else:
            cur += c
    if cur:
        lines.append(cur)
    return "<br/>".join(esc(ln) for ln in lines)


def svg_attr(value: Any) -> str:
    """Escape a string for safe insertion into an SVG attribute or text node
    (``quote=True`` so single + double quotes are escaped too)."""
    return html.escape("" if value is None else str(value), quote=True)


_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def svg_color(value: str, *, fallback: str = "#9ca3af") -> str:
    """Return a safe hex color literal, falling back to neutral grey on mismatch."""
    if isinstance(value, str) and _HEX_COLOR.match(value.strip()):
        return value.strip()
    return fallback


def score_color(score: Any) -> str:
    try:
        s = float(score)
    except (TypeError, ValueError):
        return MUTED
    if s >= 80:
        return "#16a34a"
    if s >= 50:
        return "#d97706"
    return "#dc2626"


def fmt_date(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y, %H:%M UTC")
    except (ValueError, AttributeError):
        return esc(iso)


def fmt_duration(ms: Any) -> str:
    try:
        secs = int(ms) / 1000.0
    except (TypeError, ValueError):
        return "—"
    if secs < 60:
        return f"{secs:.0f}s"
    return f"{int(secs // 60)}m {int(secs % 60)}s"


# ---------------------------------------------------------------- chart helpers


def bar(pct: Any, color: str, *, total: int = 168) -> str:
    """A horizontal progress bar using explicit cell widths that xhtml2pdf paints reliably.

    Cells carry ``padding:0`` so the thin remainder cell at high percentages (e.g. 95%)
    never collapses below xhtml2pdf's default cell padding into a negative content width
    (which raises ``flowable given negative availWidth`` and aborts the whole PDF).
    """
    try:
        width = max(0.0, min(100.0, float(pct)))
    except (TypeError, ValueError):
        width = 0.0
    filled = int(round(total * width / 100.0))
    if 0 < width < 100:
        filled = max(1, min(total - 1, filled))
    color = svg_color(color, fallback=BRAND)
    cell = 'style="background-color:{bg}; height:9px; padding:0; margin:0"'
    if width <= 0:
        return f'<table class="bartracktbl" width="{total}" cellpadding="0" cellspacing="0"><tr><td width="{total}" {cell.format(bg=LINE)}>&nbsp;</td></tr></table>'
    if width >= 100:
        return f'<table class="bartracktbl" width="{total}" cellpadding="0" cellspacing="0"><tr><td width="{total}" {cell.format(bg=color)}>&nbsp;</td></tr></table>'
    return (
        f'<table class="bartracktbl" width="{total}" cellpadding="0" cellspacing="0"><tr>'
        f'<td width="{filled}" {cell.format(bg=color)}>&nbsp;</td>'
        f'<td width="{total - filled}" {cell.format(bg=LINE)}>&nbsp;</td>'
        f'</tr></table>'
    )


def stacked_bar(segments: list[tuple[float, str]], *, total: int = 420) -> str:
    """A single stacked bar from (pct, color) segments rendered as one table row."""
    cells = [
        f'<td width="{max(1, int(round(total * max(0.0, pct) / 100.0)))}" style="background-color:{svg_color(color)}; height:14px">&nbsp;</td>'
        for pct, color in segments
        if pct and pct > 0
    ]
    if not cells:
        cells = [f'<td width="{total}" style="background-color:{LINE}; height:14px">&nbsp;</td>']
    return '<table class="stack" cellpadding="0" cellspacing="0"><tr>' + "".join(cells) + "</tr></table>"


def svg_data_uri(svg: str) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def swatch(color: str) -> str:
    """A small colored legend marker, rendered as a colored bullet glyph (U+2022, which is
    in the base-14 WinAnsi font set so it always paints). Inline text avoids the negative
    cell-width crash that an ultra-narrow swatch *column* triggers under xhtml2pdf's default
    6px cell padding inside ``KeepInFrame`` blocks."""
    return f'<font color="{svg_color(color)}">\u2022</font>'


def donut_svg(slices: list[tuple[str, float]], *, center: str, accent: str) -> str:
    """A donut chart from (color, value) slices with a centered total + caption."""
    total = sum(max(0.0, float(value)) for _, value in slices)
    size = 170
    cx = cy = 85
    radius = 56
    circumference = 2 * math.pi * radius
    accent_safe = svg_color(accent, fallback="#2563eb")
    center_safe = svg_attr(center)
    if total <= 0:
        return f"""
        <svg xmlns='http://www.w3.org/2000/svg' width='{size}' height='{size}' viewBox='0 0 {size} {size}'>
          <circle cx='{cx}' cy='{cy}' r='{radius}' fill='none' stroke='#e5e7eb' stroke-width='18'/>
          <circle cx='{cx}' cy='{cy}' r='34' fill='white'/>
          <text x='{cx}' y='90' text-anchor='middle' font-family='Helvetica, Arial, sans-serif' font-size='20' fill='{accent_safe}' font-weight='700'>0</text>
        </svg>
        """

    offset = 0.0
    pieces: list[str] = []
    for color, value in slices:
        value = max(0.0, float(value))
        if value <= 0:
            continue
        length = circumference * value / total
        gap = max(0.0, circumference - length)
        color_safe = svg_color(color)
        pieces.append(
            f"<circle cx='{cx}' cy='{cy}' r='{radius}' fill='none' stroke='{color_safe}' stroke-width='18' "
            f"stroke-linecap='butt' stroke-dasharray='{length:.3f} {gap:.3f}' stroke-dashoffset='{-offset:.3f}' "
            f"transform='rotate(-90 {cx} {cy})'/>")
        offset += length

    return f"""
    <svg xmlns='http://www.w3.org/2000/svg' width='{size}' height='{size}' viewBox='0 0 {size} {size}'>
      <circle cx='{cx}' cy='{cy}' r='{radius}' fill='none' stroke='#e5e7eb' stroke-width='18'/>
      {''.join(pieces)}
      <circle cx='{cx}' cy='{cy}' r='34' fill='white'/>
      <text x='{cx}' y='92' text-anchor='middle' font-family='Helvetica, Arial, sans-serif' font-size='26' fill='{accent_safe}' font-weight='700'>{svg_attr(str(int(total)))}</text>
    </svg>
    """


def sparkline_svg(points: list[float], *, color: str = BRAND, width: int = 760, height: int = 120) -> str:
    """A 0-100 line chart for a coverage %-over-time trend. ``points`` are y-values in
    0..100, oldest-first; ``None`` entries are skipped. Defaults are sized to fill the
    report's content width so the chart isn't stranded in the left half of its box."""
    vals = [float(p) for p in points if isinstance(p, (int, float))]
    color_safe = svg_color(color, fallback=BRAND)
    pad = 10
    if len(vals) < 2:
        return f"""
        <svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' viewBox='0 0 {width} {height}'>
          <rect x='0' y='0' width='{width}' height='{height}' fill='#f9fafb'/>
          <text x='{width // 2}' y='{height // 2 + 4}' text-anchor='middle' font-family='Helvetica, Arial, sans-serif' font-size='12' fill='#9ca3af'>Not enough history to chart a trend yet</text>
        </svg>
        """
    n = len(vals)
    inner_w = width - 2 * pad
    inner_h = height - 2 * pad
    step = inner_w / (n - 1)

    def _x(i: int) -> float:
        return pad + i * step

    def _y(v: float) -> float:
        v = max(0.0, min(100.0, v))
        return pad + inner_h * (1.0 - v / 100.0)

    pts = " ".join(f"{_x(i):.1f},{_y(v):.1f}" for i, v in enumerate(vals))
    area_pts = f"{_x(0):.1f},{height - pad:.1f} " + pts + f" {_x(n - 1):.1f},{height - pad:.1f}"
    # Gridlines + right-edge labels at 0/50/100%.
    grid = "".join(
        f"<line x1='{pad}' y1='{_y(g):.1f}' x2='{width - pad}' y2='{_y(g):.1f}' stroke='#e5e7eb' stroke-width='0.6'/>"
        f"<text x='{width - pad + 2}' y='{_y(g) + 3:.1f}' font-family='Helvetica, Arial, sans-serif' font-size='8' fill='#9ca3af'>{g}</text>"
        for g in (0, 50, 100)
    )
    dots = "".join(f"<circle cx='{_x(i):.1f}' cy='{_y(v):.1f}' r='1.8' fill='{color_safe}'/>" for i, v in enumerate(vals))
    last_x, last_y = _x(n - 1), _y(vals[-1])
    return f"""
    <svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' viewBox='0 0 {width} {height}'>
      <rect x='0' y='0' width='{width}' height='{height}' fill='#ffffff'/>
      {grid}
      <polygon points='{area_pts}' fill='{color_safe}' fill-opacity='0.08'/>
      <polyline points='{pts}' fill='none' stroke='{color_safe}' stroke-width='2.0'/>
      {dots}
      <circle cx='{last_x:.1f}' cy='{last_y:.1f}' r='3.2' fill='{color_safe}'/>
    </svg>
    """


def viz_card(title: str, subtitle: str, svg: str, legend_rows: list[tuple[str, str, str]]) -> str:
    legend = "".join(
        f"<tr><td class='viz-lb'>{swatch(color)}&nbsp;{esc(label)}</td><td class='num'>{esc(value)}</td></tr>"
        for label, value, color in legend_rows
    )
    return f"""
    <div class="viz-card">
      <div class="viz-title">{esc(title)}</div>
      <div class="viz-sub">{esc(subtitle)}</div>
      <div class="viz-body">
        <img class="viz-img" src="{svg_data_uri(svg)}" alt="{esc(title)}" />
        <table class="viz-legend" cellpadding="0" cellspacing="0">{legend}</table>
      </div>
    </div>
    """


def chip(text: str, color: str) -> str:
    return f'<span style="color:{svg_color(color, fallback=MUTED)}; font-weight:bold">{esc(text)}</span>'


def sev_chip(sev: str) -> str:
    s = normalize_severity(sev)
    return chip(s.upper(), SEV_COLOR.get(s, MUTED))


# ---------------------------------------------------------------- shared CSS + renderer


def base_css() -> str:
    """The report-agnostic CSS shared by the assessment + coverage builders: page frames,
    headings/outline, running header/footer, cover, TOC, KPIs, viz cards, bars and grids."""
    return f"""
@page {{
  size: a4 portrait;
  margin: 2.3cm 1.5cm 1.7cm 1.5cm;
  @frame header_frame {{ -pdf-frame-content: headerContent; top: 0.7cm; left: 1.5cm; width: 18cm; height: 1.1cm; }}
  @frame footer_frame {{ -pdf-frame-content: footerContent; bottom: 0.7cm; left: 1.5cm; width: 18cm; height: 0.9cm; }}
}}
body {{ font-family: Helvetica, sans-serif; font-size: 9.5px; color: {INK}; line-height: 1.45; }}
h1 {{ font-size: 17px; color: {BRAND}; margin: 0 0 8px 0; padding-bottom: 4px; border-bottom: 2px solid {BRAND};
      -pdf-outline: true; -pdf-outline-level: 0; -pdf-outline-open: false; -pdf-keep-with-next: true; }}
h2 {{ font-size: 12.5px; color: {INK}; margin: 12px 0 5px 0;
      -pdf-outline: true; -pdf-outline-level: 1; -pdf-outline-open: false; -pdf-keep-with-next: true; }}
h3 {{ font-size: 10.5px; color: {MUTED}; margin: 11px 0 4px 0; text-transform: uppercase; letter-spacing: 0.5px;
    -pdf-outline: true; -pdf-outline-level: 2; -pdf-outline-open: false; -pdf-keep-with-next: true; }}
p {{ margin: 0 0 5px 0; }}
.muted {{ color: {MUTED}; font-size: 9px; }}
a {{ color: {BRAND}; text-decoration: none; }}
.pagebreak {{ page-break-before: always; }}

/* running header / footer */
#headerContent {{ font-size: 8px; color: {MUTED}; border-bottom: 0.5px solid {LINE}; }}
#headerContent .brand {{ color: {BRAND}; font-weight: bold; font-size: 9px; }}
#footerContent {{ font-size: 8px; color: {MUTED}; border-top: 0.5px solid {LINE}; text-align: center; }}

/* cover */
.cover {{ margin-top: 3.5cm; }}
.cover-hero {{ width: 18cm; margin-bottom: 16px; }}
.cover-left {{ width: 11.5cm; vertical-align: top; }}
.cover-right {{ width: 6.5cm; vertical-align: top; }}
.cover-brand {{ font-size: 30px; font-weight: bold; color: {BRAND}; }}
.cover-sub {{ font-size: 15px; color: {INK}; margin-top: 2px; }}
.cover-pack {{ font-size: 11px; color: {MUTED}; margin: 2px 0 22px 0; }}
.cover-summary {{ font-size: 10px; color: {INK}; line-height: 1.5; padding-right: 20px; }}
.cover-meta {{ width: 18cm; margin-bottom: 18px; border-top: 0.5px solid {LINE}; border-bottom: 0.5px solid {LINE}; }}
.cover-meta .k {{ color: {MUTED}; width: 14%; padding: 6px 0; font-size: 9px; }}
.cover-meta .v {{ color: {INK}; font-weight: bold; padding: 6px 10px 6px 0; font-size: 9px; }}
.cover-score-box {{ border: 0.5px solid {LINE}; padding: 12px 12px 10px 12px; }}
.cover-score-num {{ font-size: 46px; font-weight: bold; }}
.cover-score-unit {{ font-size: 16px; color: {MUTED}; font-weight: normal; }}
.cover-score-lbl {{ font-size: 10px; color: {MUTED}; margin-bottom: 6px; }}
.cover-includes {{ width: 18cm; margin-top: 10px; border: 0.5px solid {LINE}; }}
.cover-includes td {{ font-size: 8.5px; padding: 6px 8px; color: {INK}; }}
.cover-foot {{ margin-top: 26px; font-size: 8.5px; color: {MUTED}; }}

/* table of contents */
.toc-title {{ font-size: 18px; font-weight: bold; color: {BRAND}; margin-bottom: 4px; }}
.toc-note {{ font-size: 9px; color: {MUTED}; margin-bottom: 10px; }}
.toc-table {{ width: 18cm; border-top: 0.5px solid {LINE}; border-bottom: 0.5px solid {LINE}; }}
.toc-row td {{ padding: 7px 4px; border-bottom: 0.5px solid {LINE}; font-size: 10px; }}
.toc-row.level-1 td:first-child {{ padding-left: 14px; }}
.toc-link {{ color: {INK}; }}
.toc-page {{ width: 1.5cm; text-align: right; color: {MUTED}; }}

/* visual snapshot */
.viz-grid {{ width: 100%; }}
.viz-grid td {{ width: 50%; vertical-align: top; padding: 0 4px 8px 0; }}
.viz-card {{ border: 0.5px solid {LINE}; border-radius: 4px; padding: 8px; page-break-inside: avoid; }}
.viz-title {{ font-size: 10.5px; font-weight: bold; color: {INK}; margin-bottom: 2px; }}
.viz-sub {{ font-size: 8px; color: {MUTED}; margin-bottom: 5px; }}
.viz-body {{ display: block; }}
.viz-img {{ display: block; width: 150px; margin: 0 auto 5px auto; }}
.viz-legend {{ width: 100%; font-size: 8.5px; }}
.viz-legend td {{ padding: 1.5px 2px; border-bottom: 0.5px solid {LINE}; vertical-align: middle; }}
.viz-legend .viz-lb {{ padding-left: 1px; }}

/* bars */
.bartracktbl {{ }}
.stack {{ width: 420px; height: 14px; }}
.stack td {{ padding: 0; }}
.legend {{ font-size: 8.5px; color: {MUTED}; margin-top: 4px; }}

/* kpis */
.kpis {{ width: 100%; margin: 4px 0 6px 0; }}
.kpi {{ width: 16.6%; text-align: center; border: 0.5px solid {LINE}; padding: 6px 2px; }}
.kpi-num {{ font-size: 17px; font-weight: bold; }}
.kpi-lbl {{ font-size: 7.5px; color: {MUTED}; }}
.kpi-meta {{ font-size: 8.5px; color: {MUTED}; margin-top: 3px; }}
.narrative p {{ font-size: 9.5px; }}

/* generic grids */
.grid {{ width: 100%; margin-top: 4px; }}
.grid th {{ background-color: #f3f4f6; color: {MUTED}; text-align: left; font-size: 8px; padding: 4px;
            border-bottom: 0.5px solid {LINE}; }}
.grid td {{ padding: 4px; border-bottom: 0.5px solid {LINE}; font-size: 8.5px; vertical-align: top;
            word-wrap: break-word; }}
.grid.compact td {{ font-size: 8px; padding: 3px 4px; }}
.grid .num {{ text-align: right; }}
.num {{ text-align: right; }}

/* metadata */
.meta {{ width: 100%; }}
.meta .k {{ width: 30%; color: {MUTED}; padding: 4px; border-bottom: 0.5px solid {LINE}; }}
.meta .v {{ color: {INK}; padding: 4px; border-bottom: 0.5px solid {LINE}; }}
"""


def running_frames(header_html: str, footer_html: str) -> str:
    """The header/footer frame content blocks injected once at the top of <body>."""
    return (
        f'<div id="headerContent">{header_html}</div>'
        f'<div id="footerContent">{footer_html}</div>'
    )


def render_two_pass(
    shell_fn: Callable[[str], str],
    compose_fn: Callable[[dict[str, int] | None], str],
    toc_entries: Iterable[tuple[str, str, int]],
) -> bytes:
    """Render HTML to PDF twice so the table of contents can show real page numbers.

    * ``compose_fn(page_map)`` returns the document body. On the first pass it is called
      with ``None`` (TOC shows a placeholder); on the second with a resolved
      ``{anchor: page_number}`` map.
    * ``shell_fn(body)`` wraps the body in the full ``<html>`` document (CSS + frames).
    * ``toc_entries`` is an iterable of ``(anchor, label, level)`` — the label text is
      located in the first-pass output to discover each section's page number.
    """
    from pypdf import PdfReader  # local imports keep heavy deps off the hot path
    from xhtml2pdf import pisa

    def _render(html_doc: str) -> bytes:
        buf = io.BytesIO()
        result = pisa.CreatePDF(src=html_doc, dest=buf, encoding="utf-8")
        if result.err:
            raise RuntimeError(f"PDF generation failed with {result.err} error(s)")
        return buf.getvalue()

    entries = list(toc_entries)
    first_pass = _render(shell_fn(compose_fn(None)))
    reader = PdfReader(io.BytesIO(first_pass))
    page_map: dict[str, int] = {}
    for anchor, label, _level in entries:
        for i, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            if label in text:
                page_map[anchor] = i
                break
    return _render(shell_fn(compose_fn(page_map)))
