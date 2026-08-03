"""Effective coverage over cohort x application class x control.

Replaces the four-class matrix. Three things are different, and each one is the difference
between a matrix that finds a real gap and one that reassures:

**Coverage has TWO axes.** A policy can reach every user in a cohort and only half the apps in a
class — Teams but not SharePoint. The old cell tracked users only, so that case rendered
`enforced` and the dependency split was invisible. A cell here is `covered` only when both the
user fraction AND the app fraction are whole.

**`n/a` is a state.** Entra permits only MFA and authentication strength on the
'Register or join devices' user action; every other grant is disabled by the platform. Rendering
those as gaps invents work nobody can do, so the taxonomy declares applicability and inapplicable
cells are `n/a` — never counted as uncovered, never fed to a detector.

**Report-only never counts as coverage.** It gets its own state so "we wrote the policy" cannot
be mistaken for "the policy protects anyone".

Performance note, because this matrix is ~8x the size of the one it replaces (7 cohorts x 12
classes x 14 controls = 1,176 cells, against 140): the old `_cell` rebuilt
`set(p["effective_ids"])` for every policy on every cell. At this size that is millions of set
constructions on the request path, which in this product has previously meant a frozen
application rather than a slow screen. Policies are therefore indexed by (class, control) ONCE,
and `effective_ids` is a frozenset built once in normalisation.
"""
from __future__ import annotations

from typing import Any

from app.entra import ca_taxonomy

CELL_COVERED = "covered"
CELL_PARTIAL = "partial"
CELL_REPORT_ONLY = "report_only_only"
CELL_UNCOVERED = "uncovered"
CELL_NA = "n/a"

# How many ids to carry in a sample. The matrix is big; whole id lists per cell would make the
# response enormous for data the reader gets from the drill-down anyway.
_MAX_SAMPLE = 10


