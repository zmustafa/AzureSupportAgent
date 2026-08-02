"""CSV / JSON / multi-sheet XLSX export of normalized access rows.

Two column sets: ``schema.COLUMNS`` (everything this product knows) and
``schema.SCANNER_COLUMNS`` (the frozen 46 the standalone all-azure-access scanner emits).
Passing ``columns=schema.SCANNER_COLUMNS`` projects an export back down to scanner shape so a
round trip through the import endpoint is byte-identical."""
from __future__ import annotations

import csv
import io
import json
from typing import Any

from app.iam import schema


# Excel / LibreOffice interpret a cell that starts with one of these characters as a
# formula — `=cmd|'...'!A1` is the classic vector for CSV-injection RCE on the
# admin's workstation. Prefixing with a single quote forces literal-text interpretation.
_FORMULA_TRIGGERS = ("=", "+", "-", "@")
# Tab / CR / LF can also kick off formula interpretation in some spreadsheet apps.
_FORMULA_LEADING_WS = ("\t", "\r", "\n")


def _csv_safe(value: Any) -> Any:
    """Neutralize CSV / Excel formula-injection vectors in a single cell value.

    Strings that begin with ``= + - @`` (or with leading whitespace followed by
    one of those) are prefixed with a leading apostrophe so the spreadsheet
    treats them as plain text. Non-string values pass through unchanged.
    """
    if not isinstance(value, str) or not value:
        return value
    stripped = value.lstrip("\t\r\n ")
    if stripped and stripped[0] in _FORMULA_TRIGGERS:
        return "'" + value
    if value[0] in _FORMULA_LEADING_WS and stripped and stripped[0] in _FORMULA_TRIGGERS:
        return "'" + value
    return value


def to_csv(rows: list[dict[str, Any]], columns: tuple[str, ...] | None = None) -> str:
    """Serialize rows to CSV. Defaults to the full column set; pass
    ``schema.SCANNER_COLUMNS`` for scanner-shaped output."""
    cols = columns or schema.COLUMNS
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(cols), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: _csv_safe(row.get(c, "")) for c in cols})
    return buf.getvalue()


def to_json(rows: list[dict[str, Any]], columns: tuple[str, ...] | None = None) -> str:
    """Serialize rows to a pretty JSON array, optionally projected to a column subset."""
    if columns is not None:
        rows = [{c: r.get(c, "") for c in columns} for r in rows]
    return json.dumps(rows, indent=2, default=str)


# --------------------------------------------------------------------------- XLSX workbook
# A friendlier subset/order of the schema columns for the human-readable access lenses. The
# COMPLETE record is a sheet of its own ("Access — all columns"), so nothing is only available
# outside the workbook.
#
# `effect` leads deliberately. It used to be absent entirely, which meant the eight Deny
# assignments on a real tenant sat in a sheet called "Effective Access" among 5,506 Allow rows
# with nothing to tell them apart — anyone filtering or pivoting that sheet counted a control as
# a grant. `principalExists` is here for the same reason: 100 rows on that tenant point at
# principals that no longer exist, and without the column they read as live access.
_ACCESS_HEADERS: tuple[str, ...] = (
    "effect",
    "effectivePrincipalName",
    "effectivePrincipalUserPrincipalName",
    "effectivePrincipalType",
    "principalExists",
    "roleName",
    "roleIsPrivileged",
    "roleHasDataActions",
    "surface",
    "accessPath",
    "sourceGroupName",
    "scopeDisplayName",
    "scope",
    "scopeType",
    "subscriptionName",
    "subscriptionId",
    "resourceGroup",
    "assignmentState",
    "principalDisplayName",
    "principalId",
    "condition",
    "collector",
    # Identifiers, so a row can be traced back to Azure or fed to a remediation script.
    "assignmentId",
    "roleDefinitionId",
)


def _safe_sheet_title(title: str) -> str:
    """Excel sheet titles: ≤31 chars, none of ``[]:*?/\\``."""
    for ch in "[]:*?/\\":
        title = title.replace(ch, " ")
    return title.strip()[:31] or "Sheet"


def _coerce(value: Any) -> Any:
    if isinstance(value, bool):
        return "Yes" if value else ""
    if value is None:
        return ""
    if isinstance(value, (int, float, str)):
        # Apply the same formula-injection neutralization as the CSV path. Excel
        # interprets `=...` / `+...` / `-...` / `@...` cells as formulas even in
        # an .xlsx workbook.
        return _csv_safe(value)
    return _csv_safe(str(value))


