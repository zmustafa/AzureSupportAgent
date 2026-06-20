"""Branded PDF report for the Performance Profiler.

Renders one *profile run* (the same snapshot the heatmap shows) into a paginated, branded
PDF via the shared ``app.core.pdf_common`` engine (xhtml2pdf / reportlab — pure-Python,
identical on Windows dev and the Linux container).

Document layout:

1. Cover            — title, scope, window, run date, the workload Performance Score, KPI
                      strip, a breaching/approaching/healthy state-mix bar, the binding
                      bottleneck callout.
2. Table of contents — clickable links with page numbers.
3. Executive summary — score + KPIs + a state donut + bottlenecks-by-type, plus the AI
                      analyst narrative and the score trend sparkline.
4. Ranked bottlenecks — every breaching/approaching metric, grouped by state then severity,
                      with observed vs AMBA threshold, trend and the "why".
5. Resource performance — per-resource detail: each non-healthy resource's offending cells.
6. Appendix — Heatmap — the resource × metric matrix, grouped by resource type so each block
                      stays narrow enough to paginate cleanly.
7. Appendix — Inventory — every in-scope resource and whether it is in the AMBA reference.
8. Methodology — scope, window, source, how the score is computed.

``build_evidence_content`` maps a run snapshot into the Evidence Locker's JSON content shape
so a profile run can be captured as an immutable, hash-stamped evidence snapshot.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from app.core.coverage_pdf import _portal_url, _resource_link  # canonical portal-arrow link
from app.core.pdf_common import (
    BRAND,
    INK,
    LINE,
    MUTED,
    SEV_COLOR,
    SEV_RANK,
    bar,
    base_css,
    chip,
    donut_svg,
    esc,
    esc_breakable,
    fmt_date,
    normalize_severity,
    render_two_pass,
    running_frames,
    score_color,
    sparkline_svg,
    stacked_bar,
    svg_data_uri,
    swatch,
    viz_card,
)

ACCENT = "#ea580c"  # warm orange — the Performance Profiler's 🔥 theme

# Per-metric state → (ink color, cell background) used in the heatmap + detail tables.
_STATE_COLOR = {"breaching": "#dc2626", "approaching": "#d97706", "healthy": "#16a34a", "no_data": "#9ca3af"}
_STATE_BG = {"breaching": "#fef2f2", "approaching": "#fffbeb", "healthy": "#f0fdf4", "no_data": "#f9fafb"}
_STATE_LABEL = {"breaching": "Breaching", "approaching": "Approaching", "healthy": "Healthy", "no_data": "No data"}
_STATE_ORDER = {"breaching": 0, "approaching": 1, "healthy": 2, "no_data": 3}
_SEV_LABEL = {"critical": "Critical", "error": "High", "warning": "Warning", "info": "Info"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _short_arm(value: Any) -> str:
    """`microsoft.compute/virtualmachines` → `Compute/virtualmachines` for compact display."""
    t = str(value or "")
    if "/" in t and t.lower().startswith("microsoft."):
        return t.split(".", 1)[1]
    return t


def _short_sub(sub: Any) -> str:
    s = str(sub or "")
    return s[:8] if s else ""


def _fmt_val(value: Any, unit: str = "") -> str:
    """Compact numeric formatting for observed/threshold readings."""
    v = _num(value)
    if v is None:
        return "—"
    if abs(v) >= 1000:
        txt = f"{v:,.0f}"
    elif abs(v) >= 10:
        txt = f"{v:.0f}"
    else:
        txt = f"{v:.2f}".rstrip("0").rstrip(".")
    u = str(unit or "").strip()
    if u and u not in ("count", "flag"):
        return f"{txt}{'' if u == '%' else ' '}{u}"
    return txt


def _pct_txt(pct: Any) -> str:
    v = _num(pct)
    return f"{v:.0f}%" if v is not None else "—"


def _trend_chip(trend_pct: Any) -> str:
    v = _num(trend_pct)
    if v is None or abs(v) < 0.1:
        return '<span class="muted">flat</span>'
    arrow = "▲" if v > 0 else "▼"
    color = "#dc2626" if v > 0 else "#16a34a"  # rising = worse for most perf metrics
    return f'<span style="color:{color}; font-weight:bold">{arrow} {abs(v):.0f}%</span>'


# ---------------------------------------------------------------- snapshot → normalized model


def _adapt(snap: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Performance Profiler run snapshot into the model the renderer consumes."""
    sc = snap.get("scorecard") or {}
    score = sc.get("workload_score")
    resources = list(snap.get("resources") or [])
    bottlenecks = list(snap.get("bottlenecks") or [])
    top = snap.get("top_bottleneck")
    n_breaching = _int(sc.get("breaching"))
    n_approaching = _int(sc.get("approaching"))
    n_healthy = _int(sc.get("healthy"))
    profiled = _int(sc.get("resources_profiled"), len(resources))

    if top:
        summary = (
            f"Performance score {score if score is not None else '—'}/100. Binding bottleneck: "
            f"{top.get('resource_name', '—')} — {top.get('metric_name', '')} at "
            f"{_pct_txt(top.get('pct_of_threshold'))} of its AMBA threshold ({top.get('state', '')})."
        )
    else:
        summary = (
            f"Performance score {score if score is not None else '—'}/100. No bottlenecks — every "
            f"profiled metric is within its AMBA threshold."
        )

    # Bottlenecks-by-type rollup (where the breaching/approaching signals concentrate).
    btype: Counter[str] = Counter(str(b.get("resource_type") or "—") for b in bottlenecks)

    model = {
        "title": "Performance Profile",
        "subtitle": "Azure Monitor metrics vs AMBA thresholds",
        "headline_label": "Performance score",
        "accent": ACCENT,
        "scope_name": snap.get("scope_name") or snap.get("scope_id") or "—",
        "scope_kind": snap.get("scope_kind") or "workload",
        "scope_id": snap.get("scope_id") or "",
        "generated_at": snap.get("run_at") or snap.get("generated_at"),
        "connection_configured": bool(snap.get("connection_configured")),
        "demo": bool(snap.get("demo")),
        "source": snap.get("source") or "—",
        "window": snap.get("window") or snap.get("requested_window") or "—",
        "interval": snap.get("interval") or "—",
        "error": snap.get("error") or "",
        "score": score,
        "scorecard": sc,
        "resources": resources,
        "bottlenecks": bottlenecks,
        "top_bottleneck": top,
        "all_resources": list(snap.get("all_resources") or []),
        "narrative": (snap.get("narrative") or "").strip(),
        "summary_line": summary,
        "state_counts": {"breaching": n_breaching, "approaching": n_approaching, "healthy": n_healthy},
        "btype_counts": btype.most_common(),
        "kpis": [
            ("Score", f"{score}" if score is not None else "—", "0–100, severity-weighted"),
            ("Profiled", str(profiled), "resources"),
            ("Breaching", str(n_breaching), "over threshold"),
            ("Approaching", str(n_approaching), "near threshold"),
            ("Healthy", str(n_healthy), "within limits"),
        ],
    }
    return model


