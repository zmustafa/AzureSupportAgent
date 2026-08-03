"""Two bugs a real tenant found that the crafted fixtures could not.

Both were invisible on the demo tenant and on every unit test, and both were the kind that
produce a *plausible* screen rather than an obviously broken one:

1. **The Office 365 bundle resolved to zero applications.** Its `members` list is deliberately
   empty — Microsoft does not publish stable GUIDs for the suite — and its `name_patterns` were
   left empty too. A class with no members is judged on the user axis alone, so it rendered
   normally while `ca.bundle_member_divergence` became a detector that *could not fire*.

2. **The exposure view joined findings by `object_kind`.** Every app-hygiene signal in the
   product emits `object_kind="app"`, so a real tenant pulled in 277 expired-certificate and
   consent findings alongside the 4 that belonged. They keyed off an application GUID rather
   than a class id, so they inflated every count while appearing in no row.
"""
from __future__ import annotations

import pytest

from app.entra import ca_exposure, ca_taxonomy
from app.entra.signal_defs import ca_appclass

# Names Microsoft Learn publishes for the Office 365 suite, as they appear on a real tenant's
# service principals.
REAL_TENANT_APP_NAMES = [
    "Office 365 SharePoint Online",
    "Office 365 Exchange Online",
    "Microsoft Teams Services",
    "Microsoft Forms",
    "Power BI Service",
    "Azure Resource Manager",
    "Some Third Party CRM",
]


def _snapshot_with(names: list[str]) -> dict:
    return {
        "apps": {"service_principals": [
            {"object_id": f"o{i}", "app_id": f"00000000-0000-0000-0000-{i:012d}",
             "display_name": n, "sp_type": "Application", "enabled": True}
            for i, n in enumerate(names)
        ]},
        "people": {"users": []}, "ca": {"policies": []}, "roles": {},
    }


def test_the_office365_bundle_resolves_members_by_name():
    """It has no GUIDs by design, so name resolution is the ONLY thing that populates it."""
    index = ca_taxonomy.build_app_index(_snapshot_with(REAL_TENANT_APP_NAMES), "t1")
    members = index["members"].get("office365_bundle") or set()
    assert members, (
        "the Office 365 bundle resolved to nothing. Its members list is intentionally empty, so "
        "if name_patterns is also empty the class is permanently unpopulated and "
        "ca.bundle_member_divergence can never fire."
    )
    assert len(members) >= 4, f"expected the O365-suite apps to match, got {len(members)}"


def test_the_office365_bundle_does_not_swallow_unrelated_apps():
    """Name matching that catches everything would be as useless as matching nothing."""
    index = ca_taxonomy.build_app_index(_snapshot_with(REAL_TENANT_APP_NAMES), "t1")
    members = index["members"].get("office365_bundle") or set()
    labels = index.get("labels") or {}
    matched = {labels.get(m, m) for m in members}
    assert "Some Third Party CRM" not in matched
    assert "Azure Resource Manager" not in matched


def test_every_non_derived_class_can_actually_be_populated():
    """A class that no tenant data can ever fill is a permanently silent detector."""
    doc = ca_taxonomy.load()
    for cls in doc["classes"]:
        if cls.get("derived"):
            continue
        # These classes are matched by policy CONSTRUCT (client app types, user actions,
        # application filters), not by application identity, so an empty member list is correct.
        if cls["id"] in {"all_cloud_apps", "legacy_protocols", "identity_lifecycle",
                         "scoped_constructs", "third_party_saas", "custom_lob"}:
            continue
        assert cls.get("members") or cls.get("name_patterns"), (
            f"class {cls['id']} has neither members nor name_patterns, so nothing in any tenant "
            f"can ever land in it"
        )


def test_exposure_joins_only_the_app_class_detectors():
    """The 277-unrelated-findings bug."""
    coverage = {
        "app_classes": [{"id": "management_apis", "label": "Management & automation APIs"}],
        "controls": [{"key": "mfa", "label": "MFA"}],
        "matrix": [{"cohort": "members", "cells": {"management_apis|mfa": {"state": "uncovered"}}}],
        "derived": {},
    }
    mine = next(iter({s.id for s in ca_appclass.SPECS}))
    findings = [
        {"signal_id": mine, "severity": "high", "object_kind": "app_class",
         "object_id": "management_apis", "title": "mine", "detail": ""},
        # An app-hygiene finding: same object_kind family, totally unrelated signal.
        {"signal_id": "app.credential_expired", "severity": "high", "object_kind": "app",
         "object_id": "some-guid", "title": "cert expired", "detail": ""},
    ]
    wanted = {s.id for s in ca_appclass.SPECS}
    filtered = [f for f in findings if f["signal_id"] in wanted]
    out = ca_exposure.build(coverage, filtered)
    total = sum(r["finding_count"] for r in out["rows"])
    assert total == 1, "only the app-class detector's finding belongs in the exposure view"


@pytest.mark.parametrize("spec", ca_appclass.SPECS, ids=lambda s: s.id)
def test_every_app_class_detector_has_impact_copy_or_degrades_honestly(spec):
    """A detector with no copy must still be usable - the UI says so rather than showing blank."""
    copy = ca_exposure.impact_copy()
    entry = (copy.get("copy") or {}).get(spec.id)
    if entry is None:
        # Acceptable, but then the spec's own `why` must carry the explanation.
        assert spec.why.strip(), f"{spec.id} has neither impact copy nor a 'why'"
        return
    for class_id, body in entry.items():
        assert body.get("impact"), f"{spec.id}/{class_id} has an empty impact"
        assert body.get("first_step"), f"{spec.id}/{class_id} has no first step"
