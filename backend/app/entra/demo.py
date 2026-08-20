"""Synthetic Entra tenant so the whole product area works offline.

Deliberately *not* a healthy tenant: the demo data triggers a representative spread of
signals across every pillar, including the ones that are hardest to reason about (a
break-glass account captured by a policy, an exclusion that defeats a control, a service
principal that can grant itself permissions). A demo that scores 98/100 demonstrates
nothing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.entra import cache, model
DEMO_TENANT = "demo-entra-tenant"


def _iso(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _at(days: float, hour: int) -> str:
    """A timestamp at a fixed UTC hour, so out-of-hours demo findings do not depend on when
    the demo happens to be seeded."""
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(
        hour=hour, minute=15, second=0, microsecond=0).isoformat()


def _user(uid: str, upn: str, name: str, **kw: Any) -> dict[str, Any]:
    base = {
        "id": uid, "upn": upn, "display_name": name, "mail": upn, "user_type": "Member",
        "enabled": True, "created_at": _iso(-800), "department": "IT", "company_name": "Contoso",
        "job_title": "Engineer", "employee_id": "", "usage_location": "GB",
        "on_prem_synced": False, "extension_attributes": {}, "external_user_state": "",
        "external_state_changed_at": "", "licence_count": 1,
        "last_signin": _iso(-2), "last_noninteractive_signin": _iso(-1), "signin_known": True,
        "mfa_registered": True, "mfa_capable": True, "passwordless_capable": False,
        "sspr_registered": True, "methods": ["microsoftAuthenticatorPush"],
        "phishing_resistant": False, "is_admin_reported": False, "manager_id": "",
    }
    base.update(kw)
    return base


def _group(gid: str, name: str, **kw: Any) -> dict[str, Any]:
    base = {
        "id": gid, "display_name": name, "description": "", "group_types": [], "dynamic": False,
        "unified": False, "security_enabled": True, "mail_enabled": False,
        "is_assignable_to_role": False, "membership_rule": "", "membership_rule_state": "",
        "created_at": _iso(-600), "visibility": "Private", "on_prem_synced": False,
        "owner_ids": ["u-alice"], "owners_known": True,
    }
    base.update(kw)
    return base


def _people() -> dict[str, Any]:
    users = [
        _user("u-alice", "alice@contoso.com", "Alice Admin", phishing_resistant=True,
              methods=["fido2SecurityKey", "microsoftAuthenticatorPush"], job_title="Platform Lead"),
        _user("u-bob", "bob@contoso.com", "Bob Builder", mfa_registered=False, mfa_capable=False,
              methods=[], phishing_resistant=False),
        _user("u-carol", "carol@contoso.com", "Carol Contractor", methods=["mobilePhone"],
              phishing_resistant=False),
        _user("u-dave", "dave@contoso.com", "Dave Departed", enabled=False, licence_count=2,
              last_signin=_iso(-210)),
        _user("u-erin", "erin@contoso.com", "Erin Engineer", last_signin=_iso(-260)),
        _user("u-frank", "frank@contoso.com", "Frank Fresh", last_signin="", created_at=_iso(-95)),
        _user("u-bg1", "bg-emergency-01@contoso.com", "Break Glass 01", department="", job_title="",
              mfa_registered=False, mfa_capable=False, methods=[], last_signin="", licence_count=0),
        _user("u-bg2", "bg-emergency-02@contoso.com", "Break Glass 02", department="", job_title="",
              mfa_registered=False, mfa_capable=False, methods=[], last_signin="", licence_count=0),
        _user("u-sync", "Sync_ONPREM_1234@contoso.onmicrosoft.com", "On-Premises Directory Sync",
              department="", job_title="", mfa_registered=False, methods=[], last_signin=""),
        _user("u-svc", "svc-backup@contoso.com", "svc-backup", mfa_registered=False, methods=[],
              last_signin="", department=""),
        _user("g-partner1", "pat@fabrikam.com", "Pat Partner", user_type="Guest",
              external_user_state="Accepted", company_name="", department="", job_title="",
              last_signin=_iso(-400), mfa_registered=False, methods=[]),
        _user("g-partner2", "sam@northwind.com", "Sam Supplier", user_type="Guest",
              external_user_state="PendingAcceptance", external_state_changed_at=_iso(-120),
              company_name="", department="", job_title="", last_signin="", mfa_registered=False,
              methods=[]),
        _user("g-partner3", "kim@northwind.com", "Kim Consultant", user_type="Guest",
              external_user_state="Accepted", company_name="", department="", job_title="",
              last_signin=_iso(-10), mfa_registered=True, methods=["microsoftAuthenticatorPush"]),
    ]
    groups = [
        _group("grp-admins", "Tenant Admins", is_assignable_to_role=True, owner_ids=[], owners_known=True),
        _group("grp-eng", "Engineering", owner_ids=["u-alice"]),
        _group("grp-dyn", "Dynamic Contractors", dynamic=True, group_types=["DynamicMembership"],
               membership_rule="(user.department -eq \"Contract\")", membership_rule_state="Paused",
               owner_ids=[]),
        _group("grp-ca-exclude", "CA Exclusions", owner_ids=[]),
    ]
    guests = sum(1 for u in users if u["user_type"] == "Guest")
    return model.domain_payload("people", {
        "users": users, "groups": groups,
        "capabilities": {"signin_activity": True, "mfa_registration_report": True, "group_owners": True},
        "counts": {"users": len(users), "members": len(users) - guests, "guests": guests,
                   "enabled_members": sum(1 for u in users if u["enabled"] and u["user_type"] == "Member"),
                   "disabled": sum(1 for u in users if not u["enabled"]),
                   "groups": len(groups), "role_assignable_groups": 1},
    }, item_count=len(users) + len(groups))


def _roles() -> dict[str, Any]:
    definitions = [
        {"id": "rd-ga", "template_id": "62e90394-69f5-4237-9190-012177145e10",
         "display_name": "Global Administrator", "is_built_in": True, "is_enabled": True,
         "ms_privileged": True, "tier": "tier0", "privileged": True},
        {"id": "rd-pra", "template_id": "e8611ab8-c189-46e8-94e1-60213ab1f814",
         "display_name": "Privileged Role Administrator", "is_built_in": True, "is_enabled": True,
         "ms_privileged": True, "tier": "tier0", "privileged": True},
        {"id": "rd-appadmin", "template_id": "9b895d92-2cd3-44c7-9d02-a6ac2d5ea5c3",
         "display_name": "Application Administrator", "is_built_in": True, "is_enabled": True,
         "ms_privileged": True, "tier": "tier1", "privileged": True},
        {"id": "rd-sync", "template_id": "d29b2b05-8046-44ba-8758-1e26182fcf32",
         "display_name": "Directory Synchronization Accounts", "is_built_in": True, "is_enabled": True,
         "ms_privileged": True, "tier": "tier0", "privileged": True},
        {"id": "rd-reader", "template_id": "88d8e3e3-8f55-4a1e-953a-9b9898b8876b",
         "display_name": "Directory Readers", "is_built_in": True, "is_enabled": True,
         "ms_privileged": False, "tier": "tier2", "privileged": False},
    ]

    def _a(aid: str, role: str, pid: str, name: str, ptype: str = "User", **kw: Any) -> dict[str, Any]:
        meta = next(d for d in definitions if d["id"] == role)
        base = {
            "id": aid, "role_id": role, "role_name": meta["display_name"], "role_tier": meta["tier"],
            "role_privileged": meta["privileged"], "principal_id": pid, "principal_type": ptype,
            "principal_name": name, "principal_upn": "", "principal_user_type": "",
            "principal_app_id": "", "principal_enabled": True, "scope": "/",
            "assignment_kind": "active", "source": "direct", "activated": False,
            "permanent": True, "end": "", "permanence_known": True,
        }
        base.update(kw)
        return base

    assignments = [
        _a("as-1", "rd-ga", "u-alice", "Alice Admin"),
        _a("as-2", "rd-ga", "u-bg1", "Break Glass 01"),
        _a("as-3", "rd-ga", "u-bg2", "Break Glass 02"),
        _a("as-4", "rd-pra", "u-alice", "Alice Admin"),
        _a("as-5", "rd-appadmin", "u-erin", "Erin Engineer"),
        _a("as-6", "rd-sync", "u-sync", "On-Premises Directory Sync"),
        _a("as-7", "rd-ga", "u-sync", "On-Premises Directory Sync"),
        _a("as-8", "rd-appadmin", "sp-legacy", "Legacy Sync Connector", ptype="ServicePrincipal"),
        _a("as-9", "rd-appadmin", "g-partner1", "Pat Partner", principal_user_type="Guest"),
        _a("as-10", "rd-reader", "grp-admins", "Tenant Admins", ptype="Group"),
    ]
    # Eligible assignments: Carol is eligible for a tier-0 role and has never activated it,
    # which is exactly the case PIM exists to surface.
    eligible = [
        _a("el-1", "rd-pra", "u-carol", "Carol Contractor", assignment_kind="eligible",
           permanent=True, end="", member_type="Direct", status="Provisioned"),
        _a("el-2", "rd-appadmin", "u-bob", "Bob Builder", assignment_kind="eligible",
           permanent=False, end=_iso(90), member_type="Direct", status="Provisioned"),
    ]
    group_members = {"grp-admins": ["u-bob", "u-carol"]}
    derived = [
        {**assignments[-1], "id": "as-10:u-bob", "principal_id": "u-bob", "principal_type": "User",
         "principal_name": "", "source": "group", "source_group_id": "grp-admins",
         "source_group_name": "Tenant Admins"},
    ]
    return model.domain_payload("roles", {
        "definitions": definitions, "assignments": assignments, "group_derived": derived,
        "eligible": eligible, "group_members": group_members, "sync_account_ids": ["u-sync"],
        "capabilities": {"pim_schedules": True, "pim_eligibility": True,
                         "permanence_known": True, "pim_licensed": True},
        "counts": {"definitions": len(definitions), "active": len(assignments),
                   "group_derived": len(derived), "eligible": len(eligible),
                   "privileged_active": sum(1 for a in assignments if a["role_privileged"]),
                   "global_admins": 4},
    }, item_count=len(assignments) + len(eligible))


def _pim() -> dict[str, Any]:
    """PIM configuration health, activation history and PIM for Groups.

    Deliberately misconfigured: Global Administrator activates with no approval and no MFA,
    which is the finding an administrator most needs to see.
    """
    def _policy(role_id: str, **kw: Any) -> dict[str, Any]:
        base = {
            "role_id": role_id, "policy_id": f"pol-{role_id}", "scope_id": "/",
            "approval_required": True, "approver_count": 2, "mfa_on_activation": True,
            "auth_context_required": False, "auth_context_value": "",
            "justification_required": True, "ticket_required": False,
            "max_activation_hours": 4.0, "eligibility_expires": True,
            "assignment_expires": True, "notification_recipients": 2, "rules_seen": 12,
        }
        base.update(kw)
        from app.entra.collectors.pim import _score_health

        score, failed = _score_health(base, 8.0)
        return {**base, "score": score, "failed_controls": failed}

    policies = [
        _policy("rd-ga", approval_required=False, mfa_on_activation=False,
                max_activation_hours=24.0, notification_recipients=0),
        _policy("rd-pra", justification_required=False, max_activation_hours=12.0),
        _policy("rd-appadmin"),
    ]
    activations = [
        {"id": "req-1", "action": "selfActivate", "principal_id": "u-alice", "role_id": "rd-ga",
         "justification": "fix", "ticket_number": "", "ticket_system": "",
         "created_at": _at(-3, 10), "status": "Provisioned", "duration_hours": 8.0, "approval_id": ""},
        {"id": "req-2", "action": "selfActivate", "principal_id": "u-erin", "role_id": "rd-appadmin",
         "justification": "Investigating the failed enterprise application provisioning job",
         "ticket_number": "INC-4412", "ticket_system": "ServiceNow",
         "created_at": _at(-10, 14), "status": "Provisioned", "duration_hours": 2.0, "approval_id": "ap-1"},
        {"id": "req-3", "action": "selfActivate", "principal_id": "u-alice", "role_id": "rd-pra",
         "justification": "", "ticket_number": "", "ticket_system": "",
         # 02:00 UTC — outside the default business-hours window.
         "created_at": _at(-5, 2), "status": "Provisioned", "duration_hours": 8.0, "approval_id": ""},
    ]
    group_eligibilities = [
        {"id": "ge-1", "group_id": "grp-eng", "principal_id": "u-bob", "access_id": "member",
         "member_type": "Direct", "status": "Provisioned"},
    ]
    return model.domain_payload("pim", {
        "policies": policies,
        "activations": activations,
        "group_eligibilities": group_eligibilities,
        "capabilities": {"policies": True, "activations": True, "group_pim": True, "licensed": True},
        "counts": {"policies": len(policies), "activations": len(activations),
                   "self_activations": len(activations),
                   "group_eligibilities": len(group_eligibilities), "managed_group_ids": 1},
    }, item_count=len(policies) + len(activations))


def _activations() -> dict[str, Any]:
    """Activation sessions for the demo tenant.

    Deliberately spans both planes and both fidelities: a rich Entra request that carries a
    justification, a bare schedule instance that cannot, and Azure elevations at subscription
    and management-group scope. That mix is what the screen has to explain honestly, and it
    is what a live tenant almost always looks like.
    """
    def _session(**kw: Any) -> dict[str, Any]:
        from app.entra.collectors.activations import session as make

        return make(**kw)

    rows = [
        # Tier-0, at 02:00, no reason given — the worst combination, and the one the
        # out-of-hours and no-justification signals both exist for.
        _session(sid="entra:req:d1", plane="entra", source="entra_request",
                 principal_id="u-alice", principal_name="Alice Admin",
                 principal_upn="alice@contoso.example", role_id="rd-ga",
                 role_name="Global Administrator", scope_type="directory", scope_id="/",
                 action="selfActivate", status="Provisioned",
                 requested_at=_at(-5, 2), start=_at(-5, 2), end=_at(-5, 10),
                 justification="", requestor_id="u-alice"),
        _session(sid="entra:req:d2", plane="entra", source="entra_request",
                 principal_id="u-erin", principal_name="Erin Engineer",
                 principal_upn="erin@contoso.example", role_id="rd-appadmin",
                 role_name="Application Administrator", scope_type="directory", scope_id="/",
                 action="selfActivate", status="Provisioned",
                 requested_at=_at(-10, 14), start=_at(-10, 14), end=_at(-10, 16),
                 justification="Investigating the failed enterprise application provisioning job",
                 ticket_number="INC-4412", ticket_system="ServiceNow", requestor_id="u-erin"),
        # The fallback source: exact window, no justification field at all.
        _session(sid="entra:inst:d3", plane="entra", source="entra_instance",
                 principal_id="u-bob", principal_name="Bob Builder",
                 principal_upn="bob@contoso.example", role_id="rd-pra",
                 role_name="Privileged Role Administrator", scope_type="directory",
                 scope_id="/", action="activated", status="Provisioned",
                 start=_at(-2, 11), end=_at(-2, 19), detail_known=False),
        _session(sid="azure:req:d4", plane="azure", source="azure_request",
                 principal_id="u-erin", principal_name="Erin Engineer",
                 role_id="b24988ac", role_name="Contributor", scope_type="subscription",
                 scope_id="/subscriptions/00000000-0000-0000-0000-00000000c0de",
                 scope_name="Contoso Production", subscription_id="00000000-0000-0000-0000-00000000c0de",
                 action="SelfActivate", status="Provisioned",
                 requested_at=_at(-1, 9), start=_at(-1, 9), end=_at(-1, 17),
                 justification="deploy", requestor_id="u-erin"),
        # Management-group scope: one elevation across every subscription beneath it.
        _session(sid="azure:req:d5", plane="azure", source="azure_request",
                 principal_id="u-alice", principal_name="Alice Admin",
                 role_id="8e3af657", role_name="Owner", scope_type="managementGroup",
                 scope_id="/providers/Microsoft.Management/managementGroups/contoso-root",
                 scope_name="Contoso Root", action="SelfActivate", status="Provisioned",
                 requested_at=_at(-4, 13), start=_at(-4, 13), end=_at(-4, 21),
                 justification="Emergency change for INC-4390 — restoring the shared firewall",
                 ticket_number="INC-4390", ticket_system="ServiceNow", requestor_id="u-alice"),
        # A third party granted this one, and it never provisioned.
        _session(sid="azure:req:d6", plane="azure", source="azure_request",
                 principal_id="u-bob", principal_name="Bob Builder",
                 role_id="b24988ac", role_name="Contributor", scope_type="subscription",
                 scope_id="/subscriptions/00000000-0000-0000-0000-00000000c0de",
                 scope_name="Contoso Production", subscription_id="00000000-0000-0000-0000-00000000c0de",
                 action="AdminAssign", status="Failed",
                 requested_at=_at(-6, 15), start=_at(-6, 15), end=_at(-6, 23),
                 justification="cover", requestor_id="u-alice"),
    ]
    return model.domain_payload("activations", {
        "sessions": rows,
        "lookback_days": 90,
        "capabilities": {"entra_requests": True, "entra_instances": True,
                         "azure_requests": True, "azure_subscriptions": 2,
                         "azure_reason": "", "detail": True},
        "counts": {
            "sessions": len(rows),
            "granted": sum(1 for r in rows if r["granted"]),
            "attempts": sum(1 for r in rows if not r["granted"]),
            "entra": sum(1 for r in rows if r["plane"] == "entra"),
            "azure": sum(1 for r in rows if r["plane"] == "azure"),
            "tier0": sum(1 for r in rows if r["tier"] == "tier0"),
            "self_service": sum(1 for r in rows if r["self_service"]),
            "no_justification": sum(1 for r in rows
                                    if r["detail_known"] and not r["justification"]),
        },
    }, item_count=len(rows))


def _apps() -> dict[str, Any]:
    def _perm(name: str, tier: str, **flags: bool) -> dict[str, Any]:
        base = {"mail": False, "files": False, "chat": False, "consent_grant": False, "directory_write": False}
        base.update(flags)
        return {"permission": name, "permission_id": f"pid-{name}", "resource": "Microsoft Graph",
                "resource_app_id": "00000003-0000-0000-c000-000000000000", "kind": "application",
                "tier": tier, "flags": base, "granted_at": _iso(-300), "known": True}

    sps = [
        {"object_id": "sp-legacy", "app_id": "app-legacy", "display_name": "Legacy Sync Connector",
         "sp_type": "Application", "enabled": True, "assignment_required": False, "publisher_name": "",
         "verified_publisher": "", "app_owner_tenant_id": "demo-entra-tenant", "is_first_party": False,
         "is_external": False, "disabled_by_microsoft": "", "sso_mode": "", "reply_urls": [],
         "reply_url_risks": [], "credentials": [], "owner_ids": [], "owners_known": True,
         "granted_app_permissions": [
             _perm("Directory.ReadWrite.All", "critical", directory_write=True, consent_grant=True),
             _perm("Mail.Read", "high", mail=True),
         ],
         "granted_delegated": [], "assigned_principals": 0, "assignment_known": True,
         "provisioning_jobs": [], "orphaned": False},
        {"object_id": "sp-report", "app_id": "app-report", "display_name": "Reporting Bot",
         "sp_type": "Application", "enabled": True, "assignment_required": False, "publisher_name": "",
         "verified_publisher": "Contoso Ltd", "app_owner_tenant_id": "demo-entra-tenant",
         "is_first_party": False, "is_external": False, "disabled_by_microsoft": "", "sso_mode": "",
         "reply_urls": [], "reply_url_risks": [], "credentials": [], "owner_ids": ["u-alice"],
         "owners_known": True,
         "granted_app_permissions": [_perm("Directory.Read.All", "medium")],
         "granted_delegated": [{"id": "og-1", "resource": "Microsoft Graph", "resource_id": "sp-graph",
                                "consent_type": "AllPrincipals", "principal_id": "",
                                "scopes": ["Files.Read.All", "User.Read"], "max_tier": "high"}],
         "assigned_principals": 120, "assignment_known": True, "orphaned": False,
         "provisioning_jobs": [{"id": "job-1", "template": "scim", "code": "Quarantine",
                                "quarantine": True, "last_execution": _iso(-9)}]},
        {"object_id": "sp-orphan", "app_id": "app-orphan", "display_name": "Retired Integration",
         "sp_type": "Application", "enabled": True, "assignment_required": False, "publisher_name": "",
         "verified_publisher": "", "app_owner_tenant_id": "demo-entra-tenant", "is_first_party": False,
         "is_external": False, "disabled_by_microsoft": "", "sso_mode": "", "reply_urls": [],
         "reply_url_risks": [], "credentials": [], "owner_ids": [], "owners_known": True,
         "granted_app_permissions": [_perm("Sites.Read.All", "high", files=True)],
         "granted_delegated": [], "assigned_principals": 0, "assignment_known": True,
         "provisioning_jobs": [], "orphaned": True},
    ]
    apps = [
        {"object_id": "ao-legacy", "app_id": "app-legacy", "display_name": "Legacy Sync Connector",
         "created_at": _iso(-900), "sign_in_audience": "AzureADMultipleOrgs", "multi_tenant": True,
         "identifier_uris": [], "redirect_uris": [{"uri": "https://legacy.contoso.com/*", "type": "web",
                                                   "risk": "wildcard"}],
         "notes": "", "tags": [],
         "credentials": [
             {"id": "c1", "display_name": "prod-secret", "kind": "secret", "start": _iso(-800),
              "end": _iso(-20), "days_left": -20, "lifetime_days": 780, "expired": True},
             {"id": "c2", "display_name": "prod-secret-2", "kind": "secret", "start": _iso(-500),
              "end": _iso(25), "days_left": 25, "lifetime_days": 525, "expired": False},
             {"id": "c3", "display_name": "backup", "kind": "secret", "start": _iso(-400),
              "end": _iso(300), "days_left": 300, "lifetime_days": 700, "expired": False},
         ],
         "requested_permissions": [], "app_roles": 0, "owner_ids": [], "owners_known": True,
         "federated_credentials": [
             {"id": "fic-1", "name": "unknown-ci", "issuer": "https://ci.example.invalid",
              "subject": "*", "audiences": ["api://AzureADTokenExchange"], "trusted": False,
              "wildcard_subject": True},
         ],
         "fic_known": True, "sp_object_id": "sp-legacy", "verified_publisher": ""},
        {"object_id": "ao-report", "app_id": "app-report", "display_name": "Reporting Bot",
         "created_at": _iso(-300), "sign_in_audience": "AzureADMyOrg", "multi_tenant": False,
         "identifier_uris": [], "redirect_uris": [{"uri": "https://reports.contoso.com/cb", "type": "web",
                                                   "risk": ""}],
         "notes": "", "tags": [],
         "credentials": [{"id": "c4", "display_name": "cert", "kind": "certificate", "start": _iso(-100),
                          "end": _iso(60), "days_left": 60, "lifetime_days": 160, "expired": False}],
         "requested_permissions": [], "app_roles": 0, "owner_ids": ["u-alice"], "owners_known": True,
         "federated_credentials": [], "fic_known": True, "sp_object_id": "sp-report",
         "verified_publisher": "Contoso Ltd"},
    ]
    # Risk scores are computed with the same function the collector uses, so the demo can
    # never drift away from production behavior.
    from app.entra.collectors.apps import risk_score

    sp_by_object = {s["object_id"]: s for s in sps}
    for app in apps:
        sp = sp_by_object.get(app["sp_object_id"], {})
        app["risk"] = risk_score(app, sp)
    for sp in sps:
        owning = next((a for a in apps if a["sp_object_id"] == sp["object_id"]), {})
        sp["risk"] = risk_score(owning, sp)

    return model.domain_payload("apps", {
        "applications": apps, "service_principals": sps, "permission_catalogue_size": 480,
        "capabilities": {"granted_permissions": True, "delegated_grants": True, "owners": True,
                         "federated_credentials": True, "assignments": True, "provisioning": True},
        "counts": {"applications": len(apps), "service_principals": len(sps), "managed_identities": 0,
                   "first_party": 0, "delegated_grants": 1, "all_principals_grants": 1,
                   "high_risk_apps": sum(1 for a in apps if (a.get("risk") or {}).get("score", 0) >= 60),
                   "provisioning_quarantined": 1},
    }, item_count=len(apps) + len(sps))


def _tenant() -> dict[str, Any]:
    return model.domain_payload("tenant", {
        "tenant": {"id": DEMO_TENANT, "display_name": "Contoso (demo)", "primary_domain": "contoso.com",
                   "domains": [{"name": "contoso.com", "is_default": True, "is_initial": False,
                                "type": "Managed"}],
                   "country": "GB", "created_at": _iso(-2000), "technical_contacts": []},
        "hybrid": {"sync_enabled": True, "last_sync": _iso(-0.02)},
        "authorization_policy": {
            "present": True, "guest_user_role_id": "a0b1b346-4d3e-4e8b-98f8-753987be4970",
            "allow_invites_from": "everyone", "allow_email_verified_users_to_join": False,
            "block_msol_powershell": False,
            "user_consent_policies": ["ManagePermissionGrantsForSelf.microsoft-user-default-legacy"],
            "user_consent_unrestricted": True, "user_consent_restricted_low_risk": False,
            "user_consent_disabled": False, "allowed_to_create_apps": True,
            "allowed_to_create_security_groups": True, "allowed_to_read_other_users": True,
        },
        "authentication_methods_policy": {
            "present": True, "registration_campaign": False,
            "methods": {"Sms": True, "Voice": True, "Fido2": True, "MicrosoftAuthenticator": True},
            "sms_enabled": True, "voice_enabled": True, "fido2_enabled": True,
            "authenticator_enabled": True, "tap_enabled": False, "email_otp_enabled": False,
        },
        "admin_consent_policy": {"is_enabled": False, "notify_reviewers": False, "reviewers": 0,
                                 "present": True},
        "cross_tenant_default": {"present": True, "inbound_trust_mfa": False,
                                 "inbound_trust_compliant_device": False,
                                 "b2b_inbound_users": "allowed", "b2b_inbound_apps": "allowed",
                                 "automatic_redemption_inbound": False},
        "permission_grant_policies": [],
    }, item_count=1)


def _ca() -> dict[str, Any]:
    def _policy(pid: str, name: str, state: str, **kw: Any) -> dict[str, Any]:
        conditions = {
            "include_users": ["All"], "exclude_users": [], "include_groups": [], "exclude_groups": [],
            "include_roles": [], "exclude_roles": [], "include_guests": [], "exclude_guests": [],
            "include_apps": ["All"], "exclude_apps": [], "user_actions": [], "auth_contexts": [],
            "client_app_types": ["all"], "platforms_include": [], "platforms_exclude": [],
            "locations_include": [], "locations_exclude": [], "device_filter_mode": "",
            "device_filter_rule": "", "sign_in_risk": [], "user_risk": [],
            "service_principal_risk": [], "client_applications": {
                "include_service_principals": [], "exclude_service_principals": []},
        }
        conditions.update(kw.pop("conditions", {}))
        grant = {"operator": "OR", "controls": ["mfa"], "custom_controls": [], "terms_of_use": [],
                 "auth_strength_id": "", "auth_strength_name": "", "present": True}
        grant.update(kw.pop("grant", {}))
        session = {"sign_in_frequency": False, "sign_in_frequency_value": None,
                   "sign_in_frequency_type": "", "persistent_browser": False,
                   "persistent_browser_mode": "", "app_enforced_restrictions": False,
                   "cloud_app_security": False, "continuous_access_evaluation": "", "present": False}
        session.update(kw.pop("session", {}))
        return {"id": pid, "display_name": name, "state": state, "created_at": _iso(-400),
                "modified_at": kw.pop("modified_at", _iso(-30)),
                "conditions": conditions, "grant": grant, "session": session}

    policies = [
        _policy("ca-1", "Require MFA for all users", "enabled",
                conditions={"exclude_users": ["u-bg1", "u-bg2"], "exclude_groups": ["grp-ca-exclude"]}),
        # Deliberately captures a break-glass account that has no MFA method -> lockout risk.
        _policy("ca-2", "Require MFA for admins", "enabled",
                conditions={"include_users": [], "include_roles": ["62e90394-69f5-4237-9190-012177145e10"]}),
        _policy("ca-3", "Block legacy authentication", "enabledForReportingButNotEnforced",
                conditions={"client_app_types": ["exchangeActiveSync", "other"]},
                grant={"controls": ["block"]}, modified_at=_iso(-260)),
        _policy("ca-4", "Legacy pilot policy", "disabled"),
    ]
    return model.domain_payload("ca", {
        "policies": policies,
        "named_locations": [{"id": "loc-1", "display_name": "HQ", "kind": "ip", "is_trusted": True,
                             "ip_ranges": ["203.0.113.0/24"], "countries": [],
                             "include_unknown_countries": False}],
        "auth_strengths": [], "auth_contexts": [],
        "group_members": {"grp-ca-exclude": ["u-carol"]},
        "counts": {"policies": len(policies), "enabled": 2, "report_only": 1, "disabled": 1,
                   "named_locations": 1, "auth_strengths": 0},
    }, item_count=len(policies))


def _risk() -> dict[str, Any]:
    """Sign-in aggregates and Identity Protection state.

    The aggregate shape here is the contract the collector produces — raw sign-in rows are
    absent by construction, in the demo exactly as in a live tenant."""
    days = []
    for offset in range(14, 0, -1):
        # A deliberate failure spike five days ago, so the spike detector has something real
        # to find and the chart has a shape worth looking at.
        failure = 900 if offset == 5 else 120
        days.append({"day": _iso(-offset)[:10], "total": 4200 + failure, "success": 4200,
                     "failure": failure, "mfa": 1650})
    return model.domain_payload("risk", {
        "signins": {
            "window_start": _iso(-14), "window_end": _iso(0), "lookback_days": 14,
            "sampled": False,
            "total": sum(d["total"] for d in days),
            "success": sum(d["success"] for d in days),
            "failure": sum(d["failure"] for d in days),
            "interactive": 41_000, "mfa_challenged": 23_100,
            "failure_rate": round(sum(d["failure"] for d in days)
                                  / sum(d["total"] for d in days), 4),
            "by_day": days,
            "by_app": [
                {"app_id": "app-crm", "display_name": "Contoso CRM", "total": 18_400,
                 "failure": 210, "users": 320, "failure_rate": 0.011, "last_seen": _iso(-0.1)},
                {"app_id": "app-legacy", "display_name": "Legacy Reporting", "total": 2_100,
                 "failure": 890, "users": 14, "failure_rate": 0.424, "last_seen": _iso(-0.5)},
            ],
            "by_user_top": [
                {"user_id": "u-bob", "upn": "bob@contoso.com", "total": 640, "failure": 12,
                 "last_seen": _iso(-0.2)},
            ],
            "by_client_app": {"Browser": 38_200, "Mobile Apps and Desktop clients": 21_400,
                              "Exchange ActiveSync": 780, "Other clients": 210},
            "by_country": {"GB": 52_000, "US": 6_800, "NG": 120, "RU": 40},
            "by_ca_result": {"success": 44_000, "notApplied": 12_000, "failure": 900,
                             "reportOnlyFailure": 412},
            "by_failure_code": [
                {"code": "50126", "meaning": "Invalid username or password", "count": 1_640,
                 "users": 96, "sample": "Invalid username or password."},
                {"code": "500121", "meaning": "Multi-factor authentication denied or timed out "
                                              "by the user", "count": 22, "users": 2,
                 "sample": "Authentication failed during strong authentication request."},
                {"code": "53003", "meaning": "Blocked by Conditional Access", "count": 310,
                 "users": 41, "sample": "Access has been blocked by Conditional Access policies."},
            ],
            "legacy": [
                {"protocol": "Exchange ActiveSync", "total": 780, "success": 612, "users": 9,
                 "apps": 2, "last_success": _iso(-0.3)},
                {"protocol": "Other clients", "total": 210, "success": 0, "users": 0, "apps": 0,
                 "last_success": ""},
            ],
            "legacy_success_users": 9,
            "report_only_impact": [
                {"policy_id": "ca-legacy", "display_name": "Block legacy authentication",
                 "would_block": 412, "would_challenge": 0, "would_pass": 0, "users": 9},
            ],
            "device_compliance": {"compliant": 38_000, "not_compliant": 2_400, "unknown": 20_000},
            "unmanaged_signin_users": [
                {"user_id": "u-alice", "upn": "alice@contoso.com", "count": 34,
                 "last_seen": _iso(-0.4), "device": "ALICE-LAPTOP"},
                {"user_id": "u-erin", "upn": "erin@contoso.com", "count": 8,
                 "last_seen": _iso(-3), "device": "BYOD-4471"},
            ],
        },
        "patterns": [
            {"kind": "password_spray", "key": "198.51.100.24",
             "label": "Password spray from 198.51.100.24",
             "rule": "\u2265 12 distinct users failed with code 50126 (invalid credentials) from "
                     "one IP address in the window",
             "count": 48,
             "evidence": {"ip": "198.51.100.24", "distinct_users": 48, "threshold": 12,
                          "error_code": "50126"}},
            {"kind": "mfa_fatigue", "key": "u-bob",
             "label": "Repeated MFA denials for bob@contoso.com",
             "rule": "\u2265 5 multi-factor prompts denied or timed out (code 500121) by one user "
                     "in the window",
             "count": 14,
             "evidence": {"upn": "bob@contoso.com", "denials": 14, "threshold": 5,
                          "last_seen": _iso(-1), "error_code": "500121"}},
            {"kind": "failure_spike", "key": _iso(-5)[:10],
             "label": f"Sign-in failure spike on {_iso(-5)[:10]}",
             "rule": "Daily failures exceeded 3.0\u00d7 the trailing median for the window (and at "
                     "least 50 failures)",
             "count": 900,
             "evidence": {"day": _iso(-5)[:10], "failures": 900, "median_failures": 120,
                          "factor": 3.0}},
            {"kind": "unmanaged_device_signin", "key": "u-alice",
             "label": "Successful sign-in from a non-compliant device: alice@contoso.com",
             "rule": "A user signed in successfully from a device Intune reports as non-compliant.",
             "count": 34,
             "evidence": {"upn": "alice@contoso.com", "sign_ins": 34, "device": "ALICE-LAPTOP",
                          "last_seen": _iso(-0.4)}},
        ],
        "risky_users": [
            {"id": "u-alice", "upn": "alice@contoso.com", "name": "Alice Admin", "level": "medium",
             "state": "atRisk", "detail": "none", "last_updated": _iso(-4)},
            {"id": "u-bob", "upn": "bob@contoso.com", "name": "Bob Builder", "level": "high",
             "state": "atRisk", "detail": "none", "last_updated": _iso(-11)},
            {"id": "u-erin", "upn": "erin@contoso.com", "name": "Erin Engineer", "level": "low",
             "state": "remediated", "detail": "userPerformedSecuredPasswordReset",
             "last_updated": _iso(-30)},
        ],
        "risk_detections": [
            {"id": "det-1", "type": "unfamiliarFeatures", "level": "medium", "state": "atRisk",
             "user_id": "u-alice", "upn": "alice@contoso.com", "detected_at": _iso(-4),
             "ip": "203.0.113.9", "country": "GB", "activity": "signin"},
            {"id": "det-2", "type": "leakedCredentials", "level": "high", "state": "atRisk",
             "user_id": "u-bob", "upn": "bob@contoso.com", "detected_at": _iso(-11),
             "ip": "198.51.100.24", "country": "NG", "activity": "signin"},
            {"id": "det-3", "type": "anonymizedIPAddress", "level": "medium", "state": "atRisk",
             "user_id": "u-alice", "upn": "alice@contoso.com", "detected_at": _iso(-2),
             "ip": "192.0.2.55", "country": "GB", "activity": "signin"},
        ],
        "detection_counts": {"unfamiliarFeatures": 1, "leakedCredentials": 1,
                             "anonymizedIPAddress": 1},
        "risky_service_principals": [
            {"id": "sp-legacy", "app_id": "app-legacy", "name": "Legacy Sync Connector",
             "level": "high", "state": "atRisk", "detail": "none", "last_updated": _iso(-6),
             "enabled": True},
        ],
        "capabilities": {"signins": True, "risky_users": True, "risk_detections": True,
                         "risky_workload_identities": True, "licensed_p1": True,
                         "licensed_p2": True},
        "thresholds": {"spray_min_users": 12, "fatigue_min_denials": 5, "spike_factor": 3.0,
                       "max_signin_rows": 200_000},
        "counts": {"signins": sum(d["total"] for d in days), "risky_users": 3,
                   "risky_users_high": 1, "unremediated": 2, "risk_detections": 3,
                   "risky_service_principals": 1, "patterns": 4},
    }, item_count=7)


def _governance() -> dict[str, Any]:
    """Access reviews, entitlement and lifecycle — configured, but not well."""
    return model.domain_payload("governance", {
        "reviews": [
            {"id": "rev-guests", "display_name": "Quarterly guest review", "status": "InProgress",
             "created_at": _iso(-120), "last_modified": _iso(-30),
             "scope": {"kind": "guests", "target": "", "query": "/users?$filter=userType eq 'Guest'"},
             "reviewer_count": 1, "self_review": False, "recurrence": "quarterly",
             "auto_apply": False, "default_decision": "Approve", "default_decision_enabled": True,
             "justification_required": False,
             "instances": [{"id": "rev-guests-i1", "status": "InProgress", "start": _iso(-40),
                            "end": _iso(-12)}]},
            {"id": "rev-eng", "display_name": "Engineering group one-off",
             "status": "InProgress", "created_at": _iso(-200), "last_modified": _iso(-200),
             "scope": {"kind": "group", "target": "grp-eng", "query": "/groups/grp-eng/members"},
             "reviewer_count": 2, "self_review": False, "recurrence": "one-off",
             "auto_apply": True, "default_decision": "None", "default_decision_enabled": False,
             "justification_required": True,
             "instances": [{"id": "rev-eng-i1", "status": "InProgress", "start": _iso(-30),
                            "end": _iso(5)}]},
        ],
        "packages": [
            {"id": "pkg-partner", "display_name": "Partner collaboration",
             "description": "Access for Fabrikam collaborators", "catalog_id": "cat-1",
             "hidden": False, "created_at": _iso(-300), "resource_scopes": 3,
             "policies": [
                 {"id": "pol-1", "display_name": "External requestors", "allowed_targets": "AllExternalSubjects",
                  "approval_required": True, "review_required": False, "expires": True},
             ]},
            {"id": "pkg-internal", "display_name": "Internal tooling",
             "description": "Standard engineering tools", "catalog_id": "cat-1", "hidden": False,
             "created_at": _iso(-260), "resource_scopes": 5,
             "policies": [
                 {"id": "pol-2", "display_name": "All employees", "allowed_targets": "AllMemberUsers",
                  "approval_required": False, "review_required": False, "expires": False},
             ]},
        ],
        "assignments": [
            {"id": "asn-1", "package_id": "pkg-partner", "package_name": "Partner collaboration",
             "principal_id": "g-partner1", "principal_name": "Pat Partner",
             "principal_type": "Guest", "state": "Delivered", "expires_at": _iso(9)},
            {"id": "asn-2", "package_id": "pkg-internal", "package_name": "Internal tooling",
             "principal_id": "u-erin", "principal_name": "Erin Engineer",
             "principal_type": "User", "state": "Delivered", "expires_at": ""},
        ],
        "workflows": [
            {"id": "wf-joiner", "display_name": "Onboard pre-hire", "category": "joiner",
             "enabled": True, "scheduling_enabled": True, "task_count": 4,
             "last_modified": _iso(-90), "runs": {"total": 22, "failed": 0, "successful": 22}},
            {"id": "wf-leaver", "display_name": "Offboard leaver", "category": "leaver",
             "enabled": True, "scheduling_enabled": True, "task_count": 3,
             "last_modified": _iso(-45), "runs": {"total": 18, "failed": 4, "successful": 14}},
        ],
        "capabilities": {"access_reviews": True, "entitlement": True, "lifecycle": True,
                         "licensed_p2": True, "licensed_governance": True},
        "counts": {"reviews": 2, "reviews_active": 2, "packages": 2, "assignments": 2,
                   "workflows": 2, "workflows_enabled": 2, "leaver_workflows": 1},
    }, item_count=6)


def seed(tenant_id: str = DEMO_TENANT) -> dict[str, Any]:
    """Write a complete synthetic snapshot into the cache for ``tenant_id``."""
    for name, payload in (
        ("tenant", _tenant()), ("people", _people()), ("apps", _apps()),
        ("roles", _roles()), ("pim", _pim()), ("activations", _activations()),
        ("ca", _ca()), ("risk", _risk()), ("governance", _governance()),
    ):
        cache.write_domain(tenant_id, name, payload)
    cache.set_tenant_meta(
        tenant_id,
        licences={"p1": True, "p2": True, "governance": True, "workload_id_premium": True,
                  "detected": True, "reason": "", "skus": [{"sku": "AAD_PREMIUM_P2", "enabled_units": 500,
                                                            "consumed_units": 412, "service_plans": 4}]},
        permissions={
            "token_ok": True, "token_error": "", "granted_known": True, "claim_error": "", "probed": False,
            "granted": ["Directory.Read.All", "Application.Read.All", "Policy.Read.All",
                        "RoleManagement.Read.Directory", "Organization.Read.All", "AuditLog.Read.All",
                        "Group.Read.All", "User.Read.All", "RoleManagementPolicy.Read.Directory",
                        "RoleAssignmentSchedule.Read.Directory",
                        "PrivilegedAccess.Read.AzureAD", "PrivilegedAccess.Read.AzureADGroup",
                        "Synchronization.Read.All", "IdentityRiskyUser.Read.All",
                        "IdentityRiskEvent.Read.All", "IdentityRiskyServicePrincipal.Read.All",
                        "AccessReview.Read.All", "EntitlementManagement.Read.All",
                        "LifecycleWorkflows.Read.All", "Reports.Read.All"],
            "domains": {d: {"ok": True, "missing": [], "reason": ""}
                        for d in ("tenant", "people", "apps", "roles", "pim", "activations",
                                  "ca", "devices", "hybrid", "risk", "governance")},
            "tiers": [],
        },
    )
    cache.mark_full_refresh(tenant_id)

    from app.entra import snapshot as snapshot_mod

    snapshot_mod.invalidate(tenant_id)
    snap = snapshot_mod.analyze(tenant_id, force=True)
    return {"tenant_id": tenant_id, "score": snap["_analysis"]["score"]["score"],
            "findings": len(snap["_analysis"]["findings"])}
