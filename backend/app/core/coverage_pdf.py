"""Branded PDF reports for the three coverage detectors — Monitoring (AMBA), Telemetry and
Backup & DR — plus a combined "Estate Coverage" report that stitches all three for one scope.

Renders the *latest* cached coverage snapshot (the same payload the dashboards show) into a
paginated, branded PDF via the shared ``app.core.pdf_common`` engine (xhtml2pdf / reportlab —
pure-Python, identical on Windows dev and the Linux container).

Per-feature document layout:

1. Cover            — title, scope, date, headline coverage %.
2. Table of contents — clickable links with page numbers.
3. Executive summary — headline %, KPI strip, severity mix donut, one-line summary.
4. Trend            — %-over-time sparkline + current / previous / delta (from coverage_trends).
5. Gaps & remediation — every gap grouped by severity, with the failed check + detail.
6. Appendix         — full scanned-resource inventory.
7. Methodology      — scope, source, freshness, how the score is computed.

The estate report reuses the executive + trend + top-gaps sections for each feature under
one cover + TOC.

``build_evidence_content`` maps a snapshot into the Evidence Locker's JSON content shape so a
coverage scan can be captured as an immutable, hash-stamped evidence snapshot.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from app.core.pdf_common import (
    BRAND,
    INK,
    LINE,
    MUTED,
    SEV_COLOR,
    SEV_ORDER,
    SEV_RANK,
    bar,
    base_css,
    donut_svg,
    esc,
    fmt_date,
    normalize_severity,
    render_two_pass,
    running_frames,
    score_color,
    sparkline_svg,
    svg_data_uri,
    swatch,
    viz_card,
)

# ---------------------------------------------------------------- feature metadata

FEATURES = ("amba", "telemetry", "backupdr")

_FEATURE_META = {
    "amba": {
        "title": "Monitoring Coverage",
        "subtitle": "Azure Monitor Baseline Alerts (AMBA)",
        "headline_label": "Alert coverage",
        "accent": "#0f766e",
    },
    "telemetry": {
        "title": "Telemetry Coverage",
        "subtitle": "Diagnostic Settings / Log Coverage",
        "headline_label": "Telemetry coverage",
        "accent": "#2563eb",
    },
    "backupdr": {
        "title": "Backup & DR Coverage",
        "subtitle": "Backup & Disaster-Recovery Posture",
        "headline_label": "Protected",
        "accent": "#7c3aed",
    },
}

_SEV_LABEL = {"critical": "Critical", "error": "High", "warning": "Warning", "info": "Info"}

# Short, human remediation hints keyed off each detector's failure vocabulary. The full
# fix (Bicep / Azure Policy / runbook) is generated in-app; these orient the reader.
_BDR_FIX = {
    "backup_enabled": "Enable backup",
    "backup_policy": "Attach a backup policy",
    "policy_retention": "Set adequate retention",
    "retention": "Set adequate retention",
    "recent_job": "Fix failing backup jobs",
    "last_job": "Fix failing backup jobs",
    "offsite": "Enable geo / offsite copy",
    "geo": "Enable geo / offsite copy",
    "dr_pair": "Configure a DR pair",
    "dr": "Configure a DR pair",
    "dr_drill": "Run a failover drill",
    "drill": "Run a failover drill",
    "encryption": "Enable CMK/PMK encryption",
    "cmk": "Enable CMK encryption",
    "soft_delete": "Enable soft-delete",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _pct(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, min(100, round(float(value))))
    except (TypeError, ValueError):
        return None


def _short_arm(value: Any) -> str:
    """Last path segment of an ARM id (e.g. a Log Analytics workspace) so long, unbreakable
    resource ids don't overflow the page. Non-ARM strings pass through unchanged."""
    s = str(value or "").strip()
    if "/" in s:
        return s.rstrip("/").rsplit("/", 1)[-1] or s
    return s


def _short_sub(sub: Any) -> str:
    """A subscription id rendered as a short prefix, suppressing all-zero / demo subs (whose
    8-char prefix is all zeros) since they add only noise when every resource shares one."""
    s = str(sub or "").strip()
    if not s:
        return ""
    prefix = s.replace("-", "")[:8]
    if not prefix or set(prefix) <= {"0"}:
        return ""
    return s[:8]


def _portal_url(resource_id: Any) -> str:
    """An Azure Portal deep link to a resource's overview blade, from its ARM id.

    Only real ARM ids (``/subscriptions/<guid>/...``) with a non-zero subscription produce a
    link; demo/all-zero ids return ``""`` so the report doesn't emit dead links."""
    rid = str(resource_id or "").strip()
    if not rid.lower().startswith("/subscriptions/"):
        return ""
    parts = rid.split("/")
    sub = parts[2] if len(parts) > 2 else ""
    if not sub or set(sub.replace("-", "")) <= {"0"}:
        return ""
    return f"https://portal.azure.com/#@/resource{rid}/overview"


