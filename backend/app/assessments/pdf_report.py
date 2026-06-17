"""Branded PDF report generation for a single assessment run.

Renders the same enriched run payload that powers the JSON/CSV export (see
``app.api.assessments.export_run_endpoint``) into a paginated, branded PDF via
``xhtml2pdf`` (pure-Python, built on reportlab — no system libraries, so it runs
identically on Windows dev boxes and the Linux app container).

Document layout (front-loaded summary, full detail in the appendix):

1. Cover            — "Azure Support Agent", workload, date performed, overall score.
2. Table of contents — clickable internal links with page numbers (plus a navigable PDF outline).
3. Executive summary — one page: score, KPIs, severity mix, AI narrative, top risks.
4. Score overview    — per-pillar scores + compliance-framework coverage bars.
5. Findings & recommendations — failing controls as detail cards with remediation,
                       remaining controls in a compact per-pillar table.
6. Appendix A        — full compliance control matrices (every framework).
7. Appendix B        — every flagged resource for every failing control (no cap).
8. Appendix C        — full scanned-resource inventory.
9. Appendix D        — run metadata & methodology.

A running header (brand · workload · date) and footer (page x of y) repeat on
every page via xhtml2pdf static ``@frame`` blocks.
"""
from __future__ import annotations

import html
import io
import base64
import math
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from pypdf import PdfReader

from app.assessments import catalog

# ---------------------------------------------------------------- palette / labels

_BRAND = "#4f46e5"
_INK = "#111827"
_MUTED = "#6b7280"
_LINE = "#e5e7eb"

_SEV_RANK = {"critical": 0, "error": 1, "warning": 2, "info": 3}
_SEV_COLOR = {
    "critical": "#b91c1c",
    "error": "#dc2626",
    "warning": "#d97706",
    "info": "#2563eb",
}
_STATUS_COLOR = {
    "fail": "#dc2626",
    "error": "#b91c1c",
    "pass": "#16a34a",
    "not_applicable": "#9ca3af",
    "manual": "#2563eb",
    "waived": "#7c3aed",
}
_STATUS_LABEL = {
    "fail": "FAIL",
    "error": "ERROR",
    "pass": "PASS",
    "not_applicable": "N/A",
    "manual": "MANUAL",
    "waived": "WAIVED",
}
_FAILING = ("fail", "error")


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _svg_attr(value: Any) -> str:
    """Escape a string for safe insertion into an SVG attribute or text node.

    Uses ``html.escape(quote=True)`` so single + double quotes are escaped as
    well — important when the value lands inside ``'…'`` or ``"…"`` attribute
    delimiters in the SVG fragments below. Defense-in-depth: today every
    user-controlled field that reaches the SVG already passes through ``_esc``,
    but using a dedicated helper makes future changes explicit and prevents a
    silent regression from introducing SVG injection.
    """
    return html.escape("" if value is None else str(value), quote=True)


# Hex color pattern used to reject anything that doesn't look like a `#rrggbb` or
# `#rgb` literal before it's inlined into an SVG stroke/fill. The PDF builder only
# ever passes hardcoded hex constants today; the guard makes that an enforced
# invariant rather than a convention.
_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _svg_color(value: str, *, fallback: str = "#9ca3af") -> str:
    """Return a safe hex color literal, falling back to neutral grey on mismatch."""
    if isinstance(value, str) and _HEX_COLOR.match(value.strip()):
        return value.strip()
    return fallback


def _score_color(score: Any) -> str:
    try:
        s = float(score)
    except (TypeError, ValueError):
        return _MUTED
    if s >= 80:
        return "#16a34a"
    if s >= 50:
        return "#d97706"
    return "#dc2626"


