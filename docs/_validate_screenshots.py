"""Require screenshot coverage on every publishable page and validate asset provenance.

Run directly after capture promotion. Checks the rendered include's inputs rather than
assuming a normal Markdown-link scan can see Liquid include-generated image URLs.
Legacy assets count only when their bytes match a validated synthetic manifest image.
"""
from __future__ import annotations

import hashlib
from fnmatch import fnmatchcase
from html import unescape
import json
import os
from pathlib import Path
import re
import shlex
import struct
import sys
from urllib.parse import unquote, urlsplit

import yaml

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets/screenshots"
INCLUDE = re.compile(r"\{%-?\s*include\s+screenshot\.html\b(.*?)\s*-?%\}", re.DOTALL)
FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?)^---[ \t]*(?:\n|\Z)", re.DOTALL | re.MULTILINE)
MD_IMAGE = re.compile(r"(?<!\\)!\[[^\]\n]*\]\(\s*(.*?)\s*\)")
HTML_IMAGE = re.compile(r"<img\b[^>]*?\ssrc\s*=\s*([\"'])(.*?)\1", re.IGNORECASE | re.DOTALL)
DECORATIVE_IMAGE = re.compile(r"social|logo|badge|favicon|deploy", re.IGNORECASE)


def _config() -> dict:
    try:
        config = yaml.safe_load((ROOT / "_config.yml").read_text(encoding="utf-8-sig"))
    except yaml.YAMLError as error:
        raise ValueError(f"Invalid _config.yml: {error}") from error
    if not isinstance(config, dict):
        raise ValueError("_config.yml must contain a mapping")
    excludes = config.get("exclude", [])
    if not isinstance(excludes, list) or any(not isinstance(p, str) or not p.strip() for p in excludes):
        raise ValueError("_config.yml exclude must be a list of nonempty paths/globs")
    return config


