"""The Entra workbook export.

The behaviour under test is almost entirely about honesty. A workbook is read offline, weeks
later, by somebody who cannot ask the tool a follow-up question — so a sheet that is empty
because a Graph permission was refused has to say so IN THE FILE, and a number that was
extrapolated from a sample has to carry that word next to it.
"""
from __future__ import annotations

import io

import pytest
from openpyxl import load_workbook

from app.core import xlsx
from app.entra import export


def _wb(blob: bytes):
    return load_workbook(io.BytesIO(blob))


def _notes(wb) -> dict:
    """sheet name -> the Index note. Columns: Sheet, Section, Rows, Note."""
    return {r[0].value: r[3].value for r in wb["Index"].iter_rows(min_row=2)}


def _sections(wb) -> dict:
    return {r[0].value: r[1].value for r in wb["Index"].iter_rows(min_row=2)}


def _snapshot(**over):
    base = {
        "tenant_id": "t1",
        "loaded": True,
        "generated_at": "2026-08-02T00:00:00Z",
        "domains": {},
        "data": {},
        "licences": {},
        "permissions": {"granted": [], "granted_known": True, "domains": {}},
        "_analysis": {"score": {"score": 50, "coverage": 1.0, "pillars": []},
                      "findings": [], "by_signal": {}, "not_measured": {}, "errors": {}},
    }
    base.update(over)
    return base


# =========================================================================== blindness
def test_a_refused_domain_is_written_as_not_measured_not_as_an_empty_sheet():
    """The whole point. Entra domains go blind when a Graph scope is missing, and an empty
    sheet titled "Risky users" reads as "no risky users" — the opposite of the truth."""
    snap = _snapshot(
        domains={"risk": {"status": "blind", "missing_permissions": ["IdentityRiskEvent.Read.All"]}},
        data={"risk": {"risky_users": []}},
    )
    ws = _wb(export.to_workbook(snapshot=snap))["Sign-in summary"]
    assert ws.cell(row=2, column=1).value == "NOT MEASURED"
    detail = str(ws.cell(row=2, column=2).value)
    assert "blind" in detail
    assert "IdentityRiskEvent.Read.All" in detail, "the missing permission must be named"


@pytest.mark.parametrize("status", ["blind", "error", "not_collected", "unlicensed"])
def test_every_unreadable_status_blocks_the_sheet(status):
    snap = _snapshot(domains={"apps": {"status": status}}, data={"apps": {"service_principals": []}})
    ws = _wb(export.to_workbook(snapshot=snap))["Service principals"]
    assert ws.cell(row=2, column=1).value == "NOT MEASURED", status


def test_a_partial_domain_still_ships_its_rows_but_says_they_are_incomplete():
    """`partial` is not `blind`: the rows arrived, something alongside them was degraded.
    Suppressing them would lose real data; shipping them silently would overstate coverage."""
    snap = _snapshot(
        domains={"roles": {"status": "partial", "missing_permissions": ["RoleManagement.Read.All"]}},
        data={"roles": {"definitions": [{"display_name": "Global Administrator", "tier": "tier0"}],
                        "assignments": [], "group_derived": [], "eligible": []}},
    )
    blob = export.to_workbook(snapshot=snap)
    wb = _wb(blob)
    assert wb["Role definitions"].cell(row=2, column=1).value == "Global Administrator"
    assert "partial" in str(_notes(wb).get("Role definitions", ""))


def test_the_blind_spot_register_lists_every_domain():
    snap = _snapshot(domains={
        "ca": {"status": "ok"},
        "pim": {"status": "partial", "missing_permissions": ["RoleManagement.Read.Directory"]},
    })
    ws = _wb(export.to_workbook(snapshot=snap))["Coverage & blind spots"]
    names = {ws.cell(row=r, column=1).value for r in range(2, ws.max_row + 1)}
    assert {"ca", "pim"} <= names


