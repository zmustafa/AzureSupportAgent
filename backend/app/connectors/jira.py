"""Jira connector: create issues, comment, and search via the Jira Cloud REST API v3.

Auth: base URL + account email + API token (Basic auth). Create a token at
https://id.atlassian.com/manage-profile/security/api-tokens.
"""
from __future__ import annotations

import base64
import json
from typing import Any

import httpx

from app.connectors.base import ConnectorTool, ConnectorType, FieldSpec, err, ok


def _client(config: dict[str, Any]) -> tuple[httpx.AsyncClient | None, str | None]:
    base = (config.get("base_url", "") or "").rstrip("/")
    email = config.get("email", "")
    token = config.get("api_token", "")
    if not (base and email and token):
        return None, "Jira needs base URL, account email, and API token."
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
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


def _adf(text: str) -> dict[str, Any]:
    """Wrap plain text in an Atlassian Document Format paragraph."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text or ""}]}
        ],
    }


async def _create_issue(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    client, cerr = _client(config)
    if cerr or client is None:
        return err(cerr or "Jira not configured.")
    project = args.get("project_key") or config.get("default_project", "")
    issuetype = args.get("issue_type") or config.get("default_issue_type", "Task")
    summary = args.get("summary", "")
    if not (project and summary):
        return err("project_key and summary are required.")
    payload = {
        "fields": {
            "project": {"key": project},
            "summary": summary,
            "issuetype": {"name": issuetype},
            "description": _adf(args.get("description", "")),
        }
    }
    async with client:
        resp = await client.post("/rest/api/3/issue", json=payload)
    if resp.status_code >= 300:
        return err(f"Jira create failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    return ok(f"Created Jira issue {data.get('key')} (id {data.get('id')}).")


async def _add_comment(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    client, cerr = _client(config)
    if cerr or client is None:
        return err(cerr or "Jira not configured.")
    key = args.get("issue_key", "")
    body = args.get("comment", "")
    if not (key and body):
        return err("issue_key and comment are required.")
    async with client:
        resp = await client.post(
            f"/rest/api/3/issue/{key}/comment", json={"body": _adf(body)}
        )
    if resp.status_code >= 300:
        return err(f"Jira comment failed ({resp.status_code}): {resp.text[:300]}")
    return ok(f"Added comment to {key}.")


async def _search(config: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    client, cerr = _client(config)
    if cerr or client is None:
        return err(cerr or "Jira not configured.")
    jql = args.get("jql", "")
    if not jql:
        return err("jql is required.")
    async with client:
        resp = await client.post(
            "/rest/api/3/search",
            json={"jql": jql, "maxResults": int(args.get("max_results") or 20),
                  "fields": ["summary", "status", "assignee", "priority"]},
        )
    if resp.status_code >= 300:
        return err(f"Jira search failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    issues = [
        {
            "key": i.get("key"),
            "summary": (i.get("fields") or {}).get("summary"),
            "status": ((i.get("fields") or {}).get("status") or {}).get("name"),
        }
        for i in data.get("issues", [])
    ]
    return ok(json.dumps({"total": data.get("total", 0), "issues": issues}))


def _build_tools(config: dict[str, Any]) -> list[ConnectorTool]:
    return [
        ConnectorTool(
            name="jira_create_issue",
            description="Create a Jira issue (ticket).",
            parameters={
                "type": "object",
                "properties": {
                    "project_key": {"type": "string", "description": "e.g. OPS"},
                    "issue_type": {"type": "string", "description": "e.g. Task, Bug, Incident"},
                    "summary": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["summary"],
            },
            kind="write",
            handler=_create_issue,
        ),
        ConnectorTool(
            name="jira_add_comment",
            description="Add a comment to an existing Jira issue.",
            parameters={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "e.g. OPS-123"},
                    "comment": {"type": "string"},
                },
                "required": ["issue_key", "comment"],
            },
            kind="write",
            handler=_add_comment,
        ),
        ConnectorTool(
            name="jira_search",
            description="Search Jira issues with a JQL query.",
            parameters={
                "type": "object",
                "properties": {
                    "jql": {"type": "string", "description": "Jira Query Language string."},
                    "max_results": {"type": "integer"},
                },
                "required": ["jql"],
            },
            kind="read",
            handler=_search,
        ),
    ]


CONNECTOR = ConnectorType(
    id="jira",
    label="Jira",
    description="Create issues, comment, and search via the Jira Cloud REST API.",
    modes={
        "token": [
            FieldSpec(key="base_url", label="Jira base URL", type="url", placeholder="https://your-org.atlassian.net"),
            FieldSpec(key="email", label="Account email", placeholder="you@contoso.com"),
            FieldSpec(key="api_token", label="API token", type="password", secret=True),
            FieldSpec(key="default_project", label="Default project key", optional=True, placeholder="OPS"),
            FieldSpec(key="default_issue_type", label="Default issue type", optional=True, placeholder="Task"),
        ],
    },
    build_tools=_build_tools,
)
