"""Regression tests for the `$top`-rejecting Graph collections.

Several `/roleManagement` and `/policies` collections answer a request carrying `$top` with a
400 that would otherwise lose the entire domain. This was found against a live tenant, so it
gets a permanent test rather than a comment.
"""
from __future__ import annotations

import asyncio
import json

import httpx

from app.entra.graphclient import GraphClient, GraphError, _rejects_top


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self.handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return self.handler(request)


def _client(handler) -> GraphClient:
    c = GraphClient({"auth_method": "service_principal"})
    c._token = "header.eyJyb2xlcyI6W119.sig"  # noqa: SLF001 - bypass token acquisition
    c._client = httpx.AsyncClient(transport=_Transport(handler), timeout=5)  # noqa: SLF001
    return c


def test_rejects_top_recognises_the_real_graph_messages():
    for msg in (
        "The query specified in the URI is not valid. Query option 'Top' is not allowed.",
        "Invalid/unsupported query request.",
    ):
        assert _rejects_top(GraphError(400, msg))
    # Unrelated 400s must not trigger the retry.
    assert not _rejects_top(GraphError(400, "The tenant needs to have Microsoft Entra ID P2 license."))
    assert not _rejects_top(GraphError(403, "Query option 'Top' is not allowed."))


def test_get_all_retries_once_without_top_and_succeeds():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "%24top" in str(request.url) or "$top" in str(request.url):
            return httpx.Response(400, json={"error": {"message": "Query option 'Top' is not allowed."}})
        return httpx.Response(200, json={"value": [{"id": "rd-1"}, {"id": "rd-2"}]})

    async def run():
        c = _client(handler)
        try:
            return await c.get_all("/roleManagement/directory/roleDefinitions",
                                   select=["id"], top=999)
        finally:
            await c.aclose()

    items, truncated = asyncio.run(run())
    assert [i["id"] for i in items] == ["rd-1", "rd-2"]
    assert truncated is False
    assert len(seen) == 2, "should retry exactly once, without $top"
    assert "top" not in seen[1].lower()


def test_top_retry_is_counted_in_stats():
    def handler(request: httpx.Request) -> httpx.Response:
        if "top" in str(request.url).lower():
            return httpx.Response(400, json={"error": {"message": "Invalid/unsupported query request."}})
        return httpx.Response(200, json={"value": []})

    async def run():
        c = _client(handler)
        try:
            await c.get_all("/policies/authenticationStrengthPolicies", top=999)
            return c.stats.as_dict()
        finally:
            await c.aclose()

    assert asyncio.run(run())["top_retries"] == 1


def test_unrelated_400_is_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        calls["n"] += 1
        return httpx.Response(400, json={
            "error": {"message": "The tenant needs to have Microsoft Entra ID P2 or Microsoft Entra ID Governance license."}
        })

    async def run():
        c = _client(handler)
        try:
            await c.get_all("/roleManagement/directory/roleEligibilitySchedules", top=999)
        finally:
            await c.aclose()

    try:
        asyncio.run(run())
        raise AssertionError("expected a GraphError")
    except GraphError as exc:
        assert "P2" in exc.message
    assert calls["n"] == 1, "a licence 400 must not be retried"


def test_pim_licence_error_is_classified_as_a_licence_limitation():
    """PIM reports a missing license as a 400 with a message, not a 403 — reporting it as a
    generic collection error would be misleading."""
    from app.entra.collectors.roles import _is_licence_error, _pim_note

    licence = GraphError(400, "The tenant needs to have Microsoft Entra ID P2 or Microsoft Entra ID Governance license.")
    assert _is_licence_error(licence)
    assert "not licensed" in _pim_note("PIM eligibility schedules", licence, "PrivilegedAccess.Read.AzureAD")

    other = GraphError(400, "Invalid/unsupported query request.")
    assert not _is_licence_error(other)
    assert "not licensed" not in _pim_note("PIM eligibility schedules", other, "PrivilegedAccess.Read.AzureAD")


# ------------------------------------------------------------------ note formatting
def test_notes_are_truncated_at_a_word_boundary():
    """A Graph error sliced at a fixed character count produced coverage-banner text
    ending "...require an Entra ID Governance licen", which reads like the product lost the
    rest of the sentence rather than chose to shorten it."""
    from app.entra.collectors import clip

    long = ("Insufficient license to complete this operation. User workflows require an "
            "Entra ID Governance license. Agent-related workflows require another one.")
    out = clip(long, 100)
    assert len(out) <= 101
    assert out.endswith("…")
    assert not out[:-1].endswith(("licen", "requir", "Govern"))
    assert out[:-1].split()[-1] in long.split(), "the last word must be a whole word"


def test_clip_leaves_short_text_alone_and_normalises_whitespace():
    from app.entra.collectors import clip

    assert clip("short", 100) == "short"
    assert clip("  spread   over\n  lines ", 100) == "spread over lines"
    assert clip(None, 100) == ""


# ------------------------------------------------- $expand on the PIM schedule collections
def test_entitlement_uses_the_v1_expand_name():
    """v1.0 calls it `resourceRoleScopes`. The beta name `accessPackageResourceRoleScopes`
    400s the query and costs the entire access-package inventory."""
    import inspect

    from app.entra.collectors import governance as gov_collector

    source = inspect.getsource(gov_collector.collect)
    # The old name may appear in a comment explaining why it is wrong; what matters is that
    # it is never sent.
    assert 'expand="accessPackageResourceRoleScopes"' not in source
    assert 'expand="resourceRoleScopes"' in source


