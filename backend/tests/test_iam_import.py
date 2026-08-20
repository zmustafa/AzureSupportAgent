"""Import of a standalone all-azure-access scanner run.

The value of this path is that it lets a human with real permissions produce the data and this
product analyze it, without widening the app's own Azure/Graph access. That makes correctness of
the *parsing* important and correctness of the *provenance* critical — imported data must never
be presentable as a live scan.

The upload is untrusted input, so the hostile cases are tested alongside the happy ones.
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest

from app.iam import cache, compose, export, importer, schema


@pytest.fixture()
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_DATA", tmp_path)
    monkeypatch.setattr(cache, "_INDEX", tmp_path / "iam_cache.json")
    monkeypatch.setattr(cache, "_BLOBS", tmp_path / "iam")
    monkeypatch.setattr(cache, "_migrated", True)
    return tmp_path


def _scanner_rows(n: int = 2) -> list[dict]:
    """Rows shaped exactly like the scanner's allAzureAccess output: the frozen 46 keys only."""
    out = []
    for i in range(n):
        row = {c: "" for c in schema.SCANNER_COLUMNS}
        row.update({
            "surface": "Azure RBAC",
            "accessModel": "AzureRBAC",
            "collector": "AzureSubscriptionRbac",
            "assignmentState": "Active",
            "principalId": f"u-{i}",
            "principalType": "User",
            "principalDisplayName": f"User {i}",
            "effectivePrincipalId": f"u-{i}",
            "accessPath": "Direct",
            "roleName": "Owner",
            "roleIsPrivileged": "True",
            "roleHasDataActions": "False",
            "scope": "/subscriptions/sub-1",
            "scopeType": "subscription",
            "subscriptionId": "sub-1",
            "assignmentId": f"ra-{i}",
        })
        out.append(row)
    return out


def _json_bytes(rows) -> bytes:
    return json.dumps(rows).encode("utf-8")


def _csv_bytes(rows) -> bytes:
    import csv

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(schema.SCANNER_COLUMNS), extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


# --------------------------------------------------------------------------- happy paths
def test_import_json_populates_every_tab(isolated_cache):
    summary = importer.import_rows("t1", _json_bytes(_scanner_rows(3)), "allAzureAccess.json")
    assert summary["rows_imported"] == 3
    master = compose.build_master_rows("t1")
    assert len(master) == 3
    # Rows are full schema rows, so the grid/pivots/exports work over them unchanged.
    assert all(set(r.keys()) == set(schema.COLUMNS) for r in master)
    assert compose.compute_overview("t1")["kpis"]["total_assignments"] == 3


def test_import_csv_coerces_booleans(isolated_cache):
    """CSV gives everything as strings, so `roleIsPrivileged` arrives as "True"/"False" — taking
    them verbatim makes every row truthy and reports the whole estate as privileged."""
    importer.import_rows("t1", _csv_bytes(_scanner_rows(2)), "allAzureAccess.csv")
    master = compose.build_master_rows("t1")
    assert all(r["roleIsPrivileged"] is True for r in master)
    assert all(r["roleHasDataActions"] is False for r in master)


def test_import_reads_the_scanner_results_zip(isolated_cache):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("output/allAzureAccess.json", json.dumps(_scanner_rows(2)))
        zf.writestr("logs/all-azure-access.log", "noise")
    summary = importer.import_rows("t1", buf.getvalue(), "results.zip")
    assert summary["rows_imported"] == 2
    assert "allAzureAccess.json" in summary["source"]


