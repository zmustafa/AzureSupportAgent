"""Ownership import/export + owner→tag apply + shared tag revisions (recovery/revert).

30+ edge cases across: CSV/XLSX parsing, AI column-mapping heuristics, import preview +
materialize, owner-tag plan building, and the revision store's diff + revert logic (mocked
Azure writes)."""
from __future__ import annotations

import asyncio
import io

import pytest

from app.ownership import importer, sheet, tagging
from app.tagintel import revisions


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    import app.ownership.registry as reg
    monkeypatch.setattr(reg, "_OWNERS_PATH", tmp_path / "owners.json")
    monkeypatch.setattr(reg, "_ASSIGNMENTS_PATH", tmp_path / "assignments.json")
    monkeypatch.setattr(revisions, "_PATH", tmp_path / "tag_revisions.json")
    yield


# ============================================================ CSV / XLSX parsing
def test_parse_csv_basic():
    data = b"Name,Email,Department\nJane Doe,jane@x.com,Platform\nBob,bob@x.com,Data\n"
    out = sheet.parse_sheet("owners.csv", data)
    assert out["columns"] == ["Name", "Email", "Department"]
    assert out["row_count"] == 2
    assert out["rows"][0]["Email"] == "jane@x.com"


def test_parse_csv_semicolon_delimiter():
    data = b"name;email\nJane;jane@x.com\n"
    out = sheet.parse_sheet("o.csv", data)
    assert out["columns"] == ["name", "email"]
    assert out["rows"][0]["email"] == "jane@x.com"


def test_parse_tsv():
    data = b"name\temail\nJane\tjane@x.com\n"
    out = sheet.parse_sheet("o.tsv", data)
    assert out["rows"][0]["name"] == "Jane"


def test_parse_csv_utf8_bom_and_blank_lines():
    data = "\ufeffname,email\n\nJane,jane@x.com\n\n".encode("utf-8")
    out = sheet.parse_sheet("o.csv", data)
    assert out["columns"][0] == "name"   # BOM stripped
    assert out["row_count"] == 1


def test_parse_csv_missing_trailing_cells():
    data = b"name,email,dept\nJane,jane@x.com\n"  # row shorter than header
    out = sheet.parse_sheet("o.csv", data)
    assert out["rows"][0]["dept"] == ""


def test_parse_csv_blank_headers_get_placeholder():
    data = b"name,,email\nJane,x,jane@x.com\n"
    out = sheet.parse_sheet("o.csv", data)
    assert out["columns"][1] == "column_2"


def test_parse_empty_file_raises():
    with pytest.raises(ValueError):
        sheet.parse_sheet("o.csv", b"")


def test_parse_latin1_fallback():
    data = "name,note\nJosé,café".encode("latin-1")
    out = sheet.parse_sheet("o.csv", data)
    assert out["rows"][0]["name"]  # decoded without crashing


def test_parse_xlsx_roundtrip():
    owners = [{"id": "1", "display_name": "Jane", "email": "jane@x.com", "kind": "person"}]
    xlsx = sheet.owners_to_xlsx(owners, {"1": 3})
    assert xlsx[:2] == b"PK"  # zip magic
    parsed = sheet.parse_sheet("owners.xlsx", xlsx)
    assert "Display Name" in parsed["columns"]
    assert parsed["rows"][0]["Email"] == "jane@x.com"


def test_parse_sniff_xlsx_by_magic_without_extension():
    owners = [{"id": "1", "display_name": "Jane", "email": "jane@x.com"}]
    xlsx = sheet.owners_to_xlsx(owners)
    out = sheet.parse_sheet("upload", xlsx)  # no extension
    assert out["sheet"] == "Owners"


def _multi_sheet_xlsx() -> bytes:
    import io as _io

    from openpyxl import Workbook

    wb = Workbook()
    wb.active.title = "People"
    wb.active.append(["Name", "Email"])
    wb.active.append(["Jane", "jane@x.com"])
    teams = wb.create_sheet("Teams")
    teams.append(["Team", "Lead"])
    teams.append(["Payments", "Bob"])
    buf = _io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_list_sheet_names_csv_is_single():
    assert sheet.list_sheet_names("owners.csv", b"a,b\n1,2\n") == ["csv"]