def test_assignment_policies_expand_their_access_package():
    """v1.0 `assignmentPolicies` rows carry NO accessPackageId and do not return the
    accessPackage navigation unless expanded, so without the expand every policy failed to
    join and every access package rendered "0 policies"."""
    import inspect

    from app.entra.collectors import governance as gov_collector

    source = inspect.getsource(gov_collector.collect)
    assert 'expand="accessPackage($select=id)"' in source


def test_assignment_policy_review_flag_reads_the_v1_field_name():
    """v1.0 calls it `reviewSettings`; the beta name is `accessReviewSettings`. Reading the
    beta name reported "no review" on every access package in a tenant that ran quarterly
    reviews — a false accusation is worse than no finding."""
    import inspect

    from app.entra.collectors import governance as gov_collector

    source = inspect.getsource(gov_collector.collect)
    assert 'row.get("reviewSettings")' in source
    assert 'row.get("allowedTargetScope")' in source


def test_pim_for_groups_is_queried_per_group_not_tenant_wide():
    """`/privilegedAccess/group/eligibilitySchedules` answers 400 'The required parameters
    GroupId or PrincipalId is missing' when enumerated tenant-wide. It has to be asked per
    role-assignable group, which is also the only set where the answer matters."""
    import inspect

    from app.entra.collectors import pim as pim_collector

    source = inspect.getsource(pim_collector.collect)
    assert "privilegedAccess/group/eligibilitySchedules" in source
    assert "groupId eq" in source, "the per-group filter is mandatory for this collection"
    assert "isAssignableToRole eq true" in source, (
        "the group set must be the role-assignable ones")


def test_pim_schedule_collections_are_never_queried_with_expand():
    """Graph rejects `$expand=principal(...)` on every roleManagement schedule collection
    with a 400 whose message is the actively misleading "The filter is invalid" — even
    though no $filter was sent. Verified against a live tenant, which lost its whole
    eligible-assignment list to it. Principals are resolved by getByIds instead.
    """
    import inspect

    from app.entra.collectors import roles as roles_collector

    source = inspect.getsource(roles_collector.collect)
    for line_no, line in enumerate(source.splitlines()):
        if "Schedule" not in line:
            continue
        # Look at the call that follows this path for an expand= argument.
        window = "\n".join(source.splitlines()[line_no:line_no + 8])
        assert "expand=" not in window, (
            f"a roleManagement schedule collection is being queried with $expand:\n{window}"
        )


def test_expand_on_schedules_is_recognised_as_the_misleading_filter_400():
    """The message says 'filter' and there is no filter. Anyone debugging this later needs
    the test to say so out loud."""
    exc = GraphError(400, "The  filter is invalid.")
    assert not _rejects_top(exc), "this is not a $top problem and must not trigger that retry"


def test_principals_resolve_through_get_by_ids_when_expand_is_unavailable():
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "getByIds" in url:
            posted.append(json.loads(request.content))
            return httpx.Response(200, json={"value": [
                {"id": "u1", "displayName": "Alice", "userPrincipalName": "alice@x",
                 "@odata.type": "#microsoft.graph.user"},
            ]})
        return httpx.Response(200, json={"value": []})

    async def run():
        c = _client(handler)
        try:
            return await roles_resolve(c, ["u1", "u1", ""])
        finally:
            await c.aclose()

    from app.entra.collectors.roles import _resolve_principals as roles_resolve

    resolved = asyncio.run(run())
    assert resolved["u1"]["displayName"] == "Alice"
    assert posted[0]["ids"] == ["u1"], "ids must be de-duplicated and blanks dropped"


def test_principal_resolution_failure_costs_names_not_assignments():
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(500, json={"error": {"message": "boom"}})

    async def run():
        from app.entra.collectors.roles import _resolve_principals

        c = _client(handler)
        try:
            return await _resolve_principals(c, ["u1"])
        finally:
            await c.aclose()

    assert asyncio.run(run()) == {}, "a lookup failure must degrade to no names, not raise"


# ------------------------------------------------------------------ batch throughput
def test_batch_chunks_are_dispatched_concurrently():
    """Serial chunks are why the owner fan-out had to be capped at 5,000. 4,000 serial
    round-trips is an afternoon; concurrent ones bounded by the connection semaphore are
    a few minutes."""
    import threading

    from app.entra.graphclient import GraphRequest

    in_flight = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            in_flight["now"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        body = json.loads(request.content)
        responses = [{"id": r["id"], "status": 200, "body": {"value": [{"id": r["id"]}]}}
                     for r in body["requests"]]
        with lock:
            in_flight["now"] -= 1
        return httpx.Response(200, json={"responses": responses})

    async def run():
        c = _client(handler)
        try:
            reqs = [GraphRequest(id=str(i), url=f"/groups/{i}/owners") for i in range(200)]
            return await c.batch(reqs)
        finally:
            await c.aclose()

    out = asyncio.run(run())
    assert len(out) == 200
    assert [r.id for r in out] == [str(i) for i in range(200)], "order must be preserved"


def test_batch_collection_defaults_to_no_cap():
    """A capped ownership scan reports a subset of the estate as fully covered."""
    from app.entra.collectors import batch_collection
    from app.entra.graphclient import GraphRequest  # noqa: F401 - import shape check

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={"responses": [
            {"id": r["id"], "status": 200, "body": {"value": [{"id": "owner-1"}]}}
            for r in body["requests"]
        ]})

    async def run():
        c = _client(handler)
        try:
            return await batch_collection(
                c, [f"g{i}" for i in range(150)], lambda gid: f"/groups/{gid}/owners")
        finally:
            await c.aclose()

    out, truncated, forbidden = asyncio.run(run())
    assert len(out) == 150, "every id must be resolved when no cap is given"
    assert truncated is False
    assert forbidden == 0
