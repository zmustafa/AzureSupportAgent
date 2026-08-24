"""The Recovery Readiness workbook — the complete artifact.

The PDF is the readable report; this is the one that holds everything, because the two have
different jobs. A reader who wants the argument reads the PDF; a reader who wants to check
it, pivot it, or hand it to an auditor needs every row.

Built with the shared :class:`~app.core.xlsx.WorkbookBuilder`, which is what neutralizes
formula injection — a resource display name beginning ``=`` is attacker-influenced in a
tagged estate and would otherwise execute on open.

Three sheets exist purely so the file cannot mislead:

* **Provenance** — an auditor reading "no findings" has to be able to tell whether none were
  raised or nothing could be read.
* **Truncation** — the store caps rows. A file presented as complete that silently dropped
  the tail is worse than one that says where it stopped.
* **Assumptions & rates** — every duration band comes from constants somebody chose. A band
  whose inputs are invisible is not reviewable.
"""
from __future__ import annotations

from typing import Any

from app.resiliency import analysis, model
from app.resiliency import snapshot as snapshot_store

# Tab colors group the sheets by what they answer. Color alone is not readable to
# everyone, so `index_sheet` spells the same grouping out in a Section column.
C_FRONT = "0F6CBD"
C_ANALYSIS = "7030A0"
C_DETAIL = "107C10"
C_OBJECTIVES = "C55A11"
C_TRUST = "605E5C"


def _class_label(value: str) -> str:
    return model.RTO_LABEL.get(value, value or "")


def _rpo_text(verdict: dict[str, Any]) -> str:
    state = verdict.get("rpo_state")
    if state == model.RPO_NONE:
        return "No recovery point"
    if state == model.RPO_UNKNOWN or verdict.get("rpo_minutes") is None:
        return "Unknown"
    return _minutes_text(int(verdict["rpo_minutes"]))


def _minutes_text(minutes: int | None) -> str:
    if minutes is None:
        return ""
    if minutes == 0:
        return "0 (synchronous)"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _band_text(verdict: dict[str, Any]) -> str:
    band = verdict.get("rto_band_minutes")
    if not band:
        return ""
    low, high = band
    return f"{low // 60}-{high // 60}h" if high >= 120 else f"{low}-{high}m"


def _scenario_label(scenario: str) -> str:
    return model.SCENARIO_LABEL.get(scenario, scenario)


def _portal(resource_id: Any, host: str, label: str = "Open in Azure") -> Any:
    """A real clickable cell, or blank. `host` is empty for demo data and for a cloud we
    could not resolve, and a link into the wrong tenant is worse than no link."""
    from app.core.azure_portal import resource_url_for_host
    from app.core.xlsx import hyperlink

    url = resource_url_for_host(resource_id, host)
    return hyperlink(label, url) if url else ""


