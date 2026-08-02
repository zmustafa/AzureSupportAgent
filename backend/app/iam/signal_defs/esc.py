"""Escalation-path signals — the heaviest unbuilt pillar (18) until now.

The question is not "who is privileged" but "who can *become* privileged". Almost every tenant
has a short path from an ordinary contributor to Owner, and almost none of them go through a role
called Owner — they go through a VM's managed identity, a Key Vault control-plane right, or a
federated credential nobody reviewed.

Every signal here is gated on the graph having actually been built with the inputs it needs. An
escalation map that could not see managed identities finding "no escalation paths" is the single
most dangerous false negative in this product, because it reads as an all-clear on the exact
thing the reader came to check.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.iam import escalation
from app.iam.signals import Finding, SignalContext, SignalSpec

# A path this short from an ordinary principal is not a "path", it is a permission.
_SHORT_PATH = 2


def _graph(ctx: SignalContext) -> dict[str, Any]:
    ctx.require(
        bool(ctx.rows),
        "No access rows have been collected, so escalation paths cannot be computed.",
    )
    return ctx.escalation()


def _identities_known(ctx: SignalContext) -> bool:
    return ctx.collector_ran("ArgManagedIdentities")


def _already_tier0(graph: dict[str, Any]) -> set[str]:
    """Node ids of principals that ALREADY hold full control.

    They cannot escalate — every path they have is a permission they already exercise. Reporting
    them buries every real finding under the tenant's entire administrator list, which is how a
    findings screen stops being read."""
    return {n["id"] for n in graph.get("nodes", []) if n.get("alreadyTier0")}


# --------------------------------------------------------------------------- paths to tier 0
def _escalation_to_owner(ctx: SignalContext) -> list[Finding]:
    g = _graph(ctx)
    owners = _already_tier0(g)
    out: list[Finding] = []
    for path in g.get("paths", []):
        if path["from"] in owners:
            continue
        hops = path.get("hops", [])
        length = path["length"]
        severity = "critical" if length <= _SHORT_PATH else "error"
        chain = " → ".join(h["targetLabel"] for h in hops)
        out.append(
            Finding(
                signal_id="esc.escalation_to_owner",
                title=f"{path['fromLabel']} can become an Owner in {length} step(s)",
                severity=severity,
                pillar="esc",
                object_kind="principal",
                subject=path["from"],
                subject_label=path["fromLabel"],
                detail=(
                    f"{path['fromLabel']} does not hold Owner, but can reach full control in "
                    f"{length} step(s): {chain}. Lowest confidence in the chain: "
                    f"{path['min_confidence']}."
                ),
                count=length,
                evidence={
                    "hops": [
                        {"primitive": h["primitive"], "confidence": h["confidence"], "reason": h["reason"]}
                        for h in hops
                    ],
                    "min_confidence": path["min_confidence"],
                },
                remediation=(
                    "Remove the assignment that provides the first hop, or move it to a PIM-eligible "
                    "assignment so the capability is not standing."
                ),
                frameworks=("NIST:AC-6", "MCSB:PA-1", "CIS-Azure:1.23"),
            )
        )
    return out


def _identity_hijack_available(ctx: SignalContext) -> list[Finding]:
    """The most common real finding in every tenant: a Contributor can run code on a resource
    whose managed identity holds more than the Contributor does."""
    ctx.require(
        _identities_known(ctx),
        "Managed identities were not collected, so identity-hijack paths cannot be detected. "
        "This is usually the most common escalation path in a tenant.",
    )
    g = _graph(ctx)
    owners = _already_tier0(g)
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in g.get("edges", []):
        if e["source"] in owners:
            continue
        if str(e["data"].get("primitive", "")).startswith("identity_hijack") or \
                e["data"].get("primitive") == "automation_runas":
            by_source[e["source"]].append(e)

    labels = {n["id"]: n["label"] for n in g.get("nodes", [])}
    out: list[Finding] = []
    for source, edges in by_source.items():
        targets = sorted({labels.get(e["target"], e["target"]) for e in edges})
        out.append(
            Finding(
                signal_id="esc.identity_hijack_available",
                title=f"{labels.get(source, source)} can execute code as another identity",
                severity="error",
                pillar="esc",
                object_kind="principal",
                subject=source,
                subject_label=labels.get(source, source),
                detail=(
                    f"Holds a write/execute right on {len(edges)} resource(s) carrying a managed "
                    f"identity, so it can run code as: {', '.join(targets[:5])}"
                    + (f" and {len(targets) - 5} more." if len(targets) > 5 else ".")
                ),
                count=len(edges),
                evidence={"identities": targets[:20],
                          "primitives": sorted({e["data"]["primitive"] for e in edges})},
                remediation=(
                    "Scope the write right more narrowly, or reduce what the resource's managed "
                    "identity is granted — the identity's roles are the real blast radius."
                ),
                frameworks=("NIST:AC-6", "MCSB:PA-7"),
            )
        )
    return out


def _keyvault_control_to_data(ctx: SignalContext) -> list[Finding]:
    """Key Vault Contributor on a vault is read-every-secret, one access policy away."""
    g = _graph(ctx)
    labels = {n["id"]: n["label"] for n in g.get("nodes", [])}
    owners = _already_tier0(g)
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in g.get("edges", []):
        if e["data"].get("primitive") == "keyvault_pivot" and e["source"] not in owners:
            by_source[e["source"]].append(e)

    return [
        Finding(
            signal_id="esc.keyvault_control_to_data",
            title=f"{labels.get(src, src)} can grant itself Key Vault data access",
            severity="error",
            pillar="esc",
            object_kind="principal",
            subject=src,
            subject_label=labels.get(src, src),
            detail=(
                f"Holds Key Vault control-plane write on {len(edges)} scope(s), so it can add "
                "itself an access policy and then read every secret. Control-plane rights become "
                "data-plane rights."
            ),
            count=len(edges),
            evidence={"scopes": [labels.get(e["target"], e["target"]) for e in edges][:20]},
            remediation=(
                "Switch the vaults to RBAC authorization (`enableRbacAuthorization`), which "
                "removes the access-policy path entirely, and grant data access by role."
            ),
            frameworks=("CIS-Azure:8.4", "NIST:AC-6", "MCSB:PA-8"),
        )
        for src, edges in by_source.items()
    ]


def _escalation_from_guest(ctx: SignalContext) -> list[Finding]:
    """An external account with a path to Owner. Always critical: the credential and its
    lifecycle belong to another organisation."""
    g = _graph(ctx)
    guests = {
        str(r.get("effectivePrincipalId", "")).lower()
        for r in ctx.grants
        if "#ext#" in str(
            r.get("effectivePrincipalUserPrincipalName", "") or r.get("principalUserPrincipalName", "")
        ).lower()
    }
    ctx.require(
        any(
            r.get("effectivePrincipalUserPrincipalName") or r.get("principalUserPrincipalName")
            for r in ctx.grants
        ),
        "No principal names were resolved, so external accounts cannot be identified.",
    )
    out: list[Finding] = []
    for path in g.get("paths", []):
        pid = str(path["from"]).replace("principal::", "")
        if pid not in guests:
            continue
        out.append(
            Finding(
                signal_id="esc.escalation_from_guest",
                title=f"External account {path['fromLabel']} can become an Owner",
                severity="critical",
                pillar="esc",
                object_kind="principal",
                subject=path["from"],
                subject_label=path["fromLabel"],
                detail=(
                    f"An external (B2B) account can reach full control in {path['length']} step(s). "
                    "Their credential and its lifecycle belong to another organisation."
                ),
                count=path["length"],
                evidence={"hops": [h["primitive"] for h in path.get("hops", [])]},
                remediation="Remove the external account's access, or make it PIM-eligible with approval.",
                frameworks=("CIS-Azure:1.3", "NIST:AC-2", "MCSB:PA-4"),
            )
        )
    return out


# --------------------------------------------------------------------------- federated creds
def _federated_loose_subject(ctx: SignalContext) -> list[Finding]:
    """A federated credential turns an external OIDC identity into an Azure principal with no
    secret, no expiry, and no unusual sign-in log entry. A loose subject means anyone who can
    open a pull request — including from a fork — can assume it."""
    ctx.require(
        ctx.collector_ran("FederatedIdentityCredentials"),
        "Federated identity credentials were not collected, so external OIDC trust cannot be checked.",
    )
    out: list[Finding] = []
    for fic in ctx.federated:
        reason = escalation.loose_subject_reason(str(fic.get("subject", "")))
        if not reason:
            continue
        out.append(
            Finding(
                signal_id="esc.fic_loose_subject",
                title=f"Federated credential on {fic.get('identityName', 'an identity')} is too permissive",
                severity="critical",
                pillar="esc",
                object_kind="principal",
                subject=str(fic.get("credentialId") or fic.get("name", "")),
                subject_label=f"{fic.get('identityName', '')} · {fic.get('name', '')}",
                detail=(
                    f"{reason} Issuer: {fic.get('issuer', '(none)')}. "
                    f"Subject: {fic.get('subject', '(empty)')}."
                ),
                evidence={
                    "issuer": fic.get("issuer", ""),
                    "subject": fic.get("subject", ""),
                    "audiences": fic.get("audiences", []),
                    "identityResourceId": fic.get("identityResourceId", ""),
                },
                remediation=(
                    "Pin the subject to a single repository, branch and environment. On GitHub that "
                    "is `repo:<org>/<repo>:ref:refs/heads/<branch>` or "
                    "`repo:<org>/<repo>:environment:<protected environment>`."
                ),
                frameworks=("NIST:AC-2", "MCSB:IM-1"),
            )
        )
    return out


def _federated_unknown_issuer(ctx: SignalContext) -> list[Finding]:
    ctx.require(
        ctx.collector_ran("FederatedIdentityCredentials"),
        "Federated identity credentials were not collected.",
    )
    unknown = [f for f in ctx.federated if escalation.unknown_issuer(str(f.get("issuer", "")))]
    if not unknown:
        return []
    return [
        Finding(
            signal_id="esc.fic_unknown_issuer",
            title=f"{len(unknown)} federated credential(s) trust an unrecognised issuer",
            severity="warning",
            pillar="esc",
            object_kind="tenant",
            subject=ctx.tenant_id,
            subject_label="This tenant",
            detail=(
                "An unrecognised OIDC issuer is not necessarily malicious, but it is unreviewed — "
                "and it can mint tokens for an Azure identity with no secret and no expiry. "
                "Issuers: " + ", ".join(sorted({str(f.get("issuer", "")) for f in unknown})[:5])
            ),
            count=len(unknown),
            evidence={"issuers": sorted({str(f.get("issuer", "")) for f in unknown})[:20]},
            remediation="Confirm each issuer is an approved CI/CD or workload-identity provider.",
            frameworks=("NIST:AC-2", "MCSB:IM-1"),
        )
    ]


# --------------------------------------------------------------------------- managed identities
def _privileged_managed_identity(ctx: SignalContext) -> list[Finding]:
    ctx.require(_identities_known(ctx), "Managed identities were not collected.")
    ids = {k.lower(): v for k, v in (ctx.identities or {}).items()}
    hits: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in ctx.grants:
        if not r.get("roleIsPrivileged"):
            continue
        pid = str(r.get("effectivePrincipalId", "") or r.get("principalId", "")).lower()
        if pid in ids:
            hits[pid].append(r)

    out: list[Finding] = []
    for pid, rows in hits.items():
        ident = ids[pid]
        attached = ident.get("attachedResourceIds") or []
        out.append(
            Finding(
                signal_id="esc.mi_privileged",
                title=f"Managed identity {ident.get('identityName', pid)} holds privileged access",
                severity="error",
                pillar="esc",
                object_kind="principal",
                subject=pid,
                subject_label=str(ident.get("identityName", "") or pid),
                detail=(
                    f"A {ident.get('identityKind', 'managed')} identity holds "
                    f"{sorted({str(r.get('roleName')) for r in rows})[:3]} across {len(rows)} "
                    f"assignment(s). Anyone who can run code on "
                    + (f"{len(attached)} attached resource(s)" if attached else "its attached resource")
                    + " inherits it."
                ),
                count=len(rows),
                evidence={
                    "identityKind": ident.get("identityKind", ""),
                    "attachedResourceIds": attached[:10],
                    "roles": sorted({str(r.get("roleName")) for r in rows})[:10],
                },
                remediation=(
                    "Reduce the identity's roles to what the workload actually calls, and review who "
                    "holds write access to the resources it is attached to."
                ),
                frameworks=("NIST:AC-6", "MCSB:PA-7"),
            )
        )
    return out


def _shared_managed_identity(ctx: SignalContext) -> list[Finding]:
    """One user-assigned identity across several resources: a compromise in dev reaches prod."""
    ctx.require(_identities_known(ctx), "Managed identities were not collected.")
    out: list[Finding] = []
    for pid, ident in (ctx.identities or {}).items():
        if ident.get("identityKind") != "UserAssigned":
            continue
        attached = ident.get("attachedResourceIds") or []
        if len(attached) < 2:
            continue
        groups = {str(r).split("/resourceGroups/")[-1].split("/")[0].lower() for r in attached if "/resourceGroups/" in str(r)}
        if len(groups) < 2:
            continue
        out.append(
            Finding(
                signal_id="esc.mi_shared",
                title=f"User-assigned identity {ident.get('identityName', pid)} is shared across environments",
                severity="warning",
                pillar="esc",
                object_kind="principal",
                subject=str(pid),
                subject_label=str(ident.get("identityName", "") or pid),
                detail=(
                    f"Attached to {len(attached)} resource(s) across {len(groups)} resource groups. "
                    "A compromise of any one of them reaches everything the identity can access."
                ),
                count=len(attached),
                evidence={"attachedResourceIds": attached[:10], "resourceGroups": sorted(groups)[:10]},
                remediation="Give each workload (and each environment) its own identity.",
                frameworks=("NIST:AC-6", "MCSB:PA-7"),
            )
        )
    return out


SIGNALS: list[SignalSpec] = [
    SignalSpec(
        id="esc.escalation_to_owner",
        title="Path to full control",
        pillar="esc", severity="critical", weight=10, object_kind="principal",
        why="A principal that can become an Owner is an Owner; the role list does not show it.",
        remediation="Break the first hop of the chain.",
        frameworks=("NIST:AC-6", "MCSB:PA-1"),
        evaluate=_escalation_to_owner,
    ),
    SignalSpec(
        id="esc.identity_hijack_available",
        title="Code execution as another identity",
        pillar="esc", severity="error", weight=9, object_kind="principal",
        why="Write access to a resource is write access as its managed identity.",
        remediation="Reduce the identity's roles, or narrow who can write to the resource.",
        frameworks=("NIST:AC-6", "MCSB:PA-7"),
        evaluate=_identity_hijack_available,
    ),
    SignalSpec(
        id="esc.keyvault_control_to_data",
        title="Key Vault control plane reaches data plane",
        pillar="esc", severity="error", weight=7, object_kind="principal",
        why="Key Vault Contributor can add itself an access policy and read every secret.",
        remediation="Switch the vault to RBAC authorization.",
        frameworks=("CIS-Azure:8.4", "MCSB:PA-8"),
        evaluate=_keyvault_control_to_data,
    ),
    SignalSpec(
        id="esc.escalation_from_guest",
        title="External account can escalate",
        pillar="esc", severity="critical", weight=8, object_kind="principal",
        why="The credential and its lifecycle belong to another organisation.",
        remediation="Remove the access or make it eligible-with-approval.",
        frameworks=("CIS-Azure:1.3", "NIST:AC-2"),
        evaluate=_escalation_from_guest,
    ),
    SignalSpec(
        id="esc.fic_loose_subject",
        title="Federated credential accepts too much",
        pillar="esc", severity="critical", weight=9, object_kind="principal",
        why="A wildcard or pull-request subject lets any fork assume the identity — no secret, no expiry.",
        remediation="Pin the subject to one repository, branch and protected environment.",
        frameworks=("NIST:AC-2", "MCSB:IM-1"),
        evaluate=_federated_loose_subject,
    ),
    SignalSpec(
        id="esc.fic_unknown_issuer",
        title="Federated credential trusts an unrecognised issuer",
        pillar="esc", severity="warning", weight=5, object_kind="tenant",
        why="An unreviewed OIDC issuer can mint tokens for an Azure identity.",
        remediation="Confirm each issuer is an approved provider.",
        frameworks=("NIST:AC-2",),
        evaluate=_federated_unknown_issuer,
    ),
    SignalSpec(
        id="esc.mi_privileged",
        title="Managed identity holds privileged access",
        pillar="esc", severity="error", weight=7, object_kind="principal",
        why="Its roles are inherited by anyone who can run code on the resource it is attached to.",
        remediation="Reduce the identity's roles to what the workload calls.",
        frameworks=("NIST:AC-6", "MCSB:PA-7"),
        evaluate=_privileged_managed_identity,
    ),
    SignalSpec(
        id="esc.mi_shared",
        title="User-assigned identity shared across environments",
        pillar="esc", severity="warning", weight=4, object_kind="principal",
        why="A compromise in dev reaches prod.",
        remediation="Give each workload and environment its own identity.",
        frameworks=("NIST:AC-6",),
        evaluate=_shared_managed_identity,
    ),
]