def build(
    policies: list[dict[str, Any]],
    cohorts: list[dict[str, Any]],
    index: dict[str, Any],
    *,
    version: str = ca_taxonomy.DEFAULT_VERSION,
    controls: list[dict[str, str]],
    signin_activity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The full matrix plus the derived classes."""
    control_keys = [c["key"] for c in controls]
    # Derived classes (`shadowed_classes`, `unattributed_apps`) are conclusions drawn FROM the
    # analysis, not targets a policy can name. Running them through the control axis produces a
    # full row of "uncovered" cells that mean nothing — there is no policy anyone could write to
    # turn them green. They are reported through their own sections in the exposure view.
    classes = [c for c in ca_taxonomy.classes(version) if not c.get("derived")]

    enforced = [p for p in policies if p.get("is_enforced")]
    report_only = [p for p in policies if p.get("is_report_only")]

    # ---- index once, not per cell -------------------------------------------------------
    # (class, control) -> [(effective_ids, hit_apps, missed_apps, name)]
    idx: dict[tuple[str, str], list[tuple[frozenset[str], set[str], set[str], str]]] = {}
    ro_idx: dict[tuple[str, str], bool] = {}

    def _fill(pols: list[dict[str, Any]], into: dict, mark_only: bool) -> None:
        for p in pols:
            resolved = p.get("class_coverage") or {}
            # Built ONCE per policy, not once per cell. The matrix is ~1,176 cells and rebuilding
            # a set per policy per cell is the shape of stall this product has been bitten by.
            eff = frozenset(p.get("effective_ids") or ())
            name = p.get("display_name") or p.get("id") or ""
            for cid, detail in resolved.items():
                for ctrl in p.get("controls") or []:
                    if ctrl not in control_keys:
                        continue
                    key = (cid, ctrl)
                    if mark_only:
                        into[key] = True
                    else:
                        into.setdefault(key, []).append(
                            (eff, set(detail.get("hit") or ()), set(detail.get("missed") or ()), name)
                        )

    _fill(enforced, idx, False)
    _fill(report_only, ro_idx, True)

    matrix: list[dict[str, Any]] = []
    for cohort in cohorts:
        ids = frozenset(cohort["ids"])
        row: dict[str, Any] = {
            "cohort": cohort["key"], "label": cohort["label"], "size": cohort["size"], "cells": {}
        }
        for cls in classes:
            allowed = ca_taxonomy.applicable_controls(cls["id"], control_keys, version)
            pool = index["members"].get(cls["id"], set())
            for ctrl in control_keys:
                cell_key = f"{cls['id']}|{ctrl}"
                if ctrl not in allowed:
                    row["cells"][cell_key] = {
                        "state": CELL_NA,
                        "reason": "Entra does not offer this control for this target.",
                    }
                    continue
                row["cells"][cell_key] = _cell(
                    ids, idx.get((cls["id"], ctrl)) or [], bool(ro_idx.get((cls["id"], ctrl))), pool
                )
        matrix.append(row)

    derived = _derived_classes(policies, index, signin_activity)

    return {
        "taxonomy_version": version,
        "cohorts": [{k: v for k, v in c.items() if k != "ids"} for c in cohorts],
        "app_classes": classes,
        "derived_classes": [c for c in ca_taxonomy.classes(version) if c.get("derived")],
        "controls": controls,
        "matrix": matrix,
        "derived": derived,
        "states": [CELL_COVERED, CELL_PARTIAL, CELL_REPORT_ONLY, CELL_UNCOVERED, CELL_NA],
    }


def _cell(
    cohort_ids: frozenset[str],
    hits: list[tuple[frozenset[str], set[str], set[str], str]],
    report_only_hit: bool,
    class_apps: set[str],
) -> dict[str, Any]:
    if not cohort_ids:
        return {
            "state": CELL_NA,
            "reason": "This cohort has no members in the tenant.",
            "uncovered_total": 0,
            "uncovered_sample": [],
        }

    covered_users: set[str] = set()
    covered_apps: set[str] = set()
    policies: list[str] = []
    for eff, hit_apps, _missed, name in hits:
        reached = cohort_ids & eff
        if not reached:
            continue
        covered_users |= reached
        covered_apps |= hit_apps
        policies.append(name)

    app_total = len(class_apps)
    # A class with no resolvable apps in this tenant (an empty preset, a construct-only class)
    # is judged on the user axis alone — inventing an app denominator of zero would make every
    # such cell permanently partial.
    apps_whole = (not app_total) or covered_apps >= class_apps
    users_whole = covered_users >= cohort_ids

    if not covered_users:
        state = CELL_REPORT_ONLY if report_only_hit else CELL_UNCOVERED
    elif users_whole and apps_whole:
        state = CELL_COVERED
    else:
        state = CELL_PARTIAL

    missed_apps = sorted(class_apps - covered_apps)
    uncovered = sorted(cohort_ids - covered_users)
    return {
        "state": state,
        "users_covered": len(covered_users),
        "users_total": len(cohort_ids),
        "apps_covered": len(covered_apps & class_apps) if app_total else 0,
        "apps_total": app_total,
        "apps_missing": missed_apps[:_MAX_SAMPLE],
        "apps_missing_total": len(missed_apps),
        "policies": sorted(set(policies))[:_MAX_SAMPLE],
        # `uncovered_total` is load-bearing: the ca.admins_uncovered / users_uncovered /
        # guests_uncovered detectors gate on it, and a detector that reads a missing key
        # reports a clean tenant rather than an error. Renaming it silently disarms them.
        "uncovered_total": len(uncovered),
        "uncovered_sample": uncovered[:_MAX_SAMPLE],
    }


def _derived_classes(
    policies: list[dict[str, Any]],
    index: dict[str, Any],
    signin_activity: dict[str, Any] | None,
) -> dict[str, Any]:
    """The two classes computed FROM the analysis rather than from a policy target."""
    enforced = [p for p in policies if p.get("is_enforced")]

    # --- shadowed classes: every matching policy is disabled or report-only -----------------
    touched: dict[str, list[str]] = {}
    enforced_classes: set[str] = set()
    for p in policies:
        for cid in (p.get("class_coverage") or {}):
            touched.setdefault(cid, []).append(p.get("display_name") or p.get("id") or "")
            if p.get("is_enforced"):
                enforced_classes.add(cid)
    shadowed = sorted(cid for cid in touched if cid not in enforced_classes)

    # --- unattributed apps -----------------------------------------------------------------
    # Requires per-service-principal sign-in activity. When that has not been collected the
    # class reports NOT MEASURED, never an empty list: "0 unattributed apps" derived from data
    # nobody gathered is the most reassuring possible way to be wrong.
    activity = signin_activity or {}
    if not activity.get("measured"):
        unattributed = {
            "measured": False,
            "reason": activity.get("reason")
                or "Per-application sign-in activity has not been collected for this tenant, so "
                   "apps that are signed into but matched by no policy cannot be identified.",
            "apps": [],
            "total": 0,
        }
    else:
        active = {str(a).lower() for a in activity.get("active_app_ids") or []}
        labels = index.get("labels") or {}

        # A policy scoped to "All cloud apps" resolves to the wildcard `*`, not to a list of
        # application ids. Treating the wildcard as covering nothing (as this did) meant a
        # tenant with ten enforced all-apps policies reported 359 of its 513 active
        # applications as ungoverned. Every one of them was covered. That is not a small
        # inaccuracy: a panel that cries wolf 359 times is a panel nobody reads again, and it
        # would have buried the handful of genuinely uncovered applications.
        wildcard = [p for p in enforced if p.get("targets_all_apps")]
        explicit: set[str] = set()
        for p in enforced:
            for detail in (p.get("class_coverage") or {}).values():
                explicit |= {str(a).lower() for a in (detail.get("hit") or ()) if a != "*"}

        if wildcard:
            # Covered unless EVERY wildcard policy excludes it — one policy still reaching the
            # application is enough for it to be governed.
            excluded_by_all: set[str] | None = None
            for p in wildcard:
                exc = {str(a).lower()
                       for a in ((p.get("conditions") or {}).get("exclude_apps") or [])}
                excluded_by_all = exc if excluded_by_all is None else (excluded_by_all & exc)
            orphan = sorted((active & (excluded_by_all or set())) - explicit)
        else:
            orphan = sorted(active - explicit)

        unattributed = {
            "measured": True,
            "window_days": activity.get("window_days"),
            "apps": [{"app_id": a, "name": labels.get(a, a)} for a in orphan[:50]],
            "total": len(orphan),
            "active_total": len(active),
        }

    return {
        "shadowed_classes": {
            "classes": shadowed,
            "detail": {cid: sorted(set(touched[cid]))[:_MAX_SAMPLE] for cid in shadowed},
        },
        "unattributed_apps": unattributed,
    }
