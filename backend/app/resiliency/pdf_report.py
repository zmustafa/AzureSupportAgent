"""The Recovery Readiness PDF — the readable report.

The workbook is the complete artifact; this is the one somebody reads end to end and hands
to a steering group. That division is deliberate and is stated inside the document, because
a bounded report that does not admit its bounds is just an incomplete one.

Structure follows the house pattern (cover → contents → executive → sections → appendices)
via :func:`app.core.pdf_common.render_two_pass`.

What this report refuses to do, and why each refusal matters more than the thing it gives up:

* **No headline score.** A count of resources that cannot be recovered is actionable; a
  score out of 100 invites comparison between estates that share no assumptions.
* **No average RTO.** ``unknown`` is not a point on the scale, so a mean over it is
  undefined — and it would be the most quotable number in the document.
* **`unknown` is never colored like a pass**, and ``no recovery path`` never shares a
  treatment with "slow". Those two conflations are the whole reason this feature exists.
* **The trend refuses to draw a direction from one measurement**, and flags an improvement
  that came from losing visibility rather than gaining protection.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.core.pdf_common import (
    BRAND,
    INK,
    LINE,
    MUTED,
    base_css,
    esc,
    esc_breakable,
    fmt_date,
    render_two_pass,
    running_frames,
    sparkline_svg,
    swatch,
    viz_card,
)
from app.resiliency import analysis, model
from app.resiliency import snapshot as snapshot_store

ACCENT = "#0f766e"  # teal — Recovery Readiness's ♻️ theme

#: Colors per RTO class. `none` gets the heaviest treatment on the page and `unknown` a
#: deliberately neutral grey — never green, which would read as a pass.
RTO_COLOR: dict[str, str] = {
    model.RTO_AUTOMATIC: "#16a34a",
    model.RTO_MINUTES: "#65a30d",
    model.RTO_HOURS: "#d97706",
    model.RTO_DAY_PLUS: "#ea580c",
    model.RTO_NONE: "#b91c1c",
    model.RTO_UNKNOWN: "#9ca3af",
}

#: How much of each detail section the PDF carries. The workbook has every row; saying so is
#: what makes a bound honest rather than a silent omission.
MAX_NO_PATH = 500
MAX_BREACHES = 500
MAX_MATRIX = 1500
MAX_REASONS = 20
MAX_TYPES = 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _class_label(value: str) -> str:
    return model.RTO_LABEL.get(value, value or "—")


def _scenario_label(scenario: str) -> str:
    return model.SCENARIO_LABEL.get(scenario, scenario)


def _short_type(value: Any) -> str:
    t = str(value or "")
    return t.split(".", 1)[1] if t.lower().startswith("microsoft.") and "." in t else t


def _minutes_text(minutes: Any) -> str:
    if minutes is None or minutes == "":
        return "—"
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return "—"
    if m == 0:
        return "0 (sync)"
    if m % 1440 == 0:
        return f"{m // 1440}d"
    if m % 60 == 0:
        return f"{m // 60}h"
    return f"{m}m"


def _rpo_text(verdict: dict[str, Any]) -> str:
    state = verdict.get("rpo_state")
    if state == model.RPO_NONE:
        return "No recovery point"
    if state == model.RPO_UNKNOWN or verdict.get("rpo_minutes") is None:
        return "Unknown"
    return _minutes_text(verdict.get("rpo_minutes"))


def _rto_cell(rto_class: str) -> str:
    colour = RTO_COLOR.get(rto_class, MUTED)
    weight = "bold" if rto_class == model.RTO_NONE else "normal"
    return (f'<span style="color:{colour}; font-weight:{weight}">'
            f'{esc(_class_label(rto_class))}</span>')


# --------------------------------------------------------------------------- adapt
def _adapt(snapshot: dict[str, Any], reference_doc: dict[str, Any] | None,
           trend: dict[str, Any] | None) -> dict[str, Any]:
    summary = snapshot.get("summary") or {}
    scope = snapshot.get("scope") or {}
    facts = analysis.analyze(snapshot, reason_limit=MAX_REASONS)
    protection = summary.get("protection") or {}
    worst = summary.get("worst") or {}

    no_path_pairs = sum(
        int((v or {}).get("no_recovery_path", 0))
        for v in (summary.get("by_scenario") or {}).values())
    undetermined = sum(
        int((v or {}).get("undetermined", 0))
        for v in (summary.get("by_scenario") or {}).values())

    return {
        "scope_kind": str(scope.get("scope_kind") or ""),
        "scope_id": str(scope.get("scope_id") or ""),
        "scope_name": str(scope.get("scope_name") or scope.get("scope_id") or "this scope"),
        "generated_at": str(snapshot.get("generated_at") or ""),
        "demo": bool(snapshot.get("demo")),
        "unreadable": snapshot_store.estate_unreadable(snapshot),
        "acknowledged": bool(snapshot.get("targets_acknowledged")),
        "resources": int(summary.get("resources", 0)),
        "no_path_pairs": no_path_pairs,
        "no_path_resources": int(worst.get("no_recovery_path", 0)),
        "worst_scenario": _scenario_label(str(worst.get("scenario") or "")),
        "undetermined": undetermined,
        "protection": protection,
        "by_scenario": summary.get("by_scenario") or {},
        "breaches": snapshot.get("breaches") or [],
        "workloads": snapshot.get("workloads") or [],
        "provenance": snapshot.get("provenance") or {},
        "truncation": snapshot.get("truncation") or {},
        "rows": snapshot.get("resources") or [],
        "reference": reference_doc or {},
        "trend": trend or {},
        "facts": facts,
    }


# --------------------------------------------------------------------------- cover
def _kpi_strip(m: dict[str, Any]) -> str:
    # Every count below is derived from the resource list. If that list was never read they
    # are all zero for the same reason, so print nothing rather than five reassuring zeros.
    blank = bool(m["unreadable"])
    def _n(value: Any) -> str:
        return "\u2014" if blank else str(value)
    kpis = [
        ("Resources", _n(m["resources"]), "in this analysis"),
        ("No recovery path", _n(m["no_path_resources"]), m["worst_scenario"] or "\u2014"),
        ("Redundant, worse for logical", _n(len(m["facts"]["redundancy_gap"])),
         "corruption / deletion"),
        ("Breaching objectives", _n(len(m["breaches"])), "against agreed targets"),
        ("Undetermined", _n(m["undetermined"]), "could not be read"),
    ]
    cells = "".join(
        f'<td class="kpi"><div class="kpi-num" style="color:{INK}">{esc(value)}</div>'
        f'<div class="kpi-lbl">{esc(label)}</div>'
        f'<div class="kpi-lbl" style="margin-top:1px">{esc(sub)}</div></td>'
        for label, value, sub in kpis
    )
    return f'<table class="kpis" cellpadding="0" cellspacing="0"><tr>{cells}</tr></table>'


def _headline_card(m: dict[str, Any]) -> str:
    """A count, not a score. A score out of 100 invites comparison between estates that
    share none of the same assumptions; a count of unrecoverable resources is a work item.

    When the estate could not be read there is no count. Printing the zero would turn our
    own blindness into a green pass, which is the one thing this report must never do."""
    if m["unreadable"]:
        return f"""
    <table class="score-card" cellpadding="0" cellspacing="0"><tr><td>
      <div class="score-num" style="color:#b45309;font-size:34px">Not read</div>
      <div class="score-lbl">recovery could not be assessed</div>
      <div class="muted" style="font-size:8.5px">{esc(m['unreadable'])}</div>
    </td></tr></table>
    """
    count = m["no_path_resources"]
    colour = "#b91c1c" if count else "#16a34a"
    return f"""
    <table class="score-card" cellpadding="0" cellspacing="0"><tr><td>
      <div class="score-num" style="color:{colour}">{count}</div>
      <div class="score-lbl">resources with no recovery path</div>
      <div class="muted" style="font-size:8.5px">
        Across {esc(str(m['no_path_pairs']))} resource-scenario pairs.
        {esc(str(m['undetermined']))} could not be determined.
      </div>
    </td></tr></table>
    """


def _cover(m: dict[str, Any]) -> str:
    demo = ('<div class="callout-b" style="color:#b45309">Demo data &mdash; this is the synthetic '
            'sample estate, not a live Azure scan.</div>' if m["demo"] else "")
    unread = (f'<div class="callout-b" style="color:#b91c1c">The estate could not be read, so '
              f'this report contains no findings. Every count below is zero because nothing '
              f'was enumerated &mdash; not because nothing is at risk. {esc(m["unreadable"])}'
              f'</div>' if m["unreadable"] else "")
    ack = ("agreed" if m["acknowledged"] else "SHIPPED DEFAULTS — not yet agreed")
    return f"""
    <div class="cover">
      <table class="cover-hero" cellpadding="0" cellspacing="0">
        <tr>
          <td class="cover-left">
            <div class="cover-brand">Azure Support Agent</div>
            <div class="cover-sub">Recovery Readiness Report</div>
            <div class="cover-pack">{esc(m['scope_name'])}</div>
            <div class="cover-summary">
              Recover from what, in how long, losing how much. Every figure here is derived
              from configuration &mdash; redundancy, backup frequency, replication &mdash;
              and measured against your objectives. None of it has been proven by a drill.
            </div>
            {demo}
            {unread}
          </td>
          <td class="cover-right">{_headline_card(m)}</td>
        </tr>
      </table>

      <div class="cover-section-lbl">At a glance</div>
      {_kpi_strip(m)}

      <div class="cover-section-lbl">The reading that matters</div>
      {_thesis_callout(m)}

      <table class="cover-meta" cellpadding="0" cellspacing="0">
        <tr><td class="k">Scope</td><td class="v">{esc(m['scope_name'])}</td>
            <td class="k">Scope type</td><td class="v">{esc(m['scope_kind'].title() or '—')}</td></tr>
        <tr><td class="k">Analyzed</td><td class="v">{fmt_date(m['generated_at'])}</td>
            <td class="k">Objectives</td><td class="v">{esc(ack)}</td></tr>
        <tr><td class="k">Resources</td><td class="v">{m['resources']}</td>
            <td class="k">Objectives version</td>
            <td class="v">{esc(str(m['reference'].get('version', '—')))}</td></tr>
      </table>

      <table class="cover-includes" cellpadding="0" cellspacing="0">
        <tr><td><b>Inside</b></td><td>Executive summary &middot; how to read this report
        &middot; trend &middot; recovery by failure scenario &middot; RTO/RPO by resource
        type &middot; why &mdash; the dominant reasons &middot; resources that cannot be
        recovered &middot; breaches &middot; workload roll-up &middot; appendices.</td></tr>
      </table>

      <div class="cover-foot">Confidential &middot; for internal use. Generated
      {fmt_date(_now_iso())} by Azure Support Agent.</div>
    </div>
    """


def _thesis_callout(m: dict[str, Any]) -> str:
    rows = m["facts"]["redundancy_gap"]
    if not rows:
        return ('<table class="callout ok" cellpadding="0" cellspacing="0"><tr><td>'
                '<div class="callout-h" style="color:#15803d">No resource hides a recovery '
                'gap behind its redundancy</div><div class="callout-b" style="color:#166534">'
                'Where a resource is redundant, its answer for corruption and deletion is '
                'in the same league as its answer for infrastructure loss.</div>'
                '</td></tr></table>')
    names = ", ".join(esc(r["name"]) for r in rows[:3])
    more = f" and {len(rows) - 3} more" if len(rows) > 3 else ""
    worst = rows[0]
    headline = (
        "1 redundant resource is far worse against a bad deployment than against losing a "
        "region" if len(rows) == 1 else
        f"{len(rows)} redundant resources are far worse against a bad deployment than "
        f"against losing a region")
    return f"""
    <table class="callout" cellpadding="0" cellspacing="0"><tr><td>
      <div class="callout-h">{esc(headline)}</div>
      <div class="callout-b">Zone and geo replication copy corruption and deletion, usually
      within seconds. {esc(worst['name'])} recovers from infrastructure loss in
      &ldquo;{esc(_class_label(worst['infra_rto_class']).lower())}&rdquo; and from
      {esc(', '.join(worst['worse_for']).lower())} in
      &ldquo;{esc(_class_label(worst['logical_rto_class']).lower())}&rdquo;. Every
      redundancy check calls these resilient: {names}{esc(more)}.</div>
    </td></tr></table>
    """


# --------------------------------------------------------------------------- sections
def _executive(m: dict[str, Any], anchor: str = "exec") -> str:
    prot = m["protection"]
    return f"""
    <div class="pagebreak"></div><a name="{anchor}"></a>
    <h1>Recovery Readiness &mdash; Executive summary</h1>
    <p class="lead">
      {esc(str(m['resources']))} resources were analyzed against five failure scenarios.
      {esc(str(m['no_path_resources']))} have no recovery path from at least one of them,
      and {esc(str(len(m['breaches'])))} miss the objective set for their criticality tier.
    </p>
    {_kpi_strip(m)}
    <h2>Protection coverage</h2>
    <table class="grid" cellpadding="0" cellspacing="0">
      <tr><th>State</th><th class="num">Resources</th><th>What it means</th></tr>
      <tr><td>{swatch('#16a34a')}&nbsp;Protected</td>
          <td class="num">{prot.get('protected', 0)}</td>
          <td>A backup or replication mechanism was found.</td></tr>
      <tr><td>{swatch('#d97706')}&nbsp;Not protected</td>
          <td class="num">{prot.get('not_protected', 0)}</td>
          <td>We looked and found no mechanism.</td></tr>
      <tr><td>{swatch('#9ca3af')}&nbsp;Unknown</td>
          <td class="num">{prot.get('unknown', 0)}</td>
          <td><b>We could not look.</b> This is not a statement that they are unprotected.</td></tr>
    </table>
    """


def _how_to_read(m: dict[str, Any], anchor: str = "how-to-read") -> str:
    """Before the numbers, not in an appendix. Every misreading this section prevents is one
    that turns a cautious report into a falsely reassuring one."""
    return f"""
    <div class="pagebreak"></div><a name="{anchor}"></a>
    <h1>How to read this report</h1>
    <table class="grid" cellpadding="0" cellspacing="0">
      <tr><th style="width:26%">Rule</th><th>Why it is stated rather than assumed</th></tr>
      <tr><td><b>Derived, not drilled</b></td>
          <td>Every RTO and RPO here comes from configuration. Nothing has been proven by a
          recovery rehearsal, and a figure that has never been tested should not be quoted
          as a commitment.</td></tr>
      <tr><td><b>{swatch('#9ca3af')} Unknown &ne; unprotected</b></td>
          <td>&ldquo;Unknown&rdquo; means a source could not be read. Rendering it as a
          failure produces a full-estate false alarm; rendering it as a pass hides real
          exposure. It is counted separately from both, everywhere.</td></tr>
      <tr><td><b>{swatch('#b91c1c')} No recovery path is not &ldquo;slow&rdquo;</b></td>
          <td>It means no mechanism exists for that failure at all. It is a different kind
          of answer from &ldquo;a day or more&rdquo;, not a worse degree of it, and it is
          ranked first everywhere in this document.</td></tr>
      <tr><td><b>Redundancy is not backup</b></td>
          <td>Zone and geo replication answer infrastructure loss and do nothing for
          corruption or deletion &mdash; they copy the damage, usually within seconds. A
          resource can be flawlessly redundant and completely unrecoverable.</td></tr>
      <tr><td><b>There is no average RTO</b></td>
          <td>&ldquo;Unknown&rdquo; is not a point on the scale, so a mean over it is
          undefined. Worst class and the distribution behind it are reported instead.</td></tr>
      <tr><td><b>Medians exclude what could not be measured</b></td>
          <td>Every median RPO in this report travels with the number of resources it left
          out. A median over 41 of 44, presented as the answer for all 44, is a lie of
          omission.</td></tr>
      <tr><td><b>This report is bounded; the workbook is not</b></td>
          <td>Long sections here are capped so the document stays readable, and each says
          how many rows it omitted. Export the Excel workbook for every row.</td></tr>
    </table>
    """


def _trend_section(m: dict[str, Any], anchor: str = "trend") -> str:
    trend = m["trend"]
    if not trend.get("available"):
        reason = esc(trend.get("reason") or "No history has been recorded for this scope.")
        return f"""
        <div class="pagebreak"></div><a name="{anchor}"></a>
        <h1>Trend</h1>
        <p class="muted">{reason} A direction is deliberately not drawn from a single
        measurement &mdash; a line through one point invites a reader to see a change that
        was never measured.</p>
        """
    points = trend.get("points") or []
    series = [float(p.get("no_recovery_path", 0)) for p in points]
    deltas = trend.get("deltas") or {}
    delta = deltas.get("no_recovery_path", 0)
    arrow = "improved by" if delta < 0 else ("worsened by" if delta > 0 else "unchanged at")
    colour = "#16a34a" if delta < 0 else ("#b91c1c" if delta > 0 else MUTED)
    caveat = ""
    if trend.get("reading_degraded"):
        caveat = (f'<table class="callout" cellpadding="0" cellspacing="0"><tr><td>'
                  f'<div class="callout-h">This is not necessarily an improvement</div>'
                  f'<div class="callout-b">{esc(trend.get("caveat", ""))} Undetermined '
                  f'resources changed by {esc(str(deltas.get("undetermined", 0)))}.</div>'
                  f'</td></tr></table>')
    rows = "".join(
        f"<tr><td>{esc(fmt_date(p.get('generated_at')))}</td>"
        f"<td class='num'>{esc(str(p.get('resources', 0)))}</td>"
        f"<td class='num'>{esc(str(p.get('no_recovery_path', 0)))}</td>"
        f"<td class='num'>{esc(str(p.get('undetermined', 0)))}</td>"
        f"<td class='num'>{esc(str(p.get('breaches', 0)))}</td></tr>"
        for p in points[-15:]
    )
    return f"""
    <div class="pagebreak"></div><a name="{anchor}"></a>
    <h1>Trend</h1>
    <p class="lead">Across {len(points)} analyses, resources with no recovery path
      <span style="color:{colour}; font-weight:bold">{esc(arrow)}
      {esc(str(abs(int(delta))))}</span>.</p>
    {caveat}
    <img class="trend-img" src="{_sparkline_uri(series)}" alt="No recovery path over time" />
    <p class="muted">Gaps are real: a missing analysis leaves a gap rather than an
    interpolated point, because drawing through it would invent measurements nobody took.</p>
    <table class="grid compact" cellpadding="0" cellspacing="0">
      <tr><th>Analyzed</th><th class="num">Resources</th><th class="num">No recovery path</th>
          <th class="num">Undetermined</th><th class="num">Breaches</th></tr>
      {rows}
    </table>
    """


def _sparkline_uri(series: list[float]) -> str:
    from app.core.pdf_common import svg_data_uri

    top = max(series) if series else 0
    scaled = [(v / top) * 100 for v in series] if top else [0.0 for _ in series]
    return svg_data_uri(sparkline_svg(scaled, color=ACCENT, width=700, height=110))


def _scenarios_section(m: dict[str, Any], anchor: str = "scenarios") -> str:
    cards = []
    for scenario in model.SCENARIOS:
        dist = m["facts"]["rto_distribution"][scenario]
        applicable = sum(dist[c] for c in model.RTO_CLASSES)
        if not applicable:
            continue
        slices = [(RTO_COLOR[c], dist[c]) for c in model.RTO_CLASSES if dist[c]]
        legend = [(_class_label(c), str(dist[c]), RTO_COLOR[c])
                  for c in model.RTO_CLASSES if dist[c]]
        centre = str(dist[model.RTO_NONE]) if dist[model.RTO_NONE] else str(applicable)
        sub = ("no recovery path" if dist[model.RTO_NONE] else "resources")
        helps = ("Redundancy does not help here."
                 if scenario in model.LOGICAL_SCENARIOS else "Redundancy helps here.")
        cards.append(
            f'<td>{viz_card(_scenario_label(scenario), f"{sub} &middot; {helps}", _donut(slices, centre), legend)}</td>')

    grid = ""
    for i in range(0, len(cards), 2):
        pair = cards[i:i + 2]
        if len(pair) == 1:
            pair.append("<td></td>")
        grid += f"<tr>{''.join(pair)}</tr>"
    return f"""
    <div class="pagebreak"></div><a name="{anchor}"></a>
    <h1>Recovery by failure scenario</h1>
    <p class="lead">The same resource has a different answer for each failure. A scope that
    looks healthy on the left three and red on the right two is the estate every
    zone-centric tool calls resilient.</p>
    <table class="viz-grid" cellpadding="0" cellspacing="0">{grid}</table>
    """


def _donut(slices: list[tuple[str, int]], centre: str) -> str:
    from app.core.pdf_common import donut_svg

    total = sum(count for _, count in slices) or 1
    return donut_svg([(colour, (count / total) * 100) for colour, count in slices],
                     center=centre, accent=ACCENT)


def _by_type_section(m: dict[str, Any], anchor: str = "by-type") -> str:
    entries = m["facts"]["by_type"]
    shown = entries[:MAX_TYPES]
    parts = []
    for e in shown:
        count = e["dominant_reason_count"]
        explains = f" <b>(&times;{count})</b>" if count > 1 else ""
        parts.append(
            "<tr>"
            f"<td>{esc_breakable(_short_type(e['type']))}</td>"
            f"<td>{esc(_scenario_label(e['scenario']))}</td>"
            f"<td class='num'>{e['resources']}</td>"
            f"<td class='num' style='color:#b91c1c; font-weight:bold'>"
            f"{e['no_recovery_path'] or ''}</td>"
            f"<td class='num'>{e['breached'] or ''}</td>"
            f"<td>{_rto_cell(e['worst_rto_class'])}</td>"
            f"<td class='num'>{e['undetermined'] or ''}</td>"
            f"<td class='num'>{esc(_minutes_text(e['rpo']['median_minutes']))}</td>"
            f"<td class='num'>{e['rpo']['excluded'] or ''}</td>"
            f"<td class='why'>{esc_breakable(e['dominant_reason'], width=44)}{explains}</td>"
            "</tr>")
    rows = "".join(parts)
    omitted = ""
    if len(entries) > MAX_TYPES:
        omitted = (f'<p class="muted">{len(entries) - MAX_TYPES} further type/scenario '
                   f'combinations are omitted here and present in the workbook.</p>')
    return f"""
    <div class="pagebreak"></div><a name="{anchor}"></a>
    <h1>RTO and RPO by resource type</h1>
    <p class="lead">Ranked by consequence. Where a whole type shares one weakness, the fix
    is usually one change &mdash; the last column names the reason that explains the most
    resources in that row.</p>
    <table class="grid compact" cellpadding="0" cellspacing="0">
      <tr>
        <th>Resource type</th><th>Scenario</th><th class="num">Res.</th>
        <th class="num">No path</th><th class="num">Breach</th><th>Worst RTO</th>
        <th class="num">Undet.</th><th class="num">Median RPO</th><th class="num">RPO excl.</th>
        <th>Dominant reason</th>
      </tr>
      {rows}
    </table>
    <p class="muted">&ldquo;Median RPO&rdquo; covers only resources whose recovery point
    could be measured; &ldquo;RPO excl.&rdquo; is how many it leaves out. A type that cannot
    experience a scenario is absent from that scenario rather than shown as meeting its
    objective.</p>
    {omitted}
    """


def _reasons_section(m: dict[str, Any], anchor: str = "reasons") -> str:
    reasons = m["facts"]["reasons"]
    if not reasons:
        return ""
    rows = "".join(
        f"<tr>"
        f"<td>{esc(_scenario_label(r['scenario']))}</td>"
        f"<td>{esc_breakable(r['reason'], width=54)}</td>"
        f"<td class='num'>{r['resources']}</td>"
        f"<td class='num' style='color:#b91c1c; font-weight:bold'>"
        f"{r['no_recovery_path'] or ''}</td>"
        f"<td class='why'>{esc_breakable(', '.join(_short_type(t) for t in r['types'][:4]), width=30)}</td>"
        f"</tr>"
        for r in reasons
    )
    return f"""
    <div class="pagebreak"></div><a name="{anchor}"></a>
    <h1>Why &mdash; the reasons that explain the most</h1>
    <p class="lead">The same misconfiguration recurs across an estate. Working down this
    list moves more resources than working down a resource list, because one row here can be
    one change.</p>
    <table class="grid compact" cellpadding="0" cellspacing="0">
      <tr><th>Scenario</th><th>Reason</th><th class="num">Resources</th>
          <th class="num">No path</th><th>Types affected</th></tr>
      {rows}
    </table>
    """


def _no_path_section(m: dict[str, Any], anchor: str = "no-path") -> str:
    offenders = [o for o in m["facts"]["worst_offenders"] if o["no_recovery_path"]]
    if not offenders:
        return f"""
        <div class="pagebreak"></div><a name="{anchor}"></a>
        <h1>Resources that cannot be recovered</h1>
        <table class="callout ok" cellpadding="0" cellspacing="0"><tr><td>
          <div class="callout-h" style="color:#15803d">Every resource has a recovery path
          for every failure it can experience</div>
        </td></tr></table>
        """
    shown = offenders[:MAX_NO_PATH]
    rows = "".join(
        f"<tr>"
        f"<td>{esc_breakable(o['name'], width=26)}</td>"
        f"<td>{esc_breakable(_short_type(o['type']), width=24)}</td>"
        f"<td style='color:#b91c1c; font-weight:bold'>{esc(', '.join(o['no_recovery_path']))}</td>"
        f"<td class='why'>{esc_breakable('; '.join(o['reasons']), width=48)}</td>"
        f"</tr>"
        for o in shown
    )
    omitted = ""
    if len(offenders) > MAX_NO_PATH:
        omitted = (f'<p class="muted">{len(offenders) - MAX_NO_PATH} further resources are '
                   f'omitted here. Every one of them is in the workbook.</p>')
    return f"""
    <div class="pagebreak"></div><a name="{anchor}"></a>
    <h1>Resources that cannot be recovered</h1>
    <p class="lead">No mechanism exists for the listed failure. This is not a slow recovery;
    it is the absence of one, which is why it leads every ranking in this report.</p>
    <table class="grid compact" cellpadding="0" cellspacing="0">
      <tr><th>Resource</th><th>Type</th><th>Cannot recover from</th><th>Why</th></tr>
      {rows}
    </table>
    {omitted}
    """


def _breaches_section(m: dict[str, Any], anchor: str = "breaches") -> str:
    breaches = m["breaches"]
    if not breaches:
        return f"""
        <div class="pagebreak"></div><a name="{anchor}"></a>
        <h1>Breaches against objectives</h1>
        <table class="callout ok" cellpadding="0" cellspacing="0"><tr><td>
          <div class="callout-h" style="color:#15803d">Nothing breaches its objective in
          this scope</div>
        </td></tr></table>
        """
    shown = breaches[:MAX_BREACHES]
    rows = "".join(
        f"<tr>"
        f"<td>{esc_breakable(b.get('name', ''), width=26)}</td>"
        f"<td>{esc(_scenario_label(b.get('scenario', '')))}</td>"
        f"<td>{esc(b.get('tier', ''))}</td>"
        f"<td>{esc(_rpo_text(b))}</td>"
        f"<td>{_rto_cell(str(b.get('rto_class', '')))}</td>"
        f"<td>{esc(_minutes_text((b.get('target') or {}).get('rpo_minutes')))} / "
        f"{esc(_class_label(str((b.get('target') or {}).get('rto_class', ''))))}</td>"
        f"<td class='why'>{esc_breakable('; '.join(e.get('detail', '') for e in b.get('basis') or []), width=40)}</td>"
        f"</tr>"
        for b in shown
    )
    omitted = ""
    if len(breaches) > MAX_BREACHES:
        omitted = (f'<p class="muted">{len(breaches) - MAX_BREACHES} further breaches are '
                   f'omitted here and present in the workbook.</p>')
    ack = ("" if m["acknowledged"] else
           '<table class="callout" cellpadding="0" cellspacing="0"><tr><td>'
           '<div class="callout-h">These objectives are the shipped defaults</div>'
           '<div class="callout-b">Nobody has agreed them yet, so the breaches below are '
           'measured against numbers this product chose.</div></td></tr></table>')
    return f"""
    <div class="pagebreak"></div><a name="{anchor}"></a>
    <h1>Breaches against objectives</h1>
    <p class="lead">Ordered by consequence: no recovery path first, then total data loss,
    then the size of the miss weighted by tier.</p>
    {ack}
    <table class="grid compact" cellpadding="0" cellspacing="0">
      <tr><th>Resource</th><th>Scenario</th><th>Tier</th><th>RPO</th><th>RTO</th>
          <th>Objective (RPO / RTO)</th><th>Why</th></tr>
      {rows}
    </table>
    {omitted}
    """


def _workloads_section(m: dict[str, Any], anchor: str = "workloads") -> str:
    from app.resiliency import rollup

    workloads = m["workloads"]
    if not workloads:
        return ""
    rows = []
    for wl in workloads:
        for scenario, spec in (wl.get("scenarios") or {}).items():
            if not spec.get("applicable"):
                continue
            weakest = spec.get("weakest_link") or {}
            coverage = spec.get("coverage") or {}
            rows.append(
                f"<tr><td>{esc_breakable(wl.get('name', ''), width=24)}</td>"
                f"<td>{esc(wl.get('tier', ''))}</td>"
                f"<td>{esc(_scenario_label(scenario))}</td>"
                f"<td>{esc(_rpo_text(spec))}</td>"
                f"<td>{_rto_cell(str(spec.get('rto_class', '')))}</td>"
                f"<td>{esc_breakable(weakest.get('name', ''), width=22)}</td>"
                f"<td class='why'>{esc_breakable(weakest.get('reason', ''), width=36)}</td>"
                f"<td class='num'>{coverage.get('determined', 0)}/{coverage.get('total', 0)}</td>"
                f"</tr>")
    assumptions = "".join(f"<li>{esc(line)}</li>" for line in rollup.ASSUMPTIONS)
    return f"""
    <div class="pagebreak"></div><a name="{anchor}"></a>
    <h1>Workload roll-up</h1>
    <p class="lead">A per-resource answer does not tell you what your application's recovery
    time is. &ldquo;A day or more, because of one un-backed-up legacy virtual machine&rdquo;
    is a work item; &ldquo;a day or more&rdquo; is a statistic.</p>
    <table class="grid compact" cellpadding="0" cellspacing="0">
      <tr><th>Workload</th><th>Tier</th><th>Scenario</th><th>RPO</th><th>RTO</th>
          <th>Weakest link</th><th>Why</th><th class="num">Coverage</th></tr>
      {''.join(rows)}
    </table>
    <h3>Assumptions behind every roll-up</h3>
    <ul class="muted">{assumptions}
      <li>Undetermined components are excluded from the aggregate and counted in Coverage,
      so a quarter-measured application cannot look fully assessed.</li>
    </ul>
    """


# --------------------------------------------------------------------------- appendices
def _matrix_appendix(m: dict[str, Any], anchor: str = "appendix-matrix") -> str:
    rows = []
    for row in m["rows"]:
        for scenario in model.SCENARIOS:
            verdict = (row.get("verdicts") or {}).get(scenario) or {}
            if not verdict.get("applicable", True):
                continue
            rows.append(
                f"<tr><td>{esc_breakable(row.get('name', ''), width=24)}</td>"
                f"<td>{esc_breakable(_short_type(row.get('type')), width=22)}</td>"
                f"<td>{esc(_scenario_label(scenario))}</td>"
                f"<td>{esc(_rpo_text(verdict))}</td>"
                f"<td>{_rto_cell(str(verdict.get('rto_class', '')))}</td>"
                f"<td>{esc(verdict.get('confidence', ''))}</td>"
                f"<td class='why'>{esc_breakable('; '.join(e.get('detail', '') for e in verdict.get('basis') or []), width=40)}</td>"
                f"</tr>")
    total = len(rows)
    shown = rows[:MAX_MATRIX]
    omitted = ""
    if total > MAX_MATRIX:
        omitted = (f'<p class="muted">Showing {MAX_MATRIX} of {total} rows. The remaining '
                   f'{total - MAX_MATRIX} are in the Excel workbook, which is not bounded.</p>')
    return f"""
    <div class="pagebreak"></div><a name="{anchor}"></a>
    <h1>Appendix A &mdash; Recovery matrix</h1>
    <p class="muted">One row per resource per applicable scenario.</p>
    {omitted}
    <table class="grid compact" cellpadding="0" cellspacing="0">
      <tr><th>Resource</th><th>Type</th><th>Scenario</th><th>RPO</th><th>RTO</th>
          <th>Conf.</th><th>Why</th></tr>
      {''.join(shown)}
    </table>
    """


def _objectives_appendix(m: dict[str, Any], anchor: str = "appendix-objectives") -> str:
    doc = m["reference"]
    tier_rows = []
    for tier in doc.get("tiers") or []:
        for scenario in model.SCENARIOS:
            target = (tier.get("scenarios") or {}).get(scenario) or {}
            if not target:
                continue
            tier_rows.append(
                f"<tr><td>{esc(tier.get('label', tier.get('id', '')))}</td>"
                f"<td>{esc(_scenario_label(scenario))}</td>"
                f"<td>{esc(_class_label(str(target.get('rto_class', ''))))}</td>"
                f"<td class='num'>{esc(_minutes_text(target.get('rpo_minutes')))}</td></tr>")
    rate_rows = "".join(
        f"<tr><td>{esc(key)}</td><td class='num'>{esc(str(value))}</td></tr>"
        for key, value in (doc.get("restore_rates") or {}).items())
    mech_rows = "".join(
        f"<tr><td>{esc(key)}</td><td class='num'>{esc(str(value))}</td></tr>"
        for key, value in (doc.get("mechanism_minutes") or {}).items())
    return f"""
    <div class="pagebreak"></div><a name="{anchor}"></a>
    <h1>Appendix B &mdash; Objectives and the constants behind every band</h1>
    <p class="lead">A duration derived from a number nobody can see is not reviewable. Every
    estimate in this report comes from the constants below; they are starting points, not
    measurements, and are editable in Recovery Readiness.</p>
    <h2>Objectives (registry version {esc(str(doc.get('version', '—')))})</h2>
    <table class="grid compact" cellpadding="0" cellspacing="0">
      <tr><th>Tier</th><th>Scenario</th><th>Target RTO</th><th class="num">Target RPO</th></tr>
      {''.join(tier_rows)}
    </table>
    <h2>Restore throughput</h2>
    <table class="grid compact" cellpadding="0" cellspacing="0">
      <tr><th>Rate</th><th class="num">Value</th></tr>{rate_rows}
    </table>
    <h2>Fixed mechanism overheads (minutes)</h2>
    <table class="grid compact" cellpadding="0" cellspacing="0">
      <tr><th>Mechanism</th><th class="num">Minutes</th></tr>{mech_rows}
    </table>
    """


def _methodology(m: dict[str, Any], anchor: str = "appendix-meta") -> str:
    prov_rows = "".join(
        f"<tr><td>{esc(name)}</td><td>{esc(p.get('source', ''))}</td>"
        f"<td>{esc(fmt_date(p.get('collected_at')))}</td>"
        f"<td>{'YES' if p.get('unreadable') else 'no'}</td>"
        f"<td class='why'>{esc(p.get('reason', ''))}</td></tr>"
        for name, p in (m["provenance"] or {}).items())
    trunc = m["truncation"] or {}
    trunc_block = (
        '<p class="muted">Nothing was truncated: every row found by the analysis is stored.</p>'
        if not trunc else
        '<table class="grid compact" cellpadding="0" cellspacing="0">'
        '<tr><th>Section</th><th class="num">Stored</th><th class="num">Found</th></tr>'
        + "".join(f"<tr><td>{esc(k)}</td><td class='num'>{esc(str(v.get('exported', 0)))}</td>"
                  f"<td class='num'>{esc(str(v.get('known_total', 0)))}</td></tr>"
                  for k, v in trunc.items())
        + "</table>"
        + '<p class="muted">The analysis exceeded its stored row cap. The omitted rows are '
          'not in this report or the workbook. Narrow the scope and re-analyze for a '
          'complete picture.</p>')
    return f"""
    <div class="pagebreak"></div><a name="{anchor}"></a>
    <h1>Appendix C &mdash; Provenance and methodology</h1>
    <h2>Where each section came from</h2>
    <p class="muted">&ldquo;No findings&rdquo; and &ldquo;could not look&rdquo; are opposite
    facts. A section that could not be read says so here.</p>
    <table class="grid compact" cellpadding="0" cellspacing="0">
      <tr><th>Section</th><th>Source</th><th>Collected</th><th>Unreadable</th><th>Reason</th></tr>
      {prov_rows}
    </table>
    <h2>Completeness</h2>
    {trunc_block}
    <h2>What this analysis does not do</h2>
    <ul class="muted">
      <li><b>No changes to Azure.</b> Analysis only; remediation belongs to Backup Manager.</li>
      <li><b>No fault injection and no drills.</b> Every figure is what the configuration
      implies, not what a rehearsal proved.</li>
      <li><b>Restore throughput is assumed, not measured</b> in your environment. Timing a
      real restore and setting the rate in Appendix B will make every band more accurate.</li>
    </ul>
    <table class="meta" cellpadding="0" cellspacing="0">
      <tr><td class="k">Scope</td><td class="v">{esc(m['scope_name'])}</td></tr>
      <tr><td class="k">Scope type</td><td class="v">{esc(m['scope_kind'] or '—')}</td></tr>
      <tr><td class="k">Analyzed at</td><td class="v">{fmt_date(m['generated_at'])}</td></tr>
      <tr><td class="k">Report generated</td><td class="v">{fmt_date(_now_iso())}</td></tr>
      <tr><td class="k">Demo data</td><td class="v">{'yes' if m['demo'] else 'no'}</td></tr>
      <tr><td class="k">Objectives agreed</td>
          <td class="v">{'yes' if m['acknowledged'] else 'no — shipped defaults'}</td></tr>
    </table>
    """


# --------------------------------------------------------------------------- document
def _toc(entries: list[tuple[str, str, int]], page_map: dict[str, int] | None) -> str:
    if page_map is None:
        return ('<div class="pagebreak"></div><a name="toc"></a>'
                '<div class="toc-title">Contents</div>'
                '<div class="toc-note">Generating section links and page numbers…</div>')
    rows = "".join(
        f'<tr class="toc-row level-{level}"><td class="toc-link">'
        f'<a href="#{anchor}">{esc(label)}</a></td>'
        f'<td class="toc-page">{esc(str(page_map.get(anchor, "—")))}</td></tr>'
        for anchor, label, level in entries)
    return ('<div class="pagebreak"></div><a name="toc"></a>'
            '<div class="toc-title">Contents</div>'
            '<div class="toc-note">Section links and page numbers generated from the '
            'rendered report.</div>'
            f'<table class="toc-table" cellpadding="0" cellspacing="0">{rows}</table>')


def _doc_css() -> str:
    return base_css() + f"""