def _resource_link(name_html: str, resource_id: Any) -> str:
    """Render the resource name as plain text plus a trailing Azure-portal launch arrow.

    Per design: the resource NAME is no longer the hyperlink. Instead a small top-right
    arrow (↗) follows the name and is the clickable link, opening the resource's Azure
    portal blade in a new window/tab. When no real ARM id is available (demo/zero ids),
    only the plain name is rendered.
    """
    url = _portal_url(resource_id)
    if not url:
        return name_html
    arrow = (
        f'<a href="{url}" target="_blank" class="portal-link" '
        f'title="Open in Azure portal (new window)">&#10138;</a>'
    )
    return f"{name_html}&nbsp;{arrow}"


def _bdr_remediation(failed_checks: list[str]) -> str:
    fixes: list[str] = []
    for c in failed_checks or []:
        hint = _BDR_FIX.get(str(c).lower())
        if hint and hint not in fixes:
            fixes.append(hint)
    return "; ".join(fixes) or "Restore protection to the reference baseline"


# ---------------------------------------------------------------- snapshot → normalized model


def _adapt(feature: str, snap: dict[str, Any]) -> dict[str, Any]:
    """Normalize a feature snapshot into the common model the renderer consumes."""
    meta = _FEATURE_META.get(feature, _FEATURE_META["amba"])
    model: dict[str, Any] = {
        "feature": feature,
        "title": meta["title"],
        "subtitle": meta["subtitle"],
        "headline_label": meta["headline_label"],
        "accent": meta["accent"],
        "scope_name": snap.get("scope_name") or snap.get("scope_id") or "—",
        "scope_kind": snap.get("scope_kind") or "workload",
        "scope_id": snap.get("scope_id") or "",
        "generated_at": snap.get("generated_at"),
        "connection_configured": bool(snap.get("connection_configured")),
        "demo": bool(snap.get("demo")),
        "source": snap.get("source") or "—",
        "resources": list(snap.get("all_resources") or []),
        "kpis": [],
        "gaps": [],
        "headline_pct": None,
        "summary_line": "",
    }

    if feature == "backupdr":
        sc = snap.get("scorecard") or {}
        total, protected = _int(sc.get("total")), _int(sc.get("protected"))
        last_drill = sc.get("last_drill_days")
        model["headline_pct"] = _pct(sc.get("pct_protected"))
        model["kpis"] = [
            ("Protected", f"{protected}/{total}", f"{_int(sc.get('pct_protected'))}%"),
            ("Offsite / geo", f"{_int(sc.get('pct_offsite'))}%", "redundant copy"),
            ("Recent job", f"{_int(sc.get('pct_recent_job'))}%", "within SLA"),
            ("DR pairs", str(_int(sc.get("dr_pairs"))), f"{_int(sc.get('dr_pairs_unhealthy'))} unhealthy"),
            ("Stale DR", str(_int(sc.get("dr_pairs_stale"))), "not drilled"),
            ("Last drill", f"{_int(last_drill)}d ago" if last_drill is not None else "—", "failover test"),
        ]
        model["summary_line"] = (
            f"{protected} of {total} in-scope resources are protected "
            f"({_int(sc.get('pct_protected'))}%); {_int(sc.get('dr_pairs'))} DR pair(s) configured, "
            f"{_int(sc.get('dr_pairs_stale'))} not recently drilled."
        )
        for g in snap.get("gaps") or []:
            checks = g.get("failed_checks") or []
            checks_txt = ", ".join(checks) or "—"
            region = g.get("region") or ""
            backup_region = g.get("backup_region") or ""
            parts = [f"Failed: {checks_txt}"]
            if region and backup_region and region != backup_region:
                parts.append(f"backup in {backup_region} (vs {region})")
            vault = g.get("vault_name") or ""
            if vault:
                parts.append(f"vault {vault}")
            model["gaps"].append({
                "id": g.get("resource_id") or "",
                "name": g.get("resource_name") or "—",
                "type": g.get("resource_type") or "—",
                "rg": g.get("resource_group") or "",
                "sub": g.get("subscription_id") or "",
                "severity": normalize_severity(g.get("severity")),
                "status": g.get("status") or "gap",
                "detail": " · ".join(parts),
                "fix": _bdr_remediation(checks),
            })
    else:
        kpis = snap.get("kpis") or {}
        model["headline_pct"] = _pct(snap.get("coverage_pct"))
        if feature == "telemetry":
            total = _int(kpis.get("total_resources_in_reference"))
            with_all = _int(kpis.get("with_all_categories"))
            model["kpis"] = [
                ("In reference", str(total), "resources in scope"),
                ("Any diagnostics", str(_int(kpis.get("with_any_diag"))), f"{_int(kpis.get('pct_with_any_diag'))}%"),
                ("All categories", str(with_all), f"{_int(kpis.get('pct_with_all_categories'))}%"),
                ("To approved WS", str(_int(kpis.get("to_approved_workspace"))), f"{_int(kpis.get('pct_to_approved'))}%"),
                ("Unknown dest", str(_int(kpis.get("unknown_destinations"))), "destination drift"),
                ("Unreadable", str(_int(kpis.get("unreadable"))), "no access"),
            ]
            model["summary_line"] = (
                f"{model['headline_pct'] if model['headline_pct'] is not None else 0}% telemetry coverage — "
                f"{with_all} of {total} resources ship all recommended log/metric categories to an approved workspace."
            )
            for g in snap.get("gaps") or []:
                missing = [c for c in (g.get("missing_categories") or []) if c]
                drift_ws = [_short_arm(w) for w in (g.get("drift_workspaces") or []) if w]
                parts: list[str] = []
                if missing:
                    parts.append("Missing: " + ", ".join(missing))
                if g.get("has_drift"):
                    parts.append("drift to " + (", ".join(drift_ws) if drift_ws else "an unapproved workspace"))
                if not parts:
                    parts.append("No diagnostic settings")
                fix = "Add a diagnostic setting for the missing categories" if missing else "Route logs to an approved workspace"
                if g.get("has_drift") and missing:
                    fix = "Add the missing categories and route to an approved workspace"
                model["gaps"].append({
                    "id": g.get("resource_id") or "",
                    "name": g.get("resource_name") or "—",
                    "type": g.get("resource_type") or "—",
                    "rg": g.get("resource_group") or "",
                    "sub": g.get("subscription_id") or "",
                    "severity": normalize_severity(g.get("severity")),
                    "status": g.get("status") or "gap",
                    "detail": " · ".join(parts),
                    "fix": fix,
                })
        else:  # amba
            total = _int(kpis.get("total_resources_in_baseline"))
            model["kpis"] = [
                ("In baseline", str(total), "resources in scope"),
                ("Alerts present", str(_int(kpis.get("alerts_present"))), "configured"),
                ("Alerts missing", str(_int(kpis.get("alerts_missing"))), "gaps"),
                ("Misconfigured", str(_int(kpis.get("alerts_misconfigured"))), "wrong threshold"),
                ("Recommended", str(_int(kpis.get("recommended_total"))), "baseline alerts"),
            ]
            model["summary_line"] = (
                f"{model['headline_pct'] if model['headline_pct'] is not None else 0}% of recommended baseline "
                f"alerts are configured across {total} resources."
            )
            for g in snap.get("gaps") or []:
                alert = g.get("alert_name") or g.get("alert_key") or "alert"
                category = g.get("amba_category") or "—"
                why = (g.get("why") or "").strip()
                parts = [f"{alert} ({category})"]
                if why:
                    parts.append(why)
                model["gaps"].append({
                    "id": g.get("resource_id") or "",
                    "name": g.get("resource_name") or "—",
                    "type": g.get("resource_type") or "—",
                    "rg": g.get("resource_group") or "",
                    "sub": g.get("subscription_id") or "",
                    "severity": normalize_severity(g.get("severity")),
                    "status": g.get("status") or "missing",
                    "detail": " · ".join(parts),
                    "fix": f"Create the '{alert}' metric alert wired to an action group",
                })

    counts: Counter[str] = Counter(normalize_severity(g["severity"]) for g in model["gaps"])
    model["severity_counts"] = {s: counts.get(s, 0) for s in SEV_ORDER}
    # Gap rollups used by the exec + inventory sections.
    type_counts: Counter[str] = Counter(g["type"] for g in model["gaps"])
    model["gap_type_counts"] = type_counts.most_common()
    model["gapped_ids"] = {g["id"] for g in model["gaps"] if g.get("id")}
    model["gapped_names"] = {g["name"] for g in model["gaps"] if g.get("name")}
    return model