def test_a_sampled_total_is_never_presented_as_a_count():
    """Sign-in totals are extrapolated when the collector sampled. A reader quoting "12
    failures" from a sampled window is quoting an estimate as a measurement."""
    snap = _snapshot(
        domains={"risk": {"status": "ok"}},
        data={"risk": {"signins": {"sampled": True, "total": 1000}, "risky_users": []}},
    )
    ws = _wb(export.to_workbook(snapshot=snap))["Sign-in summary"]
    rows = {ws.cell(row=r, column=1).value: str(ws.cell(row=r, column=2).value)
            for r in range(2, ws.max_row + 1)}
    assert "extrapolated" in rows["Sampled"].lower()


def test_an_empty_but_readable_domain_says_it_is_a_real_absence():
    """Zero lifecycle workflows on a READABLE governance domain is a configuration gap, not a
    blind spot. The two look identical in an empty sheet, so the note has to separate them."""
    snap = _snapshot(domains={"governance": {"status": "ok"}},
                     data={"governance": {"reviews": [], "packages": [], "assignments": [],
                                          "workflows": []}})
    wb = _wb(export.to_workbook(snapshot=snap))
    assert "not an absence of data" in str(_notes(wb).get("Lifecycle workflows", ""))


# =========================================================================== completeness
def test_nested_detail_becomes_its_own_sheet():
    """Credentials and grants are lists inside a row. Joined into one cell, an expiring secret
    is invisible; as rows, it is filterable."""
    snap = _snapshot(
        domains={"apps": {"status": "ok"}},
        data={"apps": {
            "service_principals": [{
                "display_name": "App A", "app_id": "a1",
                "credentials": [{"kind": "secret", "end": "2026-01-01", "expired": True, "days_left": -30}],
                "granted_app_permissions": [{"permission": "Directory.ReadWrite.All",
                                             "resource": "Microsoft Graph", "tier": "critical"}],
                "granted_delegated": [{"resource": "Microsoft Graph", "consent_type": "AllPrincipals",
                                       "scopes": ["User.Read", "Mail.Read"], "max_tier": "medium"}],
                "reply_urls": ["http://localhost"],
                "reply_url_risks": [{"uri": "http://localhost", "risk": "localhost"}],
                "owner_ids": ["u1"],
            }],
            "applications": [],
        }},
    )
    wb = _wb(export.to_workbook(snapshot=snap))
    perms = [[c.value for c in r] for r in wb["App permissions granted"].iter_rows(min_row=2)]
    # One row per SCOPE, not one per grant: a delegated grant of two scopes is two facts.
    assert {p[3] for p in perms} == {"Directory.ReadWrite.All", "User.Read", "Mail.Read"}
    assert any(p[6] == "AllPrincipals" for p in perms), "tenant-wide consent must be visible"
    creds = [[c.value for c in r] for r in wb["App credentials"].iter_rows(min_row=2)]
    assert creds[0][7] == "Yes" and creds[0][8] == -30
    urls = [[c.value for c in r] for r in wb["Redirect   reply URLs"].iter_rows(min_row=2)]
    assert urls[0][5] == "localhost", "the reply-url risk must survive the join"
    assert wb["App owners"].cell(row=2, column=4).value == "u1"


def test_the_mfa_gap_is_every_enabled_user_without_a_method():
    """The screen caps this list at 500. An export that inherited the cap would be the same
    defect in a different wrapper."""
    users = [{"display_name": f"u{i}", "enabled": True, "mfa_registered": i % 2 == 0}
             for i in range(1200)]
    snap = _snapshot(domains={"people": {"status": "ok"}},
                     data={"people": {"users": users, "groups": []}})
    wb = _wb(export.to_workbook(snapshot=snap))
    assert wb["Users"].max_row - 1 == 1200
    assert wb["MFA registration gap"].max_row - 1 == 600


def test_the_index_reports_every_sheet_and_its_size():
    snap = _snapshot(domains={"people": {"status": "ok"}},
                     data={"people": {"users": [{"display_name": "a", "enabled": True}], "groups": []}})
    wb = _wb(export.to_workbook(snapshot=snap))
    assert wb.sheetnames[0] == "Index", "the contents page must come first in a 50-sheet file"
    listed = {r[0].value for r in wb["Index"].iter_rows(min_row=2)}
    assert listed >= set(wb.sheetnames) - {"Index", "Summary"}


