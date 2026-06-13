"""Lightweight layered auto-layout for architecture diagrams.

Assigns each node an (x, y) so the diagram reads top→bottom by architectural tier
(edge → presentation → application → integration → data → networking → security →
monitoring → shared). Within a tier, nodes spread left→right. Used for AI output (which
may omit coordinates) and the manual "Tidy" button. Pure, deterministic, no deps.
"""
from __future__ import annotations

from typing import Any

from app.architectures.catalog import LAYER_ORDER

_COL_W = 260       # horizontal spacing between nodes in a tier
_ROW_H = 150       # vertical spacing between tiers
_X0 = 80
_Y0 = 80


def layout_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the nodes with computed x/y by tier. Mutates copies, preserves order."""
    by_layer: dict[str, list[dict[str, Any]]] = {}
    for n in nodes:
        layer = n.get("layer") or "shared"
        if layer not in LAYER_ORDER:
            layer = "shared"
        by_layer.setdefault(layer, []).append(n)

    out: list[dict[str, Any]] = []
    row = 0
    for layer in LAYER_ORDER:
        group = by_layer.get(layer)
        if not group:
            continue
        for col, n in enumerate(group):
            m = dict(n)
            m["x"] = _X0 + col * _COL_W
            m["y"] = _Y0 + row * _ROW_H
            out.append(m)
        row += 1
    return out


def needs_layout(nodes: list[dict[str, Any]]) -> bool:
    """True when nodes lack meaningful coordinates (all at/near origin or missing)."""
    if not nodes:
        return False
    positioned = [
        n for n in nodes
        if isinstance(n.get("x"), (int, float)) and isinstance(n.get("y"), (int, float))
        and (abs(float(n["x"])) > 1 or abs(float(n["y"])) > 1)
    ]
    return len(positioned) < max(2, len(nodes) // 2)