# ---------------------------------------------------------------- section renderers


def _sev_label_chip(sev: str) -> str:
    """A severity chip using the report's display vocabulary (Critical / High / Warning /
    Info) so gap rows agree with the donut + cover legend."""
    s = normalize_severity(sev)
    from app.core.pdf_common import chip
    return chip(_SEV_LABEL.get(s, s.title()), SEV_COLOR.get(s, MUTED))


def _score_card(model: dict[str, Any], *, big: bool = True) -> str:
    """The unified headline: number + label + progress bar in one connected card. Rendered
    as a single-cell table so xhtml2pdf keeps the bordered box cohesive (a bordered <div>
    with stacked block children splits into separate boxes)."""
    pct = model["headline_pct"]
    txt = f"{pct:.0f}" if isinstance(pct, (int, float)) else "—"
    color = score_color(pct)
    return f"""
    <table class="score-card" width="200" cellpadding="0" cellspacing="0"><tr><td>
      <div class="score-num" style="color:{color}">{txt}<span class="score-unit">%</span></div>
      <div class="score-lbl">{esc(model['headline_label'])}</div>
      {bar(pct if isinstance(pct, (int, float)) else 0, color, total=176)}
    </td></tr></table>
    """


def _sev_mix_bar(model: dict[str, Any]) -> str:
    """A labeled stacked severity bar + inline legend — a visual severity mix for the cover."""
    sev = model["severity_counts"]
    total = sum(sev.values())
    order = [("critical", "Critical"), ("error", "High"), ("warning", "Warning"), ("info", "Info")]
    if total <= 0:
        return '<div class="ok-note">No open gaps — every in-scope resource meets the reference baseline.</div>'
    segs = [((sev[k] / total) * 100.0, SEV_COLOR[k]) for k, _ in order if sev[k] > 0]
    from app.core.pdf_common import stacked_bar, swatch
    # Inline colored-bullet legend (one cell per severity) under the stacked bar.
    legend_cells = "".join(
        f'<td class="lb">{swatch(SEV_COLOR[k])}&nbsp;{lbl}&nbsp;<b>{sev[k]}</b></td>'
        for k, lbl in order if sev[k] > 0
    )
    return f"""
    {stacked_bar(segs, total=510)}
    <table class="sevmix" cellpadding="0" cellspacing="0"><tr>{legend_cells}</tr></table>
    """


