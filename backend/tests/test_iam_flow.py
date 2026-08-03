"""Projection of IAM master rows into Access Map facts.

The three tests that matter most here are the ones covering what a flow diagram *cannot* say
on its own. Each is a way of producing a picture that looks right and is false:

* counting a group's own row alongside the expanded member rows doubles every group grant;
* dropping a group whose membership could not be read turns "we cannot see who" into "nobody";
* drawing a deny assignment as a ribbon states the exact opposite of what a deny does.
"""
from __future__ import annotations

from typing import Any

from app.iam import flow


def _row(**over: Any) -> dict[str, Any]:
    base = {
        "surface": "Azure RBAC",
        "assignmentState": "Active",
        "principalId": "u1", "principalType": "User", "principalDisplayName": "Alice",
        "effectivePrincipalId": "u1", "effectivePrincipalType": "User",
        "effectivePrincipalName": "Alice",
        "accessPath": "Direct",
        "sourceGroupId": "", "sourceGroupName": "",
        "roleName": "Contributor", "roleCategory": "ControlPlane", "roleIsPrivileged": False,
        "scope": "/subscriptions/s1", "scopeType": "subscription",
        "subscriptionId": "s1", "subscriptionName": "Prod",
        "resourceGroup": "", "resourceType": "", "resourceName": "",
        "managementGroupId": "", "managementGroupName": "",
        "condition": "", "effect": "Allow", "pimManaged": False,
    }
    base.update(over)
    return base


