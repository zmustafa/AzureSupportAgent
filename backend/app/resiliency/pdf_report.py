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
    """The same words the screen uses. A report that says `compute/disks` while the app says
    "Managed Disks" makes the reader do the translation."""
    from app.workloads.summarize import friendly_type

    t = str(value or "")
    return friendly_type(t) if t else "—"


#: Scope kinds as a reader names them. The cover leads with this, so it cannot be `.title()`
#: on a wire value ("Management_Group").
SCOPE_KIND_LABEL: dict[str, str] = {
    "workload": "Workload",
    "subscription": "Subscription",
    "management_group": "Management group",
    "managementgroup": "Management group",
}


def _humanize_key(key: Any) -> str:
    """`vm_restore_mbps` -> "VM restore (MB/s)". Raw registry keys are wire values."""
    text = str(key or "").strip()
    if not text:
        return "—"
    unit = ""
    for suffix, label in (("_mbps", " (MB/s)"), ("_gb_per_hour", " (GB/hour)"),
                         ("_minutes", " (minutes)"), ("_hours", " (hours)")):
        if text.endswith(suffix):
            text, unit = text[: -len(suffix)], label
            break
    words = [w for w in text.replace("-", "_").split("_") if w]
    out = " ".join(w.upper() if w.lower() in {"vm", "sql", "asr", "pitr", "rpo", "rto", "db"}
                   else w for w in words)
    return (out[:1].upper() + out[1:] if out else text) + unit


def _tier_label(m: dict[str, Any], tier_id: Any) -> str:
    """`mission_critical` is a wire value and must never reach a page."""
    raw = str(tier_id or "").strip()
    if not raw:
        return "—"
    for tier in (m.get("reference") or {}).get("tiers") or []:
        if str(tier.get("id") or "") == raw:
            return str(tier.get("label") or raw)
    return _humanize_key(raw)


def _scope_heading(m: dict[str, Any]) -> str:
    """"Workload — Contoso Reservations". The reader cares what this is about, not what
    generated it, so this is the largest thing on the cover and in the running header."""
    kind = SCOPE_KIND_LABEL.get(str(m["scope_kind"] or "").lower().replace(" ", "_"))
    name = m["scope_name"]
    return f"{kind} — {name}" if kind else str(name)


def _joined(parts: Any, sep: str = "; ") -> str:
    """Join only the non-empty pieces — an empty first element produced a leading "; "."""
    return sep.join(p for p in (str(x or "").strip() for x in (parts or [])) if p)


def _resource(m: dict[str, Any], name: Any, resource_id: Any, *, width: int = 26) -> str:
    """A resource name, linked into the Azure portal when a link is defensible.

    Blank ``portal_host`` (demo data, or a cloud we could not resolve) yields plain text —
    a link that opens someone else's 404 is worse than no link at all.
    """
    from app.core.azure_portal import resource_url_for_host

    label = esc_breakable(name, width=width)
    url = resource_url_for_host(resource_id, m.get("portal_host"))
    return f'<a href="{esc(url)}">{label}</a>' if url else label