# ---------------------------------------------------------------- shared building blocks


def _score_card(model: dict[str, Any]) -> str:
    score = model["score"]
    txt = f"{score:.0f}" if isinstance(score, (int, float)) else "—"
    color = score_color(score)
    return f"""
    <table class="score-card" width="200" cellpadding="0" cellspacing="0"><tr><td>
      <div class="score-num" style="color:{color}">{txt}<span class="score-unit">/100</span></div>
      <div class="score-lbl">{esc(model['headline_label'])}</div>
      {bar(score if isinstance(score, (int, float)) else 0, color, total=176)}
    </td></tr></table>
    """


def _kpi_strip(model: dict[str, Any]) -> str:
    cells = "".join(
        f'<td class="kpi"><div class="kpi-num" style="color:{INK}">{esc(value)}</div>'
        f'<div class="kpi-lbl">{esc(label)}</div>'
        f'<div class="kpi-lbl" style="margin-top:1px">{esc(sub)}</div></td>'
        for label, value, sub in model["kpis"]
    )
    return f'<table class="kpis" cellpadding="0" cellspacing="0"><tr>{cells}</tr></table>'


def _state_mix_bar(model: dict[str, Any]) -> str:
    s = model["state_counts"]
    total = s["breaching"] + s["approaching"] + s["healthy"]
    order = [("breaching", "Breaching"), ("approaching", "Approaching"), ("healthy", "Healthy")]
    if total <= 0:
        return '<div class="ok-note">No resources were profiled against the reference baseline.</div>'
    segs = [((s[k] / total) * 100.0, _STATE_COLOR[k]) for k, _ in order if s[k] > 0]
    legend_cells = "".join(
        f'<td class="lb">{swatch(_STATE_COLOR[k])}&nbsp;{lbl}&nbsp;<b>{s[k]}</b></td>'
        for k, lbl in order if s[k] > 0
    )
    return f"""
    {stacked_bar(segs, total=510)}
    <table class="sevmix" cellpadding="0" cellspacing="0"><tr>{legend_cells}</tr></table>
    """


def _bottleneck_callout(model: dict[str, Any]) -> str:
    top = model["top_bottleneck"]
    if not top:
        return '<div class="ok-note">✓ No bottlenecks — every profiled metric is within its AMBA threshold.</div>'
    trend = _num(top.get("trend_pct"))
    trend_txt = ""
    if trend is not None and abs(trend) >= 0.1:
        trend_txt = f", trending {'+' if trend > 0 else ''}{trend:.0f}%"
    return f"""
    <table class="callout" cellpadding="0" cellspacing="0"><tr><td>
      <div class="callout-h">{swatch('#dc2626')}&nbsp;Binding bottleneck — {esc(top.get('resource_name', '—'))} · {esc(top.get('metric_name', ''))}</div>
      <div class="callout-b">{esc(_fmt_val(top.get('observed'), top.get('unit', '')))} vs threshold
        {esc(_fmt_val(top.get('threshold'), top.get('unit', '')))}
        ({_pct_txt(top.get('pct_of_threshold'))} of threshold{esc(trend_txt)}) — {esc(top.get('state', ''))}</div>
    </td></tr></table>
    """