def build(snapshot: dict[str, Any], *, reference_doc: dict[str, Any] | None = None,
          trend: dict[str, Any] | None = None) -> bytes:
    from openpyxl.styles import Font

    from app.core.xlsx import WorkbookBuilder, as_datetime, coerce

    wb = WorkbookBuilder()
    summary = snapshot.get("summary") or {}
    scope = snapshot.get("scope") or {}
    rows = snapshot.get("resources") or []
    facts = analysis.analyze(snapshot, reason_limit=200)
    # Blank for demo data, so a synthetic id never becomes a link into somebody's tenant.
    host = str(snapshot.get("portal_host") or "")

    # ---------------------------------------------------------------- front matter
    wb.section("Overview", C_FRONT)
    ws = wb.first_sheet("Summary")
    ws.append(["Recovery Readiness — recover from what, in how long, losing how much"])
    ws.cell(row=1, column=1).font = Font(bold=True, size=14)
    worst = summary.get("worst") or {}
    protection = summary.get("protection") or {}
    unreadable = snapshot_store.estate_unreadable(snapshot)
    # First data row, before any count: a reader who stops after one line must still learn
    # that the counts below are zero because nothing was enumerated.
    unread_rows = [
        ("ESTATE COULD NOT BE READ", unreadable),
        ("", "Every count below is zero because no resource was enumerated, NOT because "
             "nothing is at risk. This workbook contains no findings."),
        ("", ""),
    ] if unreadable else []
    for label, value in [
        *unread_rows,
        ("Scope", f"{scope.get('scope_kind', '')}: {scope.get('scope_id', '')}"),
        ("Generated at (UTC)", as_datetime(snapshot.get("generated_at"))
         or snapshot.get("generated_at", "")),
        ("Demo data", bool(snapshot.get("demo"))),
        ("Objectives agreed", bool(snapshot.get("targets_acknowledged"))),
        ("Objectives version", (reference_doc or {}).get("version", "")),
        ("", ""),
        ("Resources analyzed", summary.get("resources", 0)),
        ("Worst scenario", _scenario_label(worst.get("scenario", ""))),
        ("Resources with no recovery path", worst.get("no_recovery_path", 0)),
        ("Redundant but unrecoverable", len(facts["redundancy_gap"])),
        ("Breaching an objective", len(snapshot.get("breaches") or [])),
        ("Protected", protection.get("protected", 0)),
        ("Not protected", protection.get("not_protected", 0)),
        ("Protection unknown", protection.get("unknown", 0)),
        ("", ""),
        ("How to read this",
         "Every figure is DERIVED from configuration, not proven by a recovery drill."),
        ("", "'Unknown' means a source could not be read. It is NOT a statement that a "
             "resource is unprotected."),
        ("", "'No recovery path' means no mechanism exists for that failure — it is worse "
             "than slow, not a degree of slow."),
        ("", "Redundancy does not protect against corruption or deletion; zone and geo "
             "replication copy the damage."),
        ("", "There is no average RTO in this workbook. Worst class and distribution are "
             "reported instead, because 'unknown' is not a point on the scale."),
    ]:
        ws.append([label, coerce(value)])
    wb.manifest.append(
        ("Summary", "Overview", 0, "Scope, headline counts and how to read them."))

    _trend_sheet(wb, trend)

    # ---------------------------------------------------------------- analysis
    wb.section("Analysis", C_ANALYSIS)
    _by_type_sheet(wb, facts)
    _distribution_sheet(wb, facts)
    _reasons_sheet(wb, facts)
    _thesis_sheet(wb, facts)

    # ---------------------------------------------------------------- detail
    wb.section("Detail", C_DETAIL)
    _matrix_sheet(wb, rows, host)
    _resources_sheet(wb, rows, host)
    _evidence_sheet(wb, rows)
    _breaches_sheet(wb, snapshot, host)
    _workloads_sheet(wb, snapshot)

    # ---------------------------------------------------------------- objectives
    wb.section("Objectives", C_OBJECTIVES)
    _objectives_sheet(wb, snapshot, reference_doc)
    _rates_sheet(wb, reference_doc)

    # ---------------------------------------------------------------- trust
    wb.section("Trust", C_TRUST)
    _provenance_sheet(wb, snapshot)
    _truncation_sheet(wb, snapshot)

    wb.index_sheet(colour=C_FRONT)
    return wb.to_bytes()


# --------------------------------------------------------------------------- analysis
def _by_type_sheet(wb: Any, facts: dict[str, Any]) -> None:
    from app.core.xlsx import coerce

    rows = []
    for entry in facts["by_type"]:
        rpo = entry["rpo"]
        counts = entry["rto_counts"]
        rows.append([
            coerce(entry["type"]), coerce(_scenario_label(entry["scenario"])),
            entry["resources"], entry["no_recovery_path"], entry["breached"],
            coerce(_class_label(entry["worst_rto_class"])), entry["undetermined"],
            counts[model.RTO_AUTOMATIC], counts[model.RTO_MINUTES], counts[model.RTO_HOURS],
            counts[model.RTO_DAY_PLUS], counts[model.RTO_NONE], counts[model.RTO_UNKNOWN],
            coerce(_minutes_text(rpo["best_minutes"])),
            coerce(_minutes_text(rpo["median_minutes"])),
            coerce(_minutes_text(rpo["worst_minutes"])),
            rpo["count_known"], rpo["excluded"],
            coerce(entry["dominant_reason"]), entry["dominant_reason_count"],
            coerce(", ".join(entry["examples"])),
        ])
    wb.sheet(
        "RTO-RPO by type",
        ["Resource type", "Scenario", "Resources", "No recovery path", "Breaching",
         "Worst RTO", "Undetermined",
         "RTO automatic", "RTO minutes", "RTO hours", "RTO day+", "RTO none", "RTO unknown",
         "Best RPO", "Median RPO", "Worst RPO", "RPO known", "RPO excluded",
         "Dominant reason", "Explains", "Examples"],
        rows,
        note="Ranked worst first. The RPO columns describe only the resources whose RPO "
             "could be measured — 'RPO excluded' is how many they leave out, and a median "
             "without it would be a lie of omission. Types that cannot experience a "
             "scenario are absent from it rather than shown as meeting their objective.",
    )