def _cover(model: dict[str, Any]) -> str:
    gaps = model["gaps"]
    demo_note = "demo data — not a live Azure scan" if model["demo"] else (
        "configured" if model["connection_configured"] else "not configured"
    )
    return f"""
    <div class="cover">
      <table class="cover-hero" cellpadding="0" cellspacing="0">
        <tr>
          <td class="cover-left">
            <div class="cover-brand">Azure Support Agent</div>
            <div class="cover-sub">{esc(model['title'])} Report</div>
            <div class="cover-pack">{esc(model['subtitle'])}</div>
            <div class="cover-summary">{esc(model['summary_line'])}</div>
          </td>
          <td class="cover-right">{_score_card(model)}</td>
        </tr>
      </table>

      <div class="cover-section-lbl">At a glance</div>
      {_kpi_strip(model)}

      <div class="cover-section-lbl">Open gaps by severity ({len(gaps)} total)</div>
      {_sev_mix_bar(model)}

      <table class="cover-meta" cellpadding="0" cellspacing="0">
        <tr><td class="k">Scope</td><td class="v">{esc(model['scope_name'])}</td><td class="k">Scope type</td><td class="v">{esc(model['scope_kind'].title())}</td></tr>
        <tr><td class="k">Generated</td><td class="v">{fmt_date(model['generated_at'])}</td><td class="k">Connection</td><td class="v">{esc(demo_note)}</td></tr>
        <tr><td class="k">Data source</td><td class="v">{esc(model['source'])}</td><td class="k">Resources</td><td class="v">{len(model['resources'])} evaluated</td></tr>
      </table>

      <table class="cover-includes" cellpadding="0" cellspacing="0">
        <tr><td><b>Inside</b></td><td>Executive summary &amp; KPIs · coverage trend · gaps with remediation · scanned-resource inventory · methodology.</td></tr>
      </table>

      <div class="cover-foot">Confidential · for internal use. Generated {fmt_date(_now_iso())} by Azure Support Agent.</div>
    </div>
    """


def _kpi_strip(model: dict[str, Any]) -> str:
    cells = "".join(
        f'<td class="kpi"><div class="kpi-num" style="color:{INK}">{esc(value)}</div>'
        f'<div class="kpi-lbl">{esc(label)}</div>'
        f'<div class="kpi-lbl" style="margin-top:1px">{esc(sub)}</div></td>'
        for label, value, sub in model["kpis"]
    )
    return f'<table class="kpis" cellpadding="0" cellspacing="0"><tr>{cells}</tr></table>'


def _severity_donut(model: dict[str, Any]) -> str:
    sev = model["severity_counts"]
    svg = donut_svg(
        [
            (SEV_COLOR["critical"], sev["critical"]),
            (SEV_COLOR["error"], sev["error"]),
            (SEV_COLOR["warning"], sev["warning"]),
            (SEV_COLOR["info"], sev["info"]),
        ],
        center="gaps",
        accent=INK,
    )
    legend = [
        ("Critical", str(sev["critical"]), SEV_COLOR["critical"]),
        ("High", str(sev["error"]), SEV_COLOR["error"]),
        ("Warning", str(sev["warning"]), SEV_COLOR["warning"]),
        ("Info", str(sev["info"]), SEV_COLOR["info"]),
    ]
    return viz_card("Gaps by severity", f"{sum(sev.values())} open gap(s)", svg, legend)


