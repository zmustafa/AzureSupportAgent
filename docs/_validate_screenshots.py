"""Validate screenshot includes, public artifact provenance and complete asset linkage.

Run directly after capture promotion. Checks the rendered include's inputs rather than
assuming a normal Markdown-link scan can see Liquid include-generated image URLs.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shlex
import struct
import sys

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets/screenshots"
INCLUDE = re.compile(r"\{%\s*include\s+screenshot\.html\s+(.*?)\s*%\}", re.DOTALL)


def validate() -> dict:
    errors = []
    manifest = json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))
    entries = manifest["screenshots"]
    if manifest["count"] != len(entries) or len(entries) < 100:
        errors.append("Manifest must contain at least 100 screenshots and an exact count")
    if not (ROOT / "_includes/screenshot.html").is_file():
        errors.append("Screenshot include template is missing")
    files = {entry["file"]: entry for entry in entries}
    if len(files) != len(entries):
        errors.append("Duplicate filenames in screenshot manifest")
    uses: dict[str, list[str]] = {}
    for page in ROOT.rglob("*.md"):
        if any(part in {"improvement-plans", "test-findings", "_site", "node_modules"} for part in page.parts):
            continue
        text = page.read_text(encoding="utf-8")
        if not text.startswith("---"):
            continue
        for match in INCLUDE.finditer(text):
            try:
                args = dict(token.split("=", 1) for token in shlex.split(match.group(1)))
            except ValueError:
                errors.append(f"Malformed screenshot include: {page.relative_to(ROOT)}")
                continue
            filename = args.get("file", "")
            if filename not in files:
                errors.append(f"Unmanifested screenshot {filename}: {page.relative_to(ROOT)}")
            if not args.get("title") or not args.get("caption"):
                errors.append(f"Missing title/caption: {page.relative_to(ROOT)} {filename}")
            uses.setdefault(filename, []).append(page.relative_to(ROOT).as_posix())
    hashes = set()
    for filename, entry in files.items():
        if Path(filename).name != filename:
            errors.append(f"Unsafe asset name: {filename}")
            continue
        target = ASSETS / filename
        if not target.is_file():
            errors.append(f"Missing screenshot: {filename}")
            continue
        image = target.read_bytes()
        digest = hashlib.sha256(image).hexdigest()
        if digest != entry["sha256"]:
            errors.append(f"Changed artifact hash: {filename}")
        if digest in hashes:
            errors.append(f"Duplicate image: {filename}")
        hashes.add(digest)
        if image[:8] != b"\x89PNG\r\n\x1a\n" or struct.unpack(">II", image[16:24]) != (entry["width"], entry["height"]):
            errors.append(f"Invalid image dimensions: {filename}")
        pos = 8
        while pos + 12 <= len(image):
            length = struct.unpack(">I", image[pos:pos + 4])[0]
            if image[pos + 4:pos + 8] in {b"tEXt", b"iTXt", b"zTXt", b"eXIf"}:
                errors.append(f"Unnecessary image metadata: {filename}")
            pos += length + 12
        if entry.get("source") != "synthetic-local-capture":
            errors.append(f"Unknown provenance: {filename}")
        if filename not in uses:
            errors.append(f"Screenshot not linked from a documentation page: {filename}")
    extras = sorted(p.name for p in ASSETS.glob("*.png") if p.name not in files)
    errors.extend(f"Unmanifested file: {name}" for name in extras)
    return {"screenshots": len(files), "unique_hashes": len(hashes), "linked_screenshots": len(set(uses) & set(files)),
            "pages": len({page for pages in uses.values() for page in pages}),
            "placements": sum(map(len, uses.values())), "errors": errors}


if __name__ == "__main__":
    try:
        result = validate()
        print(json.dumps(result, indent=2))
        sys.exit(1 if result["errors"] else 0)
    except (OSError, ValueError, KeyError) as error:
        print(f"Cannot validate screenshots: {error}", file=sys.stderr)
        sys.exit(2)