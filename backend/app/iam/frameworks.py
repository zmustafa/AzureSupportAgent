"""Framework mapping — *"which controls does this evidence actually cover?"*

Signals already declare their control references (`CIS-Azure:1.23`, `NIST:AC-6`, `MCSB:PA-1`).
This rolls them up into a per-framework control view for the evidence pack.

The whole value is in the honest direction of the claim. A compliance view that shows a green
tick for a control nobody measured is worse than no compliance view at all — it is the artifact
an auditor relies on, and it will be wrong in the direction of comfort. So:

- a control with **no signal mapped to it** is `not_assessed`, never `pass`;
- a control whose signals were all **blind** this run is `not_measured`, never `pass`;
- a control passes only when at least one signal measured it and none of them produced a finding.

`COVERED_CONTROLS` is deliberately a small, curated set rather than a full framework import. The
product can honestly speak to the identity and access controls it collects for; claiming coverage
of a whole framework because three of its controls are mapped is the same lie in a bigger font.
"""
from __future__ import annotations

from typing import Any

from app.iam import signals

# Control states.
PASS = "pass"
FAIL = "fail"
NOT_MEASURED = "not_measured"
NOT_ASSESSED = "not_assessed"

FRAMEWORKS = ("CIS-Azure", "NIST", "MCSB")

FRAMEWORK_NAMES = {
    "CIS-Azure": "CIS Microsoft Azure Foundations Benchmark",
    "NIST": "NIST SP 800-53 Rev. 5",
    "MCSB": "Microsoft Cloud Security Benchmark",
}

#: Human titles for the controls this product can speak to. A reference that appears on a signal
#: but is missing here still rolls up — it just carries no title, which is visible and fixable.
#: Silently dropping it would make a mapped control vanish from the report.
CONTROL_TITLES: dict[str, str] = {
    "CIS-Azure:1.3": "Guest users are reviewed and restricted",
    "CIS-Azure:1.23": "Custom subscription owner roles are not created",
    "CIS-Azure:3.7": "Anonymous public access to blob containers is disabled",
    "CIS-Azure:3.8": "Shared key authorisation is disabled on storage accounts",
    "CIS-Azure:3.9": "Storage accounts require secure transfer",
    "CIS-Azure:4.1.3": "SQL servers use Entra authentication only",
    "CIS-Azure:8.4": "Key Vault uses RBAC rather than access policies",
    "CIS-Azure:8.5": "Key Vault purge protection is enabled",
    "CIS-Azure:8.6": "Key Vault local authentication is disabled",
    "CIS-Azure:9.4": "App services do not use admin credentials",
    "NIST:AC-2": "Account management",
    "NIST:AC-3": "Access enforcement",
    "NIST:AC-6": "Least privilege",
    "MCSB:IM-1": "Use a centralised identity and authentication system",
    "MCSB:PA-1": "Separate and limit highly privileged users",
    "MCSB:PA-4": "Review and reconcile user access regularly",
    "MCSB:PA-7": "Follow just enough administration",
    "MCSB:PA-8": "Determine access process for cloud provider support",
}


def parse_ref(ref: str) -> tuple[str, str]:
    """`"CIS-Azure:3.8"` → `("CIS-Azure", "3.8")`. An unparseable ref keeps its whole text as the
    control id under an `Other` framework rather than being dropped."""
    text = (ref or "").strip()
    if ":" not in text:
        return ("Other", text)
    family, _, control = text.partition(":")
    return (family.strip(), control.strip())


def covered_controls() -> dict[str, list[str]]:
    """Every control referenced by at least one registered signal, grouped by framework."""
    out: dict[str, set[str]] = {}
    for spec in signals.all_signals():
        for ref in spec.frameworks:
            family, control = parse_ref(ref)
            out.setdefault(family, set()).add(control)
    return {k: sorted(v) for k, v in sorted(out.items())}


def map_results(results: list[Any]) -> dict[str, Any]:
    """Roll evaluated signal results up into per-framework control states.

    ``results`` are ``signals.SignalResult`` objects (spec + findings + measured). The `measured`
    flag is the important input: a control whose only signal could not run is `not_measured`, and
    stating that is the entire point of the exercise."""
    by_control: dict[tuple[str, str], dict[str, Any]] = {}

    for result in results:
        spec = result.spec
        for ref in spec.frameworks:
            key = parse_ref(ref)
            entry = by_control.setdefault(
                key,
                {
                    "framework": key[0],
                    "control": key[1],
                    "title": CONTROL_TITLES.get(f"{key[0]}:{key[1]}", ""),
                    "signals": [],
                    "measured_signals": 0,
                    "findings": 0,
                },
            )
            entry["signals"].append(spec.id)
            if getattr(result, "measured", False):
                entry["measured_signals"] += 1
                entry["findings"] += len(result.findings)

    controls: list[dict[str, Any]] = []
    for entry in by_control.values():
        if entry["measured_signals"] == 0:
            entry["state"] = NOT_MEASURED
        elif entry["findings"] > 0:
            entry["state"] = FAIL
        else:
            entry["state"] = PASS
        entry["signals"] = sorted(set(entry["signals"]))
        controls.append(entry)

    controls.sort(key=lambda c: (c["framework"], _control_sort_key(c["control"])))

    by_framework: dict[str, dict[str, Any]] = {}
    for c in controls:
        fw = by_framework.setdefault(
            c["framework"],
            {
                "framework": c["framework"],
                "name": FRAMEWORK_NAMES.get(c["framework"], c["framework"]),
                "controls": 0, "passing": 0, "failing": 0, "not_measured": 0,
            },
        )
        fw["controls"] += 1
        fw["passing"] += c["state"] == PASS
        fw["failing"] += c["state"] == FAIL
        fw["not_measured"] += c["state"] == NOT_MEASURED

    return {
        "controls": controls,
        "by_framework": sorted(by_framework.values(), key=lambda f: f["framework"]),
        # Said on every render. This is a mapping of the controls the product COLLECTS for, not
        # an assessment against a framework — an auditor reading it as the latter would be
        # relying on coverage that was never claimed.
        "limitations": [
            "This maps the identity and access controls this product collects evidence for. It is "
            "NOT a full assessment against any framework — controls with no signal mapped to them "
            "are absent from this table entirely and must not be read as passing.",
            "A control is only reported as passing when at least one signal measured it and "
            "produced no finding. A control whose signals could not run is 'not_measured'.",
        ],
    }


def _control_sort_key(control: str) -> tuple:
    """Sort `3.8` before `3.10`, and `AC-2` before `AC-6`."""
    parts = []
    for chunk in control.replace("-", ".").split("."):
        parts.append((0, int(chunk), "") if chunk.isdigit() else (1, 0, chunk))
    return tuple(parts)