def _state_donut(model: dict[str, Any]) -> str:
    s = model["state_counts"]
    svg = donut_svg(
        [
            (_STATE_COLOR["breaching"], s["breaching"]),
            (_STATE_COLOR["approaching"], s["approaching"]),
            (_STATE_COLOR["healthy"], s["healthy"]),
        ],
        center="state",
        accent=INK,
    )
    legend = [
        ("Breaching", str(s["breaching"]), _STATE_COLOR["breaching"]),
        ("Approaching", str(s["approaching"]), _STATE_COLOR["approaching"]),
        ("Healthy", str(s["healthy"]), _STATE_COLOR["healthy"]),
    ]
    return viz_card("Resources by state", f"{model['scorecard'].get('resources_profiled', 0)} profiled", svg, legend)


def _btype_card(model: dict[str, Any]) -> str:
    rows_data = model["btype_counts"][:7]
    if not rows_data:
        body = '<tr><td>No bottlenecks — baseline met.</td><td class="num">0</td></tr>'
    else:
        body = "".join(
            f'<tr><td class="viz-lb">{swatch(model["accent"])}&nbsp;{esc_breakable(_short_arm(t) or t)}</td>'
            f'<td class="num">{n}</td></tr>'
            for t, n in rows_data
        )
    return f"""
    <div class="viz-card">
      <div class="viz-title">Bottlenecks by resource type</div>
      <div class="viz-sub">Where the {len(model['bottlenecks'])} signal(s) concentrate</div>
      <div class="viz-body"><table class="viz-legend" cellpadding="0" cellspacing="0">{body}</table></div>
    </div>
    """


# ---------------------------------------------------------------- sections


def _cover(model: dict[str, Any]) -> str:
    conn = "demo data — not a live Azure scan" if model["demo"] else (
        "configured" if model["connection_configured"] else "not configured"
    )
    return f"""
    <div class="cover">
      <table class="cover-hero" cellpadding="0" cellspacing="0">
        <tr>
          <td class="cover-left">
            <div class="cover-brand">Azure Support Agent</div>
            <div class="cover-sub">Performance Profile Report</div>
            <div class="cover-pack">{esc(model['subtitle'])}</div>
            <div class="cover-summary">{esc(model['summary_line'])}</div>
          </td>
          <td class="cover-right">{_score_card(model)}</td>
        </tr>
      </table>

      <div class="cover-section-lbl">At a glance</div>
      {_kpi_strip(model)}

      <div class="cover-section-lbl">Resources by state</div>
      {_state_mix_bar(model)}

      <div class="cover-section-lbl">Binding bottleneck</div>
      {_bottleneck_callout(model)}

      <table class="cover-meta" cellpadding="0" cellspacing="0">
        <tr><td class="k">Scope</td><td class="v">{esc(model['scope_name'])}</td><td class="k">Scope type</td><td class="v">{esc(model['scope_kind'].title())}</td></tr>
        <tr><td class="k">Metric window</td><td class="v">{esc(model['window'])} · {esc(model['interval'])}</td><td class="k">Connection</td><td class="v">{esc(conn)}</td></tr>
        <tr><td class="k">Generated</td><td class="v">{fmt_date(model['generated_at'])}</td><td class="k">Resources</td><td class="v">{model['scorecard'].get('resources_profiled', 0)} profiled</td></tr>
      </table>

      <table class="cover-includes" cellpadding="0" cellspacing="0">
        <tr><td><b>Inside</b></td><td>Executive summary &amp; KPIs · analyst narrative · score trend · ranked bottlenecks · per-resource detail · heatmap matrix · resource inventory · methodology.</td></tr>
      </table>

      <div class="cover-foot">Confidential · for internal use. Generated {fmt_date(_now_iso())} by Azure Support Agent.</div>
    </div>
    """


def _narrative_block(model: dict[str, Any]) -> str:
    if not model["narrative"]:
        return ""
    # Keep it plain-text safe (the narrative is model-generated markdown-ish prose).
    text = esc(model["narrative"])
    return f"""
    <div class="narr">
      <div class="narr-h">Analyst summary</div>
      <div class="narr-b">{text}</div>
    </div>
    """


def _executive(model: dict[str, Any], *, anchor: str = "exec") -> str:
    return f"""
    <div class="pagebreak"></div>
    <a name="{anchor}"></a>
    <h1>Performance Profile — Executive summary</h1>
    <p class="lead">{esc(model['summary_line'])}</p>
    {_kpi_strip(model)}
    <table class="viz-grid" cellpadding="0" cellspacing="0"><tr>
      <td>{_state_donut(model)}</td>
      <td>{_btype_card(model)}</td>
    </tr></table>
    {_narrative_block(model)}
    """