def _facts_for(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return flow.build_facts(rows)["facts"]


# ------------------------------------------------------------------ group double counting
def _group_scenario() -> list[dict[str, Any]]:
    """What `compose.expand_group_rows` actually produces: the group row AND a row per member."""
    return [
        # The group's own assignment.
        _row(principalId="g1", principalType="Group", principalDisplayName="Platform Admins",
             effectivePrincipalId="g1", effectivePrincipalType="Group",
             effectivePrincipalName="Platform Admins", accessPath="Direct", roleName="Owner"),
        # ...and the expansion of it, one row per transitive member.
        _row(principalId="g1", principalType="Group", principalDisplayName="Platform Admins",
             effectivePrincipalId="u1", effectivePrincipalType="User",
             effectivePrincipalName="Alice", accessPath="GroupTransitive",
             sourceGroupId="g1", sourceGroupName="Platform Admins", roleName="Owner"),
        _row(principalId="g1", principalType="Group", principalDisplayName="Platform Admins",
             effectivePrincipalId="u2", effectivePrincipalType="User",
             effectivePrincipalName="Bob", accessPath="GroupTransitive",
             sourceGroupId="g1", sourceGroupName="Platform Admins", roleName="Owner"),
    ]


def test_an_expanded_group_grant_is_not_counted_twice():
    result = flow.build_facts(_group_scenario())
    principals = {f["principal"] for f in result["facts"]}
    assert principals == {"Alice", "Bob"}, (
        "the group's own row must fold into its members; keeping both counts the grant once for "
        "the group and again for every person in it"
    )
    assert result["totals"]["grants"] == 2
    assert result["totals"]["group_rows_folded"] == 1


def test_the_group_survives_as_a_dimension_even_though_its_row_folded():
    """Folding the row must not lose WHERE the access came from - that is the remediation."""
    facts = _facts_for(_group_scenario())
    assert all(f["group"] == "Platform Admins" for f in facts)
    assert all(f["access_path"] == "GroupTransitive" for f in facts)


def test_a_group_whose_members_could_not_be_read_is_kept_not_dropped():
    """"We could not enumerate the group" must never render as "nobody has this access"."""
    rows = [_row(principalId="g9", principalType="Group",
                 principalDisplayName="Opaque Group",
                 effectivePrincipalId="g9", effectivePrincipalType="Group",
                 effectivePrincipalName="Opaque Group", accessPath="Direct", roleName="Owner")]
    result = flow.build_facts(rows)
    assert len(result["facts"]) == 1
    fact = result["facts"][0]
    assert fact["principal"] == "Opaque Group"
    assert fact["access_path"] == flow.ACCESS_PATH_GROUP_UNEXPANDED, (
        "it must be visibly distinct from a direct user assignment"
    )
    assert result["totals"]["unexpanded_groups"] == 1
    assert any("could not be read" in n for n in result["notes"])


# --------------------------------------------------------------------------- deny handling
def test_deny_assignments_are_kept_out_of_the_flow():
    rows = [
        _row(roleName="Contributor"),
        _row(roleName="Contributor", effect="Deny", surface="Deny Assignment"),
    ]
    result = flow.build_facts(rows)
    assert len(result["facts"]) == 1, "a deny is not a grant and must not be drawn as one"
    assert len(result["denies"]) == 1
    assert result["totals"]["deny_rows"] == 1
    assert any("deny" in n.lower() for n in result["notes"])


def test_a_deny_is_reported_rather_than_silently_discarded():
    rows = [_row(effect="Deny", surface="Deny Assignment", roleName="Contributor")]
    result = flow.build_facts(rows)
    assert result["facts"] == []
    assert result["denies"][0]["role"] == "Contributor", "excluded from the flow, not from the answer"


# ------------------------------------------------------------------------- PIM eligibility
def test_eligible_and_active_grants_stay_distinguishable():
    rows = [
        _row(roleName="Owner", assignmentState="Active"),
        _row(roleName="Owner", assignmentState="Eligible", pimManaged=True),
    ]
    result = flow.build_facts(rows)
    states = {f["state"] for f in result["facts"]}
    assert states == {"Active", "Eligible"}, (
        "an eligible grant is permission to ask, not standing access; merging them overstates "
        "privilege and contradicts the standing-privilege KPI elsewhere in the product"
    )
    assert result["totals"]["eligible_rows"] == 1


# ------------------------------------------------------------------------------ projection
def test_identical_access_deduplicates_into_one_counted_fact():
    result = flow.build_facts([_row(), _row(), _row()])
    assert len(result["facts"]) == 1
    assert result["facts"][0]["count"] == 3
    assert result["totals"]["rows"] == 3


def test_differing_access_does_not_deduplicate():
    result = flow.build_facts([_row(roleName="Owner"), _row(roleName="Reader")])
    assert len(result["facts"]) == 2


def test_the_effective_principal_is_used_not_the_assignee():
    facts = _facts_for([_row(
        principalId="g1", principalType="Group", principalDisplayName="Some Group",
        effectivePrincipalId="u7", effectivePrincipalType="User",
        effectivePrincipalName="Carol", accessPath="GroupTransitive",
        sourceGroupId="g1", sourceGroupName="Some Group")])
    assert facts[0]["principal"] == "Carol"
    assert facts[0]["principal_type"] == "User"


def test_condition_is_a_boolean_axis_not_the_condition_text():
    """ABAC condition text is unique per assignment; as a dimension it would explode the graph."""
    facts = _facts_for([_row(condition="@Resource[tag:env] StringEquals 'prod'")])
    assert facts[0]["condition"] is True


def test_a_principal_with_no_resolvable_name_still_appears():
    facts = _facts_for([_row(
        principalDisplayName="", effectivePrincipalName="",
        effectivePrincipalUserPrincipalName="", principalUserPrincipalName="",
        principalId="orphan-1", effectivePrincipalId="orphan-1")])
    assert facts[0]["principal"] == "orphan-1", "an orphaned assignment is a finding, not a blank"


def test_every_fact_key_is_present_on_every_fact():
    """The client indexes facts by dimension; a missing key would render as an empty node."""
    for fact in _facts_for([_row(), _row(roleName="Reader", scopeType="resourceGroup")]):
        for key in flow.FACT_KEYS:
            assert key in fact


def test_truncation_is_reported_rather_than_silent(monkeypatch):
    monkeypatch.setattr(flow, "MAX_FACTS", 2)
    rows = [_row(roleName=f"Role {i}") for i in range(5)]
    result = flow.build_facts(rows)
    assert result["truncated"] is True
    assert len(result["facts"]) == 2
    assert any("Showing the" in n for n in result["notes"])


def test_a_clean_tenant_produces_no_notes():
    """Notes must mean something; emitting them unconditionally would train readers to skip them."""
    assert flow.build_facts([_row()])["notes"] == []


# ------------------------------------------------------------------------------- transport
def test_the_wire_format_round_trips_exactly():
    """The client decodes this; a lossy encoding would quietly redraw the wrong access."""
    rows = [
        _row(),
        _row(roleName="Owner", roleIsPrivileged=True, assignmentState="Eligible", pimManaged=True),
        _row(condition="@Resource[tag:env] StringEquals 'prod'"),
        _row(effect="Deny", surface="Deny Assignment"),
        _row(principalId="g1", principalType="Group", principalDisplayName="Opaque",
             effectivePrincipalId="g1", effectivePrincipalName="Opaque",
             effectivePrincipalType="Group"),
    ]
    original = flow.build_facts(rows)
    restored = flow.decode(flow.encode(original))
    assert restored["facts"] == original["facts"]
    assert restored["denies"] == original["denies"]
    assert restored["totals"] == original["totals"]
    assert restored["notes"] == original["notes"]


def test_booleans_survive_the_round_trip_as_booleans():
    """Interning stringifies everything; a bool arriving as the string "false" is truthy."""
    original = flow.build_facts([_row(roleIsPrivileged=True, condition="x", pimManaged=True)])
    restored = flow.decode(flow.encode(original))
    fact = restored["facts"][0]
    assert fact["privileged"] is True
    assert fact["condition"] is True
    assert fact["pim_managed"] is True

    off = flow.decode(flow.encode(flow.build_facts([_row()])))["facts"][0]
    assert off["privileged"] is False
    assert off["condition"] is False


def test_interning_actually_shrinks_a_repetitive_payload():
    """If this stops holding, the browser is being sent megabytes to draw a picture."""
    import json

    rows = [_row(principalDisplayName=f"User {i}", effectivePrincipalName=f"User {i}",
                 principalId=f"u{i}", effectivePrincipalId=f"u{i}")
            for i in range(500)]
    result = flow.build_facts(rows)
    plain = len(json.dumps(result))
    interned = len(json.dumps(flow.encode(result)))
    assert interned * 3 < plain, (
        f"interning saved too little ({plain} -> {interned}); the wire format is the only reason "
        f"re-pivoting in the browser does not need a round trip"
    )


def test_encoding_an_empty_result_is_still_decodable():
    restored = flow.decode(flow.encode(flow.build_facts([])))
    assert restored["facts"] == []
    assert restored["denies"] == []