.cover {{ margin-top: 1.4cm; }}
.cover-section-lbl {{ font-size: 9px; font-weight: bold; color: {MUTED}; text-transform: uppercase;
    letter-spacing: 0.5px; margin: 14px 0 4px 0; }}
.score-card {{ width: 220px; }}
.score-card td {{ border: 1px solid {LINE}; border-radius: 6px; padding: 14px 16px 12px 16px;
    background: #fafafa; }}
.score-num {{ font-size: 46px; font-weight: bold; line-height: 1.0; }}
.score-lbl {{ font-size: 10px; color: {MUTED}; margin: 2px 0 7px 0; }}
.lead {{ font-size: 10px; color: {INK}; margin-bottom: 6px; }}

.callout {{ width: 100%; margin: 2px 0; }}
.callout td {{ border: 1px solid #fecaca; border-radius: 6px; background: #fef2f2;
    padding: 8px 12px; }}
.callout.ok td {{ border: 1px solid #bbf7d0; background: #f0fdf4; }}
.callout-h {{ font-size: 10.5px; font-weight: bold; color: #b91c1c; }}
.callout-b {{ font-size: 9px; color: #7f1d1d; margin-top: 2px; }}

.trend-img {{ display: block; width: 500px; margin: 6px 0 8px 0; border: 0.5px solid {LINE}; }}
.grid .why {{ color: {MUTED}; font-size: 7.5px; }}
h1 {{ color: {ACCENT}; border-bottom-color: {ACCENT}; }}
.cover-brand {{ color: {ACCENT}; }}
a {{ color: {BRAND}; }}
ul.muted {{ margin: 2px 0 6px 16px; padding: 0; }}
ul.muted li {{ margin-bottom: 3px; }}
"""


def _shell(header_right: str, body: str) -> str:
    header = (
        '<table cellpadding="0" cellspacing="0" width="18cm"><tr>'
        '<td><span class="brand">Azure Support Agent</span> &nbsp; Recovery Readiness Report</td>'
        f'<td style="text-align:right">{esc(header_right)}</td>'
        "</tr></table>"
    )
    footer = "Confidential &nbsp;&middot;&nbsp; page <pdf:pagenumber> of <pdf:pagecount>"
    return ("<html><head><meta charset='utf-8'><style>" + _doc_css()
            + "</style></head><body>" + running_frames(header, footer) + body + "</body></html>")


def build(snapshot: dict[str, Any], *, reference_doc: dict[str, Any] | None = None,
          trend: dict[str, Any] | None = None) -> bytes:
    m = _adapt(snapshot, reference_doc, trend)
    entries: list[tuple[str, str, int]] = [
        ("exec", "Recovery Readiness — Executive summary", 0),
        ("how-to-read", "How to read this report", 0),
        ("trend", "Trend", 0),
        ("scenarios", "Recovery by failure scenario", 0),
        ("by-type", "RTO and RPO by resource type", 0),
    ]
    if m["facts"]["reasons"]:
        entries.append(("reasons", "Why — the reasons that explain the most", 0))
    entries += [
        ("no-path", "Resources that cannot be recovered", 0),
        ("breaches", "Breaches against objectives", 0),
    ]
    if m["workloads"]:
        entries.append(("workloads", "Workload roll-up", 0))
    entries += [
        ("appendix-matrix", "Appendix A — Recovery matrix", 0),
        ("appendix-objectives", "Appendix B — Objectives and the constants behind every band", 0),
        ("appendix-meta", "Appendix C — Provenance and methodology", 0),
    ]
    header_right = f"{m['scope_name']} · {fmt_date(m['generated_at'])}"

    def _compose(page_map: dict[str, int] | None) -> str:
        parts = [
            _cover(m),
            _toc(entries, page_map),
            _executive(m),
            _how_to_read(m),
            _trend_section(m),
            _scenarios_section(m),
            _by_type_section(m),
        ]
        if m["facts"]["reasons"]:
            parts.append(_reasons_section(m))
        parts += [_no_path_section(m), _breaches_section(m)]
        if m["workloads"]:
            parts.append(_workloads_section(m))
        parts += [_matrix_appendix(m), _objectives_appendix(m), _methodology(m)]
        return "".join(parts)

    return render_two_pass(lambda body: _shell(header_right, body), _compose, entries)


__all__ = ["build", "MAX_NO_PATH", "MAX_BREACHES", "MAX_MATRIX"]
