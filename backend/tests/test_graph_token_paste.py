"""Tests for the pasted Microsoft Graph token on an ``az_cli_token`` Azure connection.

An ARM access token can't be used against Microsoft Graph, so principal/group/Entra name
resolution (RBAC Access Review, Identity) needs a separately-pasted Graph token. These cover
``get_graph_token``'s az_cli_token branch and the connection model wiring.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.azure.credentials import get_graph_token
from app.core import azure_connections as ac


def _future() -> str:
    return str(int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()))


def _past() -> str:
    return str(int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()))


async def test_pasted_graph_token_returned_when_valid():
    conn = {
        "auth_method": "az_cli_token",
        "tenant_id": "t",
        "access_token": "arm-token",
        "graph_access_token": "graph-token-xyz",
        "graph_token_expires_on": _future(),
    }
    token, err = await get_graph_token(conn)
    assert token == "graph-token-xyz" and err is None


async def test_pasted_graph_token_expired_is_rejected():
    conn = {
        "auth_method": "az_cli_token",
        "graph_access_token": "graph-token-xyz",
        "graph_token_expires_on": _past(),
    }
    token, err = await get_graph_token(conn)
    assert token is None and err and "expired" in err.lower()


async def test_no_graph_token_gives_actionable_guidance():
    conn = {"auth_method": "az_cli_token", "access_token": "arm-token"}
    token, err = await get_graph_token(conn)
    assert token is None
    assert err and "ms-graph" in err  # points the operator at the az command


def test_graph_token_is_a_secret_field_and_in_defaults():
    assert "graph_access_token" in ac._SECRET_FIELDS
    assert "graph_access_token" in ac._DEFAULTS
    assert "graph_token_expires_on" in ac._DEFAULTS


def test_public_connection_masks_graph_token():
    conn = {
        "id": "c1",
        "auth_method": "az_cli_token",
        "graph_access_token": "supersecretgraphtoken",
        "graph_token_expires_on": "2026-06-15",
    }
    pub = ac.public_connection(conn)
    assert pub["has_graph_access_token"] is True
    assert pub["graph_token_expires_on"] == "2026-06-15"
    # The raw token is never exposed — only a mask/hint.
    assert "supersecretgraphtoken" not in str(pub.values())