def test_list_sheet_names_multi():
    names = sheet.list_sheet_names("book.xlsx", _multi_sheet_xlsx())
    assert names == ["People", "Teams"]


def test_parse_sheet_selects_named_sheet():
    xlsx = _multi_sheet_xlsx()
    people = sheet.parse_sheet("book.xlsx", xlsx, "People")
    assert people["sheet"] == "People" and people["columns"] == ["Name", "Email"]
    assert people["sheet_names"] == ["People", "Teams"]
    teams = sheet.parse_sheet("book.xlsx", xlsx, "Teams")
    assert teams["sheet"] == "Teams" and teams["rows"][0]["Lead"] == "Bob"


def test_parse_sheet_unknown_sheet_raises():
    with pytest.raises(ValueError):
        sheet.parse_sheet("book.xlsx", _multi_sheet_xlsx(), "Nope")


def test_parse_sheet_default_is_first_sheet():
    out = sheet.parse_sheet("book.xlsx", _multi_sheet_xlsx())  # no sheet -> active/first
    assert out["sheet"] == "People"


# ============================================================ CSV export injection defense
def test_csv_export_neutralizes_formula_injection():
    owners = [{"id": "1", "display_name": "=cmd|' /c calc'!A0", "email": "x@x.com"}]
    csv_text = sheet.owners_to_csv(owners)
    # The dangerous leading '=' must be prefixed with a quote.
    assert "'=cmd" in csv_text


def test_blank_template_has_headers():
    t = sheet.blank_template_csv()
    assert "name" in t and "email" in t and "workload" in t and "resource_ids" in t


# ============================================================ AI column mapping (heuristic)
def test_heuristic_mapping_common_headers():
    m = importer.heuristic_mapping(["Full Name", "E-Mail Address", "Team", "Notes"])
    assert m["display_name"] == "Full Name"
    assert m["email"] == "E-Mail Address"
    assert m["department"] == "Team"
    assert m["notes"] == "Notes"


def test_heuristic_mapping_resource_and_subscription():
    m = importer.heuristic_mapping(["Owner", "Resource ID", "Subscription"])
    assert m["display_name"] == "Owner"
    assert m["resource_ids"] == "Resource ID"
    assert m["subscription"] == "Subscription"


def test_heuristic_mapping_fallback_first_column_as_name():
    m = importer.heuristic_mapping(["col_a", "col_b"])
    assert m["display_name"] == "col_a"  # nothing matched → first column


def test_heuristic_mapping_empty_columns():
    assert importer.heuristic_mapping([]) == {}