def _glob_matches(parts: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    """Match path components, including zero or more directories for **."""
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return _glob_matches(parts, pattern[1:]) or bool(parts and _glob_matches(parts[1:], pattern))
    return bool(parts and fnmatchcase(parts[0], pattern[0]) and _glob_matches(parts[1:], pattern[1:]))


def _excluded(relative: Path, excludes: list[str]) -> bool:
    parts = relative.parts
    # Jekyll's ordinary page reader omits reserved/hidden paths and editor backups.
    if any(p.startswith(("_", ".", "#")) or p.endswith("~") or p == "node_modules" for p in parts):
        return True
    for raw in ["vendor/bundle", "vendor/cache", "vendor/gems", "vendor/ruby", *excludes]:
        pattern = tuple(raw.removeprefix("./").strip("/").split("/"))
        if len(pattern) == 1 and any(fnmatchcase(p, pattern[0]) for p in parts):
            return True
        # An excluded directory excludes every descendant, not just the directory entry.
        if any(_glob_matches(parts[:end], pattern) for end in range(1, len(parts) + 1)):
            return True
    return False


def _walk_error(error: OSError) -> None:
    raise error  # os.walk otherwise silently skips unreadable documentation directories.


def _published_pages(config: dict, errors: list[str]) -> dict[Path, str]:
    markdown_ext = config.get("markdown_ext", "markdown,mkdown,mkdn,mkd,md")
    if not isinstance(markdown_ext, str):
        raise ValueError("_config.yml markdown_ext must be a comma-separated string")
    extensions = {".html", ".htm", *("." + ext.strip().lstrip(".") for ext in markdown_ext.split(","))}
    pages = {}
    for directory, dirs, names in os.walk(ROOT, onerror=_walk_error):
        parent = Path(directory)
        dirs[:] = sorted(d for d in dirs if not _excluded((parent / d).relative_to(ROOT), config.get("exclude", [])))
        for name in sorted(names):
            page = parent / name
            relative = page.relative_to(ROOT)
            if page.suffix.lower() not in extensions or _excluded(relative, config.get("exclude", [])):
                continue
            text = page.read_text(encoding="utf-8-sig")
            if not re.match(r"\A---[ \t]*(?:\n|\Z)", text):
                continue  # Static files without front matter are not rendered doc pages.
            match = FRONT_MATTER.match(text)
            if not match:
                errors.append(f"Malformed front matter: {relative.as_posix()}")
                pages[page] = ""  # Fail closed: a broken page must not shrink the denominator.
                continue
            try:
                metadata = yaml.safe_load(match.group(1))
                if metadata is None:
                    metadata = {}
                if not isinstance(metadata, dict):
                    raise ValueError("front matter must contain a mapping")
            except (yaml.YAMLError, ValueError) as error:
                errors.append(f"Malformed front matter: {relative.as_posix()}: {error}")
                pages[page] = ""
                continue
            if metadata.get("published") is False:
                continue
            # nav_exclude, search_exclude and sitemap flags do not unpublish a page.
            pages[page] = text[match.end():]
    return dict(sorted(pages.items()))


def _visible_body(body: str) -> str:
    """Do not mistake metadata, commented examples or code for rendered screenshots."""
    body = re.sub(r"\{%-?\s*(comment|raw)\s*-?%\}.*?\{%-?\s*end\1\s*-?%\}", "", body, flags=re.DOTALL)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    lines = []
    fence = ""
    for line in body.splitlines(keepends=True):
        if fence:
            if re.match(r"^[ \t]{0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*$", line.rstrip("\r\n")):
                fence = ""
            continue
        opening = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
        if opening:
            fence = opening.group(1)
        else:
            lines.append(line)
    # Keep includes atomic: backticks inside quoted titles/captions are not code spans.
    return re.sub(INCLUDE.pattern + r"|(?P<ticks>`+).*?(?P=ticks)",
                  lambda match: "" if match.group("ticks") else match.group(0),
                  "".join(lines), flags=re.DOTALL)


def _legacy_screenshot(raw: str, page: Path, config: dict, approved_hashes: dict[str, str]) -> str | None:
    url = unescape(raw).strip()
    url = re.sub(r"\{\{\s*site\.(?:baseurl|url)\s*\}\}", "", url)
    url = re.sub(r"\{\{\s*(['\"])(.*?)\1\s*\|\s*(?:relative_url|absolute_url)\s*\}\}", r"\2", url)
    url = re.sub(r"\s+[\"'].*$", "", url).strip("<>")
    if "{{" in url or "{%" in url:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc:
        return None  # Never fetch external badges or assume remote pixels match local ones.
    path = unquote(parsed.path)
    baseurl = str(config.get("baseurl", "")).rstrip("/")
    if baseurl and path.startswith(baseurl + "/"):
        path = path[len(baseurl):]
    target = (ROOT / path.lstrip("/") if path.startswith("/") else page.parent / path).resolve()
    # Legacy aliases live directly in assets/. New manifest images require captioned includes.
    if target.parent != (ROOT / "assets").resolve() or target.suffix.lower() != ".png":
        return None
    if DECORATIVE_IMAGE.search(target.stem) or _excluded(target.relative_to(ROOT.resolve()), config.get("exclude", [])):
        return None
    if not target.is_file():
        return None
    return approved_hashes.get(hashlib.sha256(target.read_bytes()).hexdigest())


def validate() -> dict:
    errors: list[str] = []
    config = _config()
    pages = _published_pages(config, errors)
    if not pages:
        errors.append("No publishable documentation pages found")
    manifest = json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))
    entries = manifest["screenshots"]
    if manifest["count"] != len(entries) or len(entries) < 100:
        errors.append("Manifest must contain at least 100 screenshots and an exact count")
    has_template = (ROOT / "_includes/screenshot.html").is_file()
    if not has_template:
        errors.append("Screenshot include template is missing")
    files = {entry["file"]: entry for entry in entries}
    if len(files) != len(entries):
        errors.append("Duplicate filenames in screenshot manifest")
    hashes = set()
    approved_hashes: dict[str, str] = {}
    for filename, entry in files.items():
        asset_error_start = len(errors)
        if Path(filename).name != filename or "/" in filename or "\\" in filename or filename in {"", ".", ".."}:
            errors.append(f"Unsafe asset name: {filename}")
            continue
        target = ASSETS / filename
        if not target.is_file():
            errors.append(f"Missing screenshot: {filename}")
            continue
        if _excluded(target.relative_to(ROOT), config.get("exclude", [])):
            errors.append(f"Screenshot excluded from publication: {filename}")
        image = target.read_bytes()
        digest = hashlib.sha256(image).hexdigest()
        if digest != entry["sha256"]:
            errors.append(f"Changed artifact hash: {filename}")
        if digest in hashes:
            errors.append(f"Duplicate image: {filename}")
            approved_hashes.pop(digest, None)
        hashes.add(digest)
        if (len(image) < 33 or image[:8] != b"\x89PNG\r\n\x1a\n"
                or image[8:16] != b"\x00\x00\x00\rIHDR"
                or struct.unpack(">II", image[16:24]) != (entry["width"], entry["height"])):
            errors.append(f"Invalid image dimensions: {filename}")
        pos = 8
        ended = False
        while pos + 12 <= len(image):
            length = struct.unpack(">I", image[pos:pos + 4])[0]
            chunk = image[pos + 4:pos + 8]
            if chunk in {b"tEXt", b"iTXt", b"zTXt", b"eXIf"}:
                errors.append(f"Unnecessary image metadata: {filename}")
            if pos + length + 12 > len(image):
                errors.append(f"Truncated PNG chunk: {filename}")
                break
            pos += length + 12
            if chunk == b"IEND":
                ended = length == 0 and pos == len(image)
                break
        if not ended:
            errors.append(f"Incomplete or invalid PNG: {filename}")
        if entry.get("source") != "synthetic-local-capture":
            errors.append(f"Unknown provenance: {filename}")
        if len(errors) == asset_error_start:
            approved_hashes[digest] = filename

    valid_files = set(approved_hashes.values())
    uses: dict[str, list[str]] = {}
    covered_pages = set()
    for page, body in pages.items():
        name = page.relative_to(ROOT).as_posix()
        body = _visible_body(body)
        for match in INCLUDE.finditer(body):
            # Liquid expands before Kramdown: indenting only the include line
            # can split the generated figure from its caption inside a list.
            prefix = body[body.rfind("\n", 0, match.start()) + 1:match.start()]
            if prefix:
                errors.append(f"Screenshot include must start at column one: {name}")
                continue
            try:
                tokens = shlex.split(match.group(1).rstrip().removesuffix("-").rstrip())
                args = dict(token.split("=", 1) for token in tokens)
                if len(args) != len(tokens):
                    raise ValueError("duplicate include arguments")
            except ValueError:
                errors.append(f"Malformed screenshot include: {name}")
                continue
            filename = args.get("file", "")
            if filename not in files:
                errors.append(f"Unmanifested screenshot {filename}: {name}")
            captioned = bool(args.get("title", "").strip() and args.get("caption", "").strip())
            if not captioned:
                errors.append(f"Missing title/caption: {name} {filename}")
            if filename in valid_files and captioned and has_template:
                uses.setdefault(filename, []).append(name)
                covered_pages.add(name)
        # An image-shaped string inside include arguments is not a legacy body image.
        body = INCLUDE.sub("", body)
        if re.search(r"\{%-?\s*include\s+screenshot\.html\b", body):
            errors.append(f"Malformed screenshot include: {name}")
        urls = [match.group(1) for match in MD_IMAGE.finditer(body)]
        urls.extend(match.group(2) for match in HTML_IMAGE.finditer(body))
        for url in urls:
            try:
                filename = _legacy_screenshot(url, page, config, approved_hashes)
            except OSError as error:
                errors.append(f"Cannot read screenshot alias: {name}: {error}")
                continue
            if filename:
                uses.setdefault(filename, []).append(name)
                covered_pages.add(name)

    missing_pages = sorted(page.relative_to(ROOT).as_posix() for page in pages
                           if page.relative_to(ROOT).as_posix() not in covered_pages)
    errors.extend(f"Missing screenshot on documentation page: {page}" for page in missing_pages)
    for filename in files:
        if filename not in uses:
            errors.append(f"Screenshot not linked from a documentation page: {filename}")
    extras = sorted(p.name for p in ASSETS.glob("*.png") if p.name not in files)
    errors.extend(f"Unmanifested file: {name}" for name in extras)
    return {"screenshots": len(files), "unique_hashes": len(hashes), "linked_screenshots": len(set(uses) & set(files)),
            "pages": len(covered_pages), "totalPages": len(pages), "coveredPages": len(covered_pages),
            "coveragePercent": round(100 * len(covered_pages) / len(pages), 2) if pages else 0.0,
            "missingScreenshotPages": missing_pages,
            "placements": sum(map(len, uses.values())), "errors": errors}


if __name__ == "__main__":
    try:
        result = validate()
        print(json.dumps(result, indent=2))
        sys.exit(1 if result["errors"] else 0)
    except (OSError, ValueError, KeyError) as error:
        print(f"Cannot validate screenshots: {error}", file=sys.stderr)
        sys.exit(2)