"""AI-assisted owner import — infer which uploaded columns map to owner fields, build a
preview, and materialize owners + assignments.

The mapper accepts ANY layout (custom CSV/Excel). It maps the source columns to the owner
schema:
  display_name (required), email, department, kind, role, notes, workload, subscription,
  resource_ids.
A row may also carry a SUBJECT (workload name / subscription name / resource id(s)) so an
assignment is created linking the owner to that subject. A blank-subject row just creates the
owner in the directory to be mapped later.

The AI call is best-effort: a deterministic heuristic mapper runs first and always produces a
usable mapping; the AI refines it. So the import works even with the LLM offline.
"""
from __future__ import annotations

import json
import re
from typing import Any

# Canonical target fields the importer understands.
TARGET_FIELDS = [
    "display_name", "email", "department", "kind", "role", "notes",
    "workload", "subscription", "resource_ids",
]

# Heuristic header synonyms (lowercased substrings) → target field.
_SYNONYMS: list[tuple[str, str]] = [
    ("display_name", "display_name"), ("full name", "display_name"), ("fullname", "display_name"),
    ("name", "display_name"), ("owner", "display_name"), ("contact", "display_name"),
    ("e-mail", "email"), ("email", "email"), ("mail", "email"), ("upn", "email"),
    ("userprincipalname", "email"),
    ("department", "department"), ("dept", "department"), ("team", "department"),
    ("org", "department"), ("division", "department"), ("business unit", "department"),
    ("kind", "kind"), ("type", "kind"),
    ("role", "role"), ("lane", "role"), ("responsibility", "role"),
    ("note", "notes"), ("comment", "notes"), ("description", "notes"),
    ("workload", "workload"), ("application", "workload"), ("app name", "workload"),
    ("service", "workload"), ("system", "workload"), ("project", "workload"),
    ("subscription", "subscription"), ("sub name", "subscription"), ("sub id", "subscription"),
    ("subscriptionid", "subscription"),
    ("resource id", "resource_ids"), ("resource_id", "resource_ids"), ("resourceid", "resource_ids"),
    ("arm id", "resource_ids"), ("resource ids", "resource_ids"), ("resources", "resource_ids"),
]


def heuristic_mapping(columns: list[str]) -> dict[str, str]:
    """First-pass mapping by header synonyms. Returns {target_field: source_column}. The first
    column that matches a field wins; ``display_name`` falls back to the first column."""
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for col in columns:
        low = col.lower().strip()
        for needle, field in _SYNONYMS:
            if field in mapping:
                continue
            if needle in low:
                mapping[field] = col
                used.add(col)
                break
    if "display_name" not in mapping and columns:
        first = next((c for c in columns if c not in used), columns[0])
        mapping["display_name"] = first
    return mapping


_SYS = (
    "You map the COLUMNS of an uploaded owners spreadsheet to a fixed owner schema. "
    "Return STRICT JSON only — no prose, no markdown.\n"
    "Target fields: display_name (the person/team name; REQUIRED), email, department, kind "
    "(person|team|service), role (technical|business|security|cost|operations|escalation), "
    "notes, workload (an application/workload name), subscription (a subscription name or id), "
    "resource_ids (one or more Azure ARM resource ids, possibly comma/semicolon separated).\n"
    "Shape:\n"
    "{\n"
    '  "mapping": { "display_name": "<source column or empty>", "email": "", "department": "", '
    '"kind": "", "role": "", "notes": "", "workload": "", "subscription": "", "resource_ids": "" },\n'
    '  "confidence": 0.0,\n'
    '  "explanation": "one short sentence"\n'
    "}\n"
    "Only use column names that appear in the provided list. Leave a field empty if no column fits."
)


