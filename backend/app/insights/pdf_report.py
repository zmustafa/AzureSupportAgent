"""Board-ready PDF for a single Insight Pack digest.

Renders a persisted run (verdict, headline, bullets, findings table, sources, materiality
gate) into a branded PDF via the shared pure-Python ``xhtml2pdf``/reportlab engine
(``app.core.pdf_common`` — no system libraries, identical output on Windows and Linux).
"""
from __future__ import annotations

import io
from typing import Any

from app.core.pdf_common import base_css, esc, esc_breakable, fmt_date

_VERDICT = {
    "urgent": ("Urgent", "#dc2626"),
    "notable": ("Notable", "#d97706"),
    "nothing_notable": ("Nothing notable", "#2563eb"),
}
_RISK = {
    "critical": "#b91c1c", "high": "#dc2626", "medium": "#d97706", "low": "#2563eb",
}


def _chip(text: str, color: str) -> str:
    return (f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
            f'background:{color};color:#fff;font-size:9px;font-weight:bold;">{esc(text)}</span>')


def build_insight_pdf(run: dict[str, Any]) -> bytes:
    """Render one Insight Pack run into a digest PDF (bytes)."""
    from xhtml2pdf import pisa

    verdict = str(run.get("verdict") or "nothing_notable")
    vlabel, vcolor = _VERDICT.get(verdict, _VERDICT["nothing_notable"])
    counts = run.get("counts") or {}
    flags = counts.get("flags") or []
    bullets = run.get("bullets") or []
    table = run.get("table") or []
    sources = run.get("sources") or []

    def bullets_html() -> str:
        if not bullets:
            return '<p style="color:#6b7280;font-size:11px;">No narrative bullets for this window.</p>'
        items = "".join(f"<li style='margin:3px 0;'>{esc(b)}</li>" for b in bullets[:20])
        return f'<ul style="font-size:11px;padding-left:16px;">{items}</ul>'

    def table_html() -> str:
        if not table:
            return '<p style="color:#6b7280;font-size:11px;">No individual findings tabulated.</p>'
        head = (
            '<tr style="background:#f3f4f6;">'
            '<th style="text-align:left;padding:5px;border:1px solid #e5e7eb;">When</th>'
            '<th style="text-align:left;padding:5px;border:1px solid #e5e7eb;">Change</th>'
            '<th style="text-align:left;padding:5px;border:1px solid #e5e7eb;">Risk</th>'
            '<th style="text-align:left;padding:5px;border:1px solid #e5e7eb;">Owner</th>'
            '<th style="text-align:left;padding:5px;border:1px solid #e5e7eb;">Recommended action</th></tr>'
        )
        rows = []
        for r in table[:60]:
            risk = str(r.get("risk") or "low").lower()
            rc = _RISK.get(risk, "#64748b")
            change = esc(r.get("change", ""))
            wl = r.get("workload")
            if wl:
                change += f'<br/><span style="color:#9ca3af;font-size:9px;">{esc_breakable(wl)}</span>'
            rows.append(
                '<tr>'
                f'<td style="padding:5px;border:1px solid #e5e7eb;font-size:9px;color:#6b7280;white-space:nowrap;">{esc(r.get("time",""))}</td>'
                f'<td style="padding:5px;border:1px solid #e5e7eb;font-size:10px;">{change}</td>'
                f'<td style="padding:5px;border:1px solid #e5e7eb;">{_chip(risk.title(), rc)}</td>'
                f'<td style="padding:5px;border:1px solid #e5e7eb;font-size:9px;color:#4b5563;">{esc(r.get("owner",""))}</td>'
                f'<td style="padding:5px;border:1px solid #e5e7eb;font-size:10px;">{esc(r.get("recommended_action",""))}</td>'
                '</tr>'
            )
        return (f'<table style="width:100%;border-collapse:collapse;margin:6px 0;">'
                f'{head}{"".join(rows)}</table>')

    def sources_html() -> str:
        if not sources:
            return ""
        chips = " ".join(
            _chip(f'{s.get("source","?")} · {"ok" if s.get("ok") else "partial"}',
                  "#16a34a" if s.get("ok") else "#9ca3af")
            for s in sources
        )
        return f'<div style="margin:6px 0;">{chips}</div>'

    flag_chips = " ".join(_chip(str(f), "#7c3aed") for f in flags[:12]) if flags else ""

    body = f"""
    <h1>{esc(run.get('pack_icon',''))} {esc(run.get('pack_name','Insight Pack'))}</h1>
    <div style="margin:4px 0 8px 0;">{_chip(vlabel, vcolor)}
      {'&nbsp;' + _chip('Notified', '#4f46e5') if run.get('notified') else ''}
      {'&nbsp;' + _chip('False positive', '#6b7280') if run.get('false_positive') else ''}</div>
    <div style="color:#6b7280;font-size:11px;margin-bottom:8px;">
      <b>{esc(run.get('scope_label',''))}</b> · Last {esc(run.get('lookback_hours',''))}h
      · {esc(counts.get('changes', 0))} change(s)
      {('· ' + str(len(flags)) + ' security flag(s)') if flags else ''}
      · Generated {fmt_date(run.get('created_at'))}
    </div>
    <p style="font-size:13px;font-weight:bold;color:#111827;">{esc(run.get('headline',''))}</p>
    {('<div style="margin:4px 0;">' + flag_chips + '</div>') if flag_chips else ''}
    <h2>Summary</h2>
    {bullets_html()}
    <h2>Findings</h2>
    {table_html()}
    <h2>Provenance</h2>
    {sources_html()}
    <p style="font-size:10px;color:#6b7280;">Materiality gate: {esc(run.get('gate_reason','') or '\u2014')}</p>
    <p style="font-size:8px;color:#9ca3af;margin-top:12px;">
      Read-only Insight Pack digest. Interpretation assisted by the configured LLM; the
      notify/suppress decision is a deterministic materiality gate.
    </p>
    """
    html_doc = f"<html><head><style>{base_css()}</style></head><body>{body}</body></html>"
    buf = io.BytesIO()
    result = pisa.CreatePDF(src=html_doc, dest=buf, encoding="utf-8")
    if result.err:
        raise RuntimeError(f"PDF generation failed with {result.err} error(s)")
    return buf.getvalue()
