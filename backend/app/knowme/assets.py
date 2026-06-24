"""Know-Me embedded assets (images/screenshots) + architecture→Mermaid generation.

Images a user pastes or uploads into a Know-Me are stored as files under
``backend/.data/knowme_assets/<architecture_id>/`` and referenced from the document Markdown
as ``asset:<asset_id>`` (a stable, location-independent token). The frontend rewrites the
token to the asset API URL for display; PDF export inlines it as a data-URI. Asset metadata
lives on the Know-Me record's ``assets[]`` list (the registry already reserves it).
"""
from __future__ import annotations

import base64
import re
import uuid
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2] / ".data" / "knowme_assets"

_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg",
}
MAX_BYTES = 8 * 1024 * 1024  # 8 MB per image

ASSET_RE = re.compile(r"asset:([0-9a-fA-F-]{36})")


def _dir(architecture_id: str) -> Path:
    d = _ROOT / architecture_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_asset(architecture_id: str, *, data: bytes, content_type: str, filename: str = "") -> dict[str, Any]:
    """Persist an image and return its metadata record (no secrets → plain file)."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct not in _EXT:
        raise ValueError(f"Unsupported image type: {ct or 'unknown'}")
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Image is empty or exceeds the 8 MB limit.")
    asset_id = str(uuid.uuid4())
    ext = _EXT[ct]
    (_dir(architecture_id) / f"{asset_id}{ext}").write_bytes(data)
    return {
        "id": asset_id,
        "filename": filename or f"image{ext}",
        "content_type": ct,
        "ext": ext,
        "size": len(data),
        "ref": f"asset:{asset_id}",
        "markdown": f"![{(filename or 'image')}](asset:{asset_id})",
    }


def _find_file(architecture_id: str, asset_id: str) -> Path | None:
    d = _ROOT / architecture_id
    if not d.exists():
        return None
    for p in d.glob(f"{asset_id}.*"):
        return p
    return None


def read_asset(architecture_id: str, asset_id: str) -> tuple[bytes, str] | None:
    """Return (bytes, content_type) for an asset, or None."""
    p = _find_file(architecture_id, asset_id)
    if p is None:
        return None
    ct = next((c for c, e in _EXT.items() if e == p.suffix), "application/octet-stream")
    try:
        return p.read_bytes(), ct
    except OSError:
        return None


def delete_asset(architecture_id: str, asset_id: str) -> bool:
    p = _find_file(architecture_id, asset_id)
    if p is None:
        return False
    try:
        p.unlink()
        return True
    except OSError:
        return False


def inline_asset_data_uris(architecture_id: str, markdown: str) -> str:
    """Rewrite every ``asset:<id>`` reference in ``markdown`` to a base64 data-URI (for PDF
    export, where there is no live API to fetch from). A resized image may carry a
    ``?w=<px>`` width hint after the id — it is consumed here so it can't corrupt the
    emitted data-URI (PDF uses the image's natural size). Unknown assets are left as-is."""
    def _sub(m: re.Match[str]) -> str:
        got = read_asset(architecture_id, m.group(1))
        if got is None:
            return f"asset:{m.group(1)}"  # drop the width hint but keep the ref
        data, ct = got
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:{ct};base64,{b64}"

    # ``asset:<guid>`` optionally followed by a ``?w=<px>`` (or ``&w=``) width hint.
    return re.sub(r"asset:([0-9a-fA-F-]{36})(?:[?&]w=\d+)?", _sub, markdown or "")


def delete_all(km_id: str) -> None:
    """Remove a Know-Me's entire asset folder (on purge / orphan prune)."""
    import shutil

    d = _ROOT / km_id
    if d.exists():
        try:
            shutil.rmtree(d)
        except OSError:
            pass


def remap_dirs(remap: dict[str, str]) -> None:
    """Rename asset folders from old keys to new (used by the registry migration)."""
    for old, new in (remap or {}).items():
        if old == new:
            continue
        src, dst = _ROOT / old, _ROOT / new
        if src.exists() and not dst.exists():
            try:
                src.rename(dst)
            except OSError:
                pass


# ---------------------------------------------------------------- architecture → Mermaid
def _friendly_type(t: str) -> str:
    """Trim an ARM type to its leaf for a compact node sublabel."""
    t = (t or "").strip()
    if "/" in t:
        t = t.split("/")[-1]
    return t[:40]


def architecture_to_mermaid(arch: dict[str, Any]) -> str:
    """Build a Mermaid flowchart from an architecture's nodes/edges (backend port of the
    canvas exporter) so a Know-Me can embed the diagram without a round-trip to the UI."""
    nodes = arch.get("nodes", []) or []
    edges = arch.get("edges", []) or []

    def safe(s: str) -> str:
        return (str(s) or "").replace('"', "'").replace("\n", " ").strip()[:60]

    idmap = {n.get("id"): f"N{i}" for i, n in enumerate(nodes)}
    lines = ["flowchart TD"]
    for n in nodes:
        nid = idmap.get(n.get("id"))
        if not nid:
            continue
        name = safe(n.get("name") or n.get("id") or "node")
        sub = safe(_friendly_type(n.get("type") or n.get("resource_type") or ""))
        label = f"{name}<br/><small>{sub}</small>" if sub else name
        lines.append(f'  {nid}["{label}"]')
    for e in edges:
        s, t = idmap.get(e.get("source")), idmap.get(e.get("target"))
        if not s or not t:
            continue
        lbl = safe(e.get("label") or "")
        lines.append(f"  {s} -->|{lbl}| {t}" if lbl else f"  {s} --> {t}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)