def test_infer_mapping_falls_back_when_ai_unavailable(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("no LLM")
    monkeypatch.setattr("app.tagintel.ask._complete_json", boom)
    out = _run(importer.infer_mapping(["Name", "Email"], [{"Name": "Jane", "Email": "j@x.com"}]))
    assert out["ai"] is False
    assert out["mapping"]["display_name"] == "Name"
    assert set(out["mapping"].keys()) == set(importer.TARGET_FIELDS)


def test_infer_mapping_uses_ai_when_valid(monkeypatch):
    async def fake(system, user):
        return {"mapping": {"display_name": "Person", "email": "Contact"}, "confidence": 0.95, "explanation": "ok"}
    monkeypatch.setattr("app.tagintel.ask._complete_json", fake)
    out = _run(importer.infer_mapping(["Person", "Contact"], [{"Person": "Jane", "Contact": "j@x.com"}]))
    assert out["ai"] is True
    assert out["mapping"]["display_name"] == "Person"
    assert out["mapping"]["email"] == "Contact"


def test_infer_mapping_ignores_ai_columns_not_in_sheet(monkeypatch):
    async def fake(system, user):
        return {"mapping": {"display_name": "Ghost", "email": "Contact"}, "confidence": 0.9}
    monkeypatch.setattr("app.tagintel.ask._complete_json", fake)
    out = _run(importer.infer_mapping(["Person", "Contact"], []))
    # AI's display_name="Ghost" isn't a real column → dropped; the heuristic mapping wins.
    # "Contact" is a display_name synonym, so the heuristic resolves display_name to it.
    assert out["mapping"]["display_name"] == "Contact"
    # And the invalid AI column never leaks into the mapping.
    assert "Ghost" not in out["mapping"].values()


# ============================================================ import preview + materialize
def test_build_preview_marks_valid_and_subject():
    rows = [
        {"Name": "Jane", "Email": "j@x.com", "RID": "/subscriptions/s/x"},
        {"Name": "", "Email": "noname@x.com", "RID": ""},  # invalid (no name)
    ]
    mapping = {"display_name": "Name", "email": "Email", "resource_ids": "RID"}
    out = importer.build_preview(rows, mapping)
    assert out["valid"] == 1 and out["invalid"] == 1
    assert out["with_subject"] == 1
    assert out["rows"][0]["resource_ids"] == ["/subscriptions/s/x"]


def test_build_preview_kind_inference_team():
    rows = [{"Name": "Platform Team", "Email": ""}]
    out = importer.build_preview(rows, {"display_name": "Name"})
    assert out["rows"][0]["kind"] == "team"


def test_build_preview_splits_multiple_resource_ids():
    rows = [{"Name": "Jane", "RID": "/a/1; /a/2 , /a/3"}]
    out = importer.build_preview(rows, {"display_name": "Name", "resource_ids": "RID"})
    assert out["rows"][0]["resource_ids"] == ["/a/1", "/a/2", "/a/3"]


def test_materialize_creates_owners_and_dedupes_by_email():
    rows = [
        {"display_name": "Jane", "email": "j@x.com", "kind": "person", "role": "technical",
         "resource_ids": [], "workload": "", "subscription": "", "valid": True, "has_subject": False},
        {"display_name": "Jane D", "email": "j@x.com", "kind": "person", "role": "technical",
         "resource_ids": [], "workload": "", "subscription": "", "valid": True, "has_subject": False},
    ]
    out = importer.materialize_import("t1", rows, actor="tester")
    assert out["created"] == 1 and out["updated"] == 1  # second row dedupes onto first by email
    from app.ownership import registry
    assert len(registry.list_owners("t1")) == 1


def test_materialize_creates_resource_assignment():
    rows = [{"display_name": "Jane", "email": "j@x.com", "kind": "person", "role": "security",
             "resource_ids": ["/subscriptions/s/rg/r"], "workload": "", "subscription": "",
             "valid": True, "has_subject": True}]
    out = importer.materialize_import("t1", rows, actor="tester")
    assert out["assignments"] == 1
    from app.ownership import registry
    a = registry.list_assignments("t1")
    assert a[0]["subject_kind"] == "resource" and a[0]["role"] == "security"


def test_materialize_skips_invalid_rows():
    rows = [{"display_name": "", "valid": False, "has_subject": False, "resource_ids": []}]
    out = importer.materialize_import("t1", rows, actor="tester")
    assert out["created"] == 0 and out["skipped"] == 1


def test_materialize_unresolved_workload_recorded():
    rows = [{"display_name": "Jane", "email": "", "kind": "person", "role": "technical",
             "resource_ids": [], "workload": "Nonexistent WL", "subscription": "",
             "valid": True, "has_subject": True}]
    out = importer.materialize_import("t1", rows, actor="tester")
    assert any("Nonexistent WL" in s for s in out["unresolved_subjects"])


# ============================================================ owner → tag plan
def _res(rid, tags=None, **kw):
    return {"id": rid, "name": rid.rsplit("/", 1)[-1], "tags": tags or {},
            "resource_group": kw.get("rg", "rg1"), "subscription_id": kw.get("sub", "s1"),
            "resource_type": kw.get("type", "microsoft.compute/virtualmachines")}


def test_build_tag_plan_stages_owner_for_owned_resource(monkeypatch):
    from app.ownership import resolve as own_resolve
    monkeypatch.setattr(own_resolve, "build_context", lambda t: {})
    monkeypatch.setattr(own_resolve, "resolve_owner", lambda *a, **k: {
        "unowned": False, "owners": [{"display_name": "Jane", "email": "j@x.com", "primary": True}]})
    plan = tagging.build_tag_plan("t1", [_res("/r/1")], tag_key="owner", value_source="display_name")
    assert plan["applicable"] == 1
    assert plan["items"][0]["after"]["owner"] == "Jane"


def test_build_tag_plan_skips_unowned(monkeypatch):
    from app.ownership import resolve as own_resolve
    monkeypatch.setattr(own_resolve, "build_context", lambda t: {})
    monkeypatch.setattr(own_resolve, "resolve_owner", lambda *a, **k: {"unowned": True, "owners": []})
    plan = tagging.build_tag_plan("t1", [_res("/r/1")], tag_key="owner")
    assert plan["applicable"] == 0 and plan["no_owner"] == 1
    # The unowned resource is still shown (so the user sees the full picture), marked skipped.
    assert plan["items"][0]["status"] == "no_owner" and plan["items"][0]["skipped"] is True


def test_build_tag_plan_already_correct_is_noop(monkeypatch):
    from app.ownership import resolve as own_resolve
    monkeypatch.setattr(own_resolve, "build_context", lambda t: {})
    monkeypatch.setattr(own_resolve, "resolve_owner", lambda *a, **k: {
        "unowned": False, "owners": [{"display_name": "Jane", "primary": True}]})
    plan = tagging.build_tag_plan("t1", [_res("/r/1", {"owner": "Jane"})], tag_key="owner")
    # Value already matches → nothing to apply, but the resource is still shown as 'ok'.
    assert plan["applicable"] == 0 and plan["already_ok"] == 1
    assert plan["items"][0]["status"] == "ok" and plan["items"][0]["skipped"] is True


def test_build_tag_plan_conflict_not_overwritten(monkeypatch):
    from app.ownership import resolve as own_resolve
    monkeypatch.setattr(own_resolve, "build_context", lambda t: {})
    monkeypatch.setattr(own_resolve, "resolve_owner", lambda *a, **k: {
        "unowned": False, "owners": [{"display_name": "Jane", "primary": True}]})
    plan = tagging.build_tag_plan("t1", [_res("/r/1", {"owner": "Bob"})], tag_key="owner", overwrite=False)
    assert plan["conflicts"] == 1 and plan["applicable"] == 0
    assert plan["items"][0]["skipped"] is True


def test_build_tag_plan_conflict_overwritten_when_enabled(monkeypatch):
    from app.ownership import resolve as own_resolve
    monkeypatch.setattr(own_resolve, "build_context", lambda t: {})
    monkeypatch.setattr(own_resolve, "resolve_owner", lambda *a, **k: {
        "unowned": False, "owners": [{"display_name": "Jane", "primary": True}]})
    plan = tagging.build_tag_plan("t1", [_res("/r/1", {"owner": "Bob"})], tag_key="owner", overwrite=True)
    assert plan["applicable"] == 1
    assert plan["items"][0]["after"]["owner"] == "Jane"


def test_build_tag_plan_email_value_source(monkeypatch):
    from app.ownership import resolve as own_resolve
    monkeypatch.setattr(own_resolve, "build_context", lambda t: {})
    monkeypatch.setattr(own_resolve, "resolve_owner", lambda *a, **k: {
        "unowned": False, "owners": [{"display_name": "Jane", "email": "j@x.com", "primary": True}]})
    plan = tagging.build_tag_plan("t1", [_res("/r/1")], tag_key="owner", value_source="email")
    assert plan["items"][0]["after"]["owner"] == "j@x.com"


def test_build_tag_plan_preserves_existing_tags(monkeypatch):
    from app.ownership import resolve as own_resolve
    monkeypatch.setattr(own_resolve, "build_context", lambda t: {})
    monkeypatch.setattr(own_resolve, "resolve_owner", lambda *a, **k: {
        "unowned": False, "owners": [{"display_name": "Jane", "primary": True}]})
    plan = tagging.build_tag_plan("t1", [_res("/r/1", {"env": "prod"})], tag_key="owner")
    after = plan["items"][0]["after"]
    assert after["env"] == "prod" and after["owner"] == "Jane"


def test_build_tag_plan_custom_value_applies_to_all(monkeypatch):
    """A custom value is stamped on EVERY resource with no owner lookup (even unowned ones)."""
    from app.ownership import resolve as own_resolve
    # resolve_owner should never be called for custom; make it blow up if it is.
    monkeypatch.setattr(own_resolve, "resolve_owner",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("owner lookup ran")))
    plan = tagging.build_tag_plan(
        "t1", [_res("/r/1"), _res("/r/2", {"team": "x"})],
        tag_key="team", value_source="custom", custom_value="platform", overwrite=True,
    )
    assert plan["applicable"] == 2
    assert all(it["after"]["team"] == "platform" for it in plan["items"])
    assert plan["no_owner"] == 0


