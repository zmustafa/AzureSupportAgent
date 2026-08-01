"""Identity Blast-Radius Graph — the assembler.

Deliberately a **backend-only** feature. The Cytoscape canvas, layouts, lenses, minimap,
saved views and blast-radius BFS already exist in :mod:`app.graph` and are live-verified;
this module produces nodes and edges in exactly the shape that canvas already renders.

Two rules are load-bearing, both learned the hard way in the Azure estate graph
(``/memories/repo/graph-feature.md``):

1. **Never emit an edge whose endpoints are absent.** Cytoscape rejects the entire batch and
   blanks the canvas — a whole-screen failure caused by one orphan edge. :func:`_finish`
   filters, and a property test asserts it can never regress.
2. **Never load the whole tenant.** A 100,000-user identity graph cannot render and would
   not be legible if it could. Every entry point is scoped, and the default landing view is
   the privileged overview — a few hundred nodes that answer the question people actually
   have.

The most valuable output here is not the inventory: it is the derived ``escalates_to``
edge. Each one is an explicit, named privilege-escalation primitive with a one-sentence
explanation, not a traversal heuristic — so the answer to "why is this edge here?" is
always a rule somebody can read and argue with.
"""
from __future__ import annotations

from typing import Any, Iterable

from app.entra.collectors.roles import TIER0, TIER1, tier_of

# ------------------------------------------------------------------------- node kinds
KIND_TENANT = "entra_tenant"
KIND_USER = "entra_user"
KIND_GUEST = "entra_guest"
KIND_GROUP = "entra_group"
KIND_ROLE = "entra_role"
KIND_APP = "entra_app"
KIND_SP = "service_principal"
KIND_MANAGED_IDENTITY = "managed_identity"
KIND_PERMISSION = "oauth_permission"
KIND_CA_POLICY = "ca_policy"
# A domain whose users are authenticated by somebody else. It is a node rather than a
# property because that is what it behaves like: an external system with an edge to every
# principal it can issue tokens for.
KIND_FEDERATED_DOMAIN = "federated_domain"

NODE_KINDS: tuple[str, ...] = (
    KIND_TENANT, KIND_USER, KIND_GUEST, KIND_GROUP, KIND_ROLE, KIND_APP, KIND_SP,
    KIND_MANAGED_IDENTITY, KIND_PERMISSION, KIND_CA_POLICY, KIND_FEDERATED_DOMAIN,
)

# ------------------------------------------------------------------------- edge kinds
EDGE_MEMBER_OF = "member_of"
EDGE_OWNS = "owns"
EDGE_ACTIVE_IN = "active_in"
EDGE_ELIGIBLE_FOR = "eligible_for"
EDGE_GRANTED = "granted"
EDGE_PROTECTED_BY = "protected_by"
EDGE_EXCLUDED_FROM = "excluded_from"
EDGE_ESCALATES_TO = "escalates_to"
EDGE_CAN_ACCESS = "can_access"
EDGE_IN_TENANT = "in_tenant"
# The identity provider issues the token this principal signs in with.
EDGE_AUTHENTICATES = "authenticates"

EDGE_KINDS: tuple[str, ...] = (
    EDGE_MEMBER_OF, EDGE_OWNS, EDGE_ACTIVE_IN, EDGE_ELIGIBLE_FOR, EDGE_GRANTED,
    EDGE_PROTECTED_BY, EDGE_EXCLUDED_FROM, EDGE_ESCALATES_TO, EDGE_CAN_ACCESS, EDGE_IN_TENANT,
    EDGE_AUTHENTICATES,
)

# Caps. A graph nobody can read is worse than no graph.
MAX_NODES = 900
GROUP_COLLAPSE_THRESHOLD = 25
# How many targets one principal may reach through one primitive before the rest are
# summarised as a count. A service principal that can seize 224 applications is one finding
# with a number, not 224 arrows.
MAX_FAN_OUT = 12


# --------------------------------------------------------------------------- ids
def user_id(oid: str) -> str:
    return f"eu:{oid}"


def group_id(oid: str) -> str:
    return f"egr:{oid}"


def role_id(rid: str) -> str:
    return f"er:{rid}"


def app_id(oid: str) -> str:
    return f"ea:{oid}"


def sp_id(oid: str) -> str:
    return f"esp:{oid}"


def permission_id(name: str) -> str:
    return f"eperm:{name}"


def policy_id(pid: str) -> str:
    return f"eca:{pid}"


def tenant_id_node(tid: str) -> str:
    return f"et:{tid}"


def _node(nid: str, kind: str, label: str, **data: Any) -> dict[str, Any]:
    return {"id": nid, "kind": kind, "label": label or nid, "data": data,
            "badges": {}, "expandable": False}


def _edge(source: str, target: str, kind: str, *, label: str = "",
          **data: Any) -> dict[str, Any]:
    return {"id": f"{source}__{kind}__{target}", "source": source, "target": target,
            "kind": kind, "label": label, "data": data}