def to_workbook(
    *,
    rows: list[dict[str, Any]],
    overview: dict[str, Any],
    pivots: dict[str, list[dict[str, Any]]],
    pivot_labels: dict[str, str],
    directory: dict[str, Any],
    findings: dict[str, Any] | None = None,
    rightsizing: dict[str, Any] | None = None,
    bypass: dict[str, Any] | None = None,
    escalation: dict[str, Any] | None = None,
    scanners: list[dict[str, Any]] | None = None,
    score: dict[str, Any] | None = None,
    dataplane: list[dict[str, Any]] | None = None,
) -> bytes:
    """Build the multi-sheet ``.xlsx`` workbook: everything the review screen can show.

    The access lenses are only half of it. A workbook that carries the grid but not the
    FINDINGS, the right-sizing, the shadow-access sweep or the escalation paths hands somebody
    a list of role assignments and calls it an access review — the analysis is the part a
    reviewer cannot reproduce themselves from the portal.

    Every analysis sheet distinguishes "nothing found" from "not measured". A sheet that is
    empty because the sweep never ran is written with the reason instead of the rows, because
    an empty sheet titled "Shadow Access" reads as a clean result.

    ``rows`` is the (optionally filtered) master row set. The optional payloads are passed in
    rather than computed here so this stays a pure formatter.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="0F6CBD")

    def _sheet(title: str, headers: list[str], data: list[list[Any]]) -> None:
        ws = wb.create_sheet(_safe_sheet_title(title))
        ws.append(headers)
        for c in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=c)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(vertical="center")
        for r in data:
            ws.append([_coerce(v) for v in r])
        # Freeze the header + size columns to their content (bounded).
        ws.freeze_panes = "A2"
        for ci, h in enumerate(headers, start=1):
            width = len(str(h))
            for r in data[:200]:
                if ci - 1 < len(r):
                    width = max(width, len(str(_coerce(r[ci - 1]))))
            ws.column_dimensions[get_column_letter(ci)].width = min(60, max(10, width + 2))
        if data:
            ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(data) + 1}"

    def _rows_for(predicate) -> list[list[Any]]:
        return [[r.get(h, "") for h in _ACCESS_HEADERS] for r in rows if predicate(r)]

    def _blind_sheet(title: str, reason: str) -> None:
        """A sheet for something that could NOT be measured.

        Deliberately not an empty grid. "Shadow Access" with no rows reads as "no shadow access",
        which is the opposite of "the sweep never ran"."""
        _sheet(title, ["Status", "Why"], [["NOT MEASURED", reason]])

    # 1. Summary — KPIs + generation metadata.
    kpis = overview.get("kpis", {})
    summary: list[list[Any]] = [["Metric", "Value"]]
    label_map = [
        ("Total grants", "total_assignments"),
        ("Unique principals", "unique_principals"),
        ("Privileged", "privileged"),
        ("Data-plane", "data_plane"),
        ("Group-derived", "group_derived"),
        ("Service-principal owners", "owners"),
        ("Entra directory roles", "entra_roles"),
        ("PIM eligible", "eligible"),
        ("Scopes", "scopes"),
        ("Subscriptions", "subscriptions"),
    ]
    ws0 = wb.active
    ws0.title = "Summary"
    ws0.append(["RBAC — Access Review export"])
    ws0.cell(row=1, column=1).font = Font(bold=True, size=14)
    ws0.append(["Generated", overview.get("generated_at", "")])
    ws0.append(["Tenant", overview.get("tenant_id", "")])
    ws0.append(["Demo dataset", "Yes" if overview.get("demo") else "No"])
    ws0.append([])
    ws0.append(["Metric", "Value"])
    hdr_row = ws0.max_row
    for c in range(1, 3):
        ws0.cell(row=hdr_row, column=c).font = header_font
        ws0.cell(row=hdr_row, column=c).fill = header_fill
    for label, key in label_map:
        ws0.append([label, kpis.get(key, 0)])
    ws0.column_dimensions["A"].width = 26
    ws0.column_dimensions["B"].width = 40

    # 2–7. Access lenses.
    _sheet("Effective Access", list(_ACCESS_HEADERS), _rows_for(lambda r: True))
    # The complete record, every schema column. The lenses above are for reading; this is for
    # anyone who needs the field the lens left out.
    _sheet(
        "Access - all columns",
        list(schema.COLUMNS),
        [[r.get(h, "") for h in schema.COLUMNS] for r in rows],
    )
    _sheet("Privileged", list(_ACCESS_HEADERS), _rows_for(lambda r: bool(r.get("roleIsPrivileged"))))
    _sheet("Group-Derived", list(_ACCESS_HEADERS), _rows_for(lambda r: r.get("accessPath") == schema.PATH_GROUP))
    _sheet("SP Owners", list(_ACCESS_HEADERS), _rows_for(lambda r: r.get("accessPath") == schema.PATH_OWNER))
    _sheet("Entra Roles", list(_ACCESS_HEADERS), _rows_for(lambda r: r.get("surface") == schema.SURFACE_ENTRA))
    kv_rows = _rows_for(lambda r: r.get("surface") == schema.SURFACE_KEY_VAULT)
    if kv_rows:
        _sheet("Key Vault", list(_ACCESS_HEADERS), kv_rows)

    # 8. Scopes freshness.
    scope_headers = ["displayName", "scopeType", "status", "row_count", "collectors_attention", "generated_at", "demo"]
    _sheet(
        "Scopes",
        ["Scope", "Type", "Status", "Grants", "Attention", "Generated", "Demo"],
        [[s.get(h, "") for h in scope_headers] for s in overview.get("scopes", [])],
    )

    # 9. Role definitions (directory reference).
    rd = directory.get("role_defs", []) or []
    if rd:
        rd_headers = ["roleName", "roleCategory", "roleIsPrivileged", "roleHasDataActions", "actionsCount", "dataActionsCount", "description"]
        _sheet("Role Definitions", ["Role", "Category", "Privileged", "Data actions", "Actions", "Data actions #", "Description"],
               [[r.get(h, "") for h in rd_headers] for r in rd])

    # 10. Principal directory (the resolved GUID → name map).
    pr = directory.get("principals", []) or []
    if pr:
        pr_headers = ["displayName", "principalType", "userPrincipalName", "appId", "principalId", "source"]
        _sheet("Principals", ["Name", "Type", "UPN", "App ID", "Object ID", "Source"],
               [[p.get(h, "") for h in pr_headers] for p in pr])

    # 11. Insights — every pivot flattened.
    insight_data: list[list[Any]] = []
    for key, items in pivots.items():
        title = pivot_labels.get(key, key)
        for it in items:
            insight_data.append([title, it.get("label", ""), it.get("count", 0)])
    _sheet("Insights", ["Pivot", "Label", "Count"], insight_data)

    # 12. Diagnostics — collector statuses + any errors.
    diag_headers = ["collector", "scopeLabel", "status", "rowsAdded", "message"]
    _sheet(
        "Diagnostics",
        ["Collector", "Scope", "Status", "Rows", "Message"],
        [[c.get(h, "") for h in diag_headers] for c in overview.get("collectors", [])],
    )

    # ---- 13+. The analysis. Everything below is a judgement the reader cannot reconstruct
    # from a list of role assignments, which is exactly why leaving it out made the workbook a
    # data dump rather than a review.
    if score is not None:
        pillar_rows: list[list[Any]] = [[
            "OVERALL", score.get("grade", ""), score.get("score", ""),
            f"{round((score.get('coverage') or 0) * 100)}% of the weighted checks could be measured",
            "", "",
        ]]
        for p in score.get("pillars", []) or []:
            pillar_rows.append([
                p.get("label", p.get("key", "")),
                p.get("state", ""),
                # A blind pillar has no score. Writing 0 would read as "scored zero".
                "not measured" if p.get("score") is None else p.get("score"),
                p.get("reason", "") or p.get("desc", ""),
                p.get("findings", 0),
                f"{p.get('signals_measured', 0)}/{p.get('signals', 0)} checks measured",
            ])
        _sheet("Posture Score", ["Pillar", "State", "Score", "Notes", "Findings", "Coverage"], pillar_rows)

    if findings is not None:
        items = findings.get("findings") or []
        _sheet(
            "Findings",
            ["Severity", "Pillar", "Signal", "Title", "Subject", "Detail", "Count", "State",
             "Remediation", "Frameworks", "Fingerprint"],
            [[
                f.get("severity", ""), f.get("pillar", ""), f.get("signal_id", ""),
                f.get("title", ""), f.get("subject_label", "") or f.get("subject", ""),
                f.get("detail", ""), f.get("count", 0), f.get("state", ""),
                f.get("remediation", ""), ", ".join(f.get("frameworks") or []), f.get("id", ""),
            ] for f in items],
        )
        # The checks that could NOT run, as their own sheet. Folding them into the findings list
        # would let "we could not look" disappear into "nothing found".
        unmeasured = findings.get("unmeasured") or []
        _sheet(
            "Findings - not measured",
            ["Signal", "Title", "Pillar", "Why it could not be checked"],
            [[u.get("signal_id", ""), u.get("title", ""), u.get("pillar", ""), u.get("reason", "")]
             for u in unmeasured],
        )

    if scanners is not None:
        _sheet(
            "Scanners",
            ["Scanner", "Cadence", "Severity floor", "Blocked", "Total", "New", "Resolved",
             "Persisting", "Last run", "Due"],
            [[
                s.get("name", ""), s.get("cadence", ""), s.get("severity_floor", ""),
                "; ".join(s.get("blocked") or []) or "no",
                *( [ (s.get("counts") or {}).get(k, "") for k in ("total", "new", "resolved", "persisting") ]
                   if s.get("counts") else ["not measured"] * 4 ),
                s.get("last_run_at", ""), "yes" if s.get("due") else "no",
            ] for s in scanners],
        )

    if rightsizing is not None:
        if not rightsizing.get("measured"):
            _blind_sheet(
                "Right-sizing",
                "; ".join(rightsizing.get("limitations") or ["Usage has not been collected for this tenant."]),
            )
        else:
            _sheet(
                "Right-sizing",
                ["Principal", "Type", "Scope", "Current roles", "Granted actions", "Used actions",
                 "Unused %", "Confidence", "Recommendation", "Note", "Window (days)"],
                [[
                    r.get("principalName", "") or r.get("principalId", ""), r.get("principalType", ""),
                    r.get("scopeName", "") or r.get("scope", ""),
                    ", ".join(r.get("currentRoles") or []),
                    r.get("grantedActionCount", 0), r.get("usedActionCount", 0),
                    round((r.get("unusedRatio") or 0) * 100),
                    r.get("confidence", ""), r.get("recommendation", ""), r.get("note", ""),
                    r.get("window", ""),
                ] for r in (rightsizing.get("recommendations") or [])],
            )

    if bypass is not None:
        if bypass.get("never_loaded"):
            _blind_sheet(
                "Shadow Access",
                "The RBAC-bypass sweep has not run, so shared keys, local authentication, admin "
                "users and SQL logins have NOT been checked. This is not a clean result.",
            )
        else:
            _sheet(
                "Shadow Access",
                ["Severity", "Family", "Resource", "Type", "Subscription", "Resource group",
                 "Bypass kind", "Finding", "Open", "RBAC-only possible", "Who can reach it",
                 "Detail", "Remediation"],
                [[
                    b.get("severity", ""), b.get("family", ""), b.get("resourceName", ""),
                    b.get("resourceType", ""), b.get("subscriptionId", ""), b.get("resourceGroup", ""),
                    b.get("bypassKind", ""), b.get("title", ""),
                    "yes" if b.get("enabled") else "no",
                    "yes" if b.get("rbacOnlyPossible") else "no",
                    b.get("reachableCount", "") if b.get("reachabilityAvailable") else "not determined",
                    b.get("detail", ""), b.get("remediation", ""),
                ] for b in (bypass.get("rows") or [])],
            )

    if escalation is not None:
        paths = escalation.get("paths") or []
        _sheet(
            "Escalation Paths",
            ["From", "To", "Hops", "Min confidence", "Route", "Primitives"],
            [[
                p.get("fromLabel", "") or p.get("from", ""),
                (p.get("hops") or [{}])[-1].get("targetLabel", "") or p.get("to", ""),
                p.get("length", len(p.get("hops") or [])),
                p.get("min_confidence", ""),
                " -> ".join(
                    [str((p.get("hops") or [{}])[0].get("sourceLabel", ""))]
                    + [str(h.get("targetLabel", "")) for h in (p.get("hops") or [])]
                ),
                ", ".join(dict.fromkeys(str(h.get("primitive", "")) for h in (p.get("hops") or []))),
            ] for p in paths],
        )
        # What the map could not see travels WITH it. A path list without its blind spots is an
        # invitation to read "no path" as "no path exists".
        limits = escalation.get("limitations") or []
        if limits:
            _sheet("Escalation - blind spots", ["What this map cannot see"], [[x] for x in limits])

    if dataplane is not None:
        _sheet(
            "Data-plane Coverage",
            ["Service", "Azure RBAC is the whole picture", "Why not", "Other doors"],
            [[
                s.get("label", ""),
                "yes" if s.get("rbac_is_complete") else "NO",
                s.get("blind_reason", ""),
                "; ".join(s.get("doors") or []),
            ] for s in dataplane],
        )

    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()