def _distribution_sheet(wb: Any, facts: dict[str, Any]) -> None:
    from app.core.xlsx import coerce

    rows = []
    for scenario in model.SCENARIOS:
        dist = facts["rto_distribution"][scenario]
        rpo = facts["rpo_distribution"][scenario]
        rows.append([
            coerce(_scenario_label(scenario)),
            dist[model.RTO_AUTOMATIC], dist[model.RTO_MINUTES], dist[model.RTO_HOURS],
            dist[model.RTO_DAY_PLUS], dist[model.RTO_NONE], dist[model.RTO_UNKNOWN],
            dist["not_applicable"],
            rpo["count_known"], rpo["none"], rpo["unknown"],
            coerce(_minutes_text(rpo["median_minutes"])),
        ])
    wb.sheet(
        "RTO-RPO distribution",
        ["Scenario", "Automatic", "Minutes", "Hours", "A day or more", "No recovery path",
         "Unknown", "Not applicable",
         "RPO known", "No recovery point", "RPO unknown", "Median RPO (known only)"],
        rows,
        note="Counts, never percentages: a percentage would have to fold 'unknown' into "
             "either the numerator or the denominator, and it belongs in neither.",
    )


def _reasons_sheet(wb: Any, facts: dict[str, Any]) -> None:
    from app.core.xlsx import coerce

    wb.sheet(
        "Reason index",
        ["Scenario", "Reason", "Resources", "No recovery path", "Evidence kind", "Source",
         "Resource types", "Examples"],
        [[coerce(_scenario_label(r["scenario"])), coerce(r["reason"]), r["resources"],
          r["no_recovery_path"], coerce(r["kind"]), coerce(r["source"]),
          coerce(", ".join(r["types"])), coerce(", ".join(r["examples"]))]
         for r in facts["reasons"]],
        note="The same misconfiguration recurs. Fixing the top rows here moves more of the "
             "estate than working down a resource list, because one row can be one change.",
    )


def _thesis_sheet(wb: Any, facts: dict[str, Any]) -> None:
    from app.core.xlsx import coerce

    wb.sheet(
        "Redundancy gap",
        ["Resource", "Type", "Zone redundant", "Replication", "Infrastructure RTO",
         "Logical RTO", "Much worse for", "No recovery path", "Why"],
        [[coerce(r["name"]), coerce(r["type"]), coerce(r["zone_redundant"]),
          coerce(r["replication"]), coerce(_class_label(r["infra_rto_class"])),
          coerce(_class_label(r["logical_rto_class"])),
          coerce(", ".join(r["worse_for"])), coerce(r["unrecoverable"]),
          coerce(r["reason"])]
         for r in facts["redundancy_gap"]],
        note="Redundancy makes infrastructure loss look solved on these resources while a "
             "bad deployment or a deletion is dramatically worse. No zone-centric tool "
             "flags them, because every infrastructure answer is green.",
    )