def _gaps_by_type_card(model: dict[str, Any]) -> str:
    """Top resource types contributing gaps — additive to the KPI strip (vs. the old
    headline card, which just re-listed the KPIs). A plain swatch+count list (no nested
    bars) so it never overflows the narrow exec column."""
    rows_data = model["gap_type_counts"][:7]
    accent = model["accent"]
    if not rows_data:
        body = '<tr><td>No open gaps — baseline met.</td><td class="num">0</td></tr>'
    else:
        body = "".join(
            f'<tr><td class="viz-lb">{swatch(accent)}&nbsp;{esc(_short_arm(t) or t)}</td>'
            f'<td class="num">{n}</td></tr>'
            for t, n in rows_data
        )
    return f"""
    <div class="viz-card">
      <div class="viz-title">Gaps by resource type</div>
      <div class="viz-sub">Where the {len(model['gaps'])} open gap(s) concentrate</div>
      <div class="viz-body"><table class="viz-legend" cellpadding="0" cellspacing="0">{body}</table></div>
    </div>
    """


def _executive(model: dict[str, Any], *, anchor: str = "exec") -> str:
    return f"""
    <div class="pagebreak"></div>
    <a name="{anchor}"></a>
    <h1>{esc(model['title'])} — Executive summary</h1>
    <p class="lead">{esc(model['summary_line'])}</p>
    {_kpi_strip(model)}
    <table class="viz-grid" cellpadding="0" cellspacing="0"><tr>
      <td>{_severity_donut(model)}</td>
      <td>{_gaps_by_type_card(model)}</td>
    </tr></table>
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
        delta_txt = f'<span style="color:{dcolor}; font-weight:bold">{arrow} {abs(delta)} pts</span> vs. previous scan'
    else:
        delta_txt = '<span class="muted">No previous scan to compare against yet.</span>'
    svg = sparkline_svg(ys, color=model["accent"])
    cur_txt = f"{current}%" if isinstance(current, (int, float)) else "—"
    prev_txt = f"{previous}%" if isinstance(previous, (int, float)) else "—"
    return f"""
    <a name="{anchor}"></a>
    <h2>Coverage trend</h2>
    <p class="muted">How this scope's {esc(model['headline_label'].lower())} has moved over time
      ({len(points)} recorded scan{'s' if len(points) != 1 else ''}).</p>
    <table class="trend-head" cellpadding="0" cellspacing="0"><tr>
      <td><div class="trend-big" style="color:{score_color(current)}">{cur_txt}</div><div class="kpi-lbl">current</div></td>
      <td><div class="trend-big" style="color:{MUTED}">{prev_txt}</div><div class="kpi-lbl">previous</div></td>
      <td class="trend-delta">{delta_txt}</td>
    </tr></table>
    <img class="trend-img" src="{svg_data_uri(svg)}" alt="Coverage trend" />
    """


def _gaps_by_type_table(model: dict[str, Any]) -> str:
    """A compact 'gaps by resource type' rollup table for the top of the gaps section."""
    data = model["gap_type_counts"]
    if not data:
        return ""
    cells = "".join(
        f'<td class="gt"><b>{n}</b>&nbsp;<span class="muted">{esc(_short_arm(t) or t)}</span></td>'
        for t, n in data[:6]
    )
    return f'<table class="gaptypes" cellpadding="0" cellspacing="0"><tr>{cells}</tr></table>'


def _gaps_section(model: dict[str, Any], *, anchor: str = "gaps", cap: int | None = None, page_break: bool = True) -> str:
    gaps = sorted(
        model["gaps"],
        key=lambda g: (SEV_RANK.get(g["severity"], 9), g["type"], g["name"].lower()),
    )
    total = len(gaps)
    shown = gaps if cap is None else gaps[:cap]
    brk = '<div class="pagebreak"></div>' if page_break else ""
    if not gaps:
        return f"""
        {brk}
        <a name="{anchor}"></a>
        <h1>Gaps &amp; remediation</h1>
        <p class="ok-note">✓ No open coverage gaps for this scope — every in-scope resource meets the reference baseline.</p>
        """
    rows = []
    for g in shown:
        sub = _short_sub(g["sub"])
        loc = esc(g["rg"]) + (f'<br/><span class="muted">{esc(sub)}</span>' if sub else "")
        name_link = _resource_link(f'<b>{esc(g["name"])}</b>', g.get("id"))
        rows.append(
            f'<tr>'
            f'<td>{_sev_label_chip(g["severity"])}</td>'
            f'<td>{name_link}<br/><span class="muted">{esc(_short_arm(g["type"]) or g["type"])}</span></td>'
            f'<td>{loc or "—"}</td>'
            f'<td>{esc(g["detail"])}</td>'
            f'<td class="fix">{esc(g.get("fix") or "—")}</td>'
            f'</tr>'
        )
    more = ""
    if cap is not None and total > cap:
        more = f'<p class="muted">… and {total - cap} more gap(s). See the per-feature {esc(model["title"])} report for the full list.</p>'
    return f"""
    {brk}
    <a name="{anchor}"></a>
    <h1>Gaps &amp; remediation</h1>
    <p class="muted">{total} open gap(s), grouped by severity then resource type. Each row is one resource failing the reference baseline — the ➚ arrow opens the resource in the Azure portal.</p>
    {_gaps_by_type_table(model)}
    <table class="grid gaps" cellpadding="0" cellspacing="0">
      <tr><th width="8%">Severity</th><th width="22%">Resource</th><th width="15%">Group / sub</th><th width="30%">Why it's a gap</th><th width="25%">Remediation</th></tr>
      {''.join(rows)}
    </table>
    {more}
    """


def _resources_section(model: dict[str, Any], *, anchor: str = "appendix-resources") -> str:
    resources = model["resources"]
    if not resources:
        return ""
    gapped_ids = model["gapped_ids"]
    gapped_names = model["gapped_names"]

    def _is_gapped(r: dict[str, Any]) -> bool:
        return (r.get("id") in gapped_ids) or (r.get("name") in gapped_names)

    # Gapped resources first, then alphabetical — so the reader sees what needs attention.
    ordered = sorted(resources, key=lambda r: (0 if _is_gapped(r) else 1, str(r.get("type") or ""), str(r.get("name") or "")))
    rows = []
    for r in ordered:
        gapped = _is_gapped(r)
        status = '<span style="color:#dc2626; font-weight:bold">✗ gap</span>' if gapped else '<span style="color:#16a34a">✓</span>'
        sub = _short_sub(r.get("subscription_id"))
        name_link = _resource_link(esc(r.get("name") or "—"), r.get("id"))
        rows.append(
            f'<tr>'
            f'<td>{status}</td>'
            f'<td>{name_link}</td>'
            f'<td>{esc(_short_arm(r.get("type")) or r.get("type") or "—")}</td>'
            f'<td>{esc(r.get("resource_group") or "")}</td>'
            f'<td>{esc(r.get("location") or "")}</td>'
            f'<td>{esc(sub)}</td>'
            f'</tr>'
        )
    gapped_count = sum(1 for r in resources if _is_gapped(r))
    return f"""
    <div class="pagebreak"></div>
    <a name="{anchor}"></a>
    <h1>Appendix — Scanned resource inventory</h1>
    <p class="muted">{len(resources)} resource(s) evaluated — {gapped_count} with open gap(s), shown first. The ➚ arrow opens the resource in the Azure portal.</p>
    <table class="grid compact" cellpadding="0" cellspacing="0">
      <tr><th width="10%">Status</th><th width="26%">Name</th><th width="26%">Type</th><th width="18%">Resource group</th><th width="12%">Location</th><th width="8%">Sub</th></tr>
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
      <tr><td class="k">Snapshot generated</td><td class="v">{fmt_date(model['generated_at'])}</td></tr>
      <tr><td class="k">Data source</td><td class="v">{esc(model['source'])}</td></tr>
      <tr><td class="k">Connection</td><td class="v">{'configured' if model['connection_configured'] else 'demo / not configured'}</td></tr>
      <tr><td class="k">Resources evaluated</td><td class="v">{len(model['resources'])}</td></tr>
      <tr><td class="k">Open gaps</td><td class="v">{len(model['gaps'])}</td></tr>
    </table>
    <p class="muted" style="margin-top:8px">
      Each resource in scope is audited against an editable, versioned per-type reference baseline.
      The headline percentage is the share of in-scope resources (or recommended controls) that meet the
      baseline. Gaps roll up into the relevant Well-Architected pillar. This report renders the latest cached
      snapshot; re-run the scan in the app to refresh.
    </p>
    """


# ---------------------------------------------------------------- shells + CSS

def _doc_css() -> str:
    return base_css() + f"""
