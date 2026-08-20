"""A blocked collector must name the blocker the operator can actually act on.

Graph is inconsistent: PIM reports a missing license as a 400 with a message, lifecycle
workflows report one as a 403 -- the same status as a genuine consent failure. Reading the
403 as 'not permitted' told operators to grant LifecycleWorkflows.Read.All when they already
held it, and no amount of consent would ever have changed the outcome.
"""
from __future__ import annotations

from app.entra.collectors.governance import _gov_note
from app.entra.collectors.roles import _is_licence_error
from app.entra.graphclient import GraphError, GraphPermissionError

LICENCE_403 = (
    "Insufficient license to complete this operation. User workflows require an "
    "Entra ID Governance license."
)
LICENCE_400 = (
    "The tenant needs to have Microsoft Entra ID P2 or Microsoft Entra ID Governance license."
)
DENIED_403 = "Attempted to perform an unauthorized operation."


def _permission_error(message: str) -> GraphPermissionError:
    """Always a 403 — that is what the class means, and the point of these tests."""
    return GraphPermissionError(message)


def _graph_error(message: str, status: int) -> GraphError:
    return GraphError(status, message)


def test_a_403_carrying_a_licence_message_is_a_licence_error():
    assert _is_licence_error(_permission_error(LICENCE_403)) is True


def test_a_400_carrying_a_licence_message_is_still_a_licence_error():
    assert _is_licence_error(_graph_error(LICENCE_400, 400)) is True


def test_a_plain_403_is_not_a_licence_error():
    assert _is_licence_error(_permission_error(DENIED_403)) is False


def test_an_unrelated_status_is_not_a_licence_error():
    assert _is_licence_error(_graph_error(LICENCE_400, 500)) is False


def test_the_note_names_the_licence_not_the_scope_when_licence_is_the_blocker():
    note = _gov_note("Lifecycle workflows", _permission_error(LICENCE_403),
                     "LifecycleWorkflows.Read.All",
                     "this tenant is not licensed for Entra ID Governance.")
    assert "not licensed for Entra ID Governance" in note
    assert "LifecycleWorkflows.Read.All" not in note, "do not ask for a scope already held"


def test_the_note_names_the_scope_when_consent_really_is_the_blocker():
    note = _gov_note("Access reviews", _permission_error(DENIED_403),
                     "AccessReview.Read.All", "this tenant is not licensed for P2.")
    assert "AccessReview.Read.All" in note
    assert "not permitted" in note


def test_an_ordinary_failure_is_reported_as_itself():
    note = _gov_note("Entitlement management", _graph_error("gateway timeout", 504),
                     "EntitlementManagement.Read.All", "needs a licence")
    assert "gateway timeout" in note
    assert "not permitted" not in note