def test_build_tag_plan_custom_blank_value_is_noop(monkeypatch):
    plan = tagging.build_tag_plan("t1", [_res("/r/1")], tag_key="team",
                                  value_source="custom", custom_value="   ")
    assert plan["applicable"] == 0


# ============================================================ revisions: save / diff / revert
def test_revision_save_and_list():
    revisions.save_revision("t1", "c1", source="tagintel", description="x",
                            before={"/r/1": {}}, after={"/r/1": {"owner": "Jane"}}, names={"/r/1": "r1"})
    lst = revisions.list_revisions("t1")
    assert len(lst) == 1 and lst[0]["resource_count"] == 1


def test_revision_diff_rows_added_changed_removed():
    rev = {"before": {"/r/1": {"a": "1", "b": "old"}}, "after": {"/r/1": {"b": "new", "c": "2"}},
           "names": {"/r/1": "r1"}}
    rows = revisions.diff_rows(rev)
    assert rows[0]["added"] == {"c": "2"}
    assert rows[0]["removed"] == {"a": "1"}
    assert rows[0]["changed"]["b"] == {"from": "old", "to": "new"}


def test_revision_revert_restores_before_via_replace(monkeypatch):
    # Capture: before had owner=Bob, after set owner=Jane. Revert must Replace back to {owner:Bob}.
    rev = revisions.save_revision("t1", "c1", source="ownership", description="apply",
                                  before={"/r/1": {"owner": "Bob"}}, after={"/r/1": {"owner": "Jane"}},
                                  names={"/r/1": "r1"}, applied=1)
    calls = []

    async def fake_read(conn, rids, **k):
        return ({rid.lower(): {"owner": "Jane"} for rid in rids}, {r.lower(): "r1" for r in rids}, "")

    async def fake_set(conn, rid, tags, *, operation="Merge"):
        calls.append((rid, dict(tags), operation))
        return True, ""

    monkeypatch.setattr("app.azure.tag_ops.read_current_tags", fake_read)
    monkeypatch.setattr("app.azure.tag_ops.set_resource_tags", fake_set)
    out = _run(revisions.revert_revision("t1", rev["id"], {"id": "c1"}, actor="tester"))
    assert out["ok"] is True and out["reverted"] == 1
    # Replace was called with the prior tag set.
    assert calls[0][2] == "Replace" and calls[0][1] == {"owner": "Bob"}
    # Original marked reverted; a new inverse revision was recorded.
    assert revisions.get_revision("t1", rev["id"])["status"] == "reverted"
    assert out["new_revision"]["reverts_id"] == rev["id"]


