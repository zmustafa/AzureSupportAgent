"""Render a (subset of) Mermaid flowcharts to a PNG, fully locally with Pillow.

Server-side PDFs (chat-transcript ticket attachments) can't run the browser-based mermaid.js the
UI uses, and we deliberately avoid Node / network rendering services. This module parses the most
common diagram the assistant emits — ``graph``/``flowchart`` with nodes and directed edges — and
draws it with a small layered layout. Anything it can't parse (sequence/class/gantt/etc.) returns
``None`` so the caller can fall back to showing the raw source as a code block.
"""
from __future__ import annotations

import io
import re
from typing import Optional

# Visual constants (rendered at 2x then embedded for crispness).
_SCALE = 2
_PAD = 18 * _SCALE
_NODE_PAD_X = 12 * _SCALE
_NODE_PAD_Y = 8 * _SCALE
_LAYER_GAP = 46 * _SCALE
_SIBLING_GAP = 26 * _SCALE
_FONT_SIZE = 13 * _SCALE
_MAX_LABEL_CH = 22

_INK = (17, 24, 39)
_BRAND = (79, 70, 229)
_EDGE = (107, 114, 128)
_NODE_BG = (238, 242, 255)
_NODE_BORDER = (165, 180, 252)
_DIAMOND_BG = (254, 249, 195)
_DIAMOND_BORDER = (250, 204, 21)
_WHITE = (255, 255, 255)

_DIR_ALIASES = {"TB": "TB", "TD": "TB", "BT": "BT", "LR": "LR", "RL": "RL"}

# Node-shape openers/closers, longest first so e.g. ([ ]) wins over [ ].
_SHAPES = [
    ("([", "])", "stadium"),
    ("[[", "]]", "subroutine"),
    ("[(", ")]", "cylinder"),
    ("((", "))", "circle"),
    ("{{", "}}", "hexagon"),
    ("[", "]", "rect"),
    ("(", ")", "round"),
    ("{", "}", "diamond"),
    (">", "]", "flag"),
]

_EDGE_RE = re.compile(
    r"""^(?P<src>.+?)\s*
        (?P<link>-{2,3}>|-{2,3}|-\.->|-\.-|={2,3}>|={2,3}|--x|--o)\s*
        (?:\|(?P<lbl1>[^|]*)\|\s*)?
        (?P<dst>.+?)$""",
    re.VERBOSE,
)
# `A -- text --> B` form (label embedded in the link).
_EDGE_MIDLABEL_RE = re.compile(
    r"""^(?P<src>.+?)\s+
        (?:-{2,3}|={2,3}|-\.)\s*(?P<lbl>[^->|]+?)\s*(?:-{1,3}>|={1,3}>|\.->)\s+
        (?P<dst>.+?)$""",
    re.VERBOSE,
)


class _Node:
    __slots__ = ("id", "label", "shape", "layer", "w", "h", "x", "y", "order")

    def __init__(self, nid: str, label: str, shape: str) -> None:
        self.id = nid
        self.label = label
        self.shape = shape
        self.layer = 0
        self.w = 0
        self.h = 0
        self.x = 0.0
        self.y = 0.0
        self.order = 0


def _split_node(token: str) -> tuple[str, str, str]:
    """Parse ``id[Label]`` (or other shapes) → (id, label, shape). Bare ``id`` → rect with id text."""
    token = token.strip()
    for opener, closer, shape in _SHAPES:
        i = token.find(opener)
        if i > 0 and token.endswith(closer):
            nid = token[:i].strip()
            label = token[i + len(opener): len(token) - len(closer)].strip()
            label = label.strip('"').strip("'").strip()
            return nid, (label or nid), shape
    return token, token, "rect"


def _wrap(label: str) -> list[str]:
    label = re.sub(r"<br\s*/?>", "\n", label, flags=re.IGNORECASE)
    out: list[str] = []
    for hard in label.split("\n"):
        words = hard.split()
        if not words:
            out.append("")
            continue
        line = words[0]
        for w in words[1:]:
            if len(line) + 1 + len(w) <= _MAX_LABEL_CH:
                line += " " + w
            else:
                out.append(line)
                line = w
        out.append(line)
    return out or [""]