def _trend_section(model: dict[str, Any], trend: dict[str, Any] | None, *, anchor: str = "trend") -> str:
    trend = trend or {}
    points = trend.get("points") or []
    ys = [p.get("pct") for p in points if isinstance(p.get("pct"), (int, float))]
    current = trend.get("current")
    previous = trend.get("previous")
    delta = trend.get("delta")
    if isinstance(delta, (int, float)):
        arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "■")
        dcolor = "#16a34a" if delta > 0 else ("#dc2626" if delta < 0 else MUTED)
        delta_txt = f'<span style="color:{dcolor}; font-weight:bold">{arrow} {abs(delta)} pts</span> vs. previous run'
    else:
        delta_txt = '<span class="muted">No previous run to compare against yet.</span>'
    svg = sparkline_svg(ys, color=model["accent"])
    cur_txt = f"{current}" if isinstance(current, (int, float)) else "—"
    prev_txt = f"{previous}" if isinstance(previous, (int, float)) else "—"
    return f"""
    <a name="{anchor}"></a>
    <h2>Score trend</h2>
    <p class="muted">How this scope's performance score has moved over time
      ({len(points)} recorded run{'s' if len(points) != 1 else ''}). Higher is better.</p>
    <table class="trend-head" cellpadding="0" cellspacing="0"><tr>
      <td><div class="trend-big" style="color:{score_color(current)}">{cur_txt}</div><div class="kpi-lbl">current</div></td>
      <td><div class="trend-big" style="color:{MUTED}">{prev_txt}</div><div class="kpi-lbl">previous</div></td>
      <td class="trend-delta">{delta_txt}</td>
    </tr></table>
    <img class="trend-img" src="{svg_data_uri(svg)}" alt="Score trend" />
    """


def _sev_chip(sev: str) -> str:
    s = normalize_severity(sev)
    return chip(_SEV_LABEL.get(s, s.title()), SEV_COLOR.get(s, MUTED))


def _state_chip(state: str) -> str:
    return chip(_STATE_LABEL.get(state, state.title()), _STATE_COLOR.get(state, MUTED))


def _bottlenecks_section(model: dict[str, Any], *, anchor: str = "bottlenecks") -> str:
    bottlenecks = sorted(
        model["bottlenecks"],
        key=lambda b: (
            _STATE_ORDER.get(b.get("state"), 9),
            SEV_RANK.get(normalize_severity(b.get("severity")), 9),
            -(_num(b.get("pct_of_threshold")) or 0),
        ),
    )
    if not bottlenecks:
        return f"""
        <div class="pagebreak"></div>
        <a name="{anchor}"></a>
        <h1>Ranked bottlenecks</h1>
        <p class="ok-note">✓ No bottlenecks — every profiled metric is within its AMBA threshold for this scope.</p>
        """
    rows = []
    for b in bottlenecks:
        name_link = _resource_link(f'<b>{esc_breakable(b.get("resource_name", "—"))}</b>', b.get("resource_id"))
        obs = _fmt_val(b.get("observed"), b.get("unit", ""))
        thr = _fmt_val(b.get("threshold"), b.get("unit", ""))
        why = esc((b.get("why") or "").strip())
        rows.append(
            f'<tr>'
            f'<td>{_state_chip(b.get("state", ""))}</td>'
            f'<td>{_sev_chip(b.get("severity", ""))}</td>'
            f'<td>{name_link}<br/><span class="muted">{esc_breakable(_short_arm(b.get("resource_type")) or b.get("resource_type") or "")}</span></td>'
            f'<td>{esc(b.get("metric_name") or b.get("metric") or "—")}</td>'
            f'<td class="num">{esc(obs)} / {esc(thr)}<br/><b>{_pct_txt(b.get("pct_of_threshold"))}</b></td>'
            f'<td>{_trend_chip(b.get("trend_pct"))}</td>'
            f'<td class="why">{why or "—"}</td>'
            f'</tr>'
        )
    return f"""
    <div class="pagebreak"></div>
    <a name="{anchor}"></a>
    <h1>Ranked bottlenecks</h1>
    <p class="muted">{len(bottlenecks)} metric(s) breaching or approaching their AMBA threshold, worst first.
      "Observed / threshold" shows the worst reading in the window against the baseline; the ➚ arrow opens the resource in the Azure portal.</p>
    <table class="grid btl" cellpadding="0" cellspacing="0">
      <tr><th width="9%">State</th><th width="8%">Severity</th><th width="20%">Resource</th><th width="17%">Metric</th><th width="14%">Observed / thr</th><th width="7%">Trend</th><th width="25%">Why it matters</th></tr>
      {''.join(rows)}
    </table>
    """