def test_the_summary_warns_that_the_file_carries_personal_data():
    ws = _wb(export.to_workbook(snapshot=_snapshot()))["Summary"]
    text = " ".join(str(ws.cell(row=r, column=1).value) for r in range(1, ws.max_row + 1))
    assert "PERSONAL DATA" in text


# =========================================================================== the xlsx helper
def test_a_formula_in_a_display_name_cannot_execute():
    """`=cmd|'/c calc'!A1` in a display name is a working RCE against the reviewer's
    workstation when the file is opened. Every cell is neutralised on the way in."""
    snap = _snapshot(
        domains={"people": {"status": "ok"}},
        data={"people": {"users": [{"display_name": "=cmd|'/c calc'!A1", "enabled": True}],
                         "groups": []}},
    )
    value = _wb(export.to_workbook(snapshot=snap))["Users"].cell(row=2, column=1).value
    assert str(value).startswith("'="), value


@pytest.mark.parametrize("raw", ["=1+1", "+1", "-1", "@SUM(A1)", " =1+1", "\t=1+1"])
def test_every_formula_trigger_is_neutralised(raw):
    assert str(xlsx.cell_safe(raw)).startswith("'")


def test_sheet_titles_are_legal_and_unique():
    """Excel rejects `[]:*?/\\` and truncates at 31 characters, and openpyxl silently renames a
    duplicate — so two long titles colliding would produce a sheet that is not what it says."""
    used: set[str] = set()
    a = xlsx.safe_title("Redirect / reply URLs", used)
    assert "/" not in a
    long_a = xlsx.safe_title("A" * 40 + " one", used)
    long_b = xlsx.safe_title("A" * 40 + " two", used)
    assert len(long_a) <= 31 and len(long_b) <= 31
    assert long_a != long_b, "two titles truncating to the same 31 chars must be disambiguated"


def test_booleans_read_as_yes_and_blank_not_as_true_and_false():
    assert xlsx.coerce(True) == "Yes"
    assert xlsx.coerce(False) == ""
    assert xlsx.coerce(None) == ""
    assert xlsx.coerce(["a", "b"]) == "a, b"


def test_activation_history_comes_from_the_ledger_not_just_the_snapshot():
    """The snapshot holds only what the current collection saw. The screen merges it with a
    durable ledger that reaches past Graph's 30-day retention, and reading the snapshot alone
    lost 13 of 183 sessions on a live tenant \u2014 silently, which is the worst way to lose them."""
    snap = _snapshot(
        domains={"activations": {"status": "ok"}},
        data={"activations": {"sessions": [{"principal_name": "recent", "role_name": "GA"}]}},
    )
    merged = [{"principal_name": "recent", "role_name": "GA"},
              {"principal_name": "from the ledger", "role_name": "GA"}]
    wb = _wb(export.to_workbook(snapshot=snap, activations=merged))
    names = {r[2].value for r in wb["Activation sessions"].iter_rows(min_row=2)}
    assert names == {"recent", "from the ledger"}

    # And without the merge it must fall back rather than render nothing.
    wb2 = _wb(export.to_workbook(snapshot=snap))
    assert wb2["Activation sessions"].max_row - 1 == 1


# =========================================================================== tab colours
def _full_wb():
    """A workbook with every section populated, built from one readable snapshot."""
    snap = _snapshot(
        domains={d: {"status": "ok"} for d in
                 ("ca", "roles", "pim", "activations", "apps", "risk", "people", "governance")},
        data={
            "ca": {"policies": [{"display_name": "p", "conditions": {}}], "named_locations": [],
                   "auth_strengths": []},
            "_ca_analysis": {"coverage": {}, "conflicts": [], "breakglass": {}},
            "roles": {"definitions": [], "assignments": [], "group_derived": [], "eligible": []},
            "pim": {"policies": [], "group_eligibilities": []},
            "activations": {"sessions": []},
            "apps": {"service_principals": [], "applications": []},
            "risk": {"signins": {}, "risky_users": [], "risk_detections": [],
                     "risky_service_principals": [], "patterns": []},
            "people": {"users": [], "groups": []},
            "governance": {"reviews": [], "packages": [], "assignments": [], "workflows": []},
            "tenant": {},
            "_azure_link": {"available": False, "reason": "no Azure connection"},
        },
    )
    return _wb(export.to_workbook(snapshot=snap, escalations=[], setup_tiers=[], scanners=[]))