# --------------------------------------------------------------------------- detail
def _matrix_sheet(wb: Any, rows: list[dict[str, Any]], host: str = "") -> None:
    from app.core.xlsx import coerce

    out = []
    for row in rows:
        for scenario in model.SCENARIOS:
            verdict = (row.get("verdicts") or {}).get(scenario) or {}
            if not verdict.get("applicable", True):
                continue
            breach = verdict.get("breach") or {}
            target = verdict.get("target") or {}
            out.append([
                coerce(row.get("name")), coerce(row.get("id")), _portal(row.get("id"), host),
                coerce(row.get("type")),
                coerce(row.get("subscription_id")), coerce(row.get("resource_group")),
                coerce(row.get("location")), coerce(row.get("tier_label")),
                coerce(_scenario_label(scenario)),
                coerce(_rpo_text(verdict)),
                coerce(_class_label(verdict.get("rto_class", ""))),
                coerce(_band_text(verdict)), coerce(verdict.get("confidence")),
                coerce(breach.get("state", "")),
                coerce(_minutes_text(target.get("rpo_minutes"))),
                coerce(_class_label(target.get("rto_class", ""))),
                coerce("; ".join(e.get("detail", "") for e in verdict.get("basis", []))),
                coerce("; ".join(verdict.get("rto_assumptions") or [])),
            ])
    wb.sheet(
        "Recovery matrix",
        ["Resource", "Resource ID", "Open in Azure", "Type", "Subscription",
         "Resource group", "Region",
         "Tier", "Scenario", "RPO", "RTO class", "RTO band", "Confidence",
         "Against objective", "Target RPO", "Target RTO", "Why", "Assumptions"],
        out,
        note="One row per resource per applicable scenario. An RTO band is an estimate with "
             "its assumptions stated; it has not been verified by a drill.",
    )


def _resources_sheet(wb: Any, rows: list[dict[str, Any]], host: str = "") -> None:
    from app.core.xlsx import coerce

    wb.sheet(
        "Resources",
        ["Resource", "Resource ID", "Open in Azure", "Type", "Region", "Zone redundant",
         "Replication",
         "Protection", "Backup frequency", "Retention (days)", "Recovery point age (h)",
         "Vault redundancy", "Replicated", "Worst scenario", "Worst RTO"],
        [[coerce(r.get("name")), coerce(r.get("id")), _portal(r.get("id"), host),
          coerce(r.get("type")),
          coerce(r.get("location")),
          coerce((r.get("redundancy") or {}).get("zone_redundant")),
          coerce((r.get("redundancy") or {}).get("replication")),
          coerce((r.get("protection") or {}).get("state")),
          coerce((r.get("protection") or {}).get("frequency")),
          coerce((r.get("protection") or {}).get("retention_days")),
          coerce((r.get("protection") or {}).get("recovery_point_age_hours")),
          coerce((r.get("protection") or {}).get("vault_redundancy")),
          coerce((r.get("dr") or {}).get("replicated")),
          coerce(_scenario_label((r.get("worst") or {}).get("scenario", ""))),
          coerce(_class_label((r.get("worst") or {}).get("rto_class", "")))]
         for r in rows],
        note="Protection 'unknown' means the backup estate was not readable for this scope. "
             "It is not the same as 'not_protected'.",
    )


def _evidence_sheet(wb: Any, rows: list[dict[str, Any]]) -> None:
    """One row per fact, so reasoning can be filtered and pivoted.

    The matrix carries the same reasons joined into one cell, which is readable but neither
    filterable nor countable — and reasoning nobody can group is reasoning nobody uses."""
    from app.core.xlsx import coerce

    out = []
    for row in rows:
        for scenario in model.SCENARIOS:
            verdict = (row.get("verdicts") or {}).get(scenario) or {}
            if not verdict.get("applicable", True):
                continue
            for item in verdict.get("basis") or []:
                out.append([
                    coerce(row.get("name")), coerce(row.get("type")),
                    coerce(_scenario_label(scenario)),
                    coerce(_class_label(verdict.get("rto_class", ""))),
                    coerce(item.get("kind")), coerce(item.get("detail")),
                    coerce(item.get("source")), coerce(verdict.get("confidence")),
                ])
    wb.sheet(
        "Reasoning",
        ["Resource", "Type", "Scenario", "RTO class", "Evidence kind", "Detail", "Source",
         "Confidence"],
        out,
        note="Every configuration fact that contributed to a verdict, one per row. A "
             "verdict with no rows here would be an opinion.",
    )


