"""ServiceNow connector: create/update incidents, add work notes, and search via the
ServiceNow Table API.

Auth: instance URL + username + password (Basic auth) against the REST Table API
(``/api/now/table/...``). Use a dedicated integration user with the ``itil`` (or a
scoped) role. OAuth can be layered on later; Basic is the standard starting point.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

import httpx

from app.connectors.base import ConnectorTool, ConnectorType, FieldSpec, err, ok

# Incident numbers are alphanumeric with optional separators (e.g. INC0010023). We
# reject anything else so a value can't smuggle ServiceNow encoded-query operators
# (notably "^") into a sysparm_query and target a different record.
_NUMBER_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def _valid_number(number: str) -> bool:
    return bool(_NUMBER_RE.match((number or "").strip()))


def _client(config: dict[str, Any]) -> tuple[httpx.AsyncClient | None, str | None]:
    base = (config.get("instance_url", "") or "").rstrip("/")
    user = config.get("username", "")
    password = config.get("password", "")
    if not (base and user and password):
        return None, "ServiceNow needs instance URL, username, and password."
    if not base.startswith("http"):
        base = f"https://{base}"
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    return (
        httpx.AsyncClient(
            base_url=base,
            timeout=30,
            headers={
                "Authorization": f"Basic {auth}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        ),
        None,
    )


def _incident_summary(rec: dict[str, Any]) -> dict[str, Any]:
    """Trim a ServiceNow incident record to the fields the model cares about."""
    return {
        "number": rec.get("number"),
        "sys_id": rec.get("sys_id"),
        "short_description": rec.get("short_description"),
        "state": rec.get("state"),
        "priority": rec.get("priority"),
        "assigned_to": (rec.get("assigned_to") or {}).get("value")
        if isinstance(rec.get("assigned_to"), dict)
        else rec.get("assigned_to"),
        "opened_at": rec.get("opened_at"),
    }


async def _create_incident(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    client, cerr = _client(config)
    if cerr or client is None:
        return err(cerr or "ServiceNow not configured.")
    short_description = args.get("short_description", "")
    if not short_description:
        return err("short_description is required.")
    payload: dict[str, Any] = {"short_description": short_description}
    if args.get("description"):
        payload["description"] = args["description"]
    # Allow caller overrides, else fall back to connector defaults.
    urgency = args.get("urgency") or config.get("default_urgency", "")
    impact = args.get("impact") or config.get("default_impact", "")
    assignment_group = args.get("assignment_group") or config.get("default_assignment_group", "")
    caller_id = args.get("caller_id") or config.get("default_caller_id", "")
    if urgency:
        payload["urgency"] = str(urgency)
    if impact:
        payload["impact"] = str(impact)
    if assignment_group:
        payload["assignment_group"] = assignment_group
    if caller_id:
        payload["caller_id"] = caller_id
    async with client:
        resp = await client.post("/api/now/table/incident", json=payload)
    if resp.status_code >= 300:
        return err(f"ServiceNow create failed ({resp.status_code}): {resp.text[:300]}")
    rec = (resp.json() or {}).get("result", {})
    return ok(
        f"Created ServiceNow incident {rec.get('number')} (sys_id {rec.get('sys_id')})."
    )


async def _add_work_note(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    client, cerr = _client(config)
    if cerr or client is None:
        return err(cerr or "ServiceNow not configured.")
    number = args.get("number", "")
    note = args.get("note", "")
    if not (number and note):
        return err("number and note are required.")
    if not _valid_number(number):
        return err("Invalid incident number format.")
    # Public "comment" (visible to caller) vs internal "work_notes".
    field = "comments" if args.get("public") else "work_notes"
    client_ref = client
    async with client_ref:
        # Resolve the incident sys_id from its number.
        lookup = await client_ref.get(
            "/api/now/table/incident",
            params={"sysparm_query": f"number={number}", "sysparm_limit": 1,
                    "sysparm_fields": "sys_id,number"},
        )
        if lookup.status_code >= 300:
            return err(f"ServiceNow lookup failed ({lookup.status_code}): {lookup.text[:200]}")
        results = (lookup.json() or {}).get("result", [])
        if not results:
            return err(f"Incident {number} not found.")
        sys_id = results[0].get("sys_id")
        resp = await client_ref.patch(
            f"/api/now/table/incident/{sys_id}", json={field: note}
        )
    if resp.status_code >= 300:
        return err(f"ServiceNow note failed ({resp.status_code}): {resp.text[:300]}")
    label = "comment" if args.get("public") else "work note"
    return ok(f"Added {label} to {number}.")


async def _update_incident(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    client, cerr = _client(config)
    if cerr or client is None:
        return err(cerr or "ServiceNow not configured.")
    number = args.get("number", "")
    if not number:
        return err("number is required.")
    if not _valid_number(number):
        return err("Invalid incident number format.")
    fields: dict[str, Any] = {}
    for key in ("state", "priority", "urgency", "impact", "assignment_group", "assigned_to"):
        if args.get(key) not in (None, ""):
            fields[key] = str(args[key])
    if args.get("close_notes"):
        fields["close_notes"] = args["close_notes"]
    if args.get("close_code"):
        fields["close_code"] = args["close_code"]
    if not fields:
        return err("Provide at least one field to update (e.g. state, priority).")
    async with client:
        lookup = await client.get(
            "/api/now/table/incident",
            params={"sysparm_query": f"number={number}", "sysparm_limit": 1,
                    "sysparm_fields": "sys_id"},
        )
        if lookup.status_code >= 300:
            return err(f"ServiceNow lookup failed ({lookup.status_code}): {lookup.text[:200]}")
        results = (lookup.json() or {}).get("result", [])
        if not results:
            return err(f"Incident {number} not found.")
        sys_id = results[0].get("sys_id")
        resp = await client.patch(f"/api/now/table/incident/{sys_id}", json=fields)
    if resp.status_code >= 300:
        return err(f"ServiceNow update failed ({resp.status_code}): {resp.text[:300]}")
    return ok(f"Updated incident {number}: {', '.join(fields.keys())}.")


async def _get_incident(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    client, cerr = _client(config)
    if cerr or client is None:
        return err(cerr or "ServiceNow not configured.")
    number = args.get("number", "")
    if not number:
        return err("number is required.")
    if not _valid_number(number):
        return err("Invalid incident number format.")
    async with client:
        resp = await client.get(
            "/api/now/table/incident",
            params={"sysparm_query": f"number={number}", "sysparm_limit": 1,
                    "sysparm_display_value": "true"},
        )
    if resp.status_code >= 300:
        return err(f"ServiceNow get failed ({resp.status_code}): {resp.text[:300]}")
    results = (resp.json() or {}).get("result", [])
    if not results:
        return err(f"Incident {number} not found.")
    rec = results[0]
    return ok(json.dumps(_incident_summary(rec)))


async def _search_incidents(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    client, cerr = _client(config)
    if cerr or client is None:
        return err(cerr or "ServiceNow not configured.")
    query = args.get("query", "")  # encoded query, e.g. "active=true^priority=1"
    limit = int(args.get("max_results") or 20)
    params = {
        "sysparm_limit": limit,
        "sysparm_display_value": "true",
        "sysparm_fields": "number,sys_id,short_description,state,priority,assigned_to,opened_at",
    }
    if query:
        params["sysparm_query"] = query
    async with client:
        resp = await client.get("/api/now/table/incident", params=params)
    if resp.status_code >= 300:
        return err(f"ServiceNow search failed ({resp.status_code}): {resp.text[:300]}")
    results = (resp.json() or {}).get("result", [])
    incidents = [_incident_summary(r) for r in results]
    return ok(json.dumps({"count": len(incidents), "incidents": incidents}))


def _build_tools(config: dict[str, Any]) -> list[ConnectorTool]:
    return [
        ConnectorTool(
            name="servicenow_create_incident",
            description="Create a ServiceNow incident (ticket).",
            parameters={
                "type": "object",
                "properties": {
                    "short_description": {"type": "string", "description": "One-line summary of the incident."},
                    "description": {"type": "string", "description": "Detailed description / findings."},
                    "urgency": {"type": "string", "description": "1=High, 2=Medium, 3=Low."},
                    "impact": {"type": "string", "description": "1=High, 2=Medium, 3=Low."},
                    "assignment_group": {"type": "string", "description": "Assignment group sys_id or name."},
                    "caller_id": {"type": "string", "description": "Caller sys_id or user id."},
                },
                "required": ["short_description"],
            },
            kind="write",
            handler=_create_incident,
        ),
        ConnectorTool(
            name="servicenow_add_work_note",
            description="Add a work note (internal) or comment (customer-visible) to an incident.",
            parameters={
                "type": "object",
                "properties": {
                    "number": {"type": "string", "description": "Incident number, e.g. INC0010023."},
                    "note": {"type": "string"},
                    "public": {"type": "boolean", "description": "If true, post as a customer-visible comment; otherwise an internal work note."},
                },
                "required": ["number", "note"],
            },
            kind="write",
            handler=_add_work_note,
        ),
        ConnectorTool(
            name="servicenow_update_incident",
            description="Update fields on an incident (state, priority, assignment, close notes/code).",
            parameters={
                "type": "object",
                "properties": {
                    "number": {"type": "string", "description": "Incident number, e.g. INC0010023."},
                    "state": {"type": "string", "description": "Incident state value, e.g. 2 (In Progress), 6 (Resolved), 7 (Closed)."},
                    "priority": {"type": "string"},
                    "urgency": {"type": "string"},
                    "impact": {"type": "string"},
                    "assignment_group": {"type": "string"},
                    "assigned_to": {"type": "string"},
                    "close_code": {"type": "string", "description": "Required by some instances when resolving."},
                    "close_notes": {"type": "string"},
                },
                "required": ["number"],
            },
            kind="write",
            handler=_update_incident,
        ),
        ConnectorTool(
            name="servicenow_get_incident",
            description="Fetch a single ServiceNow incident by number.",
            parameters={
                "type": "object",
                "properties": {
                    "number": {"type": "string", "description": "Incident number, e.g. INC0010023."},
                },
                "required": ["number"],
            },
            kind="read",
            handler=_get_incident,
        ),
        ConnectorTool(
            name="servicenow_search_incidents",
            description="Search ServiceNow incidents using an encoded query (e.g. 'active=true^priority=1').",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "ServiceNow encoded query string."},
                    "max_results": {"type": "integer"},
                },
                "required": [],
            },
            kind="read",
            handler=_search_incidents,
        ),
    ]


CONNECTOR = ConnectorType(
    id="servicenow",
    label="ServiceNow",
    description="Create/update incidents, add work notes, and search via the ServiceNow Table API.",
    modes={
        "basic": [
            FieldSpec(
                key="instance_url",
                label="Instance URL",
                type="url",
                placeholder="https://your-instance.service-now.com",
            ),
            FieldSpec(key="username", label="Integration username", placeholder="svc_azure_agent"),
            FieldSpec(key="password", label="Password", type="password", secret=True),
            FieldSpec(
                key="default_assignment_group",
                label="Default assignment group",
                optional=True,
                placeholder="sys_id or name",
            ),
            FieldSpec(
                key="default_caller_id",
                label="Default caller (user id / sys_id)",
                optional=True,
            ),
            FieldSpec(
                key="default_urgency",
                label="Default urgency",
                optional=True,
                placeholder="1=High, 2=Medium, 3=Low",
            ),
            FieldSpec(
                key="default_impact",
                label="Default impact",
                optional=True,
                placeholder="1=High, 2=Medium, 3=Low",
            ),
        ],
    },
    build_tools=_build_tools,
)
