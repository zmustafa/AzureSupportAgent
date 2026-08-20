"""Risk and sign-in intelligence.

The load-bearing test in this file is the first one: if a raw sign-in row ever reaches the
persisted payload, a busy tenant's snapshot becomes gigabytes and the feature is unusable.
The aggregator is written to make that impossible; this asserts it stays impossible.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.entra.collectors import risk as risk_collector
from app.entra.signal_defs import risk as risk_signals
from app.entra.signals import SignalContext, SignalUnavailable

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


def _ctx(**kw):
    return SignalContext(now=NOW, tenant_id="t1", **kw)


def _signin(**kw):
    base = {
        "id": "s1", "createdDateTime": "2026-07-30T09:00:00Z", "userId": "u1",
        "userPrincipalName": "u1@x", "appId": "app1", "appDisplayName": "App One",
        "clientAppUsed": "Browser", "ipAddress": "203.0.113.1",
        "status": {"errorCode": 0}, "location": {"countryOrRegion": "GB"},
        "deviceDetail": {"isCompliant": True, "displayName": "PC"},
        "authenticationRequirement": "singleFactorAuthentication",
        "appliedConditionalAccessPolicies": [],
    }
    base.update(kw)
    return base


def _aggregate(rows):
    agg = risk_collector._Aggregator()  # noqa: SLF001 - the unit under test
    for row in rows:
        agg.add(row)
    return agg


# ================================================================ no raw rows survive
def test_aggregates_never_retain_a_raw_signin_row():
    """A tenant with 40 million sign-ins must produce a payload of a few kilobytes."""
    agg = _aggregate([_signin(id=f"s{i}", userId=f"u{i % 7}") for i in range(500)])
    payload = agg.payload(sampled=False, lookback_days=30)

    blob = repr(payload)
    assert "appliedConditionalAccessPolicies" not in blob
    assert "createdDateTime" not in blob, "a raw Graph field name means a raw row leaked"
    assert "deviceDetail" not in blob
    assert payload["total"] == 500
    # Every unbounded dimension is capped.
    assert len(payload["by_user_top"]) <= 50
    assert len(payload["by_app"]) <= 100


def test_sampling_is_recorded_and_propagated():
    """A silently sampled chart is a lie. The flag must survive into the payload."""
    payload = _aggregate([_signin()]).payload(sampled=True, lookback_days=7)
    assert payload["sampled"] is True
    assert payload["lookback_days"] == 7


def test_success_and_failure_are_counted_separately():
    agg = _aggregate([
        _signin(status={"errorCode": 0}),
        _signin(status={"errorCode": 50126, "failureReason": "Invalid password"}),
        _signin(status={"errorCode": 50126, "failureReason": "Invalid password"}),
    ])
    payload = agg.payload(sampled=False, lookback_days=30)
    assert payload["success"] == 1
    assert payload["failure"] == 2
    assert payload["failure_rate"] == pytest.approx(2 / 3, abs=0.001)
    codes = {c["code"]: c for c in payload["by_failure_code"]}
    assert codes["50126"]["count"] == 2
    assert codes["50126"]["meaning"] == "Invalid username or password"


# ==================================================================== legacy auth
def test_legacy_success_is_separated_from_legacy_failure():
    """A *successful* legacy sign-in means MFA was bypassed; a failed one means nothing."""
    agg = _aggregate([
        _signin(clientAppUsed="Exchange ActiveSync", status={"errorCode": 0}, userId="u1"),
        _signin(clientAppUsed="Exchange ActiveSync", status={"errorCode": 50126}, userId="u2"),
        _signin(clientAppUsed="IMAP4", status={"errorCode": 50126}, userId="u3"),
    ])
    payload = agg.payload(sampled=False, lookback_days=30)
    legacy = {row["protocol"]: row for row in payload["legacy"]}
    assert legacy["Exchange ActiveSync"]["total"] == 2
    assert legacy["Exchange ActiveSync"]["success"] == 1
    assert legacy["IMAP4"]["success"] == 0
    assert payload["legacy_success_users"] == 1


def test_browser_signins_are_not_counted_as_legacy():
    payload = _aggregate([_signin(clientAppUsed="Browser")]).payload(sampled=False, lookback_days=30)
    assert payload["legacy"] == []


# ======================================================== deterministic patterns
def test_password_spray_needs_enough_distinct_users():
    below = _aggregate([
        _signin(userId=f"u{i}", ipAddress="198.51.100.5", status={"errorCode": 50126})
        for i in range(risk_collector.SPRAY_MIN_USERS - 1)
    ])
    assert [p for p in below.patterns() if p["kind"] == "password_spray"] == []

    at = _aggregate([
        _signin(userId=f"u{i}", ipAddress="198.51.100.5", status={"errorCode": 50126})
        for i in range(risk_collector.SPRAY_MIN_USERS)
    ])
    hits = [p for p in at.patterns() if p["kind"] == "password_spray"]
    assert len(hits) == 1
    assert hits[0]["evidence"]["distinct_users"] == risk_collector.SPRAY_MIN_USERS
    assert hits[0]["rule"], "every pattern must state the rule that produced it"


def test_spray_from_many_ips_is_not_a_spray():
    """One user per IP is normal failure noise, not a spray."""
    agg = _aggregate([
        _signin(userId=f"u{i}", ipAddress=f"198.51.100.{i}", status={"errorCode": 50126})
        for i in range(30)
    ])
    assert [p for p in agg.patterns() if p["kind"] == "password_spray"] == []


def test_mfa_fatigue_counts_denials_per_user():
    agg = _aggregate([
        _signin(userId="u1", status={"errorCode": 500121})
        for _ in range(risk_collector.FATIGUE_MIN_DENIALS)
    ])
    hits = [p for p in agg.patterns() if p["kind"] == "mfa_fatigue"]
    assert len(hits) == 1
    assert hits[0]["evidence"]["denials"] == risk_collector.FATIGUE_MIN_DENIALS


def test_mfa_fatigue_below_threshold_is_silent():
    agg = _aggregate([
        _signin(userId="u1", status={"errorCode": 500121})
        for _ in range(risk_collector.FATIGUE_MIN_DENIALS - 1)
    ])
    assert [p for p in agg.patterns() if p["kind"] == "mfa_fatigue"] == []


def test_failure_spike_needs_a_baseline_and_a_real_jump():
    rows = []
    for day in range(1, 8):                    # a week of quiet days
        for _ in range(5):
            rows.append(_signin(createdDateTime=f"2026-07-0{day}T09:00:00Z",
                                status={"errorCode": 50126}))
    quiet = _aggregate(rows)
    assert [p for p in quiet.patterns() if p["kind"] == "failure_spike"] == []

    rows += [_signin(createdDateTime="2026-07-09T09:00:00Z", status={"errorCode": 50126})
             for _ in range(400)]
    spiky = _aggregate(rows)
    hits = [p for p in spiky.patterns() if p["kind"] == "failure_spike"]
    assert len(hits) == 1
    assert hits[0]["evidence"]["day"] == "2026-07-09"


def test_unmanaged_device_signins_are_recorded_without_the_privileged_join():
    """The collector must not read the roles domain \u2014 that join belongs to the signal."""
    agg = _aggregate([
        _signin(userId="u1", deviceDetail={"isCompliant": False, "displayName": "BYOD"}),
        _signin(userId="u1", deviceDetail={"isCompliant": False, "displayName": "BYOD"}),
        _signin(userId="u2", deviceDetail={"isCompliant": True, "displayName": "PC"}),
    ])
    payload = agg.payload(sampled=False, lookback_days=30)
    rows = {r["user_id"]: r for r in payload["unmanaged_signin_users"]}
    assert rows["u1"]["count"] == 2
    assert "u2" not in rows


def test_unmanaged_device_signins_are_one_pattern_not_one_per_account():
    """A real tenant produced 1,261 rows here, burying the spray and fatigue detections the
    tab exists to surface."""
    agg = _aggregate([
        _signin(userId=f"u{i}", userPrincipalName=f"u{i}@x",
                deviceDetail={"isCompliant": False, "displayName": "BYOD"})
        for i in range(300)
    ])
    unmanaged = [p for p in agg.patterns() if p["kind"] == "unmanaged_device_signin"]
    assert len(unmanaged) == 1
    assert unmanaged[0]["count"] == 300
    assert unmanaged[0]["evidence"]["accounts"] == 300
    assert len(unmanaged[0]["evidence"]["top_accounts"]) == 10, "the worst offenders stay visible"


def test_the_pattern_list_is_bounded():
    agg = _aggregate([
        _signin(userId=f"u{i}", status={"errorCode": 500121})
        for i in range(500) for _ in range(risk_collector.FATIGUE_MIN_DENIALS)
    ])
    assert len(agg.patterns()) <= risk_collector._MAX_PATTERNS  # noqa: SLF001


def test_the_unmanaged_list_is_long_enough_for_the_privileged_join():
    """Truncating it to the usual top-50 would silently miss an administrator ranked 51st
    by sign-in volume."""
    agg = _aggregate([
        _signin(userId=f"u{i}", userPrincipalName=f"u{i}@x",
                deviceDetail={"isCompliant": False, "displayName": "BYOD"})
        for i in range(400)
    ])
    payload = agg.payload(sampled=False, lookback_days=30)
    assert len(payload["unmanaged_signin_users"]) == 400
    assert payload["unmanaged_signin_user_total"] == 400


def test_an_account_with_no_name_in_the_log_falls_back_to_its_id():
    """Service principals, deleted users and some federated flows carry neither a UPN nor a
    display name."""
    agg = _aggregate([
        _signin(userId="a928ebfd", userPrincipalName="", userDisplayName="",
                status={"errorCode": 500121})
        for _ in range(risk_collector.FATIGUE_MIN_DENIALS)
    ])
    pattern = [p for p in agg.patterns() if p["kind"] == "mfa_fatigue"][0]
    assert "a928ebfd" in pattern["label"]
    assert pattern["evidence"]["object_id"] == "a928ebfd"


def test_pattern_ids_are_resolved_to_names_after_collection():
    """A raw GUID in a finding title is useless to whoever has to act on it."""
    import asyncio

    class _Client:
        async def get_by_ids(self, ids):
            assert ids == ["a928ebfd"]
            return {"a928ebfd": {"id": "a928ebfd", "displayName": "Nightly Sync",
                                 "@odata.type": "#microsoft.graph.servicePrincipal"}}

    patterns = [{
        "kind": "mfa_fatigue", "key": "a928ebfd",
        "label": "Repeated MFA denials for a928ebfd", "rule": "\u2026", "count": 5,
        "evidence": {"upn": "", "display_name": "", "object_id": "a928ebfd"},
    }]
    out = asyncio.run(risk_collector.resolve_pattern_names(_Client(), patterns))
    assert out[0]["label"] == "Repeated MFA denials for Nightly Sync"
    assert out[0]["evidence"]["display_name"] == "Nightly Sync"
    assert out[0]["evidence"]["resolved_kind"] == "servicePrincipal"


def test_name_resolution_never_costs_the_pattern():
    import asyncio

    from app.entra.graphclient import GraphError

    class _Broken:
        async def get_by_ids(self, ids):  # noqa: ARG002
            raise GraphError(500, "boom")

    patterns = [{
        "kind": "mfa_fatigue", "key": "x", "label": "Repeated MFA denials for x",
        "rule": "\u2026", "count": 5,
        "evidence": {"upn": "", "display_name": "", "object_id": "x"},
    }]
    out = asyncio.run(risk_collector.resolve_pattern_names(_Broken(), patterns))
    assert out[0]["label"] == "Repeated MFA denials for x"


def test_already_named_patterns_are_not_looked_up():
    import asyncio

    class _Fail:
        async def get_by_ids(self, ids):  # noqa: ARG002
            raise AssertionError("must not be called when a name is already present")

    patterns = [{
        "kind": "mfa_fatigue", "key": "u1", "label": "Repeated MFA denials for a@x",
        "rule": "\u2026", "count": 5,
        "evidence": {"upn": "a@x", "display_name": "", "object_id": "u1"},
    }]
    assert asyncio.run(risk_collector.resolve_pattern_names(_Fail(), patterns)) == patterns


def test_the_signin_read_stays_serial():
    """Reading disjoint createdDateTime windows concurrently is the obvious speed-up for the
    slowest domain in the product, and it was measured against a 20k-seat tenant: six readers
    exhausted the client's 429 retries and lost the entire sign-in dataset. The endpoint is
    rate-limited per tenant, so extra readers buy no throughput. The same volume read
    serially never throttled once."""
    import inspect

    assert "asyncio.gather" not in inspect.getsource(risk_collector)


def test_an_expired_paging_token_costs_one_page_not_the_whole_read():
    """A twenty-minute pagination outlives its own continuation token. Graph answers "Skip
    token has expired. Restart pagination from the first page" and get_all raises, which
    discarded all 200,000 events already read on a live full refresh. The log is
    newest-first, so createdDateTime gives a resume point."""
    import asyncio

    from app.entra.graphclient import GraphError

    pages = [
        {"value": [{"id": "a", "createdDateTime": "2026-07-30T10:00:00Z", "userId": "u1"},
                   {"id": "b", "createdDateTime": "2026-07-30T09:00:00Z", "userId": "u1"}],
         "@odata.nextLink": "https://graph.microsoft.com/dead-token"},
    ]
    resumed = {"value": [
        # Graph repeats the boundary second on resume; it must not be counted twice.
        {"id": "b", "createdDateTime": "2026-07-30T09:00:00Z", "userId": "u1"},
        {"id": "c", "createdDateTime": "2026-07-30T08:00:00Z", "userId": "u2"},
    ]}
    calls: list[str] = []

    class _Client:
        async def get(self, url):
            calls.append(url)
            if "dead-token" in url:
                raise GraphError(400, "Skip token has expired. Restart pagination from the "
                                      "first page.")
            # The resume bound travels through a query string, so it arrives percent-encoded.
            return resumed if "09%3A00%3A00Z" in url else pages[0]

    class _Ctx:
        async def say(self, *_a):
            return None

    agg = risk_collector._Aggregator()
    read, capped, resumes = asyncio.run(
        risk_collector._read_signins(_Client(), _Ctx(), agg, "2026-07-01T00:00:00Z"))

    assert resumes == 1
    assert capped is False
    assert read == 3, "two from the first page, one new from the resumed page"
    assert agg.total == 3
    assert any("09%3A00%3A00Z" in c for c in calls), \
        "the resume must be bounded at the oldest row already read"


def test_a_failure_that_is_not_an_expired_token_still_surfaces():
    """Resuming past a permission or license error would hide it behind a partial dataset."""
    import asyncio

    import pytest as _pytest

    from app.entra.graphclient import GraphError

    class _Client:
        async def get(self, _url):
            raise GraphError(403, "Insufficient privileges")

    class _Ctx:
        async def say(self, *_a):
            return None

    with _pytest.raises(GraphError):
        asyncio.run(risk_collector._read_signins(
            _Client(), _Ctx(), risk_collector._Aggregator(), "2026-07-01T00:00:00Z"))


def test_resuming_is_bounded():
    """A token that dies on every single page is a broken read, not a slow one. The read
    keeps making progress, so only the resume ceiling can stop it."""
    import asyncio

    import pytest as _pytest

    from app.entra.graphclient import GraphError

    seq = {"n": 0}

    class _Client:
        async def get(self, url):
            if "skiptoken" in url:
                raise GraphError(400, "Skip token has expired.")
            seq["n"] += 1
            return {"value": [{"id": f"r{seq['n']}",
                               "createdDateTime": f"2026-07-30T10:{60 - seq['n']:02d}:00Z"}],
                    "@odata.nextLink": "https://graph.microsoft.com/x?$skiptoken=zz"}

    class _Ctx:
        async def say(self, *_a):
            return None

    with _pytest.raises(GraphError):
        asyncio.run(risk_collector._read_signins(
            _Client(), _Ctx(), risk_collector._Aggregator(), "2026-07-01T00:00:00Z"))
    assert seq["n"] == risk_collector._MAX_RESUMES + 1


def test_the_row_cap_still_holds_across_resumes():
    import asyncio

    seq = {"n": 0}

    class _Client:
        async def get(self, _url):
            # Distinct ids and descending timestamps, as the real log returns.
            start = seq["n"]
            seq["n"] += 999
            return {"value": [{"id": f"r{start + i}",
                               "createdDateTime": f"2026-07-30T10:00:{(start + i) % 60:02d}Z"}
                              for i in range(999)],
                    "@odata.nextLink": "https://graph.microsoft.com/next"}

    class _Ctx:
        async def say(self, *_a):
            return None

    read, capped, _ = asyncio.run(risk_collector._read_signins(
        _Client(), _Ctx(), risk_collector._Aggregator(), "2026-07-01T00:00:00Z"))
    assert capped is True
    assert read == risk_collector.MAX_SIGNIN_ROWS


def test_a_resumed_page_of_nothing_new_ends_the_read():
    """If the resume bound hands back only rows already counted, continuing would spin."""
    import asyncio

    from app.entra.graphclient import GraphError

    page = {"value": [{"id": "a", "createdDateTime": "2026-07-30T10:00:00Z"}]}
    calls = {"n": 0}

    class _Client:
        async def get(self, url):
            calls["n"] += 1
            if calls["n"] == 1:
                return {**page, "@odata.nextLink": "https://graph.microsoft.com/dead"}
            if "dead" in url:
                raise GraphError(400, "Skip token has expired.")
            return dict(page)   # the resume returns only the row we already have

    class _Ctx:
        async def say(self, *_a):
            return None

    read, _capped, resumes = asyncio.run(risk_collector._read_signins(
        _Client(), _Ctx(), risk_collector._Aggregator(), "2026-07-01T00:00:00Z"))
    assert read == 1, "the boundary row must not be counted a second time"
    assert resumes == 1
    assert calls["n"] < 10, "the read has to stop, not spin on the boundary"


def test_failed_signin_from_an_unmanaged_device_is_not_a_finding():
    """A blocked sign-in from a non-compliant device is the control working."""
    agg = _aggregate([_signin(userId="u1", status={"errorCode": 53000},
                              deviceDetail={"isCompliant": False})])
    assert agg.payload(sampled=False, lookback_days=30)["unmanaged_signin_users"] == []


def test_report_only_impact_is_captured_per_policy():
    agg = _aggregate([
        _signin(appliedConditionalAccessPolicies=[
            {"id": "p1", "displayName": "Block legacy", "result": "reportOnlyFailure"}]),
        _signin(appliedConditionalAccessPolicies=[
            {"id": "p1", "displayName": "Block legacy", "result": "reportOnlyFailure"}]),
        _signin(appliedConditionalAccessPolicies=[
            {"id": "p1", "displayName": "Block legacy", "result": "reportOnlySuccess"}]),
    ])
    impact = agg.payload(sampled=False, lookback_days=30)["report_only_impact"][0]
    assert impact["would_block"] == 2
    assert impact["would_pass"] == 1


# ============================================ live-discovered Graph constraints
def test_the_signin_select_never_asks_for_authentication_requirement():
    """`authenticationRequirement` does not exist on signIn in v1.0. Selecting it 400s the
    whole query, which cost a fully-permissioned tenant every single sign-in — the domain
    reported 'blind' while the permission was granted and the endpoint worked."""
    assert "authenticationRequirement" not in risk_collector.SIGNIN_SELECT
    assert "isInteractive" in risk_collector.SIGNIN_SELECT
    assert "appliedConditionalAccessPolicies" in risk_collector.SIGNIN_SELECT


def test_identity_protection_uses_the_500_page_ceiling():
    """/identityProtection/* rejects $top above 500 with 'Invalid page size specified',
    losing the entire Identity Protection dataset."""
    assert risk_collector.RISK_PAGE == 500
    assert risk_collector.SIGNIN_PAGE == 999


def test_mfa_is_counted_from_conditional_access_grant_controls():
    """The narrower claim we can actually substantiate, and it is labeled as such."""
    agg = _aggregate([
        _signin(appliedConditionalAccessPolicies=[
            {"id": "p1", "displayName": "Require MFA", "result": "success",
             "enforcedGrantControls": ["Mfa"]}]),
        _signin(appliedConditionalAccessPolicies=[
            {"id": "p2", "displayName": "Block legacy", "result": "success",
             "enforcedGrantControls": ["Block"]}]),
        _signin(appliedConditionalAccessPolicies=[]),
    ])
    payload = agg.payload(sampled=False, lookback_days=30)
    assert payload["mfa_challenged"] == 1
    assert payload["mfa_metric"] == "ca_enforced", (
        "the metric must declare what it measures; a bare 'mfa_challenged' would overclaim")


def test_is_interactive_is_preferred_over_the_client_app_heuristic():
    agg = _aggregate([
        _signin(clientAppUsed="Exchange ActiveSync", isInteractive=True),
        _signin(clientAppUsed="Browser", isInteractive=False),
    ])
    assert agg.payload(sampled=False, lookback_days=30)["interactive"] == 1


def test_the_client_app_heuristic_still_applies_when_is_interactive_is_absent():
    """Older log entries omit the field; falling back keeps the count meaningful."""
    row = _signin(clientAppUsed="Browser")
    row.pop("isInteractive", None)
    agg = _aggregate([row])
    assert agg.payload(sampled=False, lookback_days=30)["interactive"] == 1


# =================================================================== signal joins
def _data(**kw):
    base = {
        "risk": {"capabilities": {"signins": True, "risky_users": True, "risk_detections": True,
                                  "risky_workload_identities": True},
                 "risky_users": [], "risk_detections": [], "risky_service_principals": [],
                 "patterns": [], "signins": {"sampled": False}},
        "people": {"users": []},
        "roles": {"assignments": [], "group_derived": [], "eligible": [], "definitions": []},
        "apps": {"service_principals": []},
        "ca": {"named_locations": []},
    }
    base.update(kw)
    return base


def test_privileged_at_risk_fires_at_any_risk_level():
    """The combination is what matters, not the level Microsoft assigned."""
    data = _data(
        risk={**_data()["risk"],
              "risky_users": [{"id": "u1", "upn": "admin@x", "name": "Admin", "level": "low",
                               "state": "atRisk", "detail": "", "last_updated": ""}]},
        roles={"assignments": [{"principal_id": "u1", "role_privileged": True,
                                "role_name": "Global Administrator"}],
               "group_derived": [], "eligible": [], "definitions": []},
        people={"users": [{"id": "u1", "upn": "admin@x", "mfa_registered": True}]},
    )
    out = risk_signals._privileged_user_at_risk(data, _ctx())  # noqa: SLF001
    assert len(out) == 1
    assert out[0]["severity"] == "critical"
    assert out[0]["evidence"]["risk_level"] == "low"


def test_privileged_at_risk_ignores_a_remediated_user():
    data = _data(
        risk={**_data()["risk"],
              "risky_users": [{"id": "u1", "upn": "admin@x", "name": "", "level": "high",
                               "state": "remediated", "detail": "", "last_updated": ""}]},
        roles={"assignments": [{"principal_id": "u1", "role_privileged": True,
                                "role_name": "Global Administrator"}],
               "group_derived": [], "eligible": [], "definitions": []},
    )
    assert risk_signals._privileged_user_at_risk(data, _ctx()) == []  # noqa: SLF001


def test_privileged_at_risk_names_the_self_remediation_problem():
    data = _data(
        risk={**_data()["risk"],
              "risky_users": [{"id": "u1", "upn": "admin@x", "name": "", "level": "high",
                               "state": "atRisk", "detail": "", "last_updated": ""}]},
        roles={"assignments": [{"principal_id": "u1", "role_privileged": True,
                                "role_name": "Global Administrator"}],
               "group_derived": [], "eligible": [], "definitions": []},
        people={"users": [{"id": "u1", "upn": "admin@x", "mfa_registered": False}]},
    )
    out = risk_signals._privileged_user_at_risk(data, _ctx())  # noqa: SLF001
    assert "cannot self-remediate" in out[0]["detail"]


def test_risk_signals_report_not_measured_when_the_capability_is_absent():
    """Blind must never be reported as clean."""
    data = _data(risk={"capabilities": {"signins": False, "risky_users": False},
                       "risky_users": [], "patterns": [], "signins": {}})
    for fn in (risk_signals._privileged_user_at_risk,        # noqa: SLF001
               risk_signals._high_risk_unremediated,          # noqa: SLF001
               risk_signals._legacy_auth_success,             # noqa: SLF001
               risk_signals._signin_failure_rate):            # noqa: SLF001
        with pytest.raises(SignalUnavailable):
            fn(data, _ctx())


def test_pattern_findings_carry_the_rule_and_the_sampling_flag():
    data = _data(risk={**_data()["risk"],
                       "signins": {"sampled": True},
                       "patterns": [{"kind": "password_spray", "key": "1.2.3.4",
                                     "label": "Password spray from 1.2.3.4",
                                     "rule": "\u2265 12 distinct users failed",
                                     "count": 20, "evidence": {"ip": "1.2.3.4"}}]})
    fn = risk_signals._signin_pattern("password_spray", "risk.password_spray_pattern", "high")  # noqa: SLF001
    out = fn(data, _ctx())
    assert len(out) == 1
    assert out[0]["evidence"]["rule"]
    assert out[0]["evidence"]["sampled"] is True, "a sampled window must be stated on the finding"


def test_unmanaged_device_signal_only_fires_for_privileged_users():
    data = _data(
        risk={**_data()["risk"],
              "signins": {"sampled": False,
                          "unmanaged_signin_users": [
                              {"user_id": "u1", "upn": "admin@x", "count": 4, "device": "BYOD",
                               "last_seen": ""},
                              {"user_id": "u2", "upn": "normal@x", "count": 9, "device": "BYOD",
                               "last_seen": ""}]}},
        roles={"assignments": [{"principal_id": "u1", "role_privileged": True,
                                "role_name": "Global Administrator"}],
               "group_derived": [], "eligible": [], "definitions": []},
    )
    out = risk_signals._priv_signin_unmanaged_device(data, _ctx())  # noqa: SLF001
    assert [f["object_id"] for f in out] == ["u1"]


def test_failure_rate_signal_ignores_a_tenant_with_no_traffic():
    """A 100% failure rate over four sign-ins is noise, not a finding."""
    data = _data(risk={**_data()["risk"],
                       "signins": {"sampled": False, "total": 4, "failure_rate": 1.0,
                                   "failure": 4, "by_failure_code": []}})
    assert risk_signals._signin_failure_rate(data, _ctx()) == []  # noqa: SLF001


def test_risky_workload_identity_reports_what_it_can_reach():
    data = _data(
        risk={**_data()["risk"],
              "risky_service_principals": [{"id": "sp1", "name": "Sync", "level": "high",
                                            "state": "atRisk", "detail": ""}]},
        apps={"service_principals": [
            {"object_id": "sp1", "display_name": "Sync",
             "granted_app_permissions": [{"permission": "Mail.ReadWrite", "tier": "critical"}]}]},
    )
    out = risk_signals._risky_workload_identity(data, _ctx())  # noqa: SLF001
    assert out[0]["evidence"]["granted_permissions"] == ["Mail.ReadWrite"]