def _breaches_sheet(wb: Any, snapshot: dict[str, Any], host: str = "") -> None:
    from app.core.xlsx import coerce

    wb.sheet(
        "Breaches",
        ["Resource", "Resource ID", "Open in Azure", "Type", "Scenario", "Tier", "RPO",
         "RTO class", "Target RPO (min)", "Target RTO", "No recovery path", "Why"],
        [[coerce(b.get("name")), coerce(b.get("resource_id")),
          _portal(b.get("resource_id"), host), coerce(b.get("type")),
          coerce(_scenario_label(b.get("scenario", ""))),
          coerce(b.get("tier")), coerce(_rpo_text(b)),
          coerce(_class_label(b.get("rto_class", ""))),
          coerce((b.get("target") or {}).get("rpo_minutes")),
          coerce(_class_label((b.get("target") or {}).get("rto_class", ""))),
          coerce(b.get("no_recovery_path")),
          coerce("; ".join(e.get("detail", "") for e in b.get("basis", [])))]
         for b in snapshot.get("breaches") or []],
        note="Ordered by consequence: no recovery path first, then total data loss, then "
             "the size of the miss weighted by tier.",
    )


def _workloads_sheet(wb: Any, snapshot: dict[str, Any]) -> None:
    from app.core.xlsx import coerce

    out = []
    for wl in snapshot.get("workloads") or []:
        for scenario, spec in (wl.get("scenarios") or {}).items():
            if not spec.get("applicable"):
                continue
            weakest = spec.get("weakest_link") or {}
            coverage = spec.get("coverage") or {}
            out.append([
                coerce(wl.get("name")), coerce(wl.get("tier")),
                coerce(_scenario_label(scenario)),
                coerce(_rpo_text(spec)), coerce(_class_label(spec.get("rto_class", ""))),
                coerce(weakest.get("name", "")), coerce(weakest.get("reason", "")),
                coerce(f"{coverage.get('determined', 0)}/{coverage.get('total', 0)}"),
            ])
    wb.sheet(
        "Workloads",
        ["Workload", "Tier", "Scenario", "RPO", "RTO class", "Weakest link", "Why",
         "Coverage"],
        out,
        note="; ".join([
            "Every component is treated as required; a genuinely redundant pair would "
            "recover faster.",
            "Components are assumed to recover in parallel — an ordered recovery can take "
            "longer than this figure.",
        ]),
    )


# --------------------------------------------------------------------------- objectives
def _objectives_sheet(wb: Any, snapshot: dict[str, Any],
                      reference_doc: dict[str, Any] | None) -> None:
    from app.core.xlsx import coerce

    doc = reference_doc or {}
    if not doc:
        wb.blind_sheet("Objectives",
                       "The objectives registry could not be read for this export.")
        return
    acknowledged = bool(snapshot.get("targets_acknowledged"))
    status = "Agreed" if acknowledged else "SHIPPED DEFAULT — not agreed by anyone"
    out = []
    for tier in doc.get("tiers") or []:
        for scenario in model.SCENARIOS:
            target = (tier.get("scenarios") or {}).get(scenario) or {}
            if not target:
                continue
            out.append([
                coerce(tier.get("id")), coerce(tier.get("label")),
                coerce(_scenario_label(scenario)),
                coerce(_class_label(target.get("rto_class", ""))),
                coerce(target.get("rpo_minutes")),
                coerce(_minutes_text(target.get("rpo_minutes"))),
                # Repeated per row on purpose: a filtered or sorted view must not be able to
                # separate a target from the fact that nobody agreed to it.
                status,
            ])
    by = doc.get("acknowledged_by")
    at = doc.get("acknowledged_at")
    wb.sheet(
        "Objectives",
        ["Tier", "Tier label", "Scenario", "Target RTO", "Target RPO (min)", "Target RPO",
         "Status"],
        out,
        note=(f"Registry version {doc.get('version', '?')}, "
              f"{'agreed' if acknowledged else 'STILL THE SHIPPED DEFAULTS'}"
              f"{f' by {by}' if by else ''}{f' at {at}' if at else ''}. "
              "Without this sheet the Breaches sheet's target columns are unverifiable."),
    )