def test_revision_revert_already_reverted_blocked(monkeypatch):
    rev = revisions.save_revision("t1", "c1", source="ownership", description="x",
                                  before={"/r/1": {}}, after={"/r/1": {"owner": "Jane"}})

    async def fake_read(conn, rids, **k):
        return ({}, {}, "")

    async def fake_set(conn, rid, tags, *, operation="Merge"):
        return True, ""
    monkeypatch.setattr("app.azure.tag_ops.read_current_tags", fake_read)
    monkeypatch.setattr("app.azure.tag_ops.set_resource_tags", fake_set)
    _run(revisions.revert_revision("t1", rev["id"], {"id": "c1"}, actor="t"))
    again = _run(revisions.revert_revision("t1", rev["id"], {"id": "c1"}, actor="t"))
    assert again["ok"] is False and "already" in again["error"].lower()


def test_revision_revert_missing_returns_error():
    out = _run(revisions.revert_revision("t1", "nope", {"id": "c1"}, actor="t"))
    assert out["ok"] is False


def test_revision_revert_partial_failure_counts(monkeypatch):
    rev = revisions.save_revision("t1", "c1", source="ownership", description="x",
                                  before={"/r/1": {}, "/r/2": {}},
                                  after={"/r/1": {"o": "a"}, "/r/2": {"o": "b"}})

    async def fake_read(conn, rids, **k):
        return ({}, {}, "")

    async def fake_set(conn, rid, tags, *, operation="Merge"):
        return (rid != "/r/2"), ("" if rid != "/r/2" else "boom")
    monkeypatch.setattr("app.azure.tag_ops.read_current_tags", fake_read)
    monkeypatch.setattr("app.azure.tag_ops.set_resource_tags", fake_set)
    out = _run(revisions.revert_revision("t1", rev["id"], {"id": "c1"}, actor="t"))
    assert out["reverted"] == 1 and out["failed"] == 1 and out["ok"] is False