async def infer_mapping(columns: list[str], sample_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Heuristic mapping refined by the AI (best-effort). Always returns a usable mapping."""
    base = heuristic_mapping(columns)
    result = {"mapping": base, "confidence": 0.6, "explanation": "Matched columns by name.", "ai": False}
    try:
        from app.tagintel.ask import _complete_json

        sample = sample_rows[:8]
        user = "COLUMNS:\n" + json.dumps(columns) + "\n\nSAMPLE ROWS:\n" + json.dumps(sample, default=str)[:4000]
        out = await _complete_json(_SYS, user)
        if isinstance(out, dict) and isinstance(out.get("mapping"), dict):
            ai_map = {f: c for f, c in out["mapping"].items() if f in TARGET_FIELDS and c in columns}
            if ai_map.get("display_name"):
                merged = dict(base)
                merged.update(ai_map)
                result = {
                    "mapping": {f: merged.get(f, "") for f in TARGET_FIELDS},
                    "confidence": float(out.get("confidence", 0.8) or 0.8),
                    "explanation": str(out.get("explanation", "") or "AI-inferred column mapping."),
                    "ai": True,
                }
    except Exception:  # noqa: BLE001 — heuristic mapping is the guaranteed fallback
        pass
    # Ensure every target field key is present.
    result["mapping"] = {f: result["mapping"].get(f, "") for f in TARGET_FIELDS}
    return result


def _split_ids(val: str) -> list[str]:
    return [p.strip() for p in re.split(r"[;,\n]", val or "") if p.strip()]


def build_preview(rows: list[dict[str, Any]], mapping: dict[str, str], *, limit: int = 200) -> dict[str, Any]:
    """Project rows through the mapping into owner records + resolved subject hints. Reports
    which rows are valid (have a name) and the subject linkage each row carries."""
    name_col = mapping.get("display_name", "")
    out: list[dict[str, Any]] = []
    valid = 0
    with_subject = 0
    for raw in rows[:limit]:
        name = (raw.get(name_col, "") if name_col else "").strip()
        kind = (raw.get(mapping.get("kind", ""), "") or "").strip().lower()
        if kind not in ("person", "team", "service"):
            kind = "team" if any(w in name.lower() for w in ("team", "squad", "group")) else "person"
        resource_ids = _split_ids(raw.get(mapping.get("resource_ids", ""), "")) if mapping.get("resource_ids") else []
        workload = (raw.get(mapping.get("workload", ""), "") or "").strip()
        subscription = (raw.get(mapping.get("subscription", ""), "") or "").strip()
        rec = {
            "display_name": name,
            "email": (raw.get(mapping.get("email", ""), "") or "").strip(),
            "department": (raw.get(mapping.get("department", ""), "") or "").strip(),
            "kind": kind,
            "role": (raw.get(mapping.get("role", ""), "") or "").strip().lower() or "technical",
            "notes": (raw.get(mapping.get("notes", ""), "") or "").strip(),
            "workload": workload,
            "subscription": subscription,
            "resource_ids": resource_ids,
        }
        has_subject = bool(workload or subscription or resource_ids)
        rec["valid"] = bool(name)
        rec["has_subject"] = has_subject
        if rec["valid"]:
            valid += 1
        if has_subject:
            with_subject += 1
        out.append(rec)
    return {
        "rows": out,
        "total": len(rows),
        "preview_count": len(out),
        "valid": valid,
        "invalid": len(out) - valid,
        "with_subject": with_subject,
    }


def _resolve_workload_id(name: str, workloads: list[dict[str, Any]]) -> tuple[str, str]:
    """Best-effort workload name → (id, canonical name). Exact (case-insensitive) match first,
    then a unique substring match. Returns ("","") when unresolved/ambiguous."""
    if not name:
        return "", ""
    low = name.strip().lower()
    exact = [w for w in workloads if (w.get("name", "") or "").strip().lower() == low]
    if len(exact) == 1:
        return exact[0]["id"], exact[0].get("name", "")
    partial = [w for w in workloads if low in (w.get("name", "") or "").strip().lower()]
    if len(partial) == 1:
        return partial[0]["id"], partial[0].get("name", "")
    return "", ""


def materialize_import(
    tenant_id: str,
    preview_rows: list[dict[str, Any]],
    *,
    actor: str = "",
    create_assignments: bool = True,
) -> dict[str, Any]:
    """Create/update owners (deduped by email, else display_name) and — when a row carries a
    subject — the matching assignments. Returns a summary with per-category counts."""
    from app.ownership import registry

    existing = registry.list_owners(tenant_id, include_deleted=False)
    by_email = {(o.get("email", "") or "").lower(): o for o in existing if o.get("email")}
    by_name = {(o.get("display_name", "") or "").lower(): o for o in existing}

    try:
        from app.workloads.registry import list_workloads
        workloads = list_workloads()
    except Exception:  # noqa: BLE001
        workloads = []

    created = 0
    updated = 0
    assignments_made = 0
    unresolved_subjects: list[str] = []
    skipped = 0

    for rec in preview_rows:
        if not rec.get("valid"):
            skipped += 1
            continue
        email = (rec.get("email", "") or "").lower()
        name = (rec.get("display_name", "") or "").strip()
        match = by_email.get(email) if email else None
        if match is None:
            match = by_name.get(name.lower())
        payload = {
            "display_name": name,
            "email": rec.get("email", ""),
            "department": rec.get("department", ""),
            "kind": rec.get("kind", "person"),
            "notes": rec.get("notes", ""),
            "source": "manual",
            "created_by": actor,
        }
        if match is not None:
            payload["id"] = match["id"]
            owner = registry.upsert_owner(tenant_id, payload)
            updated += 1
        else:
            owner = registry.upsert_owner(tenant_id, payload)
            created += 1
            by_name[name.lower()] = owner
            if email:
                by_email[email] = owner

        if not create_assignments:
            continue
        role = rec.get("role", "technical") or "technical"
        # Resource id assignments.
        for rid in rec.get("resource_ids", []) or []:
            registry.upsert_assignment(tenant_id, {
                "owner_id": owner["id"], "subject_kind": "resource", "subject_id": rid,
                "subject_name": rid.rsplit("/", 1)[-1], "role": role, "primary": True,
                "source": "import", "created_by": actor,
            })
            assignments_made += 1
        # Workload assignment (resolve name → id).
        wl_name = rec.get("workload", "")
        if wl_name:
            wid, canon = _resolve_workload_id(wl_name, workloads)
            if wid:
                registry.upsert_assignment(tenant_id, {
                    "owner_id": owner["id"], "subject_kind": "workload", "subject_id": wid,
                    "subject_name": canon, "role": role, "primary": True,
                    "source": "import", "created_by": actor,
                })
                assignments_made += 1
            else:
                unresolved_subjects.append(f"workload:{wl_name}")
        # Subscription assignment (value may be a name or a guid — store as-is).
        sub = rec.get("subscription", "")
        if sub:
            registry.upsert_assignment(tenant_id, {
                "owner_id": owner["id"], "subject_kind": "subscription", "subject_id": sub,
                "subject_name": sub, "role": role, "primary": True,
                "source": "import", "created_by": actor,
            })
            assignments_made += 1

    return {
        "created": created,
        "updated": updated,
        "assignments": assignments_made,
        "skipped": skipped,
        "unresolved_subjects": unresolved_subjects[:50],
    }