def _rates_sheet(wb: Any, reference_doc: dict[str, Any] | None) -> None:
    from app.core.xlsx import coerce
    from app.resiliency import rollup

    doc = reference_doc or {}
    out: list[list[Any]] = []
    for key, value in (doc.get("restore_rates") or {}).items():
        out.append(["Restore rate", coerce(key), coerce(value),
                    "Throughput assumed when estimating how long a restore takes."])
    for key, value in (doc.get("mechanism_minutes") or {}).items():
        out.append(["Mechanism overhead (minutes)", coerce(key), coerce(value),
                    "Fixed time for this mechanism, independent of data volume."])
    for line in rollup.ASSUMPTIONS:
        out.append(["Roll-up assumption", "", "", coerce(line)])
    wb.sheet(
        "Assumptions and rates",
        ["Kind", "Name", "Value", "Meaning"],
        out,
        note="These constants produce every duration band in this workbook. They are "
             "starting points, not measurements — if you have timed your own restores, set "
             "them in Recovery Readiness and re-export.",
    )


# --------------------------------------------------------------------------- trust
def _provenance_sheet(wb: Any, snapshot: dict[str, Any]) -> None:
    from app.core.xlsx import coerce

    wb.sheet(
        "Provenance",
        ["Section", "Source", "Collected at", "Unreadable", "Truncated", "Reason"],
        [[name, coerce(p.get("source")), coerce(p.get("collected_at")),
          coerce(p.get("unreadable")), coerce(p.get("truncated")), coerce(p.get("reason"))]
         for name, p in (snapshot.get("provenance") or {}).items()],
        note="A section that could not be read says so here. 'No findings' and 'could not "
             "look' are opposite facts.",
        dates={"Collected at"},
    )


def _truncation_sheet(wb: Any, snapshot: dict[str, Any]) -> None:
    """Where the stored analysis stopped.

    The snapshot store caps rows. A file presented as complete that silently dropped its
    tail is worse than one that says where it stopped, because only the second can be
    checked."""
    from app.core.xlsx import coerce

    truncation = snapshot.get("truncation") or {}
    if not truncation:
        # Never an empty grid: a sheet called "Truncation" with no rows is ambiguous, and
        # the reader has to be able to tell "nothing was dropped" from "nobody checked".
        wb.sheet(
            "Truncation",
            ["Section", "Rows in this file", "Rows found", "Rows omitted"],
            [["Nothing was truncated — every row the analysis found is in this file.",
              "", "", 0]],
            note="Nothing was truncated: every row found is in this file.",
        )
        return
    rows = [[coerce(section), coerce(info.get("exported")), coerce(info.get("known_total")),
             coerce(int(info.get("known_total", 0)) - int(info.get("exported", 0)))]
            for section, info in truncation.items()]
    wb.sheet(
        "Truncation",
        ["Section", "Rows in this file", "Rows found", "Rows omitted"],
        rows,
        note="This analysis exceeded the stored row cap. The omitted rows are NOT in this "
             "file and were not considered by the summary sheets. Narrow the scope and "
             "re-analyze for a complete picture.",
    )


def _trend_sheet(wb: Any, trend: dict[str, Any] | None) -> None:
    from app.core.xlsx import coerce

    if not trend or not trend.get("available"):
        reason = (trend or {}).get("reason") or "No history has been recorded for this scope."
        wb.blind_sheet("Trend", f"No direction can be shown. {reason}")
        return
    wb.sheet(
        "Trend",
        ["Analyzed at", "Resources", "No recovery path", "Undetermined", "Breaches",
         "Protected", "Not protected", "Protection unknown"],
        [[coerce(p.get("generated_at")), coerce(p.get("resources")),
          coerce(p.get("no_recovery_path")), coerce(p.get("undetermined")),
          coerce(p.get("breaches")), coerce(p.get("protected")),
          coerce(p.get("not_protected")), coerce(p.get("protection_unknown"))]
         for p in trend.get("points") or []],
        note=(trend.get("caveat") or
              "One row per analysis. Gaps are real: no row means no analysis was run, and "
              "nothing is interpolated across them."),
        dates={"Analyzed at"},
    )


__all__ = ["build"]