@pytest.mark.parametrize("member", [
    "output/allAzureAccess.json",
    "output/allazureaccess.json",
    "all-azure-access-20260801/output/allAzureAccess.json",  # zip rooted at the run folder
])
def test_zip_member_lookup_is_case_and_prefix_tolerant(isolated_cache, member):
    """Zip member names are case-sensitive and the run folder may or may not be the archive root;
    a strict match silently reports "archive contains no access file" for a valid upload."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(member, json.dumps(_scanner_rows(1)))
    assert importer.import_rows("t1", buf.getvalue(), "results.zip")["rows_imported"] == 1
    importer.purge_imported("t1")


def test_import_sniffs_content_when_the_extension_is_missing(isolated_cache):
    summary = importer.import_rows("t1", _json_bytes(_scanner_rows(1)), "download")
    assert summary["rows_imported"] == 1


def test_round_trip_through_scanner_export_is_lossless(isolated_cache):
    """Import → export(scanner) → import must be stable. This is the contract that makes the
    46-column schema worth freezing."""
    importer.import_rows("t1", _json_bytes(_scanner_rows(3)), "allAzureAccess.json")
    exported = export.to_json(compose.build_master_rows("t1"), columns=schema.SCANNER_COLUMNS)
    reparsed = json.loads(exported)
    assert set(reparsed[0].keys()) == set(schema.SCANNER_COLUMNS)

    importer.purge_imported("t1")
    importer.import_rows("t1", exported.encode("utf-8"), "allAzureAccess.json")
    again = compose.build_master_rows("t1")
    assert len(again) == 3
    assert {r["assignmentId"] for r in again} == {"ra-0", "ra-1", "ra-2"}


# --------------------------------------------------------------------------- provenance
def test_imported_rows_and_slice_are_flagged_as_imported(isolated_cache):
    """Imported data must never be presentable as a live scan — the freshness column and the
    Diagnostics tab both key off this."""
    importer.import_rows("t1", _json_bytes(_scanner_rows(1)), "allAzureAccess.json", label="Q3 audit")
    assert all(r["imported"] is True for r in compose.build_master_rows("t1"))

    meta = next(m for m in cache.list_scope_meta("t1"))
    assert meta["imported"] is True
    assert meta["importSource"].endswith("allAzureAccess.json")
    assert meta["importedAt"]
    assert meta["displayName"] == "Imported: Q3 audit"
    # It is not demo data, and must not be swept up by the demo purge.
    assert meta["demo"] is False


def test_purge_imported_leaves_live_and_demo_scopes_alone(isolated_cache):
    cache.write_scope("t1", "/subscriptions/real", meta={"demo": False, "displayName": "Real"}, rows=[schema.make_row(principalId="r1")])
    cache.write_scope("t1", "/subscriptions/demo", meta={"demo": True, "displayName": "Demo"}, rows=[schema.make_row(principalId="d1")])
    importer.import_rows("t1", _json_bytes(_scanner_rows(2)), "allAzureAccess.json")

    assert importer.purge_imported("t1") == 1
    left = {m["scope"] for m in cache.list_scope_meta("t1")}
    assert left == {"/subscriptions/real", "/subscriptions/demo"}
    # Idempotent.
    assert importer.purge_imported("t1") == 0


def test_demo_purge_does_not_remove_imported_data(isolated_cache):
    importer.import_rows("t1", _json_bytes(_scanner_rows(1)), "allAzureAccess.json")
    cache.purge_demo("t1")
    assert len(compose.build_master_rows("t1")) == 1


# --------------------------------------------------------------------------- honesty
def test_unknown_columns_are_reported_never_silently_dropped(isolated_cache):
    """A newer scanner version adding a column must not lose data with no signal."""
    rows = _scanner_rows(1)
    rows[0]["brandNewColumn"] = "something"
    summary = importer.import_rows("t1", _json_bytes(rows), "allAzureAccess.json")
    assert summary["unknown_columns"] == ["brandNewColumn"]


def test_missing_columns_are_reported_and_defaulted(isolated_cache):
    rows = [{"surface": "Azure RBAC", "principalId": "u-1", "roleName": "Reader", "scope": "/subscriptions/s"}]
    summary = importer.import_rows("t1", _json_bytes(rows), "allAzureAccess.json")
    assert "assignmentId" in summary["missing_columns"]
    master = compose.build_master_rows("t1")
    assert set(master[0].keys()) == set(schema.COLUMNS)
    assert master[0]["assignmentId"] == ""


# --------------------------------------------------------------------------- hostile input
def test_empty_upload_is_rejected(isolated_cache):
    with pytest.raises(importer.ImportError_, match="empty"):
        importer.import_rows("t1", b"", "allAzureAccess.json")


def test_oversized_upload_is_rejected(isolated_cache, monkeypatch):
    monkeypatch.setattr(importer, "MAX_BYTES", 10)
    with pytest.raises(importer.ImportError_, match="over the"):
        importer.import_rows("t1", b"[" + b"x" * 50 + b"]", "allAzureAccess.json")


def test_too_many_rows_is_rejected(isolated_cache, monkeypatch):
    monkeypatch.setattr(importer, "MAX_ROWS", 1)
    with pytest.raises(importer.ImportError_, match="row limit"):
        importer.import_rows("t1", _json_bytes(_scanner_rows(3)), "allAzureAccess.json")


def test_malformed_json_is_rejected_with_a_useful_message(isolated_cache):
    with pytest.raises(importer.ImportError_, match="Not valid JSON"):
        importer.import_rows("t1", b"[{oops", "allAzureAccess.json")


def test_the_wrong_scanner_artifact_fails_loudly(isolated_cache):
    """collectorStatus.csv parses fine as CSV *and shares the `collector` column with an access
    export*, so a "any known column" check waves it through and silently blanks the grid."""
    wrong = b"collector,status,rowsAdded\nAzureSubscriptionRbac,Succeeded,12\n"
    with pytest.raises(importer.ImportError_, match="does not look like an access export"):
        importer.import_rows("t1", wrong, "collectorStatus.csv")


def test_other_scanner_artifacts_are_also_rejected(isolated_cache):
    for name, body in [
        ("errorsWarnings.csv", b"collector,status,errorMessage\nX,Failed,boom\n"),
        ("principalResolution.csv", b"source,generatedAt\nGraph,2026-01-01\n"),
    ]:
        with pytest.raises(importer.ImportError_):
            importer.import_rows("t1", body, name)


def test_a_json_object_that_is_not_an_array_is_rejected(isolated_cache):
    with pytest.raises(importer.ImportError_, match="array"):
        importer.import_rows("t1", b'{"summary": 1}', "summary.json")


def test_a_wrapped_rows_array_is_accepted(isolated_cache):
    payload = json.dumps({"rows": _scanner_rows(2)}).encode("utf-8")
    assert importer.import_rows("t1", payload, "wrapped.json")["rows_imported"] == 2


def test_a_bad_zip_is_rejected(isolated_cache):
    with pytest.raises(importer.ImportError_):
        importer.import_rows("t1", b"PK\x03\x04garbage", "results.zip")


def test_a_zip_without_the_access_file_is_rejected(isolated_cache):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("logs/all-azure-access.log", "noise")
    with pytest.raises(importer.ImportError_, match="no output/allAzureAccess"):
        importer.import_rows("t1", buf.getvalue(), "results.zip")


def test_import_never_evaluates_uploaded_content(isolated_cache):
    """Values are coerced to str by make_row; nothing in the payload can become a callable or an
    attribute assignment."""
    rows = _scanner_rows(1)
    rows[0]["roleName"] = "__import__('os').system('echo pwned')"
    rows[0]["principalDisplayName"] = {"nested": "object"}
    importer.import_rows("t1", _json_bytes(rows), "allAzureAccess.json")
    master = compose.build_master_rows("t1")
    assert master[0]["roleName"] == "__import__('os').system('echo pwned')"
    assert isinstance(master[0]["principalDisplayName"], str)


def test_formula_injection_survives_the_round_trip_neutralised(isolated_cache):
    """An imported cell starting with `=` is a CSV-injection vector aimed at whoever opens the
    re-export in Excel."""
    rows = _scanner_rows(1)
    rows[0]["principalDisplayName"] = '=cmd|\'/c calc\'!A1'
    importer.import_rows("t1", _json_bytes(rows), "allAzureAccess.json")
    csv_text = export.to_csv(compose.build_master_rows("t1"))
    assert "'=cmd" in csv_text