def _fmt_date(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y, %H:%M UTC")
    except (ValueError, AttributeError):
        return _esc(iso)


def _fmt_duration(ms: Any) -> str:
    try:
        secs = int(ms) / 1000.0
    except (TypeError, ValueError):
        return "—"
    if secs < 60:
        return f"{secs:.0f}s"
    return f"{int(secs // 60)}m {int(secs % 60)}s"


# ---------------------------------------------------------------- chart helpers (CSS-only)


def _bar(pct: Any, color: str) -> str:
    """A horizontal progress bar using explicit cell widths that xhtml2pdf paints reliably."""
    try:
        width = max(0.0, min(100.0, float(pct)))
    except (TypeError, ValueError):
        width = 0.0
    total = 168
    filled = int(round(total * width / 100.0))
    if 0 < width < 100:
        filled = max(1, min(total - 1, filled))
    if width <= 0:
        return f'<table class="bartracktbl" cellpadding="0" cellspacing="0"><tr><td width="{total}" style="background-color:{_LINE}; height:9px">&nbsp;</td></tr></table>'
    if width >= 100:
        return f'<table class="bartracktbl" cellpadding="0" cellspacing="0"><tr><td width="{total}" style="background-color:{color}; height:9px">&nbsp;</td></tr></table>'
    return (
        f'<table class="bartracktbl" cellpadding="0" cellspacing="0"><tr>'
        f'<td width="{filled}" style="background-color:{color}; height:9px">&nbsp;</td>'
        f'<td width="{total - filled}" style="background-color:{_LINE}; height:9px">&nbsp;</td>'
        f'</tr></table>'
    )


def _stacked_bar(segments: list[tuple[float, str]]) -> str:
    """A single stacked bar from (pct, color) segments rendered as one table row."""
    total = 420
    cells = [
        f'<td width="{max(1, int(round(total * max(0.0, pct) / 100.0)))}" style="background-color:{color}; height:14px">&nbsp;</td>'
        for pct, color in segments
        if pct and pct > 0
    ]
    if not cells:
        cells = [f'<td width="{total}" style="background-color:#e5e7eb; height:14px">&nbsp;</td>']
    return (
        '<table class="stack" cellpadding="0" cellspacing="0"><tr>'
        + "".join(cells)
        + "</tr></table>"
    )


def _svg_data_uri(svg: str) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def _donut_svg(slices: list[tuple[str, float]], *, center: str, accent: str) -> str:
    total = sum(max(0.0, float(value)) for _, value in slices)
    size = 170
    cx = cy = 85
    radius = 56
    circumference = 2 * math.pi * radius
    # Defense-in-depth: every dynamic string that lands inside an SVG attribute is
    # escaped (`_svg_attr`) or color-validated (`_svg_color`) here, even though
    # current callers only pass hardcoded values. Prevents a silent regression
    # from a future caller passing user-controlled data into the chart.
    accent_safe = _svg_color(accent, fallback="#2563eb")
    center_safe = _svg_attr(center)
    if total <= 0:
        return f"""
        <svg xmlns='http://www.w3.org/2000/svg' width='{size}' height='{size}' viewBox='0 0 {size} {size}'>
          <circle cx='{cx}' cy='{cy}' r='{radius}' fill='none' stroke='#e5e7eb' stroke-width='18'/>
          <circle cx='{cx}' cy='{cy}' r='33' fill='white'/>
          <text x='{cx}' y='82' text-anchor='middle' font-family='Helvetica, Arial, sans-serif' font-size='14' fill='{accent_safe}' font-weight='700'>0</text>
          <text x='{cx}' y='100' text-anchor='middle' font-family='Helvetica, Arial, sans-serif' font-size='8' fill='#6b7280'>{center_safe}</text>
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
        color_safe = _svg_color(color)
        pieces.append(
            f"<circle cx='{cx}' cy='{cy}' r='{radius}' fill='none' stroke='{color_safe}' stroke-width='18' "
            f"stroke-linecap='butt' stroke-dasharray='{length:.3f} {gap:.3f}' stroke-dashoffset='{-offset:.3f}' "
            f"transform='rotate(-90 {cx} {cy})'/>")
        offset += length

    return f"""
    <svg xmlns='http://www.w3.org/2000/svg' width='{size}' height='{size}' viewBox='0 0 {size} {size}'>
      <circle cx='{cx}' cy='{cy}' r='{radius}' fill='none' stroke='#e5e7eb' stroke-width='18'/>
      {''.join(pieces)}
      <circle cx='{cx}' cy='{cy}' r='33' fill='white'/>
      <text x='{cx}' y='82' text-anchor='middle' font-family='Helvetica, Arial, sans-serif' font-size='14' fill='{accent_safe}' font-weight='700'>{_svg_attr(str(int(total)))}</text>
      <text x='{cx}' y='100' text-anchor='middle' font-family='Helvetica, Arial, sans-serif' font-size='8' fill='#6b7280'>{center_safe}</text>
    </svg>
    """


def _viz_card(title: str, subtitle: str, svg: str, legend_rows: list[tuple[str, str, str]]) -> str:
    legend = "".join(
        f"<tr><td><span class='viz-swatch' style='background:{_svg_color(color)}'></span></td>"
        f"<td>{_esc(label)}</td><td class='num'>{_esc(value)}</td></tr>"
        for label, value, color in legend_rows
    )
    return f"""
    <div class="viz-card">
      <div class="viz-title">{_esc(title)}</div>
      <div class="viz-sub">{_esc(subtitle)}</div>
      <div class="viz-body">
        <img class="viz-img" src="{_svg_data_uri(svg)}" alt="{_esc(title)}" />
        <table class="viz-legend" cellpadding="0" cellspacing="0">{legend}</table>
      </div>
    </div>
    """


def _findings_by_pillar(findings: list[dict]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for finding in findings:
        counts[str(finding.get("pillar") or "other")] += 1
    return counts


def _resource_types(findings: list[dict]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for finding in findings:
        if finding.get("status") not in _FAILING:
            continue
        for resource in finding.get("flagged_resources") or []:
            counts[str(resource.get("type") or "unknown")] += 1
    return counts


def _visual_summary(payload: dict) -> str:
    findings = payload.get("findings") or []
    totals = payload.get("totals") or {}
    scores = payload.get("scores") or {}
    failing = [f for f in findings if f.get("status") in _FAILING]

    outcome_counts = Counter({
        "pass": int(totals.get("passed", 0)),
        "fail": int(totals.get("failed", 0)),
        "manual": int(totals.get("manual", 0)),
        "waived": int(totals.get("waived", 0)),
        "n/a": int(totals.get("not_applicable", 0)),
        "error": int(totals.get("error", 0)),
    })
    outcome_total = max(sum(outcome_counts.values()), 1)
    pct = lambda n: f"{n * 100 // outcome_total}%"
    outcome_svg = _donut_svg(
        [
            ("#16a34a", outcome_counts["pass"]),
            ("#dc2626", outcome_counts["fail"]),
            ("#2563eb", outcome_counts["manual"]),
            ("#7c3aed", outcome_counts["waived"]),
            ("#9ca3af", outcome_counts["n/a"]),
            ("#b91c1c", outcome_counts["error"]),
        ],
        center="control outcome mix",
        accent="#111827",
    )

    severity_counts = Counter((f.get("severity") or "info") for f in failing)
    severity_svg = _donut_svg(
        [("#b91c1c", severity_counts["critical"]), ("#dc2626", severity_counts["error"]), ("#d97706", severity_counts["warning"]), ("#2563eb", severity_counts["info"])],
        center="failing severity",
        accent="#111827",
    )

    pillar_counts = _findings_by_pillar(findings)
    pillar_svg = _donut_svg(
        [
            ("#4f46e5", pillar_counts.get("security", 0)),
            ("#0f766e", pillar_counts.get("reliability", 0)),
            ("#d97706", pillar_counts.get("cost", 0)),
            ("#db2777", pillar_counts.get("operational_excellence", 0)),
            ("#2563eb", pillar_counts.get("performance_efficiency", 0)),
            ("#6b7280", pillar_counts.get("other", 0)),
        ],
        center="findings by pillar",
        accent="#111827",
    )

    remediation_counts = Counter({
        "command": sum(1 for f in failing if (f.get("remediation_command") or "").strip()),
        "guidance": sum(1 for f in failing if (f.get("remediation") or "").strip() and not (f.get("remediation_command") or "").strip()),
        "missing": sum(1 for f in failing if not (f.get("remediation") or "").strip() and not (f.get("remediation_command") or "").strip()),
    })
    remediation_svg = _donut_svg(
        [("#16a34a", remediation_counts["command"]), ("#d97706", remediation_counts["guidance"]), ("#9ca3af", remediation_counts["missing"])],
        center="remediation readiness",
        accent="#111827",
    )

    resource_counts = _resource_types(findings)
    top_resources = resource_counts.most_common(5)
    resource_other = sum(resource_counts.values()) - sum(v for _, v in top_resources)
    resource_palette = ["#4f46e5", "#0f766e", "#d97706", "#db2777", "#2563eb"]
    resource_slices = [(resource_palette[idx], value) for idx, (_, value) in enumerate(top_resources)]
    resource_svg = _donut_svg(
        resource_slices + ([('#9ca3af', resource_other)] if resource_other > 0 else []),
        center="flagged resource types",
        accent="#111827",
    )

    score_rows = []
    for pillar in catalog.PILLARS:
        sc = scores.get(pillar) or {}
        label = catalog.PILLAR_META.get(pillar, {"label": pillar.title()})["label"]
        value = sc.get("score")
        value_txt = f"{value:.0f}" if isinstance(value, (int, float)) else "—"
        score_rows.append(
            f"<tr><td>{_esc(label)}</td><td class='barcell'>{_bar(value if isinstance(value, (int, float)) else 0, _score_color(value))}</td><td class='num'>{_esc(value_txt)}</td></tr>"
        )
    scores_table = (
        '<table class="scoreviz" cellpadding="0" cellspacing="0">'
        '<thead><tr><th>Pillar</th><th>Score bar</th><th>Score</th></tr></thead>'
        f"<tbody>{''.join(score_rows)}</tbody></table>"
    )

    return f"""
    <div class="pagebreak"></div>
    <a name="visuals"></a>
    <h1>Visual snapshot</h1>
    <div class="muted">Five donut charts and one bar chart summarize the assessment before the detailed score and finding pages.</div>
    <table class="viz-grid" cellpadding="0" cellspacing="0">
      <tr>
        <td>{_viz_card('1. Control outcome mix', 'How the assessment breaks down by pass / fail / manual / waived / N/A.', outcome_svg, [
          ('Passed', f"{outcome_counts['pass']} ({pct(outcome_counts['pass'])})", '#16a34a'),
          ('Failed', f"{outcome_counts['fail']} ({pct(outcome_counts['fail'])})", '#dc2626'),
          ('Manual', f"{outcome_counts['manual']} ({pct(outcome_counts['manual'])})", '#2563eb'),
          ('Waived', f"{outcome_counts['waived']} ({pct(outcome_counts['waived'])})", '#7c3aed'),
          ('Not applicable', f"{outcome_counts['n/a']} ({pct(outcome_counts['n/a'])})", '#9ca3af'),
        ])}</td>
        <td>{_viz_card('2. Failing severity mix', 'Critical / error / warning / info counts for failing controls.', severity_svg, [
          ('Critical', str(severity_counts['critical']), '#b91c1c'),
          ('Error', str(severity_counts['error']), '#dc2626'),
          ('Warning', str(severity_counts['warning']), '#d97706'),
          ('Info', str(severity_counts['info']), '#2563eb'),
        ])}</td>
      </tr>
      <tr>
        <td>{_viz_card('3. Findings by pillar', 'Where the assessment findings are concentrated across the Well-Architected pillars.', pillar_svg, [
          ('Security', str(pillar_counts.get('security', 0)), '#4f46e5'),
          ('Reliability', str(pillar_counts.get('reliability', 0)), '#0f766e'),
          ('Cost', str(pillar_counts.get('cost', 0)), '#d97706'),
          ('Operational excellence', str(pillar_counts.get('operational_excellence', 0)), '#db2777'),
          ('Performance efficiency', str(pillar_counts.get('performance_efficiency', 0)), '#2563eb'),
        ])}</td>
        <td>{_viz_card('4. Remediation readiness', 'How many failing controls have a command, written guidance, or no guidance yet.', remediation_svg, [
          ('Command available', str(remediation_counts['command']), '#16a34a'),
          ('Guidance only', str(remediation_counts['guidance']), '#d97706'),
          ('Needs review', str(remediation_counts['missing']), '#9ca3af'),
        ])}</td>
      </tr>
      <tr>
        <td>{_viz_card('5. Flagged resource types', 'Which resource types show up most often in failing findings.', resource_svg, [
          *[(label.title(), str(value), color) for (label, value), color in zip(top_resources, resource_palette)],
          *([('Other', str(resource_other), '#9ca3af')] if resource_other > 0 else []),
        ])}</td>
        <td>
          <div class="viz-card">
            <div class="viz-title">6. Pillar score bars</div>
            <div class="viz-sub">A compact bar visualization of the scored pillars, ordered as reported by the assessment.</div>
            <div class="viz-body">{scores_table}</div>
          </div>
        </td>
      </tr>
    </table>
    """


def _chip(text: str, color: str) -> str:
    return f'<span style="color:{color}; font-weight:bold">{_esc(text)}</span>'


def _status_chip(status: str) -> str:
    return _chip(_STATUS_LABEL.get(status, (status or "").upper()), _STATUS_COLOR.get(status, _MUTED))


def _sev_chip(sev: str) -> str:
    return _chip((sev or "info").upper(), _SEV_COLOR.get(sev, _MUTED))


# ---------------------------------------------------------------- sections


def _sorted_findings(findings: list[dict]) -> list[dict]:
    return sorted(
        findings,
        key=lambda f: (
            list(catalog.PILLARS).index(f.get("pillar")) if f.get("pillar") in catalog.PILLARS else 99,
            _SEV_RANK.get(f.get("severity"), 9),
            0 if f.get("status") in _FAILING else 1,
            _esc(f.get("title")),
        ),
    )


def _cover(payload: dict) -> str:
        score = payload.get("overall_score")
        score_txt = f"{score:.0f}" if isinstance(score, (int, float)) else "—"
        pack_id = (payload.get("trigger") or "").lower()
        pack = catalog.PACKS.get(pack_id)
        pack_label = pack["label"] if pack else "Well-Architected Assessment"
        totals = payload.get("totals") or {}
        summary = (payload.get("summary") or "").strip().split("\n\n", 1)[0]
        return f"""
        <div class="cover">
            <table class="cover-hero" cellpadding="0" cellspacing="0">
                <tr>
                    <td class="cover-left">
                        <div class="cover-brand">Azure Support Agent</div>
                        <div class="cover-sub">Assessment Report</div>
                        <div class="cover-pack">{_esc(pack_label)}</div>
                        <div class="cover-summary">{_esc(summary or 'Branded assessment output with score, findings, compliance mappings, and remediation detail.')}</div>
                    </td>
                    <td class="cover-right">
                        <div class="cover-score-box">
                            <div class="cover-score-num" style="color:{_score_color(score)}">{score_txt}<span class="cover-score-unit">/100</span></div>
                            <div class="cover-score-lbl">Overall score</div>
                            {_bar(score if isinstance(score, (int, float)) else 0, _score_color(score))}
                        </div>
                    </td>
                </tr>
            </table>
            <table class="cover-meta" cellpadding="0" cellspacing="0">
                <tr><td class="k">Workload</td><td class="v">{_esc(payload.get('workload_name') or '—')}</td><td class="k">Triggered by</td><td class="v">{_esc(payload.get('triggered_by') or '—')}</td></tr>
                <tr><td class="k">Date performed</td><td class="v">{_fmt_date(payload.get('ended_at') or payload.get('started_at'))}</td><td class="k">Catalog version</td><td class="v">{_esc(payload.get('catalog_version') or '—')}</td></tr>
                <tr><td class="k">Pillars</td><td class="v">{_esc(', '.join((payload.get('pillars') or [])) or '—')}</td><td class="k">Resources scanned</td><td class="v">{int(payload.get('resource_count') or 0)}</td></tr>
            </table>
            {_kpi_table(totals, payload)}
            <table class="cover-includes" cellpadding="0" cellspacing="0">
                <tr>
                    <td><b>Includes</b></td>
                    <td>Executive summary, visual scorecards, detailed findings, compliance coverage, flagged resources, and full run metadata.</td>
                </tr>
            </table>
            <div class="cover-foot">
                Passed {int(totals.get('passed', 0))} · Failed {int(totals.get('failed', 0))} ·
                N/A {int(totals.get('not_applicable', 0))} · Waived {int(totals.get('waived', 0))}
                &nbsp;|&nbsp; Generated {_fmt_date(datetime.now(timezone.utc).isoformat())}
            </div>
        </div>
        """


def _toc_entries() -> list[tuple[str, str, int]]:
    return [
        ("exec", "Executive summary", 0),
        ("visuals", "Visual snapshot", 0),
        ("scores", "Score overview", 0),
        ("findings", "Findings & recommendations", 0),
        ("appendix-compliance", "Appendix A — Compliance coverage", 1),
        ("appendix-resources", "Appendix B — Flagged resources", 1),
        ("appendix-inventory", "Appendix C — Scanned resource inventory", 1),
        ("appendix-meta", "Appendix D — Run metadata & methodology", 1),
    ]


def _toc(page_map: dict[str, int] | None = None) -> str:
    if page_map is None:
        return """
        <div class="pagebreak"></div>
        <a name="toc"></a>
        <div class="toc-title">Contents</div>
        <div class="toc-note">Generating section links and page numbers…</div>
        """
    rows = []
    for anchor, label, level in _toc_entries():
        page_txt = str(page_map.get(anchor, "—")) if page_map else "—"
        rows.append(
            f'<tr class="toc-row level-{level}"><td class="toc-link"><a href="#{anchor}">{_esc(label)}</a></td><td class="toc-page">{_esc(page_txt)}</td></tr>'
        )
    return f"""
    <div class="pagebreak"></div>
    <a name="toc"></a>
    <div class="toc-title">Contents</div>
    <div class="toc-note">Section links and page numbers generated from the rendered report.</div>
    <table class="toc-table" cellpadding="0" cellspacing="0">{''.join(rows)}</table>
    """


def _kpi_table(totals: dict, payload: dict) -> str:
    cells = [
        ("Passed", int(totals.get("passed", 0)), "#16a34a"),
        ("Failed", int(totals.get("failed", 0)), "#dc2626"),
        ("Not applicable", int(totals.get("not_applicable", 0)), "#6b7280"),
        ("Manual", int(totals.get("manual", 0)), "#2563eb"),
        ("Waived", int(totals.get("waived", 0)), "#7c3aed"),
        ("Errors", int(totals.get("error", 0)), "#b91c1c"),
    ]
    tds = "".join(
        f'<td class="kpi"><div class="kpi-num" style="color:{c}">{v}</div>'
        f'<div class="kpi-lbl">{_esc(label)}</div></td>'
        for label, v, c in cells
    )
    conf = payload.get("confidence") or "—"
    comp = payload.get("completeness_pct")
    comp_txt = f"{comp}%" if comp is not None else "—"
    return (
        f'<table class="kpis" cellpadding="0" cellspacing="0"><tr>{tds}</tr></table>'
        f'<div class="kpi-meta">Confidence: <b>{_esc(conf)}</b> &nbsp;·&nbsp; '
        f'Completeness: <b>{_esc(comp_txt)}</b> &nbsp;·&nbsp; '
        f'Resources scanned: <b>{int(payload.get("resource_count") or 0)}</b> &nbsp;·&nbsp; '
        f'AI-assisted: <b>{"yes" if payload.get("used_ai") else "no"}</b></div>'
    )


def _severity_mix(findings: list[dict]) -> str:
    counts: dict[str, int] = {}
    for f in findings:
        if f.get("status") in _FAILING:
            counts[f.get("severity") or "info"] = counts.get(f.get("severity") or "info", 0) + 1
    total = sum(counts.values())
    if not total:
        return '<div class="muted">No failing controls — nothing to prioritise.</div>'
    order = ["critical", "error", "warning", "info"]
    segs = [(100.0 * counts.get(s, 0) / total, _SEV_COLOR[s]) for s in order]
    legend = " &nbsp; ".join(
        f'{_chip(s.capitalize(), _SEV_COLOR[s])} {counts.get(s, 0)}' for s in order if counts.get(s)
    )
    return f'{_stacked_bar(segs)}<div class="legend">{legend}</div>'


def _summary_narrative(payload: dict) -> str:
    summary = (payload.get("summary") or "").strip()
    if not summary:
        return '<div class="muted">No AI narrative was generated for this run.</div>'
    paras = [p.strip() for p in summary.replace("\r", "").split("\n\n") if p.strip()]
    return "".join(f"<p>{_esc(p)}</p>" for p in paras)


def _top_risks(findings: list[dict]) -> str:
    failing = [f for f in _sorted_findings(findings) if f.get("status") in _FAILING]
    top = failing[:5]
    if not top:
        return '<div class="muted">No failing controls.</div>'
    rows = "".join(
        f"<tr><td>{_sev_chip(f.get('severity'))}</td>"
        f"<td>{_esc(f.get('title'))}</td>"
        f"<td class=\"num\">{int(f.get('flagged_count') or 0)}</td></tr>"
        for f in top
    )
    return (
        '<table class="grid" cellpadding="0" cellspacing="0">'
        '<thead><tr><th style="width:18%">Severity</th><th>Control</th>'
        '<th style="width:16%">Resources</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )


def _executive(payload: dict) -> str:
    findings = payload.get("findings") or []
    totals = payload.get("totals") or {}
    return f"""
    <div class="pagebreak"></div>
    <a name="exec"></a>
    <h1>Executive summary</h1>
    <h3>Key metrics</h3>
    {_kpi_table(totals, payload)}
    <h3>Risk by severity (failing controls)</h3>
    {_severity_mix(findings)}
    <h3>Top risks</h3>
    {_top_risks(findings)}
    <h3>AI narrative</h3>
    <div class="narrative">{_summary_narrative(payload)}</div>
    """


def _scores(payload: dict) -> str:
    scores = payload.get("scores") or {}
    rows = []
    for pillar in catalog.PILLARS:
        sc = scores.get(pillar)
        if not sc:
            continue
        meta = catalog.PILLAR_META.get(pillar, {"label": pillar.title()})
        val = sc.get("score")
        val_txt = f"{val:.0f}" if isinstance(val, (int, float)) else "—"
        rows.append(
            f'<tr><td class="pillar-name">{_esc(meta["label"])}</td>'
            f'<td class="pillar-bar">{_bar(val if isinstance(val, (int, float)) else 0, _score_color(val))}</td>'
            f'<td class="pillar-score" style="color:{_score_color(val)}">{val_txt}</td>'
            f'<td class="pillar-counts">{int(sc.get("passed", 0))}P / {int(sc.get("failed", 0))}F'
            f' / {int(sc.get("na", 0))}N/A</td></tr>'
        )
    pillar_tbl = (
        '<table class="pillars" cellpadding="0" cellspacing="0">' + "".join(rows) + "</table>"
        if rows
        else '<div class="muted">No pillar scores.</div>'
    )

    compliance = payload.get("compliance") or {}
    crows = []
    for fw_key, fw in compliance.items():
        cov = fw.get("coverage")
        if cov is None and not fw.get("controls"):
            continue
        cov_val = cov if isinstance(cov, (int, float)) else 0
        cov_txt = f"{cov}%" if cov is not None else "n/a"
        crows.append(
            f'<tr><td class="pillar-name">{_esc(fw.get("label", fw_key))}</td>'
            f'<td class="pillar-bar">{_bar(cov_val, _score_color(cov_val))}</td>'
            f'<td class="pillar-score" style="color:{_score_color(cov_val)}">{_esc(cov_txt)}</td>'
            f'<td class="pillar-counts">{int(fw.get("passed", 0))}/{int(fw.get("total", 0))} controls</td></tr>'
        )
    comp_tbl = (
        '<table class="pillars" cellpadding="0" cellspacing="0">' + "".join(crows) + "</table>"
        if crows
        else '<div class="muted">No framework mappings in this run.</div>'
    )

    return f"""
    <div class="pagebreak"></div>
    <a name="scores"></a>
    <h1>Score overview</h1>
    <h3>Pillar scores</h3>
    {pillar_tbl}
    <h3>Compliance framework coverage</h3>
    {comp_tbl}
    """


def _finding_card(f: dict) -> str:
    fw = f.get("frameworks") or {}
    fw_bits = []
    for key in ("cis", "nist", "iso", "mcsb", "pci"):
        vals = fw.get(key) or []
        if vals:
            fw_bits.append(f"<b>{key.upper()}</b> {_esc(', '.join(vals))}")
    fw_line = (
        f'<div class="frameworks">{" &nbsp;·&nbsp; ".join(fw_bits)}</div>' if fw_bits else ""
    )
    desc = (f.get("description") or "").strip()
    rem = (f.get("remediation") or "").strip()
    cmd = (f.get("remediation_command") or "").strip()
    rationale = (f.get("rationale") or "").strip()
    parts = [
        '<div class="card">',
        '<table class="card-head" cellpadding="0" cellspacing="0"><tr>',
        f'<td class="card-title">{_esc(f.get("title"))}</td>',
        f'<td class="card-meta">{_sev_chip(f.get("severity"))} &nbsp; {_status_chip(f.get("status"))}'
        f' &nbsp; <span class="muted">{int(f.get("flagged_count") or 0)} affected</span></td>',
        "</tr></table>",
        fw_line,
    ]
    if desc:
        parts.append(f'<div class="card-desc">{_esc(desc)}</div>')
    if rationale:
        parts.append(f'<div class="card-desc"><i>{_esc(rationale)}</i></div>')
    if rem:
        parts.append(f'<div class="rec"><b>Recommendation.</b> {_esc(rem)}</div>')
    if cmd:
        parts.append(f'<div class="cmd">{_esc(cmd)}</div>')
    parts.append("</div>")
    return "".join(parts)


def _findings(payload: dict) -> str:
    findings = payload.get("findings") or []
    scores = payload.get("scores") or {}
    blocks = [
        '<div class="pagebreak"></div>',
        '<a name="findings"></a>',
        "<h1>Findings &amp; recommendations</h1>",
        '<div class="muted">Failing controls are shown in full with remediation guidance; '
        "passing, not-applicable and waived controls are listed compactly per pillar.</div>",
    ]
    pillars_present = [p for p in catalog.PILLARS if any(f.get("pillar") == p for f in findings)]
    extra = sorted({f.get("pillar") for f in findings if f.get("pillar") not in catalog.PILLARS})
    for pillar in [*pillars_present, *[e for e in extra if e]]:
        group = [f for f in _sorted_findings(findings) if f.get("pillar") == pillar]
        if not group:
            continue
        meta = catalog.PILLAR_META.get(pillar, {"label": (pillar or "Other").title()})
        sc = scores.get(pillar) or {}
        val = sc.get("score")
        val_txt = f"{val:.0f}/100" if isinstance(val, (int, float)) else ""
        blocks.append(f'<h2>{_esc(meta["label"])} <span class="h2-score">{val_txt}</span></h2>')
        failing = [f for f in group if f.get("status") in _FAILING]
        others = [f for f in group if f.get("status") not in _FAILING]
        if failing:
            blocks.extend(_finding_card(f) for f in failing)
        else:
            blocks.append('<div class="muted">No failing controls in this pillar.</div>')
        if others:
            rows = "".join(
                f"<tr><td>{_status_chip(o.get('status'))}</td>"
                f"<td>{_esc(o.get('title'))}</td>"
                f"<td>{_sev_chip(o.get('severity'))}</td></tr>"
                for o in others
            )
            blocks.append(
                '<table class="grid compact" cellpadding="0" cellspacing="0">'
                '<thead><tr><th style="width:16%">Status</th><th>Control</th>'
                '<th style="width:16%">Severity</th></tr></thead>'
                f"<tbody>{rows}</tbody></table>"
            )
    return "".join(blocks)


def _appendix_compliance(payload: dict) -> str:
    compliance = payload.get("compliance") or {}
    blocks = ['<div class="pagebreak"></div>', '<a name="appendix-compliance"></a>',
              "<h1>Appendix A — Compliance coverage</h1>"]
    any_fw = False
    for fw_key, fw in compliance.items():
        controls = fw.get("controls") or []
        if not controls:
            continue
        any_fw = True
        cov = fw.get("coverage")
        cov_txt = f"{cov}%" if cov is not None else "n/a"
        blocks.append(
            f'<h2>{_esc(fw.get("label", fw_key))} '
            f'<span class="h2-score">{int(fw.get("passed", 0))}/{int(fw.get("total", 0))} · {_esc(cov_txt)}</span></h2>'
        )
        rows = []
        for c in controls:
            checks = c.get("checks") or []
            check_txt = "; ".join(_esc(ck.get("title")) for ck in checks) or "—"
            rows.append(
                f'<tr><td class="num">{_esc(c.get("control"))}</td>'
                f"<td>{_status_chip(c.get('status'))}</td>"
                f"<td>{check_txt}</td></tr>"
            )
        blocks.append(
            '<table class="grid compact" cellpadding="0" cellspacing="0">'
            '<thead><tr><th style="width:16%">Control</th><th style="width:16%">Status</th>'
            '<th>Mapped checks</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>'
        )
    if not any_fw:
        blocks.append('<div class="muted">No framework mappings in this run.</div>')
    return "".join(blocks)


def _appendix_resources(payload: dict) -> str:
    findings = _sorted_findings(payload.get("findings") or [])
    blocks = ['<div class="pagebreak"></div>', '<a name="appendix-resources"></a>',
              "<h1>Appendix B — Flagged resources</h1>",
              '<div class="muted">Every resource flagged by a failing control, with its Azure '
              "portal deep link. No results are truncated.</div>"]
    any_res = False
    for f in findings:
        resources = f.get("flagged_resources") or []
        if not resources or f.get("status") not in _FAILING:
            continue
        any_res = True
        blocks.append(
            f'<h2>{_esc(f.get("title"))} '
            f'<span class="h2-score">{len(resources)} resources</span></h2>'
        )
        rows = []
        for r in resources:
            portal = r.get("portal_url") or ""
            name = _esc(r.get("name"))
            name_cell = f'<a href="{_esc(portal)}">{name}</a>' if portal else name
            rows.append(
                f"<tr><td>{name_cell}</td>"
                f"<td>{_esc(r.get('type'))}</td>"
                f"<td>{_esc(r.get('resource_group'))}</td>"
                f"<td>{_esc(r.get('subscription_name') or r.get('subscription_id'))}</td></tr>"
            )
        blocks.append(
            '<table class="grid compact" cellpadding="0" cellspacing="0">'
            '<thead><tr><th style="width:28%">Resource</th><th style="width:28%">Type</th>'
            '<th style="width:20%">Resource group</th><th>Subscription</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>'
        )
    if not any_res:
        blocks.append('<div class="muted">No flagged resources.</div>')
    return "".join(blocks)


def _appendix_inventory(payload: dict) -> str:
    resources = payload.get("resources") or []
    blocks = ['<div class="pagebreak"></div>', '<a name="appendix-inventory"></a>',
              f"<h1>Appendix C — Scanned resource inventory</h1>",
              f'<div class="muted">All {len(resources)} resources in the assessed scope.</div>']
    if not resources:
        blocks.append('<div class="muted">No resource inventory captured for this run.</div>')
        return "".join(blocks)
    rows = "".join(
        f"<tr><td>{_esc(r.get('name'))}</td>"
        f"<td>{_esc(r.get('type'))}</td>"
        f"<td>{_esc(r.get('resource_group'))}</td>"
        f"<td>{_esc(r.get('location'))}</td></tr>"
        for r in resources
    )
    blocks.append(
        '<table class="grid compact" cellpadding="0" cellspacing="0">'
        '<thead><tr><th style="width:34%">Name</th><th style="width:34%">Type</th>'
        '<th style="width:18%">Resource group</th><th>Location</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )
    return "".join(blocks)


def _appendix_meta(payload: dict) -> str:
    rows = [
        ("Run id", payload.get("id")),
        ("Workload", payload.get("workload_name")),
        ("Pillars", ", ".join(payload.get("pillars") or []) or "—"),
        ("Trigger / pack", payload.get("trigger")),
        ("Triggered by", payload.get("triggered_by")),
        ("Started", _fmt_date(payload.get("started_at"))),
        ("Ended", _fmt_date(payload.get("ended_at"))),
        ("Duration", _fmt_duration(payload.get("duration_ms"))),
        ("Catalog version", payload.get("catalog_version")),
        ("Confidence", payload.get("confidence")),
        ("Completeness", f"{payload.get('completeness_pct')}%" if payload.get("completeness_pct") is not None else "—"),
        ("AI-assisted", "yes" if payload.get("used_ai") else "no"),
        ("Is baseline", "yes" if payload.get("is_baseline") else "no"),
    ]
    body = "".join(
        f'<tr><td class="k">{_esc(k)}</td><td class="v">{_esc(v if v not in (None, "") else "—")}</td></tr>'
        for k, v in rows
    )
    return f"""
    <div class="pagebreak"></div>
    <a name="appendix-meta"></a>
    <h1>Appendix D — Run metadata &amp; methodology</h1>
    <table class="meta" cellpadding="0" cellspacing="0">{body}</table>
    <h3>Methodology</h3>
    <p class="muted">Controls are evaluated with a hybrid engine combining Azure Resource Graph
    queries, platform metrics, and (where enabled) AI-assisted rationale. Each control is scored
    pass / fail / not-applicable / manual / waived; pillar and overall scores are severity-weighted.
    Compliance coverage maps each control to CIS, NIST 800-53, ISO 27001, Microsoft Cloud Security
    Benchmark and PCI DSS, taking the worst status across the checks mapped to a given control.</p>
    """


# ---------------------------------------------------------------- shell + render

_CSS = f"""
@page {{
  size: a4 portrait;
  margin: 2.3cm 1.5cm 1.7cm 1.5cm;
  @frame header_frame {{ -pdf-frame-content: headerContent; top: 0.7cm; left: 1.5cm; width: 18cm; height: 1.1cm; }}
  @frame footer_frame {{ -pdf-frame-content: footerContent; bottom: 0.7cm; left: 1.5cm; width: 18cm; height: 0.9cm; }}
}}
body {{ font-family: Helvetica, sans-serif; font-size: 9.5px; color: {_INK}; line-height: 1.45; }}
h1 {{ font-size: 17px; color: {_BRAND}; margin: 0 0 8px 0; padding-bottom: 4px; border-bottom: 2px solid {_BRAND};
      -pdf-outline: true; -pdf-outline-level: 0; -pdf-outline-open: false; -pdf-keep-with-next: true; }}
h2 {{ font-size: 12.5px; color: {_INK}; margin: 12px 0 5px 0;
      -pdf-outline: true; -pdf-outline-level: 1; -pdf-outline-open: false; -pdf-keep-with-next: true; }}
h3 {{ font-size: 10.5px; color: {_MUTED}; margin: 11px 0 4px 0; text-transform: uppercase; letter-spacing: 0.5px;
    -pdf-outline: true; -pdf-outline-level: 2; -pdf-outline-open: false; -pdf-keep-with-next: true; }}
.h2-score {{ font-size: 9px; color: {_MUTED}; font-weight: normal; }}
p {{ margin: 0 0 5px 0; }}
.muted {{ color: {_MUTED}; font-size: 9px; }}
a {{ color: {_BRAND}; text-decoration: none; }}
.pagebreak {{ page-break-before: always; }}

/* running header / footer */
#headerContent {{ font-size: 8px; color: {_MUTED}; border-bottom: 0.5px solid {_LINE}; }}
#headerContent .brand {{ color: {_BRAND}; font-weight: bold; font-size: 9px; }}
#footerContent {{ font-size: 8px; color: {_MUTED}; border-top: 0.5px solid {_LINE}; text-align: center; }}

/* cover */
.cover {{ margin-top: 3.5cm; }}
.cover-hero {{ width: 18cm; margin-bottom: 16px; }}
.cover-left {{ width: 11.5cm; vertical-align: top; }}
.cover-right {{ width: 6.5cm; vertical-align: top; }}
.cover-brand {{ font-size: 30px; font-weight: bold; color: {_BRAND}; }}
.cover-sub {{ font-size: 15px; color: {_INK}; margin-top: 2px; }}
.cover-pack {{ font-size: 11px; color: {_MUTED}; margin: 2px 0 22px 0; }}
.cover-summary {{ font-size: 10px; color: {_INK}; line-height: 1.5; padding-right: 20px; }}
.cover-meta {{ width: 18cm; margin-bottom: 18px; border-top: 0.5px solid {_LINE}; border-bottom: 0.5px solid {_LINE}; }}
.cover-meta .k {{ color: {_MUTED}; width: 14%; padding: 6px 0; font-size: 9px; }}
.cover-meta .v {{ color: {_INK}; font-weight: bold; padding: 6px 10px 6px 0; font-size: 9px; }}
.cover-score-box {{ border: 0.5px solid {_LINE}; padding: 12px 12px 10px 12px; }}
.cover-score-num {{ font-size: 46px; font-weight: bold; }}
.cover-score-unit {{ font-size: 16px; color: {_MUTED}; font-weight: normal; }}
.cover-score-lbl {{ font-size: 10px; color: {_MUTED}; margin-bottom: 6px; }}
.cover-includes {{ width: 18cm; margin-top: 10px; border: 0.5px solid {_LINE}; }}
.cover-includes td {{ font-size: 8.5px; padding: 6px 8px; color: {_INK}; }}
.cover-foot {{ margin-top: 26px; font-size: 8.5px; color: {_MUTED}; }}

/* table of contents */
.toc-title {{ font-size: 18px; font-weight: bold; color: {_BRAND}; margin-bottom: 4px; }}
.toc-note {{ font-size: 9px; color: {_MUTED}; margin-bottom: 10px; }}
.toc-table {{ width: 18cm; border-top: 0.5px solid {_LINE}; border-bottom: 0.5px solid {_LINE}; }}
.toc-row td {{ padding: 7px 4px; border-bottom: 0.5px solid {_LINE}; font-size: 10px; }}
.toc-row.level-1 td:first-child {{ padding-left: 14px; }}
.toc-link {{ color: {_INK}; }}
.toc-page {{ width: 1.5cm; text-align: right; color: {_MUTED}; }}

/* visual snapshot */
.viz-grid {{ width: 100%; }}
.viz-grid td {{ width: 50%; vertical-align: top; padding: 0 4px 8px 0; }}
.viz-card {{ border: 0.5px solid {_LINE}; border-radius: 4px; padding: 8px; page-break-inside: avoid; }}
.viz-title {{ font-size: 10.5px; font-weight: bold; color: {_INK}; margin-bottom: 2px; }}
.viz-sub {{ font-size: 8px; color: {_MUTED}; margin-bottom: 5px; }}
.viz-body {{ display: block; }}
.viz-img {{ display: block; width: 100%; max-width: 170px; margin: 0 auto 4px auto; }}
.viz-legend {{ width: 100%; font-size: 8px; }}
.viz-legend td {{ padding: 1px 2px; border-bottom: 0.5px solid {_LINE}; }}
.viz-swatch {{ display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 4px; }}
.scoreviz {{ width: 100%; font-size: 8px; }}
.scoreviz th {{ text-align: left; background-color: #f3f4f6; padding: 3px 4px; color: {_MUTED}; }}
.scoreviz td {{ padding: 3px 4px; border-bottom: 0.5px solid {_LINE}; }}
.scoreviz .barcell {{ width: 62%; }}

/* bars */
.bartracktbl {{ width: 168px; }}
.stack {{ width: 420px; height: 14px; }}
.stack td {{ padding: 0; }}
.legend {{ font-size: 8.5px; color: {_MUTED}; margin-top: 4px; }}

/* kpis */
.kpis {{ width: 100%; margin: 4px 0 6px 0; }}
.kpi {{ width: 16.6%; text-align: center; border: 0.5px solid {_LINE}; padding: 6px 2px; }}
.kpi-num {{ font-size: 17px; font-weight: bold; }}
.kpi-lbl {{ font-size: 7.5px; color: {_MUTED}; }}
.kpi-meta {{ font-size: 8.5px; color: {_MUTED}; margin-top: 3px; }}
.narrative p {{ font-size: 9.5px; }}

/* pillar / framework score rows */
.pillars {{ width: 100%; }}
.pillars td {{ padding: 4px 4px; border-bottom: 0.5px solid {_LINE}; vertical-align: middle; }}
.pillar-name {{ width: 28%; font-weight: bold; }}
.pillar-bar {{ width: 44%; }}
.pillar-score {{ width: 10%; text-align: right; font-weight: bold; }}
.pillar-counts {{ width: 18%; text-align: right; color: {_MUTED}; font-size: 8px; }}

/* finding cards */
.card {{ border: 0.5px solid {_LINE}; border-left: 3px solid {_SEV_COLOR['error']}; padding: 6px 8px; margin: 6px 0;
         -pdf-keep-in-frame-mode: shrink; }}
.card-head {{ width: 100%; }}
.card-title {{ font-weight: bold; font-size: 10px; }}
.card-meta {{ text-align: right; font-size: 8.5px; }}
.frameworks {{ font-size: 8px; color: {_MUTED}; margin-top: 2px; }}
.card-desc {{ font-size: 9px; margin-top: 4px; }}
.rec {{ font-size: 9px; margin-top: 4px; }}
.cmd {{ font-family: Courier, monospace; font-size: 8px; background-color: #f3f4f6; color: #111827;
        padding: 4px 5px; margin-top: 4px; }}

/* generic grids */
.grid {{ width: 100%; margin-top: 4px; }}
.grid th {{ background-color: #f3f4f6; color: {_MUTED}; text-align: left; font-size: 8px; padding: 4px;
            border-bottom: 0.5px solid {_LINE}; }}
.grid td {{ padding: 4px; border-bottom: 0.5px solid {_LINE}; font-size: 8.5px; vertical-align: top; }}
.grid.compact td {{ font-size: 8px; padding: 3px 4px; }}
.grid .num {{ text-align: right; }}
.num {{ text-align: right; }}

/* metadata */
.meta {{ width: 100%; }}
.meta .k {{ width: 30%; color: {_MUTED}; padding: 4px; border-bottom: 0.5px solid {_LINE}; }}
.meta .v {{ color: {_INK}; padding: 4px; border-bottom: 0.5px solid {_LINE}; }}
"""


def _shell(payload: dict, body: str) -> str:
    workload = _esc(payload.get("workload_name") or "—")
    date = _fmt_date(payload.get("ended_at") or payload.get("started_at"))
    header = (
        '<div id="headerContent">'
        '<table cellpadding="0" cellspacing="0" width="18cm"><tr>'
        '<td><span class="brand">Azure Support Agent</span> &nbsp; Assessment Report</td>'
        f'<td style="text-align:right">{workload} &nbsp;·&nbsp; {date}</td>'
        "</tr></table></div>"
    )
    footer = (
        '<div id="footerContent">Confidential &nbsp;·&nbsp; '
        "page <pdf:pagenumber> of <pdf:pagecount></div>"
    )
    return (
        "<html><head><meta charset='utf-8'><style>"
        + _CSS
        + "</style></head><body>"
        + header
        + footer
        + body
        + "</body></html>"
    )


def build_pdf(payload: dict[str, Any]) -> bytes:
    """Render an enriched assessment-run payload to PDF bytes.

    ``payload`` is the full run dict (``_run_dict(full=True)``) with each flagged
    resource already enriched with ``portal_url`` and ``subscription_name``.
    """
    from xhtml2pdf import pisa  # local import keeps the heavy dep off the hot path

    def _compose(page_map: dict[str, int] | None = None) -> str:
        return "".join(
            [
                _cover(payload),
                _toc(page_map),
                _executive(payload),
                _visual_summary(payload),
                _scores(payload),
                _findings(payload),
                _appendix_compliance(payload),
                _appendix_resources(payload),
                _appendix_inventory(payload),
                _appendix_meta(payload),
            ]
        )

    def _render(html_doc: str) -> bytes:
        buf = io.BytesIO()
        result = pisa.CreatePDF(src=html_doc, dest=buf, encoding="utf-8")
        if result.err:
            raise RuntimeError(f"PDF generation failed with {result.err} error(s)")
        return buf.getvalue()

    first_pass = _render(_shell(payload, _compose()))
    reader = PdfReader(io.BytesIO(first_pass))
    page_map: dict[str, int] = {}
    for anchor, label, _ in _toc_entries():
        for i, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            if label in text:
                page_map[anchor] = i
                break

    html_doc = _shell(payload, _compose(page_map))
    return _render(html_doc)
