"""Tests for the Entra ID App Registrations collector + facet aggregation."""
import asyncio

from app.identity import appregs


def _snap():
    return asyncio.run(appregs.collect_app_registrations(None, tenant_id="default"))


def test_demo_snapshot_shape():
    snap = _snap()
    assert snap["source"] == "demo_dummy_data"
    assert snap["connection_configured"] is False
    assert snap["summary"]["total"] == len(snap["apps"]) > 0
    # Sorted by display name (case-insensitive).
    names = [a["displayName"].lower() for a in snap["apps"]]
    assert names == sorted(names)


def test_permission_risk_tiers():
    assert appregs.permission_risk("Directory.ReadWrite.All") == "high"
    assert appregs.permission_risk("Directory.Read.All") == "medium"
    assert appregs.permission_risk("User.Read") == "low"


def test_app_row_counts_and_flags():
    apps = appregs.build_demo_app_registrations()
    by_name = {a["displayName"]: a for a in apps}

    # Ownerless + high-risk + expired credentials case.
    legacy = by_name["Legacy Migration Tool"]
    assert legacy["ownerless"] is True
    assert legacy["highRisk"] is True
    assert legacy["expiredCredentials"] == 2
    assert legacy["applicationPermissionsCount"] == 3
    assert legacy["delegatedPermissionsCount"] == 0

    # Mixed Application + Delegated permissions, secrets + cert.
    payments = by_name["Contoso Payments API"]
    assert payments["secretsCount"] == 1
    assert payments["certsCount"] == 1
    assert payments["applicationPermissionsCount"] == 3
    assert payments["delegatedPermissionsCount"] == 2
    assert payments["highRisk"] is True  # Directory.ReadWrite.All

    # Public client — no credentials.
    mobile = by_name["Field Service Mobile"]
    assert mobile["secretsCount"] == 0 and mobile["certsCount"] == 0
    assert mobile["nextExpiryDays"] is None
    assert mobile["highRisk"] is False


def test_aggregate_facets_and_summary():
    apps = appregs.build_demo_app_registrations()
    agg = appregs.aggregate(apps)

    # Audience facet totals reconcile with the app count.
    assert sum(f["count"] for f in agg["audiences"]) == len(apps)

    # Ownerless is represented in the owners facet.
    owners = {f["value"]: f["count"] for f in agg["owners"]}
    assert owners.get("(ownerless)") == agg["summary"]["ownerless"]

    # High-risk permissions surface in the permissions facet.
    perm_values = {f["value"] for f in agg["permissions"]}
    assert "Directory.ReadWrite.All" in perm_values

    s = agg["summary"]
    assert s["total"] == len(apps)
    assert s["withSecrets"] >= 1 and s["withCerts"] >= 1
    assert s["highRisk"] >= 1 and s["ownerless"] >= 1
    assert s["expired"] >= 1 and s["expiringSoon"] >= 1
    # Perm totals equal the sum of per-app counts.
    assert s["applicationPerms"] == sum(a["applicationPermissionsCount"] for a in apps)
    assert s["delegatedPerms"] == sum(a["delegatedPermissionsCount"] for a in apps)


def test_cache_roundtrip(tmp_path, monkeypatch):
    from app.identity import appregs_cache

    monkeypatch.setattr(appregs_cache, "_CACHE_PATH", tmp_path / "appregs_cache.json")
    monkeypatch.setattr(appregs_cache, "_mem_cache", None)

    assert appregs_cache.get("t1", "c1") is None
    payload = {"apps": [], "summary": {"total": 0}}
    fetched_at = appregs_cache.set_("t1", "c1", payload)
    assert fetched_at
    hit = appregs_cache.get("t1", "c1")
    assert hit is not None
    assert hit["payload"] == payload
    assert hit["age_seconds"] >= 0


def test_workbook_multi_sheet():
    from io import BytesIO

    from openpyxl import load_workbook

    from app.identity import appregs_export

    snap = _snap()
    content = appregs_export.to_workbook(snap)
    assert isinstance(content, bytes) and content[:2] == b"PK"  # xlsx is a zip

    wb = load_workbook(BytesIO(content), read_only=True)
    names = set(wb.sheetnames)
    assert {
        "Summary", "Applications", "Credentials", "API Permissions",
        "Owners", "High Risk", "Permission Pivot",
    } <= names

    # Applications sheet: header + one row per app.
    ws = wb["Applications"]
    assert ws.max_row == len(snap["apps"]) + 1

    # Credentials sheet: header + one row per credential across all apps.
    cred_total = sum(len(a.get("credentials") or []) for a in snap["apps"])
    assert wb["Credentials"].max_row == cred_total + 1

    # API Permissions sheet: header + one row per granted permission.
    perm_total = sum(len(a.get("permissions") or []) for a in snap["apps"])
    assert wb["API Permissions"].max_row == perm_total + 1

    # High Risk sheet: header + only the flagged apps.
    hr = sum(1 for a in snap["apps"] if a.get("highRisk"))
    assert wb["High Risk"].max_row == hr + 1
    wb.close()


def test_workbook_neutralizes_formula_injection():
    from io import BytesIO

    from openpyxl import load_workbook

    from app.identity import appregs_export

    snap = _snap()
    # Inject a formula-style display name into one app.
    snap["apps"][0]["displayName"] = "=cmd|'/c calc'!A1"
    content = appregs_export.to_workbook(snap)
    wb = load_workbook(BytesIO(content), read_only=True)
    ws = wb["Applications"]
    # The cell must be neutralized to literal text (leading apostrophe / not a live formula).
    val = ws.cell(row=2, column=1).value
    assert str(val).startswith("'=") or not str(val).startswith("=")
    wb.close()

