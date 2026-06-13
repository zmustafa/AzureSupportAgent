"""Demo connectors for review/marketing without wiring live external services.

Seeds a representative spread of *disabled* connectors (Teams, Slack, Jira, ServiceNow,
PagerDuty, Splunk) with placeholder config so the Connectors screen looks populated for
demos/screenshots. They are ``disabled: True`` and carry obviously-fake config, so they
can never actually fire. Fixed ids let the seed be idempotent and the purge exact.
Seeded/removed alongside the rest of the demo dataset (Settings → Demo Data).
"""
from __future__ import annotations

from typing import Any

# (id, type, mode, name, config) — placeholder config only; all seeded disabled.
_DEMO_CONNECTORS: list[tuple[str, str, str, str, dict[str, str]]] = [
    (
        "demo-conn-teams", "teams", "webhook", "Ops Teams (demo)",
        {"webhook_url": "https://contoso.webhook.office.com/webhookb2/demo-not-real"},
    ),
    (
        "demo-conn-slack", "slack", "webhook", "SRE Slack (demo)",
        {"webhook_url": "https://hooks.slack.com/services/T000DEMO/B000DEMO/xxxxxxxxDEMO"},
    ),
    (
        "demo-conn-jira", "jira", "token", "Platform Jira (demo)",
        {
            "base_url": "https://contoso-demo.atlassian.net",
            "email": "agent@contoso.com",
            "api_token": "demo-token-not-real",
            "default_project": "OPS",
            "default_issue_type": "Task",
        },
    ),
    (
        "demo-conn-servicenow", "servicenow", "basic", "ITSM ServiceNow (demo)",
        {
            "instance_url": "https://contoso-demo.service-now.com",
            "username": "svc_azure_agent",
            "password": "demo-not-real",
            "default_assignment_group": "Cloud Platform",
        },
    ),
    (
        "demo-conn-pagerduty", "pagerduty", "events_v2", "On-call PagerDuty (demo)",
        {"routing_key": "demo-routing-key-not-real", "default_source": "azsupagent"},
    ),
    (
        "demo-conn-splunk", "splunk", "hec", "SOC Splunk (demo)",
        {
            "hec_url": "https://splunk-demo.contoso.com:8088",
            "hec_token": "00000000-0000-0000-0000-00000000demo",
            "default_index": "main",
            "default_sourcetype": "azsupagent",
        },
    ),
]

DEMO_CONNECTOR_IDS = [c[0] for c in _DEMO_CONNECTORS]
_DEMO_DETAIL = "Demo connector — placeholder config, disabled so it never fires."


def seed_demo() -> list[dict[str, Any]]:
    """Create/refresh the disabled demo connectors. Idempotent."""
    from app.connectors.registry import update_status, upsert_connector

    out: list[dict[str, Any]] = []
    for cid, ctype, mode, name, config in _DEMO_CONNECTORS:
        saved = upsert_connector(
            {"id": cid, "name": name, "type": ctype, "mode": mode, "disabled": True, "config": dict(config)}
        )
        update_status(cid, "unknown", _DEMO_DETAIL)
        out.append(saved)
    return out


def purge_demo() -> int:
    """Delete the demo connectors. Returns how many were removed."""
    from app.connectors.registry import delete_connector

    removed = 0
    for cid in DEMO_CONNECTOR_IDS:
        if delete_connector(cid):
            removed += 1
    return removed