def _resource_detail_section(model: dict[str, Any], *, anchor: str = "resource-detail") -> str:
    """Per-resource detail: each non-healthy resource and its offending metric cells."""
    rows_src = [r for r in model["resources"] if r.get("state") in ("breaching", "approaching")]
    rows_src.sort(key=lambda r: (_int(r.get("score"), 100), str(r.get("resource_name") or "")))
    if not rows_src:
        return f"""
        <div class="pagebreak"></div>
        <a name="{anchor}"></a>
        <h1>Resource performance detail</h1>
        <p class="ok-note">✓ Every profiled resource is healthy — no resource has a breaching or approaching metric.</p>
        """
    blocks = []
    for r in rows_src:
        cells = [c for c in (r.get("cells") or []) if c.get("state") in ("breaching", "approaching")]
        cells.sort(key=lambda c: (_STATE_ORDER.get(c.get("state"), 9), -(_num(c.get("pct_of_threshold")) or 0)))
        name_link = _resource_link(esc_breakable(r.get("resource_name", "—")), r.get("resource_id"))
        cell_rows = "".join(
            f'<tr>'
            f'<td>{_state_chip(c.get("state", ""))}</td>'
            f'<td>{esc(c.get("name") or c.get("metric") or "—")}</td>'
            f'<td class="num">{esc(_fmt_val(c.get("observed"), c.get("unit", "")))}</td>'
            f'<td class="num">{esc(_fmt_val(c.get("threshold"), c.get("unit", "")))}</td>'
            f'<td class="num"><b>{_pct_txt(c.get("pct_of_threshold"))}</b></td>'
            f'<td>{_trend_chip(c.get("trend_pct"))}</td>'
            f'</tr>'
            for c in cells
        )
        score = r.get("score")
        blocks.append(f"""
        <table class="rdetail-head" cellpadding="0" cellspacing="0"><tr>
          <td class="rd-name">{name_link} <span class="muted">· {esc_breakable(_short_arm(r.get("resource_type")) or "")}</span></td>
          <td class="rd-score" style="color:{score_color(score)}">{esc(str(score) if score is not None else '—')}<span class="rd-score-u">/100</span></td>
        </tr></table>
        <table class="grid compact rdetail" cellpadding="0" cellspacing="0">
          <tr><th width="14%">State</th><th width="34%">Metric</th><th width="16%">Observed</th><th width="16%">Threshold</th><th width="10%">% thr</th><th width="10%">Trend</th></tr>
          {cell_rows}
        </table>
        """)
    return f"""
    <div class="pagebreak"></div>
    <a name="{anchor}"></a>
    <h1>Resource performance detail</h1>
    <p class="muted">{len(rows_src)} resource(s) with a breaching or approaching metric, worst score first.
      Each block lists only that resource's offending metrics.</p>
    {''.join(blocks)}
    """


def _heatmap_section(model: dict[str, Any], *, anchor: str = "appendix-heatmap") -> str:
    """The resource × metric matrix, grouped by resource type so each block stays narrow."""
    resources = model["resources"]
    if not resources:
        return ""
    # Group resources by type, preserving the metric column order from each type's cells.
    groups: dict[str, dict[str, Any]] = {}
    for r in resources:
        rt = str(r.get("resource_type") or "—")
        g = groups.setdefault(rt, {"display": r.get("display") or _short_arm(rt), "metrics": [], "seen": set(), "rows": []})
        for c in (r.get("cells") or []):
            m = c.get("metric")
            if m and m not in g["seen"]:
                g["seen"].add(m)
                g["metrics"].append({"metric": m, "name": c.get("name") or m})
        g["rows"].append(r)

    blocks = []
    for rt, g in sorted(groups.items(), key=lambda kv: str(kv[1]["display"]).lower()):
        metrics = g["metrics"]
        if not metrics:
            continue
        head_cells = "".join(f'<th class="hm-h">{esc(m["name"])}</th>' for m in metrics)
        body_rows = []
        for r in sorted(g["rows"], key=lambda x: (_int(x.get("score"), 100), str(x.get("resource_name") or ""))):
            by_metric = {c.get("metric"): c for c in (r.get("cells") or [])}
            cell_html = []
            for m in metrics:
                c = by_metric.get(m["metric"])
                if not c:
                    cell_html.append('<td class="hm" style="color:#d1d5db">·</td>')
                    continue
                st = c.get("state", "no_data")
                pct = _num(c.get("pct_of_threshold"))
                txt = f"{pct:.0f}%" if pct is not None else ("—" if st == "no_data" else "ok")
                cell_html.append(
                    f'<td class="hm" style="background:{_STATE_BG.get(st, "#fff")}; color:{_STATE_COLOR.get(st, MUTED)}">{txt}</td>'
                )
            score = r.get("score")
            # The heatmap name column is ~24% of the page (~27 chars at 8px), so only break
            # genuinely long names — an 18-char break forced common names like
            # "contoso-property-api" onto two lines, which made the rows look cramped.
            name_link = _resource_link(esc_breakable(r.get("resource_name", "—"), width=26), r.get("resource_id"))
            body_rows.append(
                f'<tr><td class="hm-name">{name_link}</td>'
                f'<td class="hm-score" style="color:{score_color(score)}">{esc(str(score) if score is not None else "—")}</td>'
                f'{"".join(cell_html)}</tr>'
            )
        blocks.append(f"""
        <h2>{esc(g["display"])} <span class="muted">· {len(g["rows"])} resource(s)</span></h2>
        <table class="grid heatmap" cellpadding="0" cellspacing="0">
          <thead><tr><th class="hm-name-h">Resource</th><th class="hm-score-h">Score</th>{head_cells}</tr></thead>
          <tbody>{''.join(body_rows)}</tbody>
        </table>
        """)
    if not blocks:
        return ""
    return f"""
    <div class="pagebreak"></div>
    <a name="{anchor}"></a>
    <h1>Appendix — Performance heatmap</h1>
    <p class="muted">Each cell is the metric's worst reading as a percent of its AMBA threshold
      (<span style="color:{_STATE_COLOR['breaching']}">red = breaching</span>,
      <span style="color:{_STATE_COLOR['approaching']}">amber = approaching</span>,
      <span style="color:{_STATE_COLOR['healthy']}">green = healthy</span>, · = not applicable).
      Grouped by resource type.</p>
    {''.join(blocks)}
    """