/* coverage-report cover: tighter top margin + connected score card */
.cover {{ margin-top: 1.4cm; }}
/* portal launch arrow trailing a resource name (the name itself is not a link) */
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

/* gaps-by-type rollup row */
.gaptypes {{ width: 100%; margin: 2px 0 4px 0; }}
.gaptypes td {{ font-size: 8.5px; padding: 3px 6px; border: 0.5px solid {LINE}; background: #f9fafb; }}
.gaptypes .gt {{ width: 16.6%; }}

/* gaps table — remediation column reads as a distinct hint */
.gaps .fix {{ color: #0f766e; font-size: 8px; }}

/* trend */
.trend-head {{ width: 100%; margin: 4px 0 8px 0; }}
.trend-head td {{ width: 22%; vertical-align: bottom; padding: 2px 6px; }}
.trend-head .trend-delta {{ width: 56%; text-align: right; font-size: 10px; color: {INK}; }}
.trend-big {{ font-size: 22px; font-weight: bold; }}
.trend-img {{ display: block; width: 500px; margin: 4px 0 8px 0; border: 0.5px solid {LINE}; }}
.ok-note {{ font-size: 10px; color: #15803d; background: #f0fdf4; border: 0.5px solid #bbf7d0; padding: 8px 10px; }}
.feat-head {{ font-size: 13px; font-weight: bold; color: {BRAND}; margin: 0 0 2px 0; }}

/* estate compact feature blocks */
.feat-trend {{ font-size: 9.5px; color: {INK}; margin: 6px 0 4px 0; }}
.estate-score {{ font-size: 12px; font-weight: bold; }}
"""


def _shell(report_title: str, header_right: str, body: str) -> str:
    header = (
        '<table cellpadding="0" cellspacing="0" width="18cm"><tr>'
        f'<td><span class="brand">Azure Support Agent</span> &nbsp; {esc(report_title)}</td>'
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


# ---------------------------------------------------------------- TOC


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


def build_coverage_pdf(feature: str, snap: dict[str, Any], trend: dict[str, Any] | None = None) -> bytes:
    """Render a single feature's latest coverage snapshot to branded PDF bytes."""
    model = _adapt(feature, snap)
    entries = [
        ("exec", f"{model['title']} — Executive summary", 0),
        ("trend", "Coverage trend", 1),
        ("gaps", "Gaps & remediation", 0),
    ]
    if model["resources"]:
        entries.append(("appendix-resources", "Appendix — Scanned resource inventory", 1))
    entries.append(("appendix-meta", "Methodology & metadata", 1))

    header_right = f"{model['scope_name']} · {fmt_date(model['generated_at'])}"

    def _compose(page_map: dict[str, int] | None) -> str:
        exec_block = _executive(model)
        # The trend lives on the same page flow right after the executive summary.
        exec_block = exec_block + _trend_section(model, trend)
        parts = [
            _cover(model),
            _toc(entries, page_map),
            exec_block,
            _gaps_section(model),
        ]
        if model["resources"]:
            parts.append(_resources_section(model))
        parts.append(_methodology(model))
        return "".join(parts)

    return render_two_pass(
        lambda body: _shell(f"{model['title']} Report", header_right, body),
        _compose,
        entries,
    )


def build_estate_pdf(scope_name: str, items: list[tuple[str, dict[str, Any], dict[str, Any] | None]]) -> bytes:
    """Render a combined estate coverage report stitching every feature for one scope.

    ``items`` is a list of ``(feature, snapshot, trend)`` tuples (already fetched for the scope).
    """
    models = [(_adapt(feature, snap), trend) for feature, snap, trend in items]
    generated = next((m["generated_at"] for m, _ in models if m.get("generated_at")), _now_iso())

    pcts = [m["headline_pct"] for m, _ in models if isinstance(m["headline_pct"], (int, float))]
    blended = round(sum(pcts) / len(pcts)) if pcts else None
    total_gaps = sum(len(m["gaps"]) for m, _ in models)

    entries: list[tuple[str, str, int]] = [("overview", "Estate overview", 0)]
    for model, _trend in models:
        entries.append((f"feat-{model['feature']}", model["title"], 0))

    header_right = f"{scope_name} · {fmt_date(generated)}"

    def _blended_card() -> str:
        txt = f"{blended:.0f}" if isinstance(blended, (int, float)) else "—"
        color = score_color(blended)
        return f"""
        <table class="score-card" width="200" cellpadding="0" cellspacing="0"><tr><td>
          <div class="score-num" style="color:{color}">{txt}<span class="score-unit">%</span></div>
          <div class="score-lbl">Blended coverage</div>
          {bar(blended if isinstance(blended, (int, float)) else 0, color, total=176)}
          <div class="kpi-lbl" style="margin-top:6px">{total_gaps} open gap(s) across 3 detectors</div>
        </td></tr></table>
        """

    def _estate_cover() -> str:
        cards = []
        for model, _trend in models:
            pct = model["headline_pct"]
            txt = f"{pct:.0f}%" if isinstance(pct, (int, float)) else "—"
            cards.append(
                f'<tr>'
                f'<td class="k">{esc(model["title"])}</td>'
                f'<td class="v estate-score" style="color:{score_color(pct)}">{txt}</td>'
                f'<td>{bar(pct if isinstance(pct, (int, float)) else 0, score_color(pct), total=185)}</td>'
                f'<td class="num">{len(model["gaps"])} gaps</td>'
                f'</tr>'
            )
        return f"""
        <div class="cover">
          <table class="cover-hero" cellpadding="0" cellspacing="0">
            <tr>
              <td class="cover-left">
                <div class="cover-brand">Azure Support Agent</div>
                <div class="cover-sub">Estate Coverage Report</div>
                <div class="cover-pack">Monitoring · Telemetry · Backup &amp; DR</div>
                <div class="cover-summary">A consolidated coverage posture for <b>{esc(scope_name)}</b> across all three
                  coverage detectors, each scored against its editable reference baseline.</div>
              </td>
              <td class="cover-right">{_blended_card()}</td>
            </tr>
          </table>
          <a name="overview"></a>
          <div class="cover-section-lbl">Coverage by detector</div>
          <table class="grid" cellpadding="0" cellspacing="0">
            <tr><th width="30%">Detector</th><th width="12%">Coverage</th><th width="42%"></th><th width="16%">Open gaps</th></tr>
            {''.join(cards)}
          </table>
          <table class="cover-meta" cellpadding="0" cellspacing="0">
            <tr><td class="k">Scope</td><td class="v">{esc(scope_name)}</td><td class="k">Generated</td><td class="v">{fmt_date(generated)}</td></tr>
          </table>
          <div class="cover-foot">Confidential · for internal use. Generated {fmt_date(_now_iso())} by Azure Support Agent.</div>
        </div>
        """

    def _trend_line(model: dict[str, Any], trend: dict[str, Any] | None) -> str:
        trend = trend or {}
        cur = trend.get("current")
        delta = trend.get("delta")
        n = len(trend.get("points") or [])
        if not isinstance(cur, (int, float)):
            return ""
        if isinstance(delta, (int, float)) and delta != 0:
            arrow = "▲" if delta > 0 else "▼"
            dc = "#16a34a" if delta > 0 else "#dc2626"
            move = f' <span style="color:{dc}; font-weight:bold">{arrow} {abs(delta)} pts</span> over {n} scans'
        else:
            move = f' · {n} recorded scan{"s" if n != 1 else ""}'
        return f'<div class="feat-trend">Trend: <b>{cur}%</b> current{move}</div>'

    def _feature_block(model: dict[str, Any], trend: dict[str, Any] | None) -> str:
        return f"""
        <div class="pagebreak"></div>
        <a name="feat-{model['feature']}"></a>
        <h1>{esc(model['title'])}</h1>
        <div class="feat-head">{esc(model['subtitle'])}</div>
        <p class="lead">{esc(model['summary_line'])}</p>
        {_kpi_strip(model)}
        <div class="cover-section-lbl">Open gaps by severity ({len(model['gaps'])} total)</div>
        {_sev_mix_bar(model)}
        {_trend_line(model, trend)}
        {_gaps_section(model, anchor=f'gaps-{model["feature"]}', cap=12, page_break=False)}
        """

    def _compose(page_map: dict[str, int] | None) -> str:
        parts = [_estate_cover(), _toc(entries, page_map)]
        for model, trend in models:
            parts.append(_feature_block(model, trend))
        return "".join(parts)

    return render_two_pass(
        lambda body: _shell("Estate Coverage Report", header_right, body),
        _compose,
        entries,
    )


# ---------------------------------------------------------------- Evidence Locker mapping


def build_evidence_content(
    feature: str, snap: dict[str, Any]
) -> tuple[str, dict[str, Any], list[str], list[str], dict[str, Any]]:
    """Map a coverage snapshot into the Evidence Locker content shape.

    Returns ``(name, scope, included, tags, content)`` ready for
    ``app.evidence.registry.create_snapshot``. The content captures the gaps (findings),
    the headline metrics, and the full scanned inventory so the scan is independently
    auditable and re-renderable.
    """
    model = _adapt(feature, snap)
    findings = [
        {
            "id": f"{feature}-gap-{i}",
            "title": f"{g['name']} — {g['detail'][:140]}",
            "severity": g["severity"],
            "status": g["status"],
            "resource_name": g["name"],
            "resource_type": g["type"],
            "resource_group": g["rg"],
            "subscription_id": g["sub"],
            "feature": feature,
        }
        for i, g in enumerate(model["gaps"])
    ]
    metrics = {
        "feature": feature,
        "headline_label": model["headline_label"],
        "headline_pct": model["headline_pct"],
        "severity_counts": model["severity_counts"],
        "kpis": [{"label": label, "value": str(value), "sub": sub} for label, value, sub in model["kpis"]],
        "raw": snap.get("scorecard") or snap.get("kpis") or {},
    }
    content = {
        "findings": findings,
        "metrics": metrics,
        "inventory": list(snap.get("all_resources") or []),
    }
    name = f"{model['title']} — {model['scope_name']} — {fmt_date(model['generated_at'])}"
    scope = {"kind": model["scope_kind"], "id": model["scope_id"], "name": model["scope_name"]}
    included = ["findings", "metrics", "inventory"]
    tags = ["coverage", feature]
    return name, scope, included, tags, content