def test_every_sheet_from_one_parent_tab_shares_a_tab_colour():
    """The grouping a reader needs in a fifty-sheet file. Every Conditional Access sheet is the
    same colour, and it is not the colour any other section uses."""
    wb = _full_wb()
    by_section: dict[str, set[str]] = {}
    for name in wb.sheetnames:
        colour = wb[name].sheet_properties.tabColor
        section = _sections(wb).get(name)
        if section:
            by_section.setdefault(section, set()).add(str(colour.rgb) if colour else "")

    ca = by_section.get("Conditional Access")
    assert ca and len(ca) == 1, f"Conditional Access sheets disagree on colour: {ca}"

    # Every section is internally consistent...
    for section, colours in by_section.items():
        assert len(colours) == 1, f"{section} sheets disagree on colour: {colours}"
    # ...and no two sections share one, or the colour would group the wrong things.
    flat = [next(iter(c)) for c in by_section.values()]
    assert len(flat) == len(set(flat)), f"two sections share a colour: {flat}"


def test_the_conditional_access_sheets_are_all_in_that_section():
    wb = _full_wb()
    sections = _sections(wb)
    ca_sheets = {n for n, sec in sections.items() if sec == "Conditional Access"}
    assert {"CA policies", "CA policy conditions", "CA conflicts"} <= ca_sheets


def test_the_index_names_the_section_as_well_as_colouring_it():
    """Colour alone is not readable to everyone and does not survive a monochrome print."""
    wb = _full_wb()
    sections = _sections(wb)
    assert sections.get("CA policies") == "Conditional Access"
    assert sections.get("Users") == "Directory"
    assert sections.get("Findings") == "Findings & scanners"


def test_the_palette_covers_every_section_that_is_used():
    used = set(_sections(_full_wb()).values()) - {None, ""}
    assert used <= set(export.SECTION_COLOURS), f"section with no colour: {used - set(export.SECTION_COLOURS)}"


def test_tab_colours_are_opaque_or_excel_draws_nothing():
    """The bug this pins shipped once already.

    OOXML stores colours as aRGB and openpyxl pads a 6-digit value with ``00`` alpha \u2014 fully
    transparent. Every assertion about colours being present and distinct still passed, the
    round-trip read the value straight back, and Excel showed plain grey tabs. Only the alpha
    channel separates "purple" from "invisible"."""
    wb = _full_wb()
    for name in wb.sheetnames:
        colour = wb[name].sheet_properties.tabColor
        if colour is None:
            continue
        rgb = str(colour.rgb)
        assert len(rgb) == 8, f"{name}: {rgb} is not 8-digit ARGB"
        assert rgb[:2] == "FF", f"{name}: alpha {rgb[:2]} is transparent, Excel will draw nothing"


def test_the_colour_reaches_the_worksheet_xml_opaque():
    """Asserted on the FILE, not on the object model. The object model happily reports a colour
    that the spreadsheet will never show."""
    import zipfile

    blob = export.to_workbook(
        snapshot=_snapshot(domains={"ca": {"status": "ok"}},
                           data={"ca": {"policies": [], "named_locations": [], "auth_strengths": []},
                                 "_ca_analysis": {}}),
    )
    z = zipfile.ZipFile(io.BytesIO(blob))
    seen = []
    for entry in z.namelist():
        if entry.startswith("xl/worksheets/sheet"):
            xml = z.read(entry).decode()
            i = xml.find('tabColor rgb="')
            if i >= 0:
                seen.append(xml[i + 14: i + 22])
    assert seen, "no tabColor reached the worksheet XML at all"
    assert all(v[:2] == "FF" for v in seen), f"transparent tab colours in the file: {set(seen)}"


@pytest.mark.parametrize(("given", "expected"), [
    ("7030A0", "FF7030A0"), ("#7030A0", "FF7030A0"), ("FF7030A0", "FF7030A0"), ("", ""),
])
def test_argb_normalisation(given, expected):
    assert xlsx.argb(given) == expected
