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
    assert s["active"] + s["deactivated"] + s["notInstantiated"] + s["stateUnknown"] == len(apps)
    state_counts = {f["value"]: f["count"] for f in agg["enterpriseAppStates"]}
    assert set(state_counts) == set(appregs.ENTERPRISE_APP_STATES)
    assert sum(state_counts.values()) == len(apps)
    assert state_counts["deactivated"] == s["deactivated"] >= 1


def test_demo_signin_activity_covers_recent_dormant_and_stale():
    """The demo set has to exercise every rendered state, or the UI is untested by it."""
    snap = _snap()
    meta = snap["signin_activity"]
    assert meta["measured"] is True and meta["source"] == "demo"
    assert meta["credentials"]["measured"] is True

    by_name = {a["displayName"]: a for a in snap["apps"]}
    recent = by_name["Contoso Payments API"]
    assert recent["lastSignInKnown"] is True
    assert recent["lastSignIn"] and recent["lastSignInDays"] == 0

    # Dormant: measured, but nothing signed in. This must NOT look like "never".
    dormant = by_name["Legacy Migration Tool"]
    assert dormant["lastSignInKnown"] is True
    assert dormant["lastSignIn"] is None and dormant["lastSignInDays"] is None
    assert appregs.signin_bucket(dormant) == appregs.SIGNIN_BUCKET_NONE

    # Older than the report window is its own state, not "no sign-in".
    stale = by_name["Internal Wiki SSO"]
    assert appregs.signin_bucket(stale) == appregs.SIGNIN_BUCKET_OLD

    # Per-credential usage: the first credential is used, later ones are retirement candidates.
    gateway = by_name["Partner B2B Gateway"]
    assert [c["lastUsedKnown"] for c in gateway["credentials"]] == [True, True]
    assert all(c["lastUsed"] is None for c in gateway["credentials"])


def test_signin_buckets_partition_the_app_set():
    apps = appregs.build_demo_app_registrations()
    agg = appregs.aggregate(apps)
    buckets = {f["value"]: f["count"] for f in agg["signInActivity"]}

    assert list(buckets) == list(appregs.SIGNIN_BUCKETS)  # timeline order, not count order
    assert sum(buckets.values()) == len(apps)
    s = agg["summary"]
    assert s["signedIn30d"] + s["noRecentSignIn"] + s["signInNotMeasured"] == len(apps)
    assert s["signedIn7d"] <= s["signedIn30d"]
    # Non-vacuous: the dormant bucket is genuinely populated.
    assert s["noRecentSignIn"] >= 2


def test_unmeasured_activity_is_never_counted_as_dormant():
    apps = appregs.build_demo_app_registrations()
    for a in apps:
        a["lastSignInKnown"] = False
        a["lastSignIn"] = None
        a["lastSignInDays"] = None
    s = appregs.aggregate(apps)["summary"]
    assert s["signInNotMeasured"] == len(apps)
    assert s["noRecentSignIn"] == 0 and s["signedIn30d"] == 0


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
        "Owners", "High Risk", "Deactivated", "Permission Pivot",
    } <= names

    # Applications sheet: header + one row per app.
    ws = wb["Applications"]
    assert ws.max_row == len(snap["apps"]) + 1
    headers = [cell.value for cell in next(ws.iter_rows(min_row=1, max_row=1))]
    assert "Enterprise app state" in headers
    assert "Service principal ID" in headers
    assert "Last sign-in" in headers and "Sign-in status" in headers

    # Credentials sheet: header + one row per credential across all apps.
    cred_total = sum(len(a.get("credentials") or []) for a in snap["apps"])
    assert wb["Credentials"].max_row == cred_total + 1
    cred_headers = [cell.value for cell in next(wb["Credentials"].iter_rows(min_row=1, max_row=1))]
    assert "Last used" in cred_headers and "Usage status" in cred_headers

    # API Permissions sheet: header + one row per granted permission.
    perm_total = sum(len(a.get("permissions") or []) for a in snap["apps"])
    assert wb["API Permissions"].max_row == perm_total + 1

    # High Risk sheet: header + only the flagged apps.
    hr = sum(1 for a in snap["apps"] if a.get("highRisk"))
    assert wb["High Risk"].max_row == hr + 1
    deactivated = sum(1 for a in snap["apps"] if a.get("enterpriseAppState") == "deactivated")
    assert wb["Deactivated"].max_row == deactivated + 1
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

