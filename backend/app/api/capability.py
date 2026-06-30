"""Connection capability & blind-spot matrix endpoint.

Read-only insight into what each Azure connection can and cannot reach (ARM, Resource
Graph, Microsoft Graph, Log Analytics, Key Vault data plane, gated writes). Surfaces the
silent data-plane blind spots of pasted-token connections so users know when an answer is
running half-blind. See app.capability.probe for the inference rules."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.capability.probe import build_matrix
from app.core.security import Principal, require_permission

router = APIRouter(prefix="/capability", tags=["capability"])

read_dep = require_permission("connections.read")


@router.get("/matrix")
async def get_capability_matrix(
    live: bool = Query(False, description="Verify ARM + Microsoft Graph token acquisition for real."),
    _: Principal = Depends(read_dep),
) -> dict:
    """The capability matrix across every configured connection.

    ``live=false`` (default) infers from auth method + stored token state — instant, no
    Azure calls. ``live=true`` additionally proves ARM / Microsoft Graph reachability.
    """
    return await build_matrix(live=live)