def _inventory_section(model: dict[str, Any], *, anchor: str = "appendix-inventory") -> str:
    resources = model["all_resources"]
    if not resources:
        return ""
    ordered = sorted(
        resources,
        key=lambda r: (0 if r.get("in_reference") else 1, str(r.get("type") or ""), str(r.get("name") or "")),
    )
    rows = []
    for r in ordered:
        in_ref = bool(r.get("in_reference"))
        status = '<span style="color:#16a34a">✓ profiled</span>' if in_ref else '<span class="muted">— not in reference</span>'
        name_link = _resource_link(esc_breakable(r.get("name") or "—"), r.get("id"))
        rows.append(
            f'<tr>'
            f'<td>{status}</td>'
            f'<td>{name_link}</td>'
            f'<td>{esc(_short_arm(r.get("type")) or r.get("type") or "—")}</td>'
            f'<td>{esc(r.get("resourceGroup") or r.get("resource_group") or "")}</td>'
            f'<td>{esc(r.get("location") or "")}</td>'
            f'<td>{esc(_short_sub(r.get("subscriptionId") or r.get("subscription_id")))}</td>'
            f'</tr>'
        )
    in_ref_count = sum(1 for r in resources if r.get("in_reference"))
    return f"""
    <div class="pagebreak"></div>
    <a name="{anchor}"></a>
    <h1>Appendix — Resource inventory</h1>
    <p class="muted">{len(resources)} in-scope resource(s) — {in_ref_count} profiled against the AMBA reference, shown first.
      The ➚ arrow opens the resource in the Azure portal.</p>
    <table class="grid compact" cellpadding="0" cellspacing="0">
      <tr><th width="14%">Status</th><th width="26%">Name</th><th width="24%">Type</th><th width="18%">Resource group</th><th width="11%">Location</th><th width="7%">Sub</th></tr>
      {''.join(rows)}
    </table>
    """


def _methodology(model: dict[str, Any], *, anchor: str = "appendix-meta") -> str:
    return f"""
    <div class="pagebreak"></div>
    <a name="{anchor}"></a>
    <h1>Methodology &amp; metadata</h1>
    <table class="meta" cellpadding="0" cellspacing="0">
      <tr><td class="k">Report</td><td class="v">{esc(model['title'])} — {esc(model['subtitle'])}</td></tr>
      <tr><td class="k">Scope</td><td class="v">{esc(model['scope_name'])} ({esc(model['scope_kind'])})</td></tr>
      <tr><td class="k">Run generated</td><td class="v">{fmt_date(model['generated_at'])}</td></tr>
      <tr><td class="k">Metric window</td><td class="v">{esc(model['window'])} at {esc(model['interval'])} grain</td></tr>
      <tr><td class="k">Data source</td><td class="v">{esc(model['source'])}</td></tr>
      <tr><td class="k">Connection</td><td class="v">{'configured' if model['connection_configured'] else 'demo / not configured'}</td></tr>
      <tr><td class="k">Resources profiled</td><td class="v">{model['scorecard'].get('resources_profiled', 0)}</td></tr>
      <tr><td class="k">Bottlenecks</td><td class="v">{len(model['bottlenecks'])}</td></tr>
    </table>
    <p class="muted" style="margin-top:8px">
      Each in-scope resource is profiled against an editable, versioned per-type AMBA reference (the same
      baseline the Monitoring-Coverage detector uses). For every metric alert the profiler reads the live
      Azure Monitor series over the window, takes the worst reading in the direction of concern, and expresses
      it as a percent of the AMBA threshold — healthy (&lt;70%), approaching (70–100%) or breaching (≥100%).
      The per-resource score is a severity-weighted 0–100 (breaching costs the full weight, approaching half);
      the workload score is the mean across profiled resources. This report renders one saved profile run; re-run
      the profiler in the app to refresh.
    </p>
    """