# ------------------------------------------------------------------- escalation rules
# Each entry is an explicit, named primitive. The graph draws exactly these and nothing
# else — no transitive guessing, because an escalation edge nobody can justify is worse
# than a missing one.
ESCALATION_PRIMITIVES: tuple[dict[str, str], ...] = (
    {"key": "app_owner_role_write",
     "name": "Application owner inherits directory-write permissions",
     "rule": "The owner of an application whose service principal holds "
             "RoleManagement.ReadWrite.Directory can add a credential to it and use that "
             "principal to grant themselves any directory role.",
     "confidence": "high"},
    {"key": "app_admin_credential_write",
     "name": "Application Administrator can seize any service principal",
     "rule": "Application Administrator and Cloud Application Administrator can add "
             "credentials to any application, and therefore inherit every permission any "
             "service principal in the tenant holds.",
     "confidence": "high"},
    {"key": "group_owner_role",
     "name": "Owner of a role-assignable group inherits its roles",
     "rule": "The owner of a role-assignable group can add themselves to it and receive "
             "every directory role that group confers.",
     "confidence": "high"},
    {"key": "consent_grant",
     "name": "Can grant itself any application permission",
     "rule": "A principal holding AppRoleAssignment.ReadWrite.All can grant itself any "
             "application permission in the tenant, including Directory.ReadWrite.All.",
     "confidence": "high"},
    {"key": "application_write",
     "name": "Can add credentials to any application",
     "rule": "Application.ReadWrite.All (or Application.ReadWrite.OwnedBy on a powerful app) "
             "lets a principal add its own secret to any application registration and then "
             "authenticate as that application — inheriting every permission it holds. This is "
             "the service-principal equivalent of the Application Administrator role.",
     "confidence": "high"},
    {"key": "group_write",
     "name": "Can write the membership of a role-assignable group",
     "rule": "Group.ReadWrite.All or GroupMember.ReadWrite.All lets a principal add itself to "
             "a role-assignable group and receive every directory role that group confers.",
     "confidence": "high"},
    {"key": "password_write",
     "name": "Can reset another account's credentials",
     "rule": "User.ReadWrite.All or User-PasswordProfile.ReadWrite.All lets a principal reset "
             "passwords. Where directory role restrictions do not prevent it, that is a path "
             "to any account it can reach.",
     "confidence": "medium"},
    {"key": "priv_auth_admin",
     "name": "Privileged Authentication Administrator can reset any admin",
     "rule": "Privileged Authentication Administrator can reset the credentials of any "
             "account, including Global Administrators.",
     "confidence": "high"},
    {"key": "groups_admin_assignable",
     "name": "Groups Administrator can write role-assignable membership",
     "rule": "Groups Administrator can modify the membership of role-assignable groups and "
             "therefore grant the roles those groups confer.",
     "confidence": "medium"},
)
PRIMITIVE_BY_KEY = {p["key"]: p for p in ESCALATION_PRIMITIVES}

_ROLE_WRITE_PERMISSIONS = {"rolemanagement.readwrite.directory"}
_CONSENT_GRANT_PERMISSIONS = {"approleassignment.readwrite.all"}
# Adding a credential to an application means authenticating AS that application. Any of
# these grants that capability, which is why they escalate rather than merely being broad.
_APP_WRITE_PERMISSIONS = {"application.readwrite.all", "application.readwrite.ownedby"}
_GROUP_WRITE_PERMISSIONS = {"group.readwrite.all", "groupmember.readwrite.all"}
_PASSWORD_WRITE_PERMISSIONS = {"user.readwrite.all", "user-passwordprofile.readwrite.all",
                               "directory.accessasuser.all"}
_APP_ADMIN_ROLES = {"application administrator", "cloud application administrator"}
_PRIV_AUTH_ROLES = {"privileged authentication administrator"}
_GROUPS_ADMIN_ROLES = {"groups administrator"}

# Anything that reaches a directory role through an application. Used to decide which
# service principals are worth seizing.
_SEIZABLE = _ROLE_WRITE_PERMISSIONS | _CONSENT_GRANT_PERMISSIONS | _APP_WRITE_PERMISSIONS


def _perm_names(sp: dict[str, Any]) -> set[str]:
    return {str(p.get("permission") or "").lower()
            for p in sp.get("granted_app_permissions") or []}


