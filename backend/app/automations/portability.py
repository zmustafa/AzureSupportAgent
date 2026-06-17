"""Import/export of Workbook and Playbook definitions as portable JSON bundles.

A bundle is a self-contained, version-tagged JSON document that can be exported from one
instance and imported into another. Server-managed fields (ids, timestamps, created_by,
starter) are stripped on export and re-assigned on import so a bundle is environment- and
identity-agnostic.

Because a playbook step references a workbook by id, a playbook bundle also carries the
*referenced workbooks* inline. On import we first import those workbooks (de-duplicating
by content), then rewrite each step's ``workbook_id`` to the freshly assigned local id so
the imported playbook is immediately runnable.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.playbooks import registry as pb_registry
from app.workbooks import registry as wb_registry

BUNDLE_VERSION = 1
KIND_WORKBOOK = "workbook"
KIND_PLAYBOOK = "playbook"

# Fields that are server-managed and must never come from an imported file.
_SERVER_FIELDS = ("id", "created_at", "updated_at", "created_by", "starter")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strip(defn: dict[str, Any], keep: tuple[str, ...]) -> dict[str, Any]:
    """Project a registry record onto exportable fields (drops server-managed ones)."""
    return {k: defn[k] for k in keep if k in defn}


# Exportable field sets, derived from each registry's DEFAULTS minus server fields.
_WB_FIELDS = tuple(k for k in wb_registry.DEFAULTS if k not in _SERVER_FIELDS)
_PB_FIELDS = tuple(k for k in pb_registry.DEFAULTS if k not in _SERVER_FIELDS)


def export_workbook(workbook_id: str) -> dict[str, Any] | None:
    """A single-workbook bundle, or None if the id is unknown."""
    wb = wb_registry.get_workbook(workbook_id)
    if not wb:
        return None
    return {
        "format": "azsupagent.bundle",
        "version": BUNDLE_VERSION,
        "kind": KIND_WORKBOOK,
        "exported_at": _now(),
        "workbook": _strip(wb, _WB_FIELDS),
    }


def export_playbook(playbook_id: str) -> dict[str, Any] | None:
    """A playbook bundle that inlines every workbook its steps reference."""
    pb = pb_registry.get_playbook(playbook_id)
    if not pb:
        return None
    ref_ids: list[str] = []
    for step in pb.get("steps", []) or []:
        wid = step.get("workbook_id")
        if wid and wid not in ref_ids:
            ref_ids.append(wid)
    workbooks: list[dict[str, Any]] = []
    for wid in ref_ids:
        wb = wb_registry.get_workbook(wid)
        if wb:
            # Tag each inlined workbook with its ORIGINAL id so steps can be remapped.
            entry = _strip(wb, _WB_FIELDS)
            entry["_ref"] = wid
            workbooks.append(entry)
    return {
        "format": "azsupagent.bundle",
        "version": BUNDLE_VERSION,
        "kind": KIND_PLAYBOOK,
        "exported_at": _now(),
        "playbook": _strip(pb, _PB_FIELDS),
        "workbooks": workbooks,
    }


class ImportError_(ValueError):
    """Raised when a bundle is malformed or of an unsupported kind/version."""


def _validate_envelope(bundle: Any) -> dict[str, Any]:
    if not isinstance(bundle, dict):
        raise ImportError_("The file is not a valid bundle (expected a JSON object).")
    if bundle.get("format") != "azsupagent.bundle":
        raise ImportError_("Unrecognized file format. Expected an azsupagent export.")
    version = bundle.get("version")
    if version != BUNDLE_VERSION:
        raise ImportError_(f"Unsupported bundle version {version!r}. Expected {BUNDLE_VERSION}.")
    return bundle


def _unique_name(name: str, existing: set[str]) -> str:
    """Append ' (imported)', then ' (imported N)', until the name is free."""
    base = (name or "Untitled").strip()
    if base not in existing:
        return base
    candidate = f"{base} (imported)"
    n = 2
    while candidate in existing:
        candidate = f"{base} (imported {n})"
        n += 1
    return candidate


def _clean_workbook_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only known workbook fields from an imported record."""
    return {k: v for k, v in raw.items() if k in _WB_FIELDS}


def _clean_playbook_payload(raw: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in raw.items() if k in _PB_FIELDS}