def render_mermaid_png(source: str) -> Optional[bytes]:
    """Render a mermaid flowchart to PNG bytes, or ``None`` if unsupported / unparseable."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None

    lines = [ln.strip() for ln in (source or "").replace("\r\n", "\n").split("\n")]
    lines = [ln for ln in lines if ln and not ln.startswith("%%")]
    if not lines:
        return None

    header = lines[0].lower()
    m = re.match(r"^(?:graph|flowchart)\s+([a-z]{2})\b", header)
    if not m:
        return None  # only flowcharts are supported; let caller fall back
    direction = _DIR_ALIASES.get(m.group(1).upper(), "TB")
    horizontal = direction in ("LR", "RL")

    nodes: dict[str, _Node] = {}
    edges: list[tuple[str, str, str]] = []  # (src, dst, label)
    order_counter = 0

    def _ensure(token: str) -> str:
        nonlocal order_counter
        nid, label, shape = _split_node(token)
        if not nid:
            return ""
        n = nodes.get(nid)
        if n is None:
            n = _Node(nid, label, shape)
            n.order = order_counter
            order_counter += 1
            nodes[nid] = n
        elif label and label != nid and n.label == n.id:
            n.label, n.shape = label, shape  # later definition supplies a label
        return nid

    for raw in lines[1:]:
        body = raw.rstrip(";").strip()
        if not body or re.match(r"^(subgraph|end|classDef|class|style|linkStyle|click|direction)\b", body):
            # Subgraph framing / styling is ignored; nodes inside still parse via their edges.
            continue
        mid = _EDGE_MIDLABEL_RE.match(body)
        mm = _EDGE_RE.match(body)
        if mid:
            s = _ensure(mid.group("src"))
            d = _ensure(mid.group("dst"))
            if s and d:
                edges.append((s, d, (mid.group("lbl") or "").strip()))
        elif mm:
            s = _ensure(mm.group("src"))
            d = _ensure(mm.group("dst"))
            if s and d:
                edges.append((s, d, (mm.group("lbl1") or "").strip()))
        else:
            _ensure(body)  # standalone node declaration

    if not nodes:
        return None

    # ---- Layer assignment (longest path from sources; cycle-safe) -----------------------------
    adj: dict[str, list[str]] = {nid: [] for nid in nodes}
    indeg: dict[str, int] = {nid: 0 for nid in nodes}
    for s, d, _ in edges:
        if d not in adj[s]:
            adj[s].append(d)
            indeg[d] += 1
    # Kahn topo; remaining (cycle) nodes get appended in declaration order.
    from collections import deque

    layer: dict[str, int] = {nid: 0 for nid in nodes}
    q = deque(sorted([n for n, d in indeg.items() if d == 0], key=lambda n: nodes[n].order))
    seen = set()
    rem = dict(indeg)
    while q:
        u = q.popleft()
        if u in seen:
            continue
        seen.add(u)
        for v in adj[u]:
            if layer[v] < layer[u] + 1:
                layer[v] = layer[u] + 1
            rem[v] -= 1
            if rem[v] <= 0 and v not in seen:
                q.append(v)
    # Any node never reached (pure cycle) — place after its known predecessors.
    for nid in sorted(nodes, key=lambda n: nodes[n].order):
        if nid not in seen:
            preds = [layer[s] for s, d, _ in edges if d == nid and s in seen]
            layer[nid] = (max(preds) + 1) if preds else 0

    for nid, n in nodes.items():
        n.layer = layer[nid]

    # ---- Measure nodes ------------------------------------------------------------------------
    try:
        font = ImageFont.load_default(size=_FONT_SIZE)
    except Exception:
        font = ImageFont.load_default()
    scratch = ImageDraw.Draw(Image.new("RGB", (8, 8), _WHITE))

    def _text_size(text_lines: list[str]) -> tuple[int, int]:
        w = h = 0
        for ln in text_lines:
            bbox = scratch.textbbox((0, 0), ln or " ", font=font)
            w = max(w, bbox[2] - bbox[0])
            h += (bbox[3] - bbox[1]) + 3 * _SCALE
        return w, h

    wrapped: dict[str, list[str]] = {}
    for nid, n in nodes.items():
        wl = _wrap(n.label)
        wrapped[nid] = wl
        tw, th = _text_size(wl)
        n.w = tw + 2 * _NODE_PAD_X
        n.h = th + 2 * _NODE_PAD_Y
        if n.shape in ("circle", "diamond", "hexagon"):
            side = max(n.w, n.h)
            n.w = n.h = side if n.shape == "circle" else side
            if n.shape == "diamond":
                n.w = int(n.w * 1.25)
                n.h = int(n.h * 1.25)

    # ---- Layout (layered) ---------------------------------------------------------------------
    layers: dict[int, list[_Node]] = {}
    for n in sorted(nodes.values(), key=lambda x: x.order):
        layers.setdefault(n.layer, []).append(n)

    cross = 0.0  # running position along the cross axis per layer
    layer_extent: dict[int, int] = {}
    for li in sorted(layers):
        row = layers[li]
        pos = _PAD
        for n in row:
            if horizontal:
                n.y = pos
                pos += n.h + _SIBLING_GAP
            else:
                n.x = pos
                pos += n.w + _SIBLING_GAP
        layer_extent[li] = int(pos - _SIBLING_GAP + _PAD)

    # Main-axis (layer) coordinate.
    main = _PAD
    for li in sorted(layers):
        row = layers[li]
        depth = max((n.w if horizontal else n.h) for n in row)
        for n in row:
            if horizontal:
                n.x = main
            else:
                n.y = main
        main += depth + _LAYER_GAP
    main_extent = int(main - _LAYER_GAP + _PAD)

    cross_extent = max(layer_extent.values()) if layer_extent else _PAD * 2
    if horizontal:
        width, height = main_extent, cross_extent
    else:
        width, height = cross_extent, main_extent
    width = max(width, 120 * _SCALE)
    height = max(height, 80 * _SCALE)

    # Center each layer along the cross axis for a tidier look.
    for li, row in layers.items():
        used = layer_extent[li] - _PAD
        free = (cross_extent - used)
        shift = max(0, free // 2)
        for n in row:
            if horizontal:
                n.y += shift - _PAD // 2
            else:
                n.x += shift - _PAD // 2

    # ---- Draw ---------------------------------------------------------------------------------
    img = Image.new("RGB", (int(width), int(height)), _WHITE)
    draw = ImageDraw.Draw(img)

    def _center(n: _Node) -> tuple[float, float]:
        return n.x + n.w / 2, n.y + n.h / 2

    def _edge_point(n: _Node, toward: tuple[float, float]) -> tuple[float, float]:
        cx, cy = _center(n)
        tx, ty = toward
        dx, dy = tx - cx, ty - cy
        if dx == 0 and dy == 0:
            return cx, cy
        # Clip the center→toward ray to the node's bounding box.
        hw, hh = n.w / 2, n.h / 2
        scale = 1e9
        if dx:
            scale = min(scale, hw / abs(dx))
        if dy:
            scale = min(scale, hh / abs(dy))
        return cx + dx * scale, cy + dy * scale

    # Edges first (under nodes).
    for s, d, lbl in edges:
        ns, nd = nodes[s], nodes[d]
        p1 = _edge_point(ns, _center(nd))
        p2 = _edge_point(nd, _center(ns))
        draw.line([p1, p2], fill=_EDGE, width=max(1, _SCALE))
        # Arrowhead at p2.
        import math

        ang = math.atan2(p2[1] - p1[1], p2[0] - p1[0])
        ah = 7 * _SCALE
        for off in (-0.4, 0.4):
            ax = p2[0] - ah * math.cos(ang - off)
            ay = p2[1] - ah * math.sin(ang - off)
            draw.line([p2, (ax, ay)], fill=_EDGE, width=max(1, _SCALE))
        if lbl:
            mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
            bbox = draw.textbbox((0, 0), lbl, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.rectangle([mx - tw / 2 - 2, my - th / 2 - 1, mx + tw / 2 + 2, my + th / 2 + 1], fill=_WHITE)
            draw.text((mx - tw / 2, my - th / 2), lbl, fill=_EDGE, font=font)

    # Nodes.
    for n in nodes.values():
        x0, y0, x1, y1 = n.x, n.y, n.x + n.w, n.y + n.h
        if n.shape == "diamond":
            cx, cy = _center(n)
            draw.polygon([(cx, y0), (x1, cy), (cx, y1), (x0, cy)], fill=_DIAMOND_BG, outline=_DIAMOND_BORDER)
        elif n.shape in ("circle",):
            draw.ellipse([x0, y0, x1, y1], fill=_NODE_BG, outline=_NODE_BORDER)
        elif n.shape in ("stadium", "round"):
            r = min(n.h / 2, 14 * _SCALE)
            draw.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=_NODE_BG, outline=_NODE_BORDER)
        else:
            draw.rounded_rectangle([x0, y0, x1, y1], radius=5 * _SCALE, fill=_NODE_BG, outline=_NODE_BORDER)
        # Label (vertically centered).
        wl = wrapped[n.id]
        _, th = _text_size(wl)
        ty = n.y + (n.h - th) / 2
        for ln in wl:
            bbox = draw.textbbox((0, 0), ln or " ", font=font)
            tw = bbox[2] - bbox[0]
            draw.text((n.x + (n.w - tw) / 2, ty), ln, fill=_INK, font=font)
            ty += (bbox[3] - bbox[1]) + 3 * _SCALE

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