# ---------------------------------------------------------- tag_ops real write path (import guard)
def test_set_resource_tags_executes_real_imports(monkeypatch):
    """Exercise the REAL ``set_resource_tags`` body (not a mock) so its module imports actually run.

    Regression guard: ``set_resource_tags`` imports ``get_arm_token`` from ``app.azure.credentials``
    and ``arm_rest`` from ``app.azure.arm``. A wrong import module/name (the live bug: it imported
    ``get_arm_token`` from ``app.azure.arm``) raises ImportError ONLY at call time — which broke
    every tag revert and the ownership owner-tag apply, yet passed all mocked tests. We mock the two
    underlying functions at their SOURCE modules so the import lines execute against the real names."""
    from app.azure import tag_ops

    calls = {}

    async def _fake_token(conn):
        return "tok-123", ""

    async def _fake_rest(token, method, url, body=None):
        calls["token"], calls["method"], calls["url"], calls["body"] = token, method, url, body
        return "{}", None

    monkeypatch.setattr("app.azure.credentials.get_arm_token", _fake_token)
    monkeypatch.setattr("app.azure.arm.arm_rest", _fake_rest)

    ok, err = _run(tag_ops.set_resource_tags({"id": "c1"}, "/subscriptions/s/x", {"CostCenter": "A1"}, operation="Replace"))
    assert ok is True and err == ""
    # The real body was built with the Replace operation and our tag.
    assert calls["method"] == "PATCH" and calls["body"]["operation"] == "Replace"
    assert calls["body"]["properties"]["tags"] == {"CostCenter": "A1"}
    assert "/providers/Microsoft.Resources/tags/default" in calls["url"]


def test_set_resource_tags_merge_default(monkeypatch):
    """Default operation is Merge (preserve other tags)."""
    from app.azure import tag_ops
    seen = {}

    async def _fake_token(conn):
        return "tok", ""

    async def _fake_rest(token, method, url, body=None):
        seen["op"] = body["operation"]
        return "{}", None

    monkeypatch.setattr("app.azure.credentials.get_arm_token", _fake_token)
    monkeypatch.setattr("app.azure.arm.arm_rest", _fake_rest)
    ok, _err = _run(tag_ops.set_resource_tags({"id": "c1"}, "/x", {"k": "v"}))
    assert ok is True and seen["op"] == "Merge"


def test_read_current_tags_snapshots_resource_group(monkeypatch):
    """The snapshot must include RESOURCE GROUPS (they live in `resourcecontainers`, not `resources`).

    Regression guard for: removing tags from a resource group rebased to a no-op (0 applied) because
    the pre-apply snapshot queried only the `resources` table, so the RG came back with {} tags."""
    from app.azure import tag_ops

    rg_id = "/subscriptions/s/resourceGroups/rg-azsupagent"
    seen = {}

    async def _fake_token(conn):
        return "tok", ""

    async def _fake_query(token, kql, top=None):
        seen["kql"] = kql
        # Emulate ARG: the RG row only comes from the `resourcecontainers` union branch.
        return [{"id": rg_id, "name": "rg-azsupagent", "tags": {"FF": "34", "x": "12"}}], ""

    monkeypatch.setattr("app.azure.credentials.get_arm_token", _fake_token)
    monkeypatch.setattr("app.azure.arm.query_resource_graph", _fake_query)

    tags, names, err = _run(tag_ops.read_current_tags({"id": "c1"}, [rg_id]))
    assert err == ""
    assert "resourcecontainers" in seen["kql"]
    assert tags[rg_id.lower()] == {"FF": "34", "x": "12"}
    assert names[rg_id.lower()] == "rg-azsupagent"