def import_workbook(bundle: Any, *, actor: str) -> dict[str, Any]:
    """Import a single-workbook bundle, returning the created workbook."""
    bundle = _validate_envelope(bundle)
    if bundle.get("kind") != KIND_WORKBOOK:
        raise ImportError_("This file is not a workbook export.")
    raw = bundle.get("workbook")
    if not isinstance(raw, dict) or not str(raw.get("name", "")).strip():
        raise ImportError_("The workbook in this file is missing a name.")
    existing_names = {w.get("name", "") for w in wb_registry.list_workbooks()}
    payload = _clean_workbook_payload(raw)
    payload["name"] = _unique_name(str(payload.get("name", "")), existing_names)
    payload["created_by"] = actor
    payload["starter"] = False
    payload.pop("id", None)
    saved = wb_registry.upsert_workbook(payload)
    return saved


def import_playbook(bundle: Any, *, actor: str, tenant_id: str = "") -> dict[str, Any]:
    """Import a playbook bundle: import its inlined workbooks (de-duped by content),
    remap step references to the new local ids, then create the playbook.

    The optional ``tenant_id`` argument scopes the imported playbook to the calling
    tenant; callers MUST pass this in multi-tenant deployments to prevent a bundle
    from landing as a legacy global (tenant_id == "") record. Older callers that omit
    it keep working but yield a global playbook (legacy behavior).
    """
    bundle = _validate_envelope(bundle)
    if bundle.get("kind") != KIND_PLAYBOOK:
        raise ImportError_("This file is not a playbook export.")
    raw_pb = bundle.get("playbook")
    if not isinstance(raw_pb, dict) or not str(raw_pb.get("name", "")).strip():
        raise ImportError_("The playbook in this file is missing a name.")

    inlined = bundle.get("workbooks") or []
    # Map each inlined workbook's original ref-id -> the local id we resolve it to.
    ref_to_local: dict[str, str] = {}
    # Index current workbooks by a content signature so re-importing the same bundle
    # twice reuses existing workbooks instead of piling up duplicates.
    existing = wb_registry.list_workbooks()
    sig_to_id = {_workbook_signature(w): w["id"] for w in existing}
    existing_names = {w.get("name", "") for w in existing}

    for entry in inlined:
        if not isinstance(entry, dict):
            continue
        ref = entry.get("_ref")
        payload = _clean_workbook_payload(entry)
        if not str(payload.get("name", "")).strip():
            continue
        sig = _workbook_signature(payload)
        local_id = sig_to_id.get(sig)
        if local_id is None:
            payload["name"] = _unique_name(str(payload.get("name", "")), existing_names)
            payload["created_by"] = actor
            payload["starter"] = False
            payload.pop("id", None)
            created = wb_registry.upsert_workbook(payload)
            local_id = created["id"]
            sig_to_id[sig] = local_id
            existing_names.add(created.get("name", ""))
        if ref:
            ref_to_local[str(ref)] = local_id

    pb_payload = _clean_playbook_payload(raw_pb)
    # Remap step workbook_ids; drop a step if its workbook couldn't be resolved.
    original_step_count = len(pb_payload.get("steps", []) or [])
    new_steps: list[dict[str, Any]] = []
    for step in pb_payload.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        wid = step.get("workbook_id")
        if wid and wid in ref_to_local:
            step = dict(step)
            step["workbook_id"] = ref_to_local[wid]
            new_steps.append(step)
        elif not wid:
            new_steps.append(step)
        # else: reference unresolved -> drop the step (kept out of the playbook).
    pb_payload["steps"] = new_steps
    existing_pb_names = {p.get("name", "") for p in pb_registry.list_playbooks()}
    pb_payload["name"] = _unique_name(str(pb_payload.get("name", "")), existing_pb_names)
    pb_payload["created_by"] = actor
    pb_payload.pop("id", None)
    if tenant_id:
        pb_payload["tenant_id"] = tenant_id
    saved = pb_registry.upsert_playbook(pb_payload)
    return {
        "playbook": saved,
        "workbooks_imported": len(ref_to_local),
        "steps_dropped": original_step_count - len(new_steps),
    }


def _workbook_signature(wb: dict[str, Any]) -> str:
    """A stable content fingerprint for de-duplication (name + runtime + body + params)."""
    return json.dumps(
        {
            "name": wb.get("name", ""),
            "runtime": wb.get("runtime", ""),
            "body": wb.get("body", ""),
            "params": wb.get("params", []),
        },
        sort_keys=True,
    )
