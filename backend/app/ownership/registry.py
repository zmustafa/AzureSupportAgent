"""Ownership registries (JSON, no secrets → no encryption).

Two registries persisted under ``backend/.data/ownership/`` (the Azure Files volume in
the cloud), consistent with the other file registries (workloads, architectures, …):

* ``owners.json``       — the owner directory. One record per owner. An owner is a
  *person*, *team*, or *service* account (``kind``). Teams may carry an explicit member
  list AND/OR be linked to a real Entra/OIDC group (``group_ref``) whose membership is
  expanded on read. An owner may optionally be *linked* to a directory identity (an app
  ``User``, an Entra principal, or an OIDC subject) via ``link`` — that linkage is what
  powers "notify owner", leaver detection and the one-click "assign me".
* ``assignments.json``  — owner ↔ subject bindings. A *subject* is anything ownable:
  the four Azure scope kinds (``mg | subscription | resource_group | resource``) plus
  ``workload`` and ``architecture``. Each assignment carries an ownership ``role``
  (technical/business/security/cost/operations/escalation), a ``primary`` flag, and a
  ``source`` provenance (manual/tag/rbac/workload/ai) + ``confidence`` (for AI suggestions).

Both registries support the standard soft-delete Trash lifecycle (``deleted_at``) used by
workloads/architectures/evidence so deletes are reversible until purged.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parents[2] / ".data" / "ownership"
_OWNERS_PATH = _DIR / "owners.json"
_ASSIGNMENTS_PATH = _DIR / "assignments.json"

# An owner is a person, a team (a group of owners / a directory group), or a service
# account (a non-human principal that "owns" automation-managed resources).
OWNER_KINDS = ("person", "team", "service")

# Where an owner record came from / what real identity it is linked to.
#   manual      — typed in by a user (no directory linkage)
#   app_user    — linked to a local/SSO app User row (auth.users)
#   entra       — linked to an Entra ID principal (user/group/SP object id)
#   oidc_group  — a team backed by an OIDC token group claim
#   rbac        — promoted from an Azure RBAC principal (Owner/Contributor)
OWNER_SOURCES = ("manual", "app_user", "entra", "oidc_group", "rbac")

# The kinds of thing an owner can be assigned to (v1).
SUBJECT_KINDS = ("mg", "subscription", "resource_group", "resource", "workload", "architecture")

# Ownership lanes (a single subject can have multiple owners in different lanes — a light
# RACI: technical = responsible/operates it, business = accountable, the rest are typed
# points of contact).
OWNER_ROLES = ("technical", "business", "security", "cost", "operations", "escalation")

# Assignment provenance — how we know about this owner↔subject binding.
ASSIGNMENT_SOURCES = ("manual", "tag", "rbac", "workload", "ai")


OWNER_DEFAULTS: dict[str, Any] = {
    "kind": "person",            # person | team | service
    "display_name": "",
    "email": "",
    "department": "",            # org/department (free text; populated by import)
    "source": "manual",          # manual | app_user | entra | oidc_group | rbac
    # Optional directory linkage. Any subset may be set depending on `source`.
    "link": {
        # "user_id":          app User.id (auth.users) when source == app_user
        # "idp_id":           identity_providers.id the user/group came from
        # "external_id":      OIDC sub / SAML nameid
        # "entra_object_id":  Entra principal object id (user/group/SP)
        # "upn":              userPrincipalName / mail
    },
    # For teams: an explicit list of member owner-ids (people/services in this team).
    "members": [],
    # For teams linked to a real directory group: {kind: entra|oidc, id, name}. Membership
    # is expanded lazily (best-effort) by the directory resolver, not stored here.
    "group_ref": {},
    # Delegation (RACI escalation): while active, this owner's accountability is temporarily
    # delegated to another owner (e.g. on-call cover / vacation). {owner_id, until (ISO date),
    # reason}. Empty = no delegation. The resolver surfaces an active delegate alongside the owner.
    "delegate": {},
    "notes": "",
    "tags": [],
    "created_by": "",
    "created_at": "",
    "updated_at": "",
    # Soft-delete (Trash). ISO timestamp when trashed; "" when active.
    "deleted_at": "",
    "deleted_by": "",
}

ASSIGNMENT_DEFAULTS: dict[str, Any] = {
    "subject_kind": "",          # one of SUBJECT_KINDS
    "subject_id": "",            # ARM id (scopes/resources) | workload id | architecture id
    "subject_name": "",          # denormalized label for display
    # Denormalized scope coordinates so the resolver can do ancestor inheritance + filtering
    # without re-parsing every time (populated from the ARM id when applicable).
    "subscription_id": "",
    "resource_group": "",
    "owner_id": "",
    "role": "technical",         # one of OWNER_ROLES
    "primary": False,            # the primary owner for this subject+role
    "source": "manual",          # one of ASSIGNMENT_SOURCES
    "confidence": 1.0,           # 0..1 — < 1 for AI-suggested, pending confirmation
    "notes": "",
    # Attestation: when the owner last confirmed they still own this (ISO), and by whom.
    "attested_at": "",
    "attested_by": "",
    "created_by": "",
    "created_at": "",
    "updated_at": "",
    "deleted_at": "",
    "deleted_by": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read(path: Path, root_key: str) -> dict[str, Any]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {root_key: {}}


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _merge(defaults: dict[str, Any], record: dict[str, Any], rec_id: str) -> dict[str, Any]:
    merged = json.loads(json.dumps(defaults))  # deep copy (nested dicts/lists)
    merged.update(record)
    merged["id"] = rec_id
    return merged


# ====================================================================== Owners
def list_owners(tenant_id: str, *, include_deleted: bool = False) -> list[dict[str, Any]]:
    data = _read(_OWNERS_PATH, "owners")
    out: list[dict[str, Any]] = []
    for oid, rec in data.get("owners", {}).items():
        if (rec.get("tenant_id") or "default") != (tenant_id or "default"):
            continue
        merged = _merge(OWNER_DEFAULTS, rec, oid)
        if not include_deleted and merged.get("deleted_at"):
            continue
        out.append(merged)
    out.sort(key=lambda o: (o.get("kind", ""), o.get("display_name", "").lower()))
    return out


def list_trashed_owners(tenant_id: str) -> list[dict[str, Any]]:
    out = [o for o in list_owners(tenant_id, include_deleted=True) if o.get("deleted_at")]
    out.sort(key=lambda o: o.get("deleted_at", ""), reverse=True)
    return out


def get_owner(tenant_id: str, owner_id: str, *, include_deleted: bool = False) -> dict[str, Any] | None:
    if not owner_id:
        return None
    data = _read(_OWNERS_PATH, "owners")
    rec = data.get("owners", {}).get(owner_id)
    if rec is None:
        return None
    if (rec.get("tenant_id") or "default") != (tenant_id or "default"):
        return None
    merged = _merge(OWNER_DEFAULTS, rec, owner_id)
    if not include_deleted and merged.get("deleted_at"):
        return None
    return merged


def upsert_owner(tenant_id: str, owner: dict[str, Any]) -> dict[str, Any]:
    data = _read(_OWNERS_PATH, "owners")
    owners = data.setdefault("owners", {})
    oid = owner.get("id") or str(uuid.uuid4())
    existing = owners.get(oid, {})
    merged = dict(existing)
    for key in OWNER_DEFAULTS:
        if key in owner and owner[key] is not None:
            merged[key] = owner[key]
    merged["tenant_id"] = tenant_id or "default"
    merged["created_at"] = existing.get("created_at") or _now()
    merged["created_by"] = existing.get("created_by") or owner.get("created_by", "")
    merged["updated_at"] = _now()
    merged.pop("id", None)
    owners[oid] = merged
    _write(_OWNERS_PATH, data)
    result = get_owner(tenant_id, oid)
    assert result is not None
    return result


def delete_owner(tenant_id: str, owner_id: str, *, actor: str = "") -> bool:
    """Soft-delete an owner (move to Trash). Returns False if missing/already trashed."""
    data = _read(_OWNERS_PATH, "owners")
    rec = data.get("owners", {}).get(owner_id)
    if rec is None or (rec.get("tenant_id") or "default") != (tenant_id or "default"):
        return False
    if rec.get("deleted_at"):
        return False
    rec["deleted_at"] = _now()
    rec["deleted_by"] = actor
    rec["updated_at"] = _now()
    _write(_OWNERS_PATH, data)
    return True


def restore_owner(tenant_id: str, owner_id: str) -> dict[str, Any] | None:
    data = _read(_OWNERS_PATH, "owners")
    rec = data.get("owners", {}).get(owner_id)
    if rec is None or (rec.get("tenant_id") or "default") != (tenant_id or "default"):
        return None
    if not rec.get("deleted_at"):
        return None
    rec["deleted_at"] = ""
    rec["deleted_by"] = ""
    rec["updated_at"] = _now()
    _write(_OWNERS_PATH, data)
    return get_owner(tenant_id, owner_id)


def purge_owner(tenant_id: str, owner_id: str) -> bool:
    """Permanently delete a trashed owner (hard). Also drops its assignments."""
    data = _read(_OWNERS_PATH, "owners")
    rec = data.get("owners", {}).get(owner_id)
    if rec is None or (rec.get("tenant_id") or "default") != (tenant_id or "default"):
        return False
    if not rec.get("deleted_at"):
        return False
    del data["owners"][owner_id]
    _write(_OWNERS_PATH, data)
    # Cascade: remove assignments that referenced this owner.
    adata = _read(_ASSIGNMENTS_PATH, "assignments")
    assignments = adata.get("assignments", {})
    dropped = [aid for aid, a in assignments.items() if a.get("owner_id") == owner_id]
    for aid in dropped:
        del assignments[aid]
    if dropped:
        _write(_ASSIGNMENTS_PATH, adata)
    return True


def empty_owner_trash(tenant_id: str) -> int:
    data = _read(_OWNERS_PATH, "owners")
    owners = data.get("owners", {})
    trashed = [
        oid for oid, rec in owners.items()
        if rec.get("deleted_at") and (rec.get("tenant_id") or "default") == (tenant_id or "default")
    ]
    for oid in trashed:
        del owners[oid]
    if trashed:
        _write(_OWNERS_PATH, data)
    return len(trashed)


# ================================================================== Assignments
def list_assignments(
    tenant_id: str,
    *,
    subject_kind: str = "",
    subject_id: str = "",
    owner_id: str = "",
    include_deleted: bool = False,
) -> list[dict[str, Any]]:
    data = _read(_ASSIGNMENTS_PATH, "assignments")
    out: list[dict[str, Any]] = []
    for aid, rec in data.get("assignments", {}).items():
        if (rec.get("tenant_id") or "default") != (tenant_id or "default"):
            continue
        merged = _merge(ASSIGNMENT_DEFAULTS, rec, aid)
        if not include_deleted and merged.get("deleted_at"):
            continue
        if subject_kind and merged.get("subject_kind") != subject_kind:
            continue
        if subject_id and merged.get("subject_id") != subject_id:
            continue
        if owner_id and merged.get("owner_id") != owner_id:
            continue
        out.append(merged)
    out.sort(key=lambda a: (not a.get("primary"), a.get("role", ""), a.get("created_at", "")))
    return out


def list_trashed_assignments(tenant_id: str) -> list[dict[str, Any]]:
    out = [a for a in list_assignments(tenant_id, include_deleted=True) if a.get("deleted_at")]
    out.sort(key=lambda a: a.get("deleted_at", ""), reverse=True)
    return out


def get_assignment(tenant_id: str, assignment_id: str, *, include_deleted: bool = False) -> dict[str, Any] | None:
    if not assignment_id:
        return None
    data = _read(_ASSIGNMENTS_PATH, "assignments")
    rec = data.get("assignments", {}).get(assignment_id)
    if rec is None or (rec.get("tenant_id") or "default") != (tenant_id or "default"):
        return None
    merged = _merge(ASSIGNMENT_DEFAULTS, rec, assignment_id)
    if not include_deleted and merged.get("deleted_at"):
        return None
    return merged


def upsert_assignment(tenant_id: str, assignment: dict[str, Any]) -> dict[str, Any]:
    data = _read(_ASSIGNMENTS_PATH, "assignments")
    assignments = data.setdefault("assignments", {})
    aid = assignment.get("id") or str(uuid.uuid4())
    existing = assignments.get(aid, {})
    merged = dict(existing)
    for key in ASSIGNMENT_DEFAULTS:
        if key in assignment and assignment[key] is not None:
            merged[key] = assignment[key]
    merged["tenant_id"] = tenant_id or "default"
    merged["created_at"] = existing.get("created_at") or _now()
    merged["created_by"] = existing.get("created_by") or assignment.get("created_by", "")
    merged["updated_at"] = _now()
    merged.pop("id", None)
    assignments[aid] = merged
    _write(_ASSIGNMENTS_PATH, data)
    result = get_assignment(tenant_id, aid)
    assert result is not None
    return result


def delete_assignment(tenant_id: str, assignment_id: str, *, actor: str = "") -> bool:
    data = _read(_ASSIGNMENTS_PATH, "assignments")
    rec = data.get("assignments", {}).get(assignment_id)
    if rec is None or (rec.get("tenant_id") or "default") != (tenant_id or "default"):
        return False
    if rec.get("deleted_at"):
        return False
    rec["deleted_at"] = _now()
    rec["deleted_by"] = actor
    rec["updated_at"] = _now()
    _write(_ASSIGNMENTS_PATH, data)
    return True


def restore_assignment(tenant_id: str, assignment_id: str) -> dict[str, Any] | None:
    data = _read(_ASSIGNMENTS_PATH, "assignments")
    rec = data.get("assignments", {}).get(assignment_id)
    if rec is None or (rec.get("tenant_id") or "default") != (tenant_id or "default"):
        return None
    if not rec.get("deleted_at"):
        return None
    rec["deleted_at"] = ""
    rec["deleted_by"] = ""
    rec["updated_at"] = _now()
    _write(_ASSIGNMENTS_PATH, data)
    return get_assignment(tenant_id, assignment_id)


def purge_assignment(tenant_id: str, assignment_id: str) -> bool:
    data = _read(_ASSIGNMENTS_PATH, "assignments")
    rec = data.get("assignments", {}).get(assignment_id)
    if rec is None or (rec.get("tenant_id") or "default") != (tenant_id or "default"):
        return False
    if not rec.get("deleted_at"):
        return False
    del data["assignments"][assignment_id]
    _write(_ASSIGNMENTS_PATH, data)
    return True


def empty_assignment_trash(tenant_id: str) -> int:
    data = _read(_ASSIGNMENTS_PATH, "assignments")
    assignments = data.get("assignments", {})
    trashed = [
        aid for aid, rec in assignments.items()
        if rec.get("deleted_at") and (rec.get("tenant_id") or "default") == (tenant_id or "default")
    ]
    for aid in trashed:
        del assignments[aid]
    if trashed:
        _write(_ASSIGNMENTS_PATH, data)
    return len(trashed)


def attest_assignment(tenant_id: str, assignment_id: str, *, actor: str = "") -> dict[str, Any] | None:
    """Record that the owner confirmed they still own this subject (attestation/recert)."""
    data = _read(_ASSIGNMENTS_PATH, "assignments")
    rec = data.get("assignments", {}).get(assignment_id)
    if rec is None or (rec.get("tenant_id") or "default") != (tenant_id or "default"):
        return None
    if rec.get("deleted_at"):
        return None
    rec["attested_at"] = _now()
    rec["attested_by"] = actor
    rec["updated_at"] = _now()
    _write(_ASSIGNMENTS_PATH, data)
    return get_assignment(tenant_id, assignment_id)


def delete_all_for_tenant(tenant_id: str) -> int:
    """Hard-delete every owner + assignment for a tenant (used by demo purge). Count removed."""
    removed = 0
    odata = _read(_OWNERS_PATH, "owners")
    owners = odata.get("owners", {})
    drop = [oid for oid, r in owners.items() if (r.get("tenant_id") or "default") == (tenant_id or "default")]
    for oid in drop:
        del owners[oid]
    removed += len(drop)
    if drop:
        _write(_OWNERS_PATH, odata)
    adata = _read(_ASSIGNMENTS_PATH, "assignments")
    assignments = adata.get("assignments", {})
    dropa = [aid for aid, r in assignments.items() if (r.get("tenant_id") or "default") == (tenant_id or "default")]
    for aid in dropa:
        del assignments[aid]
    removed += len(dropa)
    if dropa:
        _write(_ASSIGNMENTS_PATH, adata)
    return removed