def escalation_edges(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Derived ``escalates_to`` edges. Pure, and unit-tested one primitive at a time."""
    roles = data.get("roles") or {}
    apps = data.get("apps") or {}
    people = data.get("people") or {}

    definitions = {str(d.get("id")): d for d in roles.get("definitions") or []}
    ga_role = next((d for d in definitions.values()
                    if str(d.get("display_name") or "").lower() == "global administrator"), None)
    ga_node = role_id(str(ga_role.get("id"))) if ga_role else ""

    # Who holds which role, by name, so a primitive can ask "is this principal an App Admin?"
    holders_by_role: dict[str, list[dict[str, Any]]] = {}
    for bucket in ("assignments", "group_derived", "eligible"):
        for row in roles.get(bucket) or []:
            name = str(row.get("role_name") or "").lower()
            holders_by_role.setdefault(name, []).append(row)

    registrations = {str(a.get("sp_object_id") or ""): a for a in apps.get("applications") or []}
    out: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    fan_out: dict[tuple[str, str], int] = {}
    _CONFIDENCE_RANK = {"high": 2, "medium": 1, "low": 0}

    def add(source: str, target: str, key: str, why: str) -> None:
        """One edge per (source, target), with a bounded fan-out per source per primitive.

        Two rules, both learned from a real 20,000-user tenant that produced 484 edges:

        * When two primitives reach the same conclusion the stronger one wins, otherwise a
          medium-confidence password-reset path masks a high-confidence application-takeover
          path that happened to be computed second, and the operator reads the weaker
          explanation.
        * A principal that can seize 224 applications gets ``MAX_FAN_OUT`` arrows and a
          count, not 224 arrows. The 225th arrow adds no information and costs legibility,
          which is the whole point of the view.
        """
        if not source or not target or source == target:
            return
        eid = f"{source}__{EDGE_ESCALATES_TO}__{target}"
        primitive = PRIMITIVE_BY_KEY[key]
        rank = _CONFIDENCE_RANK.get(primitive["confidence"], 0)

        if eid in seen:
            index = seen[eid]
            if rank > _CONFIDENCE_RANK.get(out[index]["data"]["confidence"], 0):
                also = out[index]["data"].get("also_via") or []
                edge = _edge(source, target, EDGE_ESCALATES_TO, label=primitive["name"],
                             primitive=key, reason=why, confidence=primitive["confidence"],
                             rule=primitive["rule"])
                edge["data"]["also_via"] = [*also, out[index]["data"]["primitive"]]
                out[index] = edge
            else:
                out[index]["data"].setdefault("also_via", []).append(key)
            return

        bucket = (source, key)
        count = fan_out.get(bucket, 0) + 1
        fan_out[bucket] = count
        if count > MAX_FAN_OUT:
            # Keep the total honest on the edges we did draw.
            for i in range(len(out)):
                if out[i]["source"] == source and out[i]["data"]["primitive"] == key:
                    out[i]["data"]["fan_out_total"] = count
            return

        seen[eid] = len(out)
        out.append(_edge(source, target, EDGE_ESCALATES_TO, label=primitive["name"],
                         primitive=key, reason=why, confidence=primitive["confidence"],
                         rule=primitive["rule"], fan_out_total=count))

    # 1 / 4 — service principal permission primitives.
    for sp in apps.get("service_principals") or []:
        perms = _perm_names(sp)
        oid = str(sp.get("object_id") or "")
        name = str(sp.get("display_name") or oid)
        if perms & _ROLE_WRITE_PERMISSIONS and ga_node:
            add(sp_id(oid), ga_node, "app_owner_role_write",
                f"'{name}' holds RoleManagement.ReadWrite.Directory, so it can assign any "
                f"directory role including Global Administrator.")
            registration = registrations.get(oid) or {}
            for owner in registration.get("owner_ids") or []:
                add(user_id(str(owner)), ga_node, "app_owner_role_write",
                    f"This principal owns '{name}', whose service principal holds "
                    f"RoleManagement.ReadWrite.Directory \u2014 so it can add a credential to "
                    f"that application and grant itself Global Administrator.")
        if perms & _CONSENT_GRANT_PERMISSIONS and ga_node:
            add(sp_id(oid), ga_node, "consent_grant",
                f"'{name}' holds AppRoleAssignment.ReadWrite.All, so it can grant itself any "
                f"application permission in the tenant.")
        if perms & _PASSWORD_WRITE_PERMISSIONS and ga_node:
            add(sp_id(oid), ga_node, "password_write",
                f"'{name}' can reset account credentials. Where no role restriction stops it, "
                f"that reaches any account it can enumerate.")

    # 1b — Application.ReadWrite.All: seize any *other* powerful service principal. Drawn to
    # the principals it can actually reach rather than to a role, because that is the honest
    # picture: the escalation is only as good as the best application in the tenant.
    seizable = [sp for sp in apps.get("service_principals") or []
                if _perm_names(sp) & _SEIZABLE]
    for sp in apps.get("service_principals") or []:
        if not (_perm_names(sp) & _APP_WRITE_PERMISSIONS):
            continue
        oid = str(sp.get("object_id") or "")
        name = str(sp.get("display_name") or oid)
        targets = [t for t in seizable if str(t.get("object_id") or "") != oid]
        for target in targets:
            add(sp_id(oid), sp_id(str(target.get("object_id") or "")), "application_write",
                f"'{name}' can add a credential to '{target.get('display_name')}' and "
                f"authenticate as it, inheriting every permission that application holds.")
        if not targets and ga_node:
            # No other powerful application today, but the capability itself is the finding:
            # the next application anyone registers becomes reachable.
            add(sp_id(oid), ga_node, "application_write",
                f"'{name}' can add a credential to any application registration in the tenant "
                f"and authenticate as it. No other application currently holds directory-write "
                f"permissions, but any that is created will be immediately reachable.")

    # 1c — Group write: reach the roles that role-assignable groups confer.
    assignable_preview = {str(g.get("id")): g for g in people.get("groups") or []
                          if g.get("is_assignable_to_role")}
    group_role_ids: dict[str, set[str]] = {}
    for row in roles.get("assignments") or []:
        if str(row.get("principal_type") or "") == "Group":
            group_role_ids.setdefault(str(row.get("principal_id") or ""), set()).add(
                str(row.get("role_id") or ""))
    for row in roles.get("group_derived") or []:
        gid = str(row.get("source_group_id") or "")
        if gid:
            group_role_ids.setdefault(gid, set()).add(str(row.get("role_id") or ""))
    for sp in apps.get("service_principals") or []:
        if not (_perm_names(sp) & _GROUP_WRITE_PERMISSIONS):
            continue
        oid = str(sp.get("object_id") or "")
        name = str(sp.get("display_name") or oid)
        for gid, group in assignable_preview.items():
            for rid in group_role_ids.get(gid, set()):
                add(sp_id(oid), role_id(rid), "group_write",
                    f"'{name}' can write group membership, and "
                    f"'{group.get('display_name')}' is role-assignable \u2014 so it can add a "
                    f"principal to that group and confer the role.")

    # 2 — Application Administrator seizes any service principal.
    powerful_sps = [sp for sp in apps.get("service_principals") or []
                    if _perm_names(sp) & _SEIZABLE]
    for role_name in _APP_ADMIN_ROLES:
        for holder in holders_by_role.get(role_name, []):
            pid = str(holder.get("principal_id") or "")
            for sp in powerful_sps:
                add(user_id(pid), sp_id(str(sp.get("object_id") or "")),
                    "app_admin_credential_write",
                    f"'{holder.get('principal_name') or pid}' holds {role_name.title()}, so it "
                    f"can add a credential to '{sp.get('display_name')}' and inherit every "
                    f"permission that service principal holds.")

    # 3 — owner of a role-assignable group inherits the group's roles.
    assignable = {str(g.get("id")): g for g in people.get("groups") or []
                  if g.get("is_assignable_to_role")}
    group_roles: dict[str, set[str]] = {}
    for row in roles.get("assignments") or []:
        if str(row.get("principal_type") or "") == "Group":
            group_roles.setdefault(str(row.get("principal_id") or ""), set()).add(
                str(row.get("role_id") or ""))
    for row in roles.get("group_derived") or []:
        gid = str(row.get("source_group_id") or "")
        if gid:
            group_roles.setdefault(gid, set()).add(str(row.get("role_id") or ""))
    for gid, group in assignable.items():
        for rid in group_roles.get(gid, set()):
            for owner in group.get("owner_ids") or []:
                add(user_id(str(owner)), role_id(rid), "group_owner_role",
                    f"This principal owns the role-assignable group "
                    f"'{group.get('display_name')}', so it can add itself and receive every "
                    f"role that group confers.")

    # 4 — role primitives that need no application at all.
    for role_name in _PRIV_AUTH_ROLES:
        for holder in holders_by_role.get(role_name, []):
            if ga_node:
                add(user_id(str(holder.get("principal_id") or "")), ga_node, "priv_auth_admin",
                    f"'{holder.get('principal_name')}' holds Privileged Authentication "
                    f"Administrator and can reset a Global Administrator's credentials.")
    for role_name in _GROUPS_ADMIN_ROLES:
        for holder in holders_by_role.get(role_name, []):
            for gid, group in assignable.items():
                for rid in group_roles.get(gid, set()):
                    add(user_id(str(holder.get("principal_id") or "")), role_id(rid),
                        "groups_admin_assignable",
                        f"'{holder.get('principal_name')}' holds Groups Administrator and can "
                        f"write the membership of role-assignable group "
                        f"'{group.get('display_name')}'.")

    return out


# --------------------------------------------------------------------------- builders
def _user_node(user: dict[str, Any], *, privileged: bool = False) -> dict[str, Any]:
    guest = str(user.get("user_type") or "") == "Guest"
    return _node(
        user_id(str(user.get("id") or "")), KIND_GUEST if guest else KIND_USER,
        str(user.get("display_name") or user.get("upn") or user.get("id") or ""),
        upn=str(user.get("upn") or ""), enabled=bool(user.get("enabled", True)),
        guest=guest, privileged=privileged,
        mfa_registered=user.get("mfa_registered"),
        phishing_resistant=bool(user.get("phishing_resistant")),
        last_signin=str(user.get("last_signin") or ""),
        on_prem_synced=bool(user.get("on_prem_synced")),
    )


def _group_node(group: dict[str, Any], member_count: int = 0) -> dict[str, Any]:
    return _node(
        group_id(str(group.get("id") or "")), KIND_GROUP,
        str(group.get("display_name") or group.get("id") or ""),
        assignable=bool(group.get("is_assignable_to_role")),
        dynamic=bool(group.get("dynamic")), member_count=member_count,
        owner_count=len(group.get("owner_ids") or []),
        owners_known=bool(group.get("owners_known")),
    )


def _role_node(definition: dict[str, Any]) -> dict[str, Any]:
    name = str(definition.get("display_name") or "")
    tier = str(definition.get("tier") or tier_of(name))
    return _node(role_id(str(definition.get("id") or "")), KIND_ROLE, name,
                 tier=tier, privileged=tier in ("tier0", "tier1"),
                 built_in=bool(definition.get("is_built_in")))


def _sp_node(sp: dict[str, Any]) -> dict[str, Any]:
    kind = (KIND_MANAGED_IDENTITY if str(sp.get("sp_type") or "") == "ManagedIdentity"
            else KIND_SP)
    risk = sp.get("risk") or {}
    return _node(
        sp_id(str(sp.get("object_id") or "")), kind,
        str(sp.get("display_name") or sp.get("object_id") or ""),
        app_id=str(sp.get("app_id") or ""), sp_type=str(sp.get("sp_type") or ""),
        enabled=bool(sp.get("enabled", True)),
        permission_count=len(sp.get("granted_app_permissions") or []),
        max_tier=max((str(p.get("tier") or "low")
                      for p in sp.get("granted_app_permissions") or []),
                     key=lambda t: ("low", "medium", "high", "critical").index(t)
                     if t in ("low", "medium", "high", "critical") else 0, default="low"),
        risk_score=int(risk.get("score") or 0),
        owner_count=len(sp.get("owner_ids") or []),
    )


def _policy_node(policy: dict[str, Any]) -> dict[str, Any]:
    return _node(policy_id(str(policy.get("id") or "")), KIND_CA_POLICY,
                 str(policy.get("display_name") or ""),
                 state=str(policy.get("state") or ""),
                 enforced=bool(policy.get("is_enforced")),
                 controls=list(policy.get("controls") or []))


def _finish(nodes: Iterable[dict[str, Any]], edges: Iterable[dict[str, Any]],
            *, truncated: bool = False, note: str = "") -> dict[str, Any]:
    """Deduplicate, cap, and drop every edge whose endpoints are not present.

    The dangling-edge filter is not defensive programming — Cytoscape rejects the whole
    batch when one edge points at a node that is not in the payload, which blanks the
    canvas. This is the single most important line in the module."""
    by_id: dict[str, dict[str, Any]] = {}
    for node in nodes:
        by_id.setdefault(node["id"], node)
    if len(by_id) > MAX_NODES:
        truncated = True
        keep = dict(list(by_id.items())[:MAX_NODES])
        by_id = keep
    present = set(by_id)

    kept: dict[str, dict[str, Any]] = {}
    dropped = 0
    for edge in edges:
        if edge["source"] not in present or edge["target"] not in present:
            dropped += 1
            continue
        if edge["source"] == edge["target"]:
            dropped += 1
            continue
        kept.setdefault(edge["id"], edge)

    by_kind: dict[str, int] = {}
    for node in by_id.values():
        by_kind[node["kind"]] = by_kind.get(node["kind"], 0) + 1
    for edge in kept.values():
        by_kind[edge["kind"]] = by_kind.get(edge["kind"], 0) + 1

    return {
        "nodes": list(by_id.values()),
        "edges": list(kept.values()),
        "stats": {"node_count": len(by_id), "edge_count": len(kept), "by_kind": by_kind,
                  "dropped_edges": dropped},
        "truncated": truncated,
        "note": note,
    }


# --------------------------------------------------------------------------- scopes
def privileged_overview(data: dict[str, Any], analysis: dict[str, Any] | None = None
                        ) -> dict[str, Any]:
    """The default landing view: tier-0/tier-1 role holders and how they get there.

    Usually a few hundred nodes on a real tenant — small enough to read, and the answer to
    the question people actually open a graph to ask."""
    roles = data.get("roles") or {}
    people = data.get("people") or {}
    apps = data.get("apps") or {}

    users = {str(u.get("id")): u for u in people.get("users") or []}
    groups = {str(g.get("id")): g for g in people.get("groups") or []}
    sps = {str(s.get("object_id")): s for s in apps.get("service_principals") or []}
    definitions = {str(d.get("id")): d for d in roles.get("definitions") or []}

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    wanted_roles: set[str] = set()
    privileged_principals: set[str] = set()

    def role_is_privileged(definition: dict[str, Any]) -> bool:
        name = str(definition.get("display_name") or "")
        return str(definition.get("tier") or tier_of(name)) in ("tier0", "tier1")

    for bucket, edge_kind in (("assignments", EDGE_ACTIVE_IN),
                              ("group_derived", EDGE_ACTIVE_IN),
                              ("eligible", EDGE_ELIGIBLE_FOR)):
        for row in roles.get(bucket) or []:
            rid = str(row.get("role_id") or "")
            definition = definitions.get(rid) or {
                "id": rid, "display_name": row.get("role_name"), "tier": row.get("role_tier")}
            if not role_is_privileged(definition):
                continue
            wanted_roles.add(rid)
            pid = str(row.get("principal_id") or "")
            if not pid:
                continue
            privileged_principals.add(pid)
            ptype = str(row.get("principal_type") or "User")
            if ptype == "Group":
                source = group_id(pid)
                group = groups.get(pid)
                nodes.append(_group_node(group) if group else
                             _node(source, KIND_GROUP, str(row.get("principal_name") or pid)))
            elif ptype == "ServicePrincipal":
                source = sp_id(pid)
                sp = sps.get(pid)
                nodes.append(_sp_node(sp) if sp else
                             _node(source, KIND_SP, str(row.get("principal_name") or pid)))
            else:
                source = user_id(pid)
                user = users.get(pid)
                nodes.append(_user_node(user, privileged=True) if user else
                             _node(source, KIND_USER, str(row.get("principal_name") or pid),
                                   privileged=True, upn=str(row.get("principal_upn") or "")))
            nodes.append(_role_node(definition))
            edges.append(_edge(source, role_id(rid), edge_kind,
                               label="eligible" if edge_kind == EDGE_ELIGIBLE_FOR else "active",
                               permanent=row.get("permanent"),
                               via_group=str(row.get("source_group_name") or "")))

            # The nested chain: show the group that actually confers the role.
            via = str(row.get("source_group_id") or "")
            if via:
                group = groups.get(via)
                gnode = _group_node(group) if group else _node(
                    group_id(via), KIND_GROUP, str(row.get("source_group_name") or via))
                nodes.append(gnode)
                edges.append(_edge(source, group_id(via), EDGE_MEMBER_OF, label="member"))
                edges.append(_edge(group_id(via), role_id(rid), EDGE_ACTIVE_IN, label="confers"))

    # Escalation edges bring in the principals that can *reach* privilege without holding it.
    # Only paths whose TARGET is a directory role belong here: this view answers "who can
    # end up privileged", and the dense service-principal-seizure mesh (which can be
    # hundreds of edges on a real tenant) would bury that answer. That mesh is exactly what
    # the dedicated escalation map is for.
    esc = [e for e in escalation_edges(data) if e["target"].startswith("er:")]
    esc_nodes: dict[str, dict[str, Any]] = {}
    for edge in esc:
        for endpoint in (edge["source"], edge["target"]):
            prefix, _, value = endpoint.partition(":")
            if prefix == "eu" and value in users:
                esc_nodes[endpoint] = _user_node(users[value],
                                                 privileged=value in privileged_principals)
            elif prefix == "esp" and value in sps:
                esc_nodes[endpoint] = _sp_node(sps[value])
            elif prefix == "er" and value in definitions:
                esc_nodes[endpoint] = _role_node(definitions[value])
            elif prefix == "egr" and value in groups:
                esc_nodes[endpoint] = _group_node(groups[value])
    nodes.extend(esc_nodes.values())
    edges.extend(esc)

    note = ("Privileged overview: tier-0 and tier-1 role holders, the groups that confer "
            "those roles, and every derived escalation path INTO them. Service-principal "
            "takeover chains are on the escalation map.")
    return _finish(nodes, edges, note=note)


def escalation_map(data: dict[str, Any]) -> dict[str, Any]:
    """Only the nodes on a derived escalation path. The smallest useful view."""
    roles = data.get("roles") or {}
    people = data.get("people") or {}
    apps = data.get("apps") or {}
    users = {str(u.get("id")): u for u in people.get("users") or []}
    groups = {str(g.get("id")): g for g in people.get("groups") or []}
    sps = {str(s.get("object_id")): s for s in apps.get("service_principals") or []}
    definitions = {str(d.get("id")): d for d in roles.get("definitions") or []}

    edges = escalation_edges(data)
    nodes: dict[str, dict[str, Any]] = {}
    for edge in edges:
        for endpoint in (edge["source"], edge["target"]):
            prefix, _, value = endpoint.partition(":")
            if prefix == "eu" and value in users:
                nodes[endpoint] = _user_node(users[value])
            elif prefix == "esp" and value in sps:
                nodes[endpoint] = _sp_node(sps[value])
            elif prefix == "er" and value in definitions:
                nodes[endpoint] = _role_node(definitions[value])
            elif prefix == "egr" and value in groups:
                nodes[endpoint] = _group_node(groups[value])
            else:
                nodes[endpoint] = _node(endpoint, KIND_USER, value)
    note = ("Escalation map: only principals that can reach a privileged role through a "
            "named escalation primitive. Every edge states the rule that produced it.")
    return _finish(nodes.values(), edges, note=note)


def focus_principal(data: dict[str, Any], object_id: str, *, hops: int = 2) -> dict[str, Any]:
    """Ego graph for one principal: roles, groups, owned applications, escalation paths."""
    roles = data.get("roles") or {}
    people = data.get("people") or {}
    apps = data.get("apps") or {}
    users = {str(u.get("id")): u for u in people.get("users") or []}
    groups = {str(g.get("id")): g for g in people.get("groups") or []}
    sps = {str(s.get("object_id")): s for s in apps.get("service_principals") or []}
    definitions = {str(d.get("id")): d for d in roles.get("definitions") or []}

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    is_sp = object_id in sps
    root = sp_id(object_id) if is_sp else user_id(object_id)
    if is_sp:
        nodes.append(_sp_node(sps[object_id]))
    elif object_id in users:
        nodes.append(_user_node(users[object_id], privileged=True))
    elif object_id in groups:
        root = group_id(object_id)
        nodes.append(_group_node(groups[object_id]))
    else:
        nodes.append(_node(root, KIND_USER, object_id))

    for bucket, kind in (("assignments", EDGE_ACTIVE_IN), ("group_derived", EDGE_ACTIVE_IN),
                         ("eligible", EDGE_ELIGIBLE_FOR)):
        for row in roles.get(bucket) or []:
            if str(row.get("principal_id") or "") != object_id:
                continue
            rid = str(row.get("role_id") or "")
            definition = definitions.get(rid) or {"id": rid, "display_name": row.get("role_name")}
            nodes.append(_role_node(definition))
            edges.append(_edge(root, role_id(rid), kind,
                               label="eligible" if kind == EDGE_ELIGIBLE_FOR else "active"))
            via = str(row.get("source_group_id") or "")
            if via:
                group = groups.get(via)
                nodes.append(_group_node(group) if group else
                             _node(group_id(via), KIND_GROUP,
                                   str(row.get("source_group_name") or via)))
                edges.append(_edge(root, group_id(via), EDGE_MEMBER_OF, label="member"))
                edges.append(_edge(group_id(via), role_id(rid), EDGE_ACTIVE_IN, label="confers"))

    # Applications and groups this principal owns — the escalation surface.
    for registration in apps.get("applications") or []:
        if object_id not in (registration.get("owner_ids") or []):
            continue
        target = str(registration.get("sp_object_id") or "")
        sp = sps.get(target)
        if sp:
            nodes.append(_sp_node(sp))
            edges.append(_edge(root, sp_id(target), EDGE_OWNS, label="owns"))
            for perm in (sp.get("granted_app_permissions") or [])[:12]:
                name = str(perm.get("permission") or "")
                nodes.append(_node(permission_id(name), KIND_PERMISSION, name,
                                   tier=str(perm.get("tier") or "low")))
                edges.append(_edge(sp_id(target), permission_id(name), EDGE_GRANTED,
                                   label=str(perm.get("tier") or "")))
    for group in groups.values():
        if object_id in (group.get("owner_ids") or []):
            nodes.append(_group_node(group))
            edges.append(_edge(root, group_id(str(group.get("id"))), EDGE_OWNS, label="owns"))

    if hops >= 2:
        for edge in escalation_edges(data):
            if edge["source"] != root and edge["target"] != root:
                continue
            for endpoint in (edge["source"], edge["target"]):
                prefix, _, value = endpoint.partition(":")
                if prefix == "eu" and value in users:
                    nodes.append(_user_node(users[value]))
                elif prefix == "esp" and value in sps:
                    nodes.append(_sp_node(sps[value]))
                elif prefix == "er" and value in definitions:
                    nodes.append(_role_node(definitions[value]))
                elif prefix == "egr" and value in groups:
                    nodes.append(_group_node(groups[value]))
            edges.append(edge)

    label = (users.get(object_id) or sps.get(object_id) or groups.get(object_id) or {})
    name = str(label.get("display_name") or label.get("upn") or object_id)
    return _finish(nodes, edges,
                   note=f"Everything '{name}' holds, owns, or can escalate to.")


def focus_application(data: dict[str, Any], object_id: str) -> dict[str, Any]:
    """One application: its owners, its permissions, its roles and its Azure reach."""
    apps = data.get("apps") or {}
    people = data.get("people") or {}
    roles = data.get("roles") or {}
    users = {str(u.get("id")): u for u in people.get("users") or []}
    definitions = {str(d.get("id")): d for d in roles.get("definitions") or []}
    sps = {str(s.get("object_id")): s for s in apps.get("service_principals") or []}
    sp = sps.get(object_id)
    if sp is None:
        return _finish([], [], note="No such application in this snapshot.")

    root = sp_id(object_id)
    nodes = [_sp_node(sp)]
    edges: list[dict[str, Any]] = []

    for perm in sp.get("granted_app_permissions") or []:
        name = str(perm.get("permission") or "")
        nodes.append(_node(permission_id(name), KIND_PERMISSION, name,
                           tier=str(perm.get("tier") or "low"),
                           resource=str(perm.get("resource") or "")))
        edges.append(_edge(root, permission_id(name), EDGE_GRANTED,
                           label=str(perm.get("tier") or "")))

    registration = next((a for a in apps.get("applications") or []
                         if str(a.get("sp_object_id") or "") == object_id), {})
    for owner in registration.get("owner_ids") or []:
        user = users.get(str(owner))
        nodes.append(_user_node(user) if user else _node(user_id(str(owner)), KIND_USER, str(owner)))
        edges.append(_edge(user_id(str(owner)), root, EDGE_OWNS, label="owns"))

    for row in roles.get("assignments") or []:
        if str(row.get("principal_id") or "") != object_id:
            continue
        rid = str(row.get("role_id") or "")
        definition = definitions.get(rid) or {"id": rid, "display_name": row.get("role_name")}
        nodes.append(_role_node(definition))
        edges.append(_edge(root, role_id(rid), EDGE_ACTIVE_IN, label="active"))

    # The Azure bridge, when a RBAC scan exists. Read-only and best-effort: a stale or
    # missing join simply produces no can_access edges rather than a wrong picture.
    from app.entra.azure_link import azure_power

    reach = azure_power(data.get("_azure_link") or {}, object_id) or {}
    for role_name in (reach.get("powerful_roles") or reach.get("roles") or [])[:12]:
        name = role_name if isinstance(role_name, str) else str(role_name.get("role") or "")
        if not name:
            continue
        target = permission_id(f"azure/{name}")
        nodes.append(_node(target, KIND_PERMISSION, name, tier="azure", plane="azure"))
        edges.append(_edge(root, target, EDGE_CAN_ACCESS, label="Azure RBAC"))

    return _finish(nodes, edges,
                   note=f"'{sp.get('display_name')}': owners, granted permissions, directory "
                        f"roles and Azure reach.")


def focus_role(data: dict[str, Any], role: str) -> dict[str, Any]:
    """Everyone who holds a role — directly, through a group, or as an eligible assignment."""
    roles = data.get("roles") or {}
    people = data.get("people") or {}
    apps = data.get("apps") or {}
    users = {str(u.get("id")): u for u in people.get("users") or []}
    groups = {str(g.get("id")): g for g in people.get("groups") or []}
    sps = {str(s.get("object_id")): s for s in apps.get("service_principals") or []}
    definitions = {str(d.get("id")): d for d in roles.get("definitions") or []}
    definition = definitions.get(role) or next(
        (d for d in definitions.values()
         if str(d.get("display_name") or "").lower() == role.lower()), None)
    if definition is None:
        return _finish([], [], note="No such directory role in this snapshot.")

    rid = str(definition.get("id") or "")
    nodes = [_role_node(definition)]
    edges: list[dict[str, Any]] = []
    for bucket, kind in (("assignments", EDGE_ACTIVE_IN), ("group_derived", EDGE_ACTIVE_IN),
                         ("eligible", EDGE_ELIGIBLE_FOR)):
        for row in roles.get(bucket) or []:
            if str(row.get("role_id") or "") != rid:
                continue
            pid = str(row.get("principal_id") or "")
            ptype = str(row.get("principal_type") or "User")
            if ptype == "Group":
                nodes.append(_group_node(groups[pid]) if pid in groups else
                             _node(group_id(pid), KIND_GROUP, str(row.get("principal_name") or pid)))
                source = group_id(pid)
            elif ptype == "ServicePrincipal":
                nodes.append(_sp_node(sps[pid]) if pid in sps else
                             _node(sp_id(pid), KIND_SP, str(row.get("principal_name") or pid)))
                source = sp_id(pid)
            else:
                nodes.append(_user_node(users[pid], privileged=True) if pid in users else
                             _node(user_id(pid), KIND_USER, str(row.get("principal_name") or pid),
                                   privileged=True))
                source = user_id(pid)
            edges.append(_edge(source, role_id(rid), kind,
                               label="eligible" if kind == EDGE_ELIGIBLE_FOR else "active"))

    for edge in escalation_edges(data):
        if edge["target"] != role_id(rid):
            continue
        prefix, _, value = edge["source"].partition(":")
        if prefix == "eu" and value in users:
            nodes.append(_user_node(users[value]))
        elif prefix == "esp" and value in sps:
            nodes.append(_sp_node(sps[value]))
        elif prefix == "egr" and value in groups:
            nodes.append(_group_node(groups[value]))
        edges.append(edge)

    return _finish(nodes, edges,
                   note=f"Everyone who holds '{definition.get('display_name')}' \u2014 directly, "
                        f"through a group, as an eligible assignment, or by escalation.")


def focus_policy(data: dict[str, Any], analysis: dict[str, Any], pid: str) -> dict[str, Any]:
    """One Conditional Access policy: who it covers and, more importantly, who it excludes."""
    policy = next((p for p in analysis.get("policies") or [] if str(p.get("id")) == pid), None)
    if policy is None:
        return _finish([], [], note="No such Conditional Access policy in this snapshot.")
    people = data.get("people") or {}
    users = {str(u.get("id")): u for u in people.get("users") or []}
    groups = {str(g.get("id")): g for g in people.get("groups") or []}

    root = policy_id(pid)
    nodes = [_policy_node(policy)]
    edges: list[dict[str, Any]] = []
    for uid in (policy.get("excluded_ids") or [])[:200]:
        user = users.get(str(uid))
        group = groups.get(str(uid))
        if user:
            nodes.append(_user_node(user))
            edges.append(_edge(user_id(str(uid)), root, EDGE_EXCLUDED_FROM, label="excluded"))
        elif group:
            nodes.append(_group_node(group))
            edges.append(_edge(group_id(str(uid)), root, EDGE_EXCLUDED_FROM, label="excluded"))
    for uid in (policy.get("effective_ids") or [])[:300]:
        user = users.get(str(uid))
        if user:
            nodes.append(_user_node(user))
            edges.append(_edge(user_id(str(uid)), root, EDGE_PROTECTED_BY, label="covered"))
    return _finish(nodes, edges,
                   note=f"'{policy.get('display_name')}': who it covers, and who is excluded "
                        f"from it.")


SCOPES: tuple[dict[str, str], ...] = (
    {"kind": "privileged", "label": "Privileged overview",
     "blurb": "Tier-0 and tier-1 role holders and every path into them. The default view."},
    {"kind": "escalation", "label": "Escalation map",
     "blurb": "Only the principals that can reach privilege through a named primitive."},
    {"kind": "principal", "label": "Focus a principal",
     "blurb": "One user or service principal: roles, groups, owned apps, escalation paths."},
    {"kind": "application", "label": "Focus an application",
     "blurb": "Owners, granted permissions, directory roles and Azure reach."},
    {"kind": "role", "label": "Focus a role",
     "blurb": "Everyone who holds it, however they hold it."},
    {"kind": "policy", "label": "Focus a Conditional Access policy",
     "blurb": "Covered and excluded cohorts for one policy."},
    {"kind": "federation", "label": "Federated authentication",
     "blurb": "Which external identity provider can issue tokens for privileged principals."},
)


def federation_map(data: dict[str, Any]) -> dict[str, Any]:
    """Which external identity provider can issue tokens for privileged principals.

    The question this answers is the one a federated tenant cannot answer anywhere else:
    *if that provider is compromised, whose privilege does the attacker inherit?* Entra
    accepts the provider's tokens — including its multi-factor claim, unless the trust says
    otherwise — so every privileged principal whose UPN sits on a federated domain is
    reachable from a single external system.

    Only privileged principals are drawn. Every user on the domain would be thousands of
    identical nodes saying one thing; the tier-0 and tier-1 holders are the answer.
    """
    fabric = ((data.get("tenant") or {}).get("identity_fabric")) or {}
    trusts = fabric.get("federation") or []
    if not fabric.get("readable"):
        return _finish([], [], note="The domain list could not be read, so federation is unknown.")
    if not trusts:
        return _finish([], [], note="No domain is federated. Entra authenticates every user itself.")

    roles = data.get("roles") or {}
    people = data.get("people") or {}
    users = {str(u.get("id")): u for u in people.get("users") or []}
    from app.entra.collectors.roles import privileged_principal_ids

    privileged = privileged_principal_ids(roles)

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for trust in trusts:
        domain = str(trust.get("domain") or "")
        vendor = (trust.get("vendor") or {}).get("label") or "Unrecognised provider"
        mfa = trust.get("mfa_behaviour") or {}
        nid = f"efd:{domain.lower()}"
        nodes.append(_node(
            nid, KIND_FEDERATED_DOMAIN, f"{domain} → {vendor}",
            domain=domain, vendor=vendor, issuer_uri=str(trust.get("issuer_uri") or ""),
            host=str(trust.get("host") or ""), protocol=str(trust.get("protocol") or ""),
            mfa_trusted=bool(mfa.get("trusted")), mfa_behaviour=str(mfa.get("value") or ""),
            user_count=trust.get("user_count"),
        ))
        suffix = f"@{domain.lower()}"
        for uid in privileged:
            user = users.get(str(uid))
            if not user or not str(user.get("upn") or "").lower().endswith(suffix):
                continue
            nodes.append(_user_node(user, privileged=True))
            edges.append(_edge(
                nid, user_id(str(user.get("id") or "")), EDGE_AUTHENTICATES,
                label="authenticates",
                mfa_trusted=bool(mfa.get("trusted")),
            ))
    note = ("Every privileged principal below signs in through the provider above. Where the "
            "trust's multi-factor claim is accepted, that provider can satisfy Entra MFA on "
            "their behalf.")
    return _finish(nodes, edges, note=note)


def build(data: dict[str, Any], analysis: dict[str, Any], *, scope_kind: str,
          scope_id: str = "") -> dict[str, Any]:
    """Single entry point. Unknown scopes fall back to the privileged overview."""
    if scope_kind == "escalation":
        return escalation_map(data)
    if scope_kind == "principal" and scope_id:
        return focus_principal(data, scope_id)
    if scope_kind == "application" and scope_id:
        return focus_application(data, scope_id)
    if scope_kind == "role" and scope_id:
        return focus_role(data, scope_id)
    if scope_kind == "policy" and scope_id:
        return focus_policy(data, analysis, scope_id)
    if scope_kind == "federation":
        return federation_map(data)
    return privileged_overview(data, analysis)


__all__ = [
    "ESCALATION_PRIMITIVES", "EDGE_KINDS", "NODE_KINDS", "SCOPES", "build",
    "escalation_edges", "escalation_map", "focus_application", "focus_policy",
    "focus_principal", "focus_role", "federation_map", "privileged_overview", "TIER0", "TIER1",
]