# ---------------------------------------------------------------- shell + CSS


def _doc_css() -> str:
    return base_css() + f"""
.cover {{ margin-top: 1.4cm; }}
.portal-link {{ color: {BRAND}; text-decoration: none; font-weight: bold; }}
.cover-section-lbl {{ font-size: 9px; font-weight: bold; color: {MUTED}; text-transform: uppercase;
    letter-spacing: 0.5px; margin: 14px 0 4px 0; }}
.score-card {{ width: 200px; }}
.score-card td {{ border: 1px solid {LINE}; border-radius: 6px; padding: 14px 16px 12px 16px; background: #fafafa; }}
.score-num {{ font-size: 44px; font-weight: bold; line-height: 1.0; }}
.score-unit {{ font-size: 16px; color: {MUTED}; font-weight: normal; }}
.score-lbl {{ font-size: 10px; color: {MUTED}; margin: 2px 0 7px 0; text-transform: uppercase; letter-spacing: 0.5px; }}
.sevmix {{ width: 100%; margin-top: 4px; font-size: 8.5px; color: {INK}; }}
.sevmix .lb {{ padding: 1px 14px 1px 0; vertical-align: middle; }}
.lead {{ font-size: 10px; color: {INK}; margin-bottom: 6px; }}

/* binding-bottleneck callout */
.callout {{ width: 100%; margin: 2px 0 2px 0; }}
.callout td {{ border: 1px solid #fecaca; border-radius: 6px; background: #fef2f2; padding: 8px 12px; }}
.callout-h {{ font-size: 10.5px; font-weight: bold; color: #b91c1c; }}
.callout-b {{ font-size: 9px; color: #7f1d1d; margin-top: 2px; }}

/* analyst narrative */
.narr {{ margin: 10px 0 2px 0; border: 0.5px solid {LINE}; border-radius: 6px; background: #fafafa; padding: 9px 12px; }}
.narr-h {{ font-size: 9px; font-weight: bold; color: {MUTED}; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 3px; }}
.narr-b {{ font-size: 9.5px; color: {INK}; line-height: 1.5; }}

/* trend */
.trend-head {{ width: 100%; margin: 4px 0 8px 0; }}
.trend-head td {{ width: 22%; vertical-align: bottom; padding: 2px 6px; }}
.trend-head .trend-delta {{ width: 56%; text-align: right; font-size: 10px; color: {INK}; }}
.trend-big {{ font-size: 22px; font-weight: bold; }}
.trend-img {{ display: block; width: 500px; margin: 4px 0 8px 0; border: 0.5px solid {LINE}; }}
.ok-note {{ font-size: 10px; color: #15803d; background: #f0fdf4; border: 0.5px solid #bbf7d0; padding: 8px 10px; }}

/* bottlenecks + detail tables */
.btl .why {{ color: {MUTED}; font-size: 8px; }}
.btl .num {{ font-size: 8.5px; }}
.grid .num {{ text-align: right; white-space: nowrap; }}

/* per-resource detail blocks */
.rdetail-head {{ width: 100%; margin: 10px 0 2px 0; -pdf-keep-with-next: true; }}
.rd-name {{ font-size: 10.5px; font-weight: bold; color: {INK}; }}
.rd-score {{ text-align: right; font-size: 14px; font-weight: bold; width: 70px; }}
.rd-score-u {{ font-size: 9px; color: {MUTED}; font-weight: normal; }}
.rdetail {{ margin-bottom: 4px; }}

/* heatmap matrix */
.heatmap th, .heatmap td {{ text-align: center; font-size: 7.5px; padding: 4px 3px; }}
/* Header text is TOP-aligned so single- and two-line metric names both start at the top of
   the grey band; the band's bottom padding then guarantees a consistent gap above the
   divider and the first data row (bottom-aligning made single-line headers hug the data). */
.heatmap th {{ background-color: #f3f4f6; border-left: 0.5px solid #e5e7eb;
    border-bottom: 1.5px solid #9ca3af; vertical-align: top; padding: 6px 3px 10px 3px; }}
/* Data cells: value vertically centered, generous row height for clear separation. */
.heatmap td {{ border-left: 0.5px solid #f3f4f6; vertical-align: middle; height: 18px;
    padding: 7px 3px; }}
.heatmap .hm-name-h, .heatmap .hm-name {{ text-align: left; width: 24%; border-left: 0; }}
.heatmap .hm-name {{ font-size: 8px; }}
.heatmap .hm-score-h, .heatmap .hm-score {{ width: 8%; font-weight: bold; }}
.heatmap .hm-h {{ font-size: 6.8px; line-height: 1.2; color: {MUTED}; }}
.heatmap .hm {{ font-weight: bold; }}
"""