class _Codes:
    """Distinct prose reasons to short codes, with a legend rendered under the table.

    Two problems, one fix. xhtml2pdf sizes columns from content and gives a prose column in
    a wide table barely one word per line; and the same sentence repeats verbatim for dozens
    of rows. A code plus one legend row says it once and keeps the grid readable. Nothing is
    lost — the legend carries the full text.
    """

    def __init__(self, prefix: str = "R") -> None:
        self._codes: dict[str, str] = {}
        self._prefix = prefix

    def code(self, text: Any) -> str:
        cleaned = str(text or "").strip()
        if not cleaned:
            return ""
        return self._codes.setdefault(cleaned, f"{self._prefix}{len(self._codes) + 1}")

    def legend(self, title: str = "Reason legend") -> str:
        if not self._codes:
            return ""
        rows = "".join(f"<tr><td class='num'>{esc(code)}</td><td>{esc(text)}</td></tr>"
                       for text, code in self._codes.items())
        return (f'<h3>{esc(title)}</h3>'
                f'<table class="grid compact" cellpadding="0" cellspacing="0">'
                f'<thead><tr><th class="num">Code</th><th>Reason</th></tr></thead>'
                f'{rows}</table>')


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
        # Resolved per read and blank for demo data, so a synthetic id never becomes a link
        # that 404s in the reader's own tenant.
        "portal_host": str(snapshot.get("portal_host") or ""),
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
        ("Resources", _n(m["resources"]), "analyzed in this scope"),
        ("Resources with no recovery path", _n(m["no_path_resources"]),
         f"worst: {m['worst_scenario']}" if m["worst_scenario"] else "—"),
        ("Resources redundant but exposed", _n(len(m["facts"]["redundancy_gap"])),
         "to corruption / deletion"),
        ("Resource-scenario pairs breaching", _n(len(m["breaches"])),
         "against agreed objectives"),
        ("Resource-scenario pairs undetermined", _n(m["undetermined"]),
         "a source could not be read"),
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
        This leads the report rather than the {esc(str(len(m['breaches'])))} objective
        breaches because a missing mechanism cannot be tuned — it has to be built.
        {esc(str(m['no_path_pairs']))} resource-scenario pairs are affected;
        {esc(str(m['undetermined']))} more could not be determined.
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
            <div class="cover-brand">{esc(_scope_heading(m))}</div>
            <div class="cover-sub">Recovery Readiness</div>
            <div class="cover-pack">{fmt_date(m['generated_at'])} &middot;
              {esc(str(m['resources']))} resources</div>
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
        <tr><td class="k">Analyzed</td><td class="v">{fmt_date(m['generated_at'])}</td>
            <td class="k">Objectives</td><td class="v">{esc(ack)}</td></tr>
      </table>

      <div class="cover-section-lbl">Inside</div>
      <table class="cover-includes" cellpadding="0" cellspacing="0">
        <tr><td>Executive summary &middot; how to read this report
        &middot; what to do next &middot; recovery by failure scenario &middot; RTO/RPO by
        resource type &middot; why &mdash; the dominant reasons &middot; resources that
        cannot be recovered &middot; breaches &middot; workload roll-up &middot;
        appendices.</td></tr>
      </table>
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
    names = ", ".join(_resource(m, r["name"], r.get("id"), width=40) for r in rows[:3])
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
      within seconds. {_resource(m, worst['name'], worst.get('id'), width=40)} recovers from
      infrastructure loss in
      &ldquo;{esc(_class_label(worst['infra_rto_class']).lower())}&rdquo; and from
      {esc(', '.join(worst['worse_for']).lower())} in
      &ldquo;{esc(_class_label(worst['logical_rto_class']).lower())}&rdquo;. Every
      redundancy check calls these resilient: {names}{esc(more)}.</div>
    </td></tr></table>
    """


# --------------------------------------------------------------------------- sections
def _executive(m: dict[str, Any], anchor: str = "exec") -> str:
    prot = m["protection"]
    trend = m["trend"]
    # A whole page saying "no trend yet" is a page nobody needs; one line here is the fact.
    trend_note = ""
    if not trend.get("available"):
        reason = esc(trend.get("reason") or "No history has been recorded for this scope.")
        trend_note = (f'<p class="muted"><b>Trend:</b> {reason} A direction is deliberately '
                      f'not drawn from a single measurement &mdash; a line through one point '
                      f'invites a reader to see a change that was never measured.</p>')
    return f"""
    <div class="pagebreak"></div><a name="{anchor}"></a>
    <h1>Recovery Readiness &mdash; Executive summary</h1>
    <p class="lead">
      {esc(str(m['resources']))} resources were analyzed against five failure scenarios.
      {esc(str(m['no_path_resources']))} have no recovery path from at least one of them,
      and {esc(str(len(m['breaches'])))} resource-scenario pairs miss the objective set for
      their criticality tier.
    </p>
    <h2>Protection coverage</h2>
    <table class="grid" cellpadding="0" cellspacing="0">
      <thead><tr><th>State</th><th class="num">Resources</th>
        <th>What it means</th></tr></thead>
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
    {trend_note}
    """


def _actions_section(m: dict[str, Any], anchor: str = "actions") -> str:
    """The report diagnosed well and then stopped. This turns the same facts into an ordered
    work list: one row here is usually one change, and it names what the change buys."""
    actions: list[tuple[str, str, str]] = []
    for r in m["facts"]["reasons"]:
        # Only reasons that describe something MISSING. A reason like "platform-managed
        # service; a failed instance is replaced without operator action" explains a healthy
        # verdict, and listing it as work to do would be nonsense.
        if not r["no_recovery_path"]:
            continue
        actions.append((
            esc(r["reason"]),
            f"{r['no_recovery_path']} of {r['resources']} resource(s) &middot; "
            f"{esc(_scenario_label(r['scenario']))}",
            f"{r['no_recovery_path']} gain a recovery path for this failure"))
    gap = m["facts"]["redundancy_gap"]
    if gap:
        actions.append((
            "Only replication protects these resources &mdash; there is no independent copy",
            f"{len(gap)} resource(s) &middot; corruption / deletion",
            "Replication copies the damage; a real backup is the only way back"))
    if not actions:
        return ""
    rows = "".join(
        f"<tr><td><b>{i + 1}</b></td><td>{what}</td><td>{scope}</td>"
        f"<td class='why'>{buys}</td></tr>"
        for i, (what, scope, buys) in enumerate(actions))
    return f"""
    <div class="pagebreak"></div><a name="{anchor}"></a>
    <h1>What to do next</h1>
    <p class="lead">Every row here is a missing recovery mechanism, ordered by how many
    resources it would restore. A missing mechanism cannot be tuned &mdash; it has to be
    built &mdash; which is why this list comes before anything about speed.</p>
    <table class="grid compact" cellpadding="0" cellspacing="0">
      <thead><tr><th>Order</th><th>What is missing</th><th>Where it bites</th>
        <th>What fixing it gains</th></tr></thead>
      {rows}
    </table>
    <p class="muted">Derived from the same reasons shown later in the report; nothing here
    is a recommendation the analysis did not already evidence. Resources already meeting
    their objective are deliberately absent &mdash; this is a list of gaps, not a summary.</p>
    """


def _how_to_read(m: dict[str, Any], anchor: str = "how-to-read") -> str:
    """Before the numbers, not in an appendix. Every misreading this section prevents is one
    that turns a cautious report into a falsely reassuring one."""
    return f"""
    <div class="pagebreak"></div><a name="{anchor}"></a>
    <h1>How to read this report</h1>
    <table class="grid" cellpadding="0" cellspacing="0">
      <thead><tr><th>Rule</th>
        <th>Why it is stated rather than assumed</th></tr></thead>
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
    # Handled as a one-line note in the executive summary when there is nothing to draw.
    if not trend.get("available"):
        return ""
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
    total_resources = int(m["resources"])
    for scenario in model.SCENARIOS:
        dist = m["facts"]["rto_distribution"][scenario]
        applicable = sum(dist[c] for c in model.RTO_CLASSES)
        if not applicable:
            continue
        slices = [(RTO_COLOR[c], dist[c]) for c in model.RTO_CLASSES if dist[c]]
        legend = [(_class_label(c), str(dist[c]), RTO_COLOR[c])
                  for c in model.RTO_CLASSES if dist[c]]
        # The centre is this card's OWN denominator. Scenario counts differ from the estate
        # total because some resources cannot experience some failures, and that exclusion
        # was previously invisible — the reader just saw numbers that would not add up.
        not_applicable = max(0, total_resources - applicable)
        sub = f"{applicable} of {total_resources} resources can experience this"
        if not_applicable:
            sub += f" \u00b7 {not_applicable} not applicable"
        helps = ("Redundancy does not help here."
                 if scenario in model.LOGICAL_SCENARIOS else "Redundancy helps here.")
        cards.append(
            f'<td>{viz_card(_scenario_label(scenario), f"{sub}. {helps}", _donut(slices, str(applicable)), legend)}</td>')

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
    zone-centric tool calls resilient. The number in each ring is how many resources that
    failure applies to &mdash; not every resource can experience every failure.</p>
    <table class="viz-grid" cellpadding="0" cellspacing="0">{grid}</table>
    {_colour_legend()}
    """


def _colour_legend() -> str:
    """Colour carries meaning on every page and was never defined. On a mono printer the
    distinction disappears entirely, so each class is named as well as coloured."""
    cells = "".join(
        f'<td class="lgd">{swatch(RTO_COLOR[c])}&nbsp;{esc(_class_label(c))}</td>'
        for c in model.RTO_CLASSES)
    return (f'<table class="legend-bar" cellpadding="0" cellspacing="0"><tr>{cells}</tr>'
            f'</table><p class="muted">These colours mean the same thing everywhere in this '
            f'report. &ldquo;No recovery path&rdquo; is a different kind of answer from '
            f'&ldquo;a day or more&rdquo;, not a worse degree of it.</p>')


def _donut(slices: list[tuple[str, int]], centre: str) -> str:
    from app.core.pdf_common import donut_svg

    # COUNTS, not percentages: donut_svg normalizes internally and prints the slice total in
    # the middle, so feeding it percentages made every donut read "100".
    return donut_svg([(colour, float(count)) for colour, count in slices],
                     center=centre, accent=ACCENT)


def _by_type_section(m: dict[str, Any], anchor: str = "by-type") -> str:
    entries = m["facts"]["by_type"]
    shown = entries[:MAX_TYPES]
    codes = _Codes()
    parts = []
    for e in shown:
        count = e["dominant_reason_count"]
        explains = f" (&times;{count})" if count > 1 else ""
        # Counts are folded into prose cells rather than given a column each. A column whose
        # data is one digit is ~8pt wide, and no header word fits in 8pt — which is why the
        # headers used to overprint each other.
        res = str(e["resources"])
        if e["undetermined"]:
            res += f" ({e['undetermined']} undet.)"
        recovery = []
        if e["no_recovery_path"]:
            recovery.append(f"<b style='color:#b91c1c'>{e['no_recovery_path']} no path</b>")
        if e["breached"]:
            recovery.append(f"{e['breached']} breach")
        rpo = esc(_minutes_text(e["rpo"]["median_minutes"]))
        if e["rpo"]["excluded"]:
            rpo += f" ({e['rpo']['excluded']} excl.)"
        parts.append(
            "<tr>"
            f"<td>{esc(_short_type(e['type']))}</td>"
            f"<td>{esc(_scenario_label(e['scenario']))}</td>"
            f"<td class='num'>{esc(res)}</td>"
            f"<td>{' &middot; '.join(recovery) or '&mdash;'}</td>"
            f"<td>{_rto_cell(e['worst_rto_class'])}</td>"
            f"<td class='num'>{rpo}</td>"
            f"<td>{esc(codes.code(e['dominant_reason']))}{explains}</td>"
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
      <thead><tr>
        <th>Resource type</th><th>Scenario</th><th class="num">Resources</th>
        <th>Recovery</th><th>Worst RTO</th><th class="num">RPO</th>
        <th>Why</th>
      </tr></thead>
      {rows}
    </table>
    {codes.legend('Dominant reason legend')}
    <p class="muted">&ldquo;RPO&rdquo; is the median recovery point, and covers only
    resources whose recovery point could be measured; the count in brackets is how many it
    leaves out. A type that cannot experience a scenario is absent from that scenario rather
    than shown as meeting its objective.</p>
    {omitted}
    """


