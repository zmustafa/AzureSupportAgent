"""Policy exemption management — build/validate/apply create·extend·remove operations.

Exemptions are a security-sensitive control (they waive a policy assignment at a scope), so every
write goes through guardrails (justification + expiry window) and produces BOTH the exact ARM REST
request and the equivalent ``az policy exemption`` CLI for transparency. ``plan`` is read-only
(safe on any connection); ``apply`` / ``remove`` mutate Azure via the connection's ARM token and
are gated by the caller (policy.write + connection not read-only).

ARM resource: ``{scope}/providers/Microsoft.Authorization/policyExemptions/{name}`` (PUT/DELETE),
api-version 2022-07-01-preview."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

_API_VERSION = "2022-07-01-preview"
_CATEGORIES = {"Waiver", "Mitigated"}
_NAME_RE = re.compile(r"^[A-Za-z0-9._\-() ]{1,128}$")


# --------------------------------------------------------------------------- guardrails
def load_guardrails() -> dict[str, Any]:
    """Read the exemption guardrails from app settings (with safe fallbacks)."""
    try:
        from app.core.app_settings import load_settings

        s = load_settings()
        return {
            "require_justification": bool(s.get("policy_exemption_require_justification", True)),
            "max_expiry_days": int(s.get("policy_exemption_max_expiry_days", 180) or 0),
            "block_never_expires": bool(s.get("policy_exemption_block_never_expires", True)),
        }
    except Exception:  # noqa: BLE001
        return {"require_justification": True, "max_expiry_days": 180, "block_never_expires": True}


def _parse_expiry(expires_on: str) -> tuple[datetime | None, str | None]:
    s = (expires_on or "").strip()
    if not s:
        return None, None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt, None
    except (ValueError, TypeError):
        return None, f"Invalid expiry date: {s!r} (use ISO 8601, e.g. 2026-12-31T00:00:00Z)."


def validate(payload: dict[str, Any], action: str, guardrails: dict[str, Any]) -> list[str]:
    """Return a list of guardrail violations for a create/extend payload (empty = OK).

    ``action`` ∈ {create, update}. ``remove`` has no payload validation (just an id)."""
    errors: list[str] = []
    scope = (payload.get("scope") or "").strip()
    assignment_id = (payload.get("policy_assignment_id") or "").strip()
    category = (payload.get("category") or "Waiver").strip()
    description = (payload.get("description") or "").strip()
    display_name = (payload.get("display_name") or "").strip()
    expires_on = (payload.get("expires_on") or "").strip()
    name = (payload.get("name") or "").strip()

    if action == "create":
        if not scope:
            errors.append("A target scope is required.")
        if not assignment_id:
            errors.append("A target policy assignment is required.")
        if name and not _NAME_RE.match(name):
            errors.append("Exemption name has invalid characters (allowed: letters, digits, . _ - ( ) space).")
    if category not in _CATEGORIES:
        errors.append(f"Category must be one of {sorted(_CATEGORIES)} (got {category!r}).")
    if not display_name:
        errors.append("A display name is required.")
    if guardrails.get("require_justification") and not description:
        errors.append("A justification (description) is required by policy.")

    dt, perr = _parse_expiry(expires_on)
    if perr:
        errors.append(perr)
    elif dt is None:
        # No expiry.
        if guardrails.get("block_never_expires"):
            errors.append("Never-expiring exemptions are blocked by policy — set an expiry date.")
    else:
        now = datetime.now(timezone.utc)
        if dt <= now:
            errors.append("Expiry must be in the future.")
        max_days = int(guardrails.get("max_expiry_days") or 0)
        if max_days > 0 and (dt - now).days > max_days:
            errors.append(f"Expiry exceeds the maximum allowed window of {max_days} days.")
    return errors


# --------------------------------------------------------------------------- builders
def exemption_name(payload: dict[str, Any]) -> str:
    """The resource name (slug) for the exemption — supplied, or a generated guid."""
    n = (payload.get("name") or "").strip()
    return n or f"exempt-{uuid.uuid4().hex[:12]}"


def arm_path(scope: str, name: str) -> str:
    """The ARM resource path for an exemption under a scope."""
    return f"{scope.rstrip('/')}/providers/Microsoft.Authorization/policyExemptions/{name}"


def build_body(payload: dict[str, Any]) -> dict[str, Any]:
    """The ARM request body for a PUT (create/update)."""
    props: dict[str, Any] = {
        "policyAssignmentId": payload.get("policy_assignment_id", ""),
        "exemptionCategory": payload.get("category") or "Waiver",
        "displayName": payload.get("display_name") or "",
        "description": payload.get("description") or "",
    }
    expires = (payload.get("expires_on") or "").strip()
    if expires:
        props["expiresOn"] = expires
    refs = payload.get("reference_ids")
    if isinstance(refs, list) and refs:
        props["policyDefinitionReferenceIds"] = [str(x) for x in refs]
    return {"properties": props}


def _shell_quote(v: str) -> str:
    v = v or ""
    return '"' + v.replace('"', '\\"') + '"'


def build_cli(action: str, payload: dict[str, Any], *, name: str = "", exemption_id: str = "") -> str:
    """The equivalent ``az policy exemption`` command for transparency / copy-paste."""
    if action == "remove":
        # Delete by full resource id (scope is embedded).
        return f"az policy exemption delete --ids {_shell_quote(exemption_id)}"
    scope = payload.get("scope", "")
    verb = "create" if action == "create" else "update"
    parts = [
        "az policy exemption", verb,
        f"--name {_shell_quote(name)}",
        f"--scope {_shell_quote(scope)}",
        f"--policy-assignment {_shell_quote(payload.get('policy_assignment_id', ''))}",
        f"--exemption-category {payload.get('category') or 'Waiver'}",
        f"--display-name {_shell_quote(payload.get('display_name') or '')}",
        f"--description {_shell_quote(payload.get('description') or '')}",
    ]
    expires = (payload.get("expires_on") or "").strip()
    if expires:
        parts.append(f"--expires-on {_shell_quote(expires)}")
    refs = payload.get("reference_ids")
    if isinstance(refs, list) and refs:
        parts.append("--policy-definition-reference-ids " + " ".join(_shell_quote(str(r)) for r in refs))
    return " ".join(parts)


def plan(payload: dict[str, Any], action: str) -> dict[str, Any]:
    """Build the full plan for an exemption operation WITHOUT mutating anything.

    Returns {action, valid, errors, arm: {method, path, api_version, body?}, cli, name, scope}."""
    guardrails = load_guardrails()
    if action == "remove":
        eid = (payload.get("id") or "").strip()
        errors = [] if eid else ["An exemption id is required to remove."]
        return {
            "action": "remove",
            "valid": not errors,
            "errors": errors,
            "arm": {"method": "DELETE", "path": eid, "api_version": _API_VERSION},
            "cli": build_cli("remove", payload, exemption_id=eid),
            "name": eid.rstrip("/").split("/")[-1] if eid else "",
            "scope": "",
            "guardrails": guardrails,
        }

    errors = validate(payload, action, guardrails)
    name = exemption_name(payload) if action == "create" else (payload.get("name") or exemption_name(payload))
    scope = payload.get("scope", "")
    path = arm_path(scope, name) if scope else (payload.get("id") or "")
    return {
        "action": action,
        "valid": not errors,
        "errors": errors,
        "arm": {"method": "PUT", "path": path, "api_version": _API_VERSION, "body": build_body(payload)},
        "cli": build_cli(action, payload, name=name),
        "name": name,
        "scope": scope,
        "guardrails": guardrails,
    }


# --------------------------------------------------------------------------- execute
async def apply(connection: dict[str, Any] | None, payload: dict[str, Any], action: str) -> dict[str, Any]:
    """Execute a create/update via ARM PUT. Returns {ok, resource?, error?, plan}.

    Re-validates server-side (never trust the client). Caller must enforce policy.write +
    connection-not-read-only BEFORE calling this."""
    p = plan(payload, action)
    if not p["valid"]:
        return {"ok": False, "error": "; ".join(p["errors"]), "plan": p}
    if connection is None:
        return {"ok": False, "error": "No Azure connection configured.", "plan": p}

    from app.azure.arm import arm_write
    from app.azure.credentials import get_arm_token

    token, terr = await get_arm_token(connection)
    if terr or not token:
        return {"ok": False, "error": terr or "No ARM token.", "plan": p}

    data, err, status = await arm_write(
        token, "PUT", p["arm"]["path"], body=p["arm"]["body"], api_version=_API_VERSION,
    )
    if err:
        return {"ok": False, "error": err, "status": status, "plan": p}
    return {"ok": True, "resource": data, "status": status, "plan": p}


async def remove(connection: dict[str, Any] | None, exemption_id: str) -> dict[str, Any]:
    """Delete an exemption by ARM id via ARM DELETE. Returns {ok, error?}."""
    eid = (exemption_id or "").strip()
    if not eid:
        return {"ok": False, "error": "An exemption id is required."}
    if connection is None:
        return {"ok": False, "error": "No Azure connection configured."}

    from app.azure.arm import arm_write
    from app.azure.credentials import get_arm_token

    token, terr = await get_arm_token(connection)
    if terr or not token:
        return {"ok": False, "error": terr or "No ARM token."}

    _data, err, status = await arm_write(token, "DELETE", eid, api_version=_API_VERSION)
    if err:
        return {"ok": False, "error": err, "status": status}
    return {"ok": True, "status": status}
