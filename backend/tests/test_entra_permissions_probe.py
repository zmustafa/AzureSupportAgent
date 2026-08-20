"""What a permission probe is, and is not, evidence of.

Every failure used to be reported as a missing permission. That sent operators to the Azure
portal to grant a scope they already held (or one that could not help), and in the worst
case it declared a domain blind because OUR query was malformed.

Live-verified against two real tenants on 2026-07-31:

  /roleManagement/directory/roleDefinitions?$top=1   400 "requires a minimum page size of 20"
  /roleManagement/directory/roleDefinitions?$top=20  400 "Invalid/unsupported query request"
  /roleManagement/directory/roleDefinitions          200
  /roleManagement/directory/roleEligibilitySchedules 400 AadPremiumLicenseRequired (no P2)
  /identityProtection/riskyUsers                     403 "required scopes are missing"
"""
from __future__ import annotations

from app.entra import permissions_probe as pp

ROLE_SCOPES = ["RoleManagement.Read.Directory"]


# ------------------------------------------------------------------ classification
def test_a_200_proves_the_read_is_permitted():
    assert pp.classify_probe(200) == pp.PROBE_PERMITTED


def test_only_a_403_means_the_permission_is_missing():
    assert pp.classify_probe(403, "Forbidden", "required scopes are missing") == pp.PROBE_DENIED


def test_a_licence_error_is_not_a_permission_error():
    """Graph reports a missing P2 as a 400 with a message, not a 403."""
    verdict = pp.classify_probe(
        400, "AadPremiumLicenseRequired",
        "The tenant needs to have Microsoft Entra ID P2 or Microsoft Entra ID Governance license.")
    assert verdict == pp.PROBE_UNLICENSED


def test_a_403_that_says_licence_is_a_licence_problem():
    """Lifecycle workflows answer a license gap with 403, the same status as a real denial.

    Read as a denial it told operators to grant LifecycleWorkflows.Read.All — which they
    already held — instead of saying the tenant is not licensed.
    """
    verdict = pp.classify_probe(
        403, "",
        "Insufficient license to complete this operation. User workflows require an Entra ID "
        "Governance license.")
    assert verdict == pp.PROBE_UNLICENSED


def test_a_malformed_query_is_inconclusive_not_a_denial():
    verdict = pp.classify_probe(
        400, "Request_UnsupportedQuery", "This resource requires a minimum page size of 20.")
    assert verdict == pp.PROBE_INCONCLUSIVE


def test_throttling_and_outages_are_inconclusive():
    assert pp.classify_probe(429) == pp.PROBE_INCONCLUSIVE
    assert pp.classify_probe(503) == pp.PROBE_INCONCLUSIVE


# ------------------------------------------------------------------ domain verdicts
def test_a_malformed_probe_never_blinds_a_domain_the_token_can_read():
    """The regression that mattered.

    `roleDefinitions` rejects `$top` outright, so the probe failed on every tenant and the
    privileged-access pillar was reported unpermitted on first collection.
    """
    probe = {"roles": {"status": 400, "verdict": pp.PROBE_INCONCLUSIVE,
                       "code": "Request_UnsupportedQuery",
                       "message": "This resource requires a minimum page size of 20."}}
    out = pp.evaluate_domains(ROLE_SCOPES, probe=probe)
    assert out["roles"]["ok"] is True
    assert out["roles"]["missing"] == []


def test_a_403_blinds_the_domain_even_when_the_claim_looked_fine():
    probe = {"roles": {"status": 403, "verdict": pp.PROBE_DENIED, "code": "Forbidden",
                       "message": "Attempted to perform an unauthorized operation."}}
    out = pp.evaluate_domains(ROLE_SCOPES, probe=probe)
    assert out["roles"]["ok"] is False
    assert "403" in out["roles"]["reason"]


def test_a_200_clears_a_domain_the_claim_thought_was_missing():
    probe = {"roles": {"status": 200, "verdict": pp.PROBE_PERMITTED, "code": "", "message": ""}}
    out = pp.evaluate_domains([], probe=probe)
    assert out["roles"]["ok"] is True


def test_a_licence_gap_is_reported_as_a_licence_not_a_missing_scope():
    """Telling someone to grant a scope they already hold sends them in a circle."""
    probe = {"pim": {"status": 400, "verdict": pp.PROBE_UNLICENSED,
                     "code": "AadPremiumLicenseRequired",
                     "message": "The tenant needs to have Microsoft Entra ID P2."}}
    out = pp.evaluate_domains(ROLE_SCOPES, probe=probe)
    assert out["pim"]["licence_blocked"] is True
    assert "P2" in out["pim"]["licence_reason"]
    # The permission verdict is untouched: consent is not the blocker.
    assert out["pim"]["ok"] is True
    assert out["pim"]["missing"] == []


def test_a_domain_the_probe_did_not_reach_falls_back_to_the_claim():
    out = pp.evaluate_domains(ROLE_SCOPES, probe={})
    assert out["roles"]["ok"] is True
    assert out["risk"]["ok"] is False  # genuinely not granted


def test_the_verdict_is_recorded_so_the_reason_can_be_shown():
    probe = {"risk": {"status": 403, "verdict": pp.PROBE_DENIED, "code": "Forbidden",
                      "message": "required scopes are missing in the token."}}
    out = pp.evaluate_domains(ROLE_SCOPES, probe=probe)
    assert out["risk"]["probe_status"] == 403
    assert out["risk"]["probe_verdict"] == pp.PROBE_DENIED


# ------------------------------------------------------------------ probe URLs
def test_no_probe_url_asks_roledefinitions_to_page():
    """`$top` is rejected by this collection in every form we tried."""
    assert "$top" not in pp._PROBE_URLS["roles"]  # noqa: SLF001 - pinning the fixed URL


def test_every_domain_requirement_has_a_probe_url():
    for domain in pp.DOMAIN_REQUIREMENTS:
        assert domain in pp._PROBE_URLS, f"{domain} has no live probe"  # noqa: SLF001
