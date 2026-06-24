"""Render a whole chat conversation to a branded PDF (for ticket attachments).

Uses the same pure-Python xhtml2pdf/reportlab engine the assessment & coverage reports use, so
there are no system-library dependencies and output is identical on a Windows dev box and the
Linux container. Light Markdown is rendered (headings, bold/italic/inline-code, fenced code
blocks, bullet lists); everything is HTML-escaped first so chat content can't inject markup.
"""
from __future__ import annotations

import html as _html
import io
import re
from datetime import datetime, timezone
from typing import Any

_BRAND = "#4f46e5"
_INK = "#111827"
_MUTED = "#6b7280"


def _esc(s: Any) -> str:
    return _html.escape(str(s if s is not None else ""))


def _mermaid_png(source: str) -> bytes | None:
    """Best-effort: render a mermaid flowchart to PNG bytes; ``None`` if unsupported/unavailable."""
    try:
        from app.connectors.mermaid_render import render_mermaid_png

        return render_mermaid_png(source)
    except Exception:
        return None


# Diagrams are rendered at 2x; halve to "points" and cap to the printable A4 content width (~500pt).
_DIAGRAM_SCALE = 2
_MAX_DIAGRAM_PT = 500


def _png_display_width_pt(png: bytes) -> int:
    """PNG pixel width is at bytes 16..20 (IHDR). Halve for the 2x render and cap to page width."""
    try:
        px = int.from_bytes(png[16:20], "big") or (_MAX_DIAGRAM_PT * _DIAGRAM_SCALE)
    except Exception:
        return _MAX_DIAGRAM_PT
    return max(80, min(_MAX_DIAGRAM_PT, px // _DIAGRAM_SCALE))


def _inline_md(text: str) -> str:
    """Escape, then apply inline markdown (code, bold, italic, links→text). Input is a single line."""
    t = _esc(text)
    t = re.sub(r"`([^`]+)`", r'<font face="Courier">\1</font>', t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", t)
    t = re.sub(r"\[([^\]]+)\]\((?:[^)]+)\)", r"\1", t)  # [text](url) -> text
    return t


def _md_to_html(md: str) -> str:
    """A small, safe Markdown→HTML subset sufficient for chat transcripts."""
    lines = (md or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    in_code = False
    code_buf: list[str] = []
    code_lang = ""
    in_list = False

    def _close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    def _flush_code() -> None:
        """Emit the buffered fenced block — as a rendered diagram for ```mermaid, else as code."""
        nonlocal code_buf, code_lang
        src = chr(10).join(code_buf)
        if code_lang.lower() == "mermaid":
            png = _mermaid_png(src)
            if png is not None:
                import base64
                b64 = base64.b64encode(png).decode("ascii")
                w_pt = _png_display_width_pt(png)
                out.append(
                    f'<div class="diagram"><img src="data:image/png;base64,{b64}" '
                    f'style="width:{w_pt}pt"/></div>'
                )
                code_buf = []
                code_lang = ""
                return
        out.append(f'<pre class="code">{_esc(src)}</pre>')
        code_buf = []
        code_lang = ""

    def _split_row(line: str) -> list[str]:
        """Split a Markdown table row into trimmed cells, tolerating optional edge pipes."""
        t = line.strip()
        if t.startswith("|"):
            t = t[1:]
        if t.endswith("|"):
            t = t[:-1]
        # Don't split on escaped pipes (\|).
        cells = re.split(r"(?<!\\)\|", t)
        return [c.replace("\\|", "|").strip() for c in cells]

    def _is_separator(line: str) -> bool:
        if "|" not in line and "-" not in line:
            return False
        cells = _split_row(line)
        return bool(cells) and all(re.fullmatch(r":?-{1,}:?", c.strip()) for c in cells if c.strip() != "") and any(c.strip() for c in cells)

    i = 0
    n_lines = len(lines)
    while i < n_lines:
        raw = lines[i]
        if raw.strip().startswith("```"):
            if in_code:
                _flush_code()
                in_code = False
            else:
                _close_list()
                code_lang = raw.strip()[3:].strip()
                in_code = True
            i += 1
            continue
        if in_code:
            code_buf.append(raw)
            i += 1
            continue
        s = raw.rstrip()
        if not s.strip():
            _close_list()
            i += 1
            continue
        # Markdown table: a header row with pipes immediately followed by a separator row.
        if "|" in s and i + 1 < n_lines and _is_separator(lines[i + 1]):
            _close_list()
            headers = _split_row(s)
            rows: list[list[str]] = []
            j = i + 2
            while j < n_lines and "|" in lines[j] and lines[j].strip():
                rows.append(_split_row(lines[j]))
                j += 1
            ncol = len(headers)
            thead = "".join(f"<th>{_inline_md(h)}</th>" for h in headers)
            tbody_rows = []
            for r in rows:
                cells = (r + [""] * ncol)[:ncol]
                tbody_rows.append("<tr>" + "".join(f"<td>{_inline_md(c)}</td>" for c in cells) + "</tr>")
            out.append(
                f'<table class="mdtable" repeat="1"><thead><tr>{thead}</tr></thead>'
                f'<tbody>{"".join(tbody_rows)}</tbody></table>'
            )
            i = j
            continue
        h = re.match(r"^(#{1,6})\s+(.*)$", s)
        if h:
            _close_list()
            lvl = min(len(h.group(1)), 4) + 2  # h3..h6 sizing
            out.append(f'<div class="mdh{lvl}">{_inline_md(h.group(2))}</div>')
            i += 1
            continue
        li = re.match(r"^\s*[-*+]\s+(.*)$", s)
        if li:
            if not in_list:
                out.append('<ul class="md">')
                in_list = True
            out.append(f"<li>{_inline_md(li.group(1))}</li>")
            i += 1
            continue
        _close_list()
        out.append(f'<p class="md">{_inline_md(s)}</p>')
        i += 1

    if in_code:
        _flush_code()
    _close_list()
    return "\n".join(out)


_ROLE = {
    "user": ("You", "#1d4ed8", "#eff6ff"),
    "assistant": ("Assistant", "#4f46e5", "#eef2ff"),
    "system": ("System", "#6b7280", "#f3f4f6"),
}


def build_chat_pdf(
    title: str,
    messages: list[dict[str, Any]],
    *,
    generated_at: datetime | None = None,
) -> bytes:
    """Render the conversation (list of ``{role, content, created_at, model}``) to PDF bytes."""
    now = generated_at or datetime.now(timezone.utc)
    safe_title = _esc(title or "Azure Support Agent conversation")

    blocks: list[str] = []
    for m in messages:
        role = str(m.get("role", ""))
        if role not in ("user", "assistant", "system"):
            continue
        label, color, bg = _ROLE.get(role, (role.title(), _MUTED, "#f3f4f6"))
        ts = m.get("created_at")
        ts_txt = ""
        if isinstance(ts, datetime):
            ts_txt = ts.strftime("%Y-%m-%d %H:%M UTC")
        elif ts:
            ts_txt = _esc(ts)
        model = _esc(m.get("model", "")) if role == "assistant" else ""
        meta = " · ".join([x for x in (ts_txt, model) if x])
        body_html = _md_to_html(str(m.get("content", "")))
        blocks.append(
            f'<table class="turn" style="background:{bg}"><tr><td>'
            f'<div class="who" style="color:{color}">{_esc(label)}'
            f'{f"<span class=meta> · {meta}</span>" if meta else ""}</div>'
            f'<div class="body">{body_html}</div>'
            f"</td></tr></table>"
        )

    doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    @page {{ size: A4; margin: 1.6cm 1.4cm; }}
    body {{ font-family: Helvetica, Arial, sans-serif; color: {_INK}; font-size: 10pt; }}
    .title {{ font-size: 17pt; font-weight: bold; color: {_INK}; }}
    .sub {{ color: {_MUTED}; font-size: 8.5pt; margin-top: 2px; }}
    .rule {{ border-bottom: 2px solid {_BRAND}; margin: 6px 0 12px 0; }}
    .turn {{ width: 100%; margin: 0 0 8px 0; border: 1px solid #e5e7eb; }}
    .turn td {{ padding: 7px 9px; }}
    .who {{ font-weight: bold; font-size: 9.5pt; margin-bottom: 3px; }}
    .meta {{ color: {_MUTED}; font-weight: normal; font-size: 8pt; }}
    .body p.md {{ margin: 3px 0; line-height: 1.35; }}
    .body ul.md {{ margin: 3px 0 3px 14px; }}
    .body li {{ margin: 1px 0; }}
    .mdh3 {{ font-size: 12pt; font-weight: bold; margin: 6px 0 2px; }}
    .mdh4 {{ font-size: 11pt; font-weight: bold; margin: 5px 0 2px; }}
    .mdh5, .mdh6 {{ font-size: 10pt; font-weight: bold; margin: 4px 0 2px; }}
    pre.code {{ background: #0f172a; color: #e2e8f0; font-family: Courier; font-size: 8.5pt;
                padding: 7px 9px; margin: 4px 0; white-space: pre-wrap; }}
    .diagram {{ margin: 6px 0; text-align: center; }}
    .diagram img {{ max-width: 100%; }}
    table.mdtable {{ width: 100%; border-collapse: collapse; margin: 6px 0; font-size: 8.5pt; }}
    table.mdtable th {{ background: #eef2ff; color: {_INK}; text-align: left; font-weight: bold;
                        border: 1px solid #c7d2fe; padding: 4px 6px; }}
    table.mdtable td {{ border: 1px solid #e5e7eb; padding: 4px 6px; vertical-align: top; }}
    .footer {{ color: {_MUTED}; font-size: 8pt; margin-top: 10px; }}
    </style></head><body>
    <div class="title">{safe_title}</div>
    <div class="sub">Azure Support Agent · exported {now.strftime('%Y-%m-%d %H:%M UTC')} · {len(blocks)} message(s)</div>
    <div class="rule"></div>
    {''.join(blocks) if blocks else '<p class="md">(no messages)</p>'}
    <div class="footer">Generated by Azure Support Agent.</div>
    </body></html>"""

    from xhtml2pdf import pisa

    buf = io.BytesIO()
    result = pisa.CreatePDF(src=doc, dest=buf, encoding="utf-8")
    if result.err:
        raise RuntimeError(f"Chat PDF generation failed with {result.err} error(s).")
    return buf.getvalue()
