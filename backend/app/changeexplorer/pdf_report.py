"""Incident-report PDF for a Change Explorer run (feature E1).

Board-ready, provenance-complete: window/scope, headline counts, security flags, suspicious
patterns, the operation timeline, and the top changes with actor + risk. Uses the shared
pure-Python xhtml2pdf/reportlab engine (no system libs), single-pass.
"""
from __future__ import annotations

import io
from typing import Any

from app.core.pdf_common import base_css, esc, esc_breakable, fmt_date, sev_chip


def _risk_color(label: str) -> str:
    return {
        "Critical": "#dc2626", "High": "#ea580c", "Medium": "#d97706",
        "Low": "#2563eb", "Informational": "#64748b",
    }.get(label, "#64748b")


def _chip(text: str, color: str) -> str:
    return (f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
            f'background:{color};color:#fff;font-size:9px;font-weight:bold;">{esc(text)}</span>')


def build_change_report_pdf(run: dict[str, Any]) -> bytes:
    """Render a Change Explorer run into an incident-report PDF (bytes)."""
    from xhtml2pdf import pisa

    name = run.get("workloadName", "Workload")
    window = f"{fmt_date(run.get('startTime'))} → {fmt_date(run.get('endTime'))}"
    sec = run.get("security", {}) or {}
    insights = run.get("insights", []) or []
    suspicious = [i for i in insights if str(i.get("insightType", "")).startswith("suspicious_")]
    operations = run.get("operations", []) or []
    events = run.get("events", []) or []
    top = sorted(events, key=lambda e: -int(e.get("riskScore", 0)))[:25]
    case = run.get("caseFile", {}) or {}
    pinned_ids = set(case.get("pinned", []) or [])

    def counts_row() -> str:
        cells = [
            ("Total", run.get("totalChanges", 0), "#111827"),
            ("Critical", run.get("criticalCount", 0), "#dc2626"),
            ("High", run.get("highCount", 0), "#ea580c"),
            ("Medium", run.get("mediumCount", 0), "#d97706"),
            ("Low", run.get("lowCount", 0), "#2563eb"),
            ("Flagged", sec.get("flagged_changes", 0), "#7c3aed"),
        ]
        tds = "".join(
            f'<td style="text-align:center;padding:6px;border:1px solid #e5e7eb;">'
            f'<div style="font-size:18px;font-weight:bold;color:{c};">{v}</div>'
            f'<div style="font-size:9px;color:#6b7280;text-transform:uppercase;">{esc(l)}</div></td>'
            for l, v, c in cells)
        return f'<table style="width:100%;border-collapse:collapse;margin:8px 0;"><tr>{tds}</tr></table>'

    def suspicious_html() -> str:
        if not suspicious:
            return '<p style="color:#6b7280;">No suspicious patterns detected.</p>'
        rows = "".join(
            f'<div style="margin:4px 0;padding:6px 8px;border-left:3px solid #dc2626;background:#fef2f2;">'
            f'{sev_chip(i.get("severity","Medium"))} <b>{esc(i.get("title",""))}</b>'
            f'<div style="font-size:10px;color:#374151;">{esc(i.get("summary",""))}</div></div>'
            for i in suspicious)
        return rows

    def operations_html() -> str:
        if not operations:
            return '<p style="color:#6b7280;">No grouped operations.</p>'
        rows = []
        for op in operations[:20]:
            rows.append(
                f'<tr>'
                f'<td style="padding:4px;border-bottom:1px solid #eee;font-size:9px;">{esc(fmt_date(op.get("startTime")))}</td>'
                f'<td style="padding:4px;border-bottom:1px solid #eee;">{_chip(op.get("verb","Change"), "#6366f1")}</td>'
                f'<td style="padding:4px;border-bottom:1px solid #eee;font-size:10px;">{esc_breakable(op.get("actor",""))}</td>'
                f'<td style="padding:4px;border-bottom:1px solid #eee;text-align:center;font-size:10px;">{op.get("changeCount",0)}</td>'
                f'<td style="padding:4px;border-bottom:1px solid #eee;text-align:center;">{_chip(op.get("highestRiskLabel","Low"), _risk_color(op.get("highestRiskLabel","Low")))}</td>'
                f'<td style="padding:4px;border-bottom:1px solid #eee;text-align:center;font-size:10px;">{op.get("securityFlagCount",0)}</td>'
                f'</tr>')
        return (
            '<table style="width:100%;border-collapse:collapse;">'
            '<tr style="background:#f9fafb;"><th style="padding:4px;text-align:left;font-size:9px;">Time</th>'
            '<th style="padding:4px;text-align:left;font-size:9px;">Operation</th>'
            '<th style="padding:4px;text-align:left;font-size:9px;">Actor</th>'
            '<th style="padding:4px;font-size:9px;">Changes</th><th style="padding:4px;font-size:9px;">Risk</th>'
            '<th style="padding:4px;font-size:9px;">Flags</th></tr>'
            + "".join(rows) + '</table>')

    def top_changes_html() -> str:
        rows = []
        for e in top:
            pin = "📌 " if e.get("changeId") in pinned_ids else ""
            flags = " ".join(esc(f.get("label", "")) for f in (e.get("securityFlags") or []))
            rows.append(
                f'<tr>'
                f'<td style="padding:4px;border-bottom:1px solid #eee;text-align:center;">{_chip(e.get("riskLabel","Low"), _risk_color(e.get("riskLabel","Low")))} {e.get("riskScore",0)}</td>'
                f'<td style="padding:4px;border-bottom:1px solid #eee;font-size:10px;">{pin}{esc_breakable(e.get("resourceName",""))}'
                f'<div style="font-size:8px;color:#9ca3af;">{esc(e.get("category",""))}{(" · " + flags) if flags else ""}</div></td>'
                f'<td style="padding:4px;border-bottom:1px solid #eee;font-size:9px;">{esc_breakable(e.get("actorDisplay") or e.get("actor",""))}</td>'
                f'<td style="padding:4px;border-bottom:1px solid #eee;font-size:9px;">{esc(fmt_date(e.get("eventTime")))}</td>'
                f'</tr>')
        return (
            '<table style="width:100%;border-collapse:collapse;">'
            '<tr style="background:#f9fafb;"><th style="padding:4px;font-size:9px;">Risk</th>'
            '<th style="padding:4px;text-align:left;font-size:9px;">Resource</th>'
            '<th style="padding:4px;text-align:left;font-size:9px;">Actor</th>'
            '<th style="padding:4px;text-align:left;font-size:9px;">When</th></tr>'
            + "".join(rows) + '</table>')

    def case_html() -> str:
        if not case or (not case.get("caseSummary") and not pinned_ids):
            return ""
        s = esc(case.get("caseSummary", "")) or "(no summary)"
        return (f'<h2>Investigator case notes</h2>'
                f'<div style="padding:8px;background:#fffbeb;border:1px solid #fde68a;">'
                f'<div style="font-size:11px;">{s}</div>'
                f'<div style="font-size:9px;color:#92400e;margin-top:4px;">{len(pinned_ids)} pinned change(s)</div></div>')

    body = f"""
    <h1>Change Explorer — Incident Report</h1>
    <div style="color:#6b7280;font-size:11px;margin-bottom:8px;">
      <b>{esc(name)}</b> · Window: {esc(window)} · Scope: {esc(run.get('scopeMode',''))}
      · Generated by {esc(run.get('requestedBy',''))} · Run {esc(run.get('completedAt',''))}
    </div>
    <p style="font-size:11px;">{esc(run.get('summary',''))}</p>
    {counts_row()}
    {case_html()}
    <h2>Suspicious patterns</h2>
    {suspicious_html()}
    <h2>Operations timeline</h2>
    {operations_html()}
    <h2>Top changes by risk</h2>
    {top_changes_html()}
    <p style="font-size:8px;color:#9ca3af;margin-top:12px;">
      Read-only forensic analysis from Azure Resource Graph, Activity Log and Entra audit logs.
      Identities resolved via Microsoft Graph where available; object-ids shown otherwise.
    </p>
    """
    html_doc = f"<html><head><style>{base_css()}</style></head><body>{body}</body></html>"
    buf = io.BytesIO()
    result = pisa.CreatePDF(src=html_doc, dest=buf, encoding="utf-8")
    if result.err:
        raise RuntimeError(f"PDF generation failed with {result.err} error(s)")
    return buf.getvalue()