def _reasons_section(m: dict[str, Any], anchor: str = "reasons") -> str:
    reasons = m["facts"]["reasons"]
    if not reasons:
        return ""
    rows = "".join(
        f"<tr>"
        f"<td>{esc(_scenario_label(r['scenario']))}</td>"
        f"<td>{esc(r['reason'])}</td>"
        f"<td class='num'>{r['resources']}</td>"
        f"<td class='num' style='color:#b91c1c; font-weight:bold'>"
        f"{r['no_recovery_path'] or ''}</td>"
        f"<td class='why'>{esc(', '.join(_short_type(t) for t in r['types'][:4]))}</td>"
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
      <thead><tr><th>Scenario</th><th>Reason</th>
          <th class="num">Resources</th><th class="num">No path</th>
          <th>Types affected</th></tr></thead>
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
    codes = _Codes()
    rows = "".join(
        f"<tr>"
        f"<td>{_resource(m, o['name'], o.get('id'))}</td>"
        f"<td>{esc(_short_type(o['type']))}</td>"
        f"<td style='color:#b91c1c; font-weight:bold'>{esc(', '.join(o['no_recovery_path']))}</td>"
        f"<td>{esc(codes.code(_joined(o['reasons'])))}</td>"
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
      <thead><tr><th>Resource</th><th>Type</th>
        <th>Cannot recover from</th><th>Why</th></tr></thead>
      {rows}
    </table>
    {codes.legend()}
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
    codes = _Codes()
    rows = "".join(
        f"<tr>"
        f"<td>{_resource(m, b.get('name', ''), b.get('resource_id'))}</td>"
        f"<td>{esc(_scenario_label(b.get('scenario', '')))}</td>"
        f"<td>{esc(_tier_label(m, b.get('tier', '')))}</td>"
        f"<td>{esc(_rpo_text(b))}</td>"
        f"<td>{_rto_cell(str(b.get('rto_class', '')))}</td>"
        f"<td>{esc(_minutes_text((b.get('target') or {}).get('rpo_minutes')))} / "
        f"{esc(_class_label(str((b.get('target') or {}).get('rto_class', ''))))}</td>"
        f"<td>{esc(codes.code(_joined(e.get('detail', '') for e in b.get('basis') or [])))}</td>"
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
      <thead><tr><th>Resource</th><th>Scenario</th><th>Tier</th><th>RPO</th><th>RTO</th>
          <th>Objective (RPO / RTO)</th><th>Why</th></tr></thead>
      {rows}
    </table>
    {codes.legend()}
    {omitted}
    """


def _workloads_section(m: dict[str, Any], anchor: str = "workloads") -> str:
    from app.resiliency import rollup

    workloads = m["workloads"]
    if not workloads:
        return ""
    rows = []
    codes = _Codes()
    for wl in workloads:
        for scenario, spec in (wl.get("scenarios") or {}).items():
            if not spec.get("applicable"):
                continue
            weakest = spec.get("weakest_link") or {}
            coverage = spec.get("coverage") or {}
            rows.append(
                f"<tr><td>{esc_breakable(wl.get('name', ''), width=24)}</td>"
                f"<td>{esc(_tier_label(m, wl.get('tier', '')))}</td>"
                f"<td>{esc(_scenario_label(scenario))}</td>"
                f"<td>{esc(_rpo_text(spec))}</td>"
                f"<td>{_rto_cell(str(spec.get('rto_class', '')))}</td>"
                f"<td>{_resource(m, weakest.get('name', ''), weakest.get('id'), width=22)}</td>"
                f"<td>{esc(codes.code(weakest.get('reason', '')))}</td>"
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
      <thead><tr><th>Workload</th><th>Tier</th><th>Scenario</th><th>RPO</th><th>RTO</th>
          <th>Weakest link</th><th>Why</th>
          <th class="num">Coverage</th></tr></thead>
      {''.join(rows)}
    </table>
    {codes.legend('Weakest-link reason legend')}
    <p class="muted">&ldquo;Coverage&rdquo; is how many components of that workload could be
    determined for that scenario, out of how many the scenario applies to. The denominator
    moves between rows because not every component can experience every failure.</p>
    <h3>Assumptions behind every roll-up</h3>
    <ul class="muted">{assumptions}
      <li>Undetermined components are excluded from the aggregate and counted in Coverage,
      so a quarter-measured application cannot look fully assessed.</li>
    </ul>
    """


# --------------------------------------------------------------------------- appendices
def _matrix_appendix(m: dict[str, Any], anchor: str = "appendix-matrix") -> str:
    # The same sentence repeats for dozens of rows here, which is what made this appendix six
    # pages long. Each distinct reason gets a code; the legend below carries the full text, so
    # nothing is lost and the eye can find the rows that differ.
    codes = _Codes()
    rows = []
    for row in m["rows"]:
        for scenario in model.SCENARIOS:
            verdict = (row.get("verdicts") or {}).get(scenario) or {}
            if not verdict.get("applicable", True):
                continue
            why = _joined(e.get("detail", "") for e in verdict.get("basis") or [])
            # Folded into the same code table as the basis, prefixed so the legend keeps them
            # apart. A caveat repeats across dozens of rows exactly as a reason does.
            caveats = verdict.get("caveats") or []
            limit = _joined(f"Does not cover: {c.get('detail', '')}" for c in caveats)
            rows.append(
                f"<tr><td>{_resource(m, row.get('name', ''), row.get('id'), width=24)}</td>"
                f"<td>{esc(_short_type(row.get('type')))}</td>"
                f"<td>{esc(_scenario_label(scenario))}</td>"
                f"<td>{esc(_rpo_text(verdict))}</td>"
                f"<td>{_rto_cell(str(verdict.get('rto_class', '')))}</td>"
                f"<td>{esc(verdict.get('confidence', ''))}</td>"
                f"<td>{esc(codes.code(why))}</td>"
                f"<td>{esc(codes.code(limit) if limit else '')}</td>"
                f"</tr>")
    total = len(rows)
    shown = rows[:MAX_MATRIX]
    omitted = ""
    if total > MAX_MATRIX:
        omitted = (f'<p class="muted">Showing {MAX_MATRIX} of {total} rows. The remaining '
                   f'{total - MAX_MATRIX} are in the Excel workbook, which is not bounded.</p>')
    legend = codes.legend()
    return f"""
    <div class="pagebreak"></div><a name="{anchor}"></a>
    <h1>Appendix A &mdash; Recovery matrix</h1>
    <p class="muted">One row per resource per applicable scenario. &ldquo;Why&rdquo; and
    &ldquo;Limit&rdquo; are codes into the legend that follows the table. A limit names a
    deletion this recovery path does not survive.</p>
    {omitted}
    <table class="grid compact" cellpadding="0" cellspacing="0">
      <thead><tr><th>Resource</th><th>Type</th><th>Scenario</th><th>RPO</th><th>RTO</th>
          <th>Conf.</th><th>Why</th><th>Limit</th></tr></thead>
      {''.join(shown)}
    </table>
    {legend}
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
        f"<tr><td>{esc(_humanize_key(key))}</td><td class='num'>{esc(str(value))}</td></tr>"
        for key, value in (doc.get("restore_rates") or {}).items())
    mech_rows = "".join(
        f"<tr><td>{esc(_humanize_key(key))}</td><td class='num'>{esc(str(value))}</td></tr>"
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
        f"<tr><td>{esc(_humanize_key(name))}</td><td>{esc(p.get('source', ''))}</td>"
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
      <thead><tr><th>Section</th><th>Source</th><th>Collected</th>
        <th>Unreadable</th><th>Reason</th></tr></thead>
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
      <tr><td class="k">Scope</td><td class="v">{esc(_scope_heading(m))}</td></tr>
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
/* xhtml2pdf sizes columns from the first row's `width` ATTRIBUTE, in absolute units.
   CSS width on th (and table-layout:fixed) is ignored, which is why the prose column was
   crushed to one word per line while narrow headers overprinted each other. */
.grid th {{ vertical-align: bottom; }}
.grid tr {{ page-break-inside: avoid; }}
.legend-bar {{ width: 100%; margin: 8px 0 2px 0; }}
.legend-bar .lgd {{ font-size: 8px; color: {MUTED}; padding: 3px 4px;
    border: 0.5px solid {LINE}; text-align: center; }}
h1 {{ color: {ACCENT}; border-bottom-color: {ACCENT}; }}
.cover-brand {{ color: {ACCENT}; font-size: 26px; line-height: 1.15; }}
.cover-sub {{ font-size: 13px; }}
a {{ color: {BRAND}; }}
ul.muted {{ margin: 2px 0 6px 16px; padding: 0; }}
ul.muted li {{ margin-bottom: 3px; }}
"""


def _shell(header_left: str, header_right: str, body: str) -> str:
    # Scope first, product last: a page found on a desk should identify its subject.
    header = (
        '<table cellpadding="0" cellspacing="0" width="18cm"><tr>'
        f'<td><span class="brand">{esc(header_left)}</span> &nbsp; Recovery Readiness</td>'
        f'<td style="text-align:right">{esc(header_right)}</td>'
        "</tr></table>"
    )
    footer = ("Confidential &nbsp;&middot;&nbsp; Azure Support Agent &nbsp;&middot;&nbsp; "
              "page <pdf:pagenumber> of <pdf:pagecount>")
    # Applied once here rather than on sixteen table tags: xhtml2pdf repeats the first N rows
    # of a table on every page it spans, so a continuation page keeps its column headings
    # instead of presenting unlabelled data.
    body = body.replace('<table class="grid', '<table repeat="1" class="grid')
    return ("<html><head><meta charset='utf-8'><style>" + _doc_css()
            + "</style></head><body>" + running_frames(header, footer) + body + "</body></html>")


def build(snapshot: dict[str, Any], *, reference_doc: dict[str, Any] | None = None,
          trend: dict[str, Any] | None = None) -> bytes:
    m = _adapt(snapshot, reference_doc, trend)
    has_trend = bool((trend or {}).get("available"))
    has_actions = bool(m["facts"]["reasons"] or m["facts"]["redundancy_gap"])
    entries: list[tuple[str, str, int]] = [
        ("exec", "Recovery Readiness — Executive summary", 0),
        ("how-to-read", "How to read this report", 0),
    ]
    if has_actions:
        entries.append(("actions", "What to do next", 0))
    if has_trend:
        entries.append(("trend", "Trend", 0))
    entries += [
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
    header_right = fmt_date(m['generated_at'])
    header_left = _scope_heading(m)

    def _compose(page_map: dict[str, int] | None) -> str:
        parts = [
            _cover(m),
            _toc(entries, page_map),
            _executive(m),
            _how_to_read(m),
        ]
        if has_actions:
            parts.append(_actions_section(m))
        parts += [
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

    return render_two_pass(lambda body: _shell(header_left, header_right, body), _compose, entries)


__all__ = ["build", "MAX_NO_PATH", "MAX_BREACHES", "MAX_MATRIX"]