def _shell(header_right: str, body: str) -> str:
    header = (
        '<table cellpadding="0" cellspacing="0" width="18cm"><tr>'
        '<td><span class="brand">Azure Support Agent</span> &nbsp; Performance Profile Report</td>'
        f'<td style="text-align:right">{esc(header_right)}</td>'
        "</tr></table>"
    )
    footer = "Confidential &nbsp;·&nbsp; page <pdf:pagenumber> of <pdf:pagecount>"
    return (
        "<html><head><meta charset='utf-8'><style>"
        + _doc_css()
        + "</style></head><body>"
        + running_frames(header, footer)
        + body
        + "</body></html>"
    )


def _toc(entries: list[tuple[str, str, int]], page_map: dict[str, int] | None) -> str:
    if page_map is None:
        return """
        <div class="pagebreak"></div>
        <a name="toc"></a>
        <div class="toc-title">Contents</div>
        <div class="toc-note">Generating section links and page numbers…</div>
        """
    rows = []
    for anchor, label, level in entries:
        page_txt = str(page_map.get(anchor, "—"))
        rows.append(
            f'<tr class="toc-row level-{level}"><td class="toc-link"><a href="#{anchor}">{esc(label)}</a></td>'
            f'<td class="toc-page">{esc(page_txt)}</td></tr>'
        )
    return f"""
    <div class="pagebreak"></div>
    <a name="toc"></a>
    <div class="toc-title">Contents</div>
    <div class="toc-note">Section links and page numbers generated from the rendered report.</div>
    <table class="toc-table" cellpadding="0" cellspacing="0">{''.join(rows)}</table>
    """


# ---------------------------------------------------------------- public builders


def build_performance_pdf(snap: dict[str, Any], trend: dict[str, Any] | None = None) -> bytes:
    """Render one Performance Profiler run snapshot to branded PDF bytes."""
    model = _adapt(snap)
    entries = [
        ("exec", "Performance Profile — Executive summary", 0),
        ("trend", "Score trend", 1),
        ("bottlenecks", "Ranked bottlenecks", 0),
        ("resource-detail", "Resource performance detail", 0),
    ]
    if model["resources"]:
        entries.append(("appendix-heatmap", "Appendix — Performance heatmap", 1))
    if model["all_resources"]:
        entries.append(("appendix-inventory", "Appendix — Resource inventory", 1))
    entries.append(("appendix-meta", "Methodology & metadata", 1))

    header_right = f"{model['scope_name']} · {fmt_date(model['generated_at'])}"

    def _compose(page_map: dict[str, int] | None) -> str:
        exec_block = _executive(model) + _trend_section(model, trend)
        parts = [
            _cover(model),
            _toc(entries, page_map),
            exec_block,
            _bottlenecks_section(model),
            _resource_detail_section(model),
        ]
        if model["resources"]:
            parts.append(_heatmap_section(model))
        if model["all_resources"]:
            parts.append(_inventory_section(model))
        parts.append(_methodology(model))
        return "".join(parts)

    return render_two_pass(
        lambda body: _shell(header_right, body),
        _compose,
        entries,
    )


def build_evidence_content(
    snap: dict[str, Any],
) -> tuple[str, dict[str, Any], list[str], list[str], dict[str, Any]]:
    """Map a Performance run snapshot into the Evidence Locker content shape.

    Returns ``(name, scope, included, tags, content)`` ready for
    ``app.evidence.registry.create_snapshot``. Captures the bottlenecks (findings), the
    headline metrics, and the full scanned inventory so the run is independently auditable.
    """
    model = _adapt(snap)
    findings = [
        {
            "id": f"perf-bottleneck-{i}",
            "title": f"{b.get('resource_name', '—')} — {b.get('metric_name') or b.get('metric') or 'metric'} "
                     f"at {_pct_txt(b.get('pct_of_threshold'))} of threshold",
            "severity": normalize_severity(b.get("severity")),
            "status": b.get("state") or "breaching",
            "resource_name": b.get("resource_name") or "—",
            "resource_type": b.get("resource_type") or "—",
            "metric": b.get("metric_name") or b.get("metric") or "",
            "observed": b.get("observed"),
            "threshold": b.get("threshold"),
            "pct_of_threshold": b.get("pct_of_threshold"),
            "feature": "performance",
        }
        for i, b in enumerate(model["bottlenecks"])
    ]
    metrics = {
        "feature": "performance",
        "headline_label": model["headline_label"],
        "score": model["score"],
        "state_counts": model["state_counts"],
        "kpis": [{"label": label, "value": str(value), "sub": sub} for label, value, sub in model["kpis"]],
        "raw": model["scorecard"],
    }
    content = {
        "findings": findings,
        "metrics": metrics,
        "inventory": list(snap.get("all_resources") or []),
    }
    name = f"{model['title']} — {model['scope_name']} — {fmt_date(model['generated_at'])}"
    scope = {"kind": model["scope_kind"], "id": model["scope_id"], "name": model["scope_name"]}
    included = ["findings", "metrics", "inventory"]
    tags = ["performance"]
    return name, scope, included, tags, content
