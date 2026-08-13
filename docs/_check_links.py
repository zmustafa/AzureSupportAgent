"""Find internal links in the docs site that resolve to nothing (the 404s).

Jekyll does not fail a build on a dead internal link — it renders the anchor and the reader gets
a 404 from GitHub Pages. On a 260-page site that is impossible to hold in your head, so this
resolves every internal link against the set of URLs the site actually publishes.

What counts as a published URL:

* every page's explicit ``permalink`` (this site sets one on every page),
* every ``redirect_from`` alias (jekyll-redirect-from generates a real page for each),
* every static file under the asset directories.

What is checked: markdown links, HTML ``href``/``src``, and image links, after expanding
``{{ site.baseurl }}``. External links, ``mailto:`` and bare anchors are out of scope — this
answers "does this page exist", not "is that server up".

A second, mirror-image check runs alongside it: every child of a ``has_children: true`` index
must be linked from that index's BODY, not merely listed in the theme sidebar. Otherwise a
landing page quietly stops describing the pages beneath it while every link on it still works.

Usage (from docs/):

    python _check_links.py            # report
    python _check_links.py --json     # machine-readable

Exit code 1 when anything is broken, so it can gate a docs change; 2 when the script cannot
read ``_config.yml`` and therefore cannot be trusted to run at all.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "_config.yml"

# Jekyll never publishes these, whatever _config.yml says.
ALWAYS_EXCLUDED_DIRS = {"_site", "_sass", ".jekyll-cache", "__pycache__"}

EXCLUDE_BLOCK = re.compile(r"^exclude:\s*\n((?:\s*-\s*.+\n?)+)", re.MULTILINE)


class ConfigError(RuntimeError):
    """_config.yml could not be read the way this script depends on."""


def _config_excludes() -> tuple[set[str], set[str]]:
    """Read ``exclude:`` from _config.yml, as directory names and glob patterns.

    This used to be a hand-written copy of that list. A copy is silently wrong the moment
    someone excludes a new directory: the checker keeps treating it as published, so links
    into unpublished content start passing. Read the real thing, and refuse to run if it
    cannot be found — an empty exclude set is indistinguishable from a correct one in the
    output, which is exactly the kind of vacuous pass this file exists to prevent.
    """
    if not CONFIG.is_file():
        raise ConfigError(f"{CONFIG.name} not found next to this script")
    match = EXCLUDE_BLOCK.search(CONFIG.read_text(encoding="utf-8", errors="replace"))
    if not match:
        raise ConfigError(f"no `exclude:` list found in {CONFIG.name}")
    entries = [
        line.strip().lstrip("-").strip().strip("\"'")
        for line in match.group(1).splitlines()
        if line.strip()
    ]
    entries = [e for e in entries if e]
    if not entries:
        raise ConfigError(f"`exclude:` in {CONFIG.name} parsed to nothing")
    dirs = {e.rstrip("/") for e in entries if (ROOT / e).is_dir()}
    return dirs, {e for e in entries if e.rstrip("/") not in dirs}


EXCLUDED_DIRS, EXCLUDED_PATTERNS = (set[str](), set[str]())
try:
    EXCLUDED_DIRS, EXCLUDED_PATTERNS = _config_excludes()
except ConfigError as _exc:  # exit 2, so CI can tell "cannot run" from "found problems" (1)
    print(f"cannot run: {_exc}", file=sys.stderr)
    raise SystemExit(2) from _exc

FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
PERMALINK = re.compile(r"^permalink:\s*(.+?)\s*$", re.MULTILINE)
HAS_CHILDREN = re.compile(r"^has_children:\s*true\s*$", re.MULTILINE)
REDIRECT_BLOCK = re.compile(r"^redirect_from:\s*\n((?:\s*-\s*.+\n?)+)", re.MULTILINE)
REDIRECT_INLINE = re.compile(r"^redirect_from:\s*\[(.+?)\]\s*$", re.MULTILINE)

# The URL part must allow SPACES: almost every link on this site is written
# `[text]({{ site.baseurl }}/path/)`, and the Liquid tag contains two of them. An earlier
# version of this pattern used `[^)\s]+`, which silently skipped the majority of the site's
# links and reported a confident "0 broken" — a checker that cannot see the links it is meant
# to check is worse than no checker, because it is believed.
MD_LINK = re.compile(r"!?\[[^\]]*\]\(\s*([^)]+?)\s*\)")
HTML_LINK = re.compile(r"(?:href|src)=[\"']([^\"']+)[\"']")


def _strip_title(url: str) -> str:
    """`[t](/a/b "Title")` -> `/a/b`."""
    for quote in (' "', " '"):
        if quote in url:
            url = url.split(quote, 1)[0]
    return url.strip()


def _is_excluded(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in EXCLUDED_DIRS or part in ALWAYS_EXCLUDED_DIRS for part in rel.parts):
        return True
    return any(fnmatch(rel.name, pat) or fnmatch(rel.as_posix(), pat) for pat in EXCLUDED_PATTERNS)


def _normalize(url: str) -> str:
    """A comparable form: no baseurl, no query/anchor, exactly one trailing slash."""
    url = url.split("#", 1)[0].split("?", 1)[0]
    url = url.replace("{{ site.baseurl }}", "").replace("{{site.baseurl}}", "")
    url = url.replace("{{ site.url }}", "").replace("{{ site.baseurl }}/", "/")
    if not url:
        return ""
    if not url.startswith("/"):
        return url  # relative — resolved by the caller, which knows the page
    if not url.endswith("/") and "." not in url.rsplit("/", 1)[-1]:
        url += "/"
    return url


def implicit_permalink(md: Path) -> str:
    """Where Jekyll publishes a page that sets no ``permalink``.

    The site uses ``permalink: pretty``, so ``docs/DEPLOYMENT.md`` is served at ``/DEPLOYMENT/``
    and NOT at ``/DEPLOYMENT.md``. That distinction is the whole bug this script was written
    for: a link written as ``[guide](DEPLOYMENT.md)`` is relative, so from ``/README/`` the
    browser asks for ``/README/DEPLOYMENT.md`` and gets a 404 — while the page it wanted is
    sitting at ``/DEPLOYMENT/`` the whole time."""
    rel = md.relative_to(ROOT)
    parts = list(rel.parts)
    if parts[-1] in ("index.md", "README.md") and len(parts) > 1:
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][: -len(".md")]
    return "/" + "/".join(parts) + "/" if parts else "/"


def published_urls() -> tuple[set[str], dict[str, Path]]:
    """Every URL the site serves, and where each came from."""
    urls: set[str] = set()
    owner: dict[str, Path] = {}

    for md in ROOT.rglob("*.md"):
        if _is_excluded(md):
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        fm = FRONT_MATTER.match(text)
        if not fm:
            continue  # no front matter: Jekyll does not render it as a page at all
        block = fm.group(1)
        found: list[str] = []
        pm = PERMALINK.search(block)
        if pm:
            found.append(pm.group(1).strip().strip("\"'"))
        else:
            found.append(implicit_permalink(md))
        rb = REDIRECT_BLOCK.search(block)
        if rb:
            found += [ln.strip().lstrip("-").strip().strip("\"'") for ln in rb.group(1).splitlines() if ln.strip()]
        ri = REDIRECT_INLINE.search(block)
        if ri:
            found += [p.strip().strip("\"'") for p in ri.group(1).split(",")]
        for u in found:
            n = _normalize(u)
            if n:
                urls.add(n)
                owner.setdefault(n, md)

    # Static files are served at their path on disk.
    for f in ROOT.rglob("*"):
        if f.is_dir() or f.suffix == ".md" or _is_excluded(f):
            continue
        rel = "/" + f.relative_to(ROOT).as_posix()
        urls.add(rel)
        owner.setdefault(rel, f)

    return urls, owner


def links_in(md: Path) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for lineno, line in enumerate(md.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        for pattern in (MD_LINK, HTML_LINK):
            for m in pattern.finditer(line):
                out.append((_strip_title(m.group(1)), lineno))
    return out


def check() -> tuple[list[dict[str, object]], int]:
    urls, _owner = published_urls()
    broken: list[dict[str, object]] = []
    examined = 0

    for md in sorted(ROOT.rglob("*.md")):
        if _is_excluded(md):
            continue
        for raw, lineno in links_in(md):
            if raw.startswith(("http://", "https://", "mailto:", "#", "tel:", "data:")):
                continue
            if "{{" in raw and "site.baseurl" not in raw and "site.url" not in raw:
                continue  # some other Liquid expression; not statically resolvable
            target = _normalize(raw)
            if not target:
                continue
            examined += 1
            if not target.startswith("/"):
                # Relative to the page's own directory.
                base = "/" + md.parent.relative_to(ROOT).as_posix().strip(".").strip("/")
                target = _normalize((base.rstrip("/") + "/" + target).replace("//", "/"))
            if target in urls:
                continue
            # A directory index may be published either way round.
            alt = target.rstrip("/") if target.endswith("/") else target + "/"
            if alt in urls:
                continue
            # A `.md` link is the classic form of this bug: the PAGE exists, but it is served at
            # its pretty URL. Name the fix rather than just reporting the miss.
            suggestion = ""
            if target.endswith(".md"):
                pretty = target[: -len(".md")] + "/"
                if pretty in urls:
                    suggestion = pretty
            broken.append({
                "page": md.relative_to(ROOT).as_posix(),
                "line": lineno,
                "link": raw,
                "resolved": target,
                "suggestion": suggestion,
            })
    return broken, examined


def _published_url(md: Path) -> str:
    """Where a page is served, or "" when Jekyll does not render it as a page at all."""
    fm = FRONT_MATTER.match(md.read_text(encoding="utf-8", errors="replace"))
    if not fm:
        return ""
    pm = PERMALINK.search(fm.group(1))
    return _normalize(pm.group(1).strip().strip("\"'") if pm else implicit_permalink(md))


def unlinked_children() -> tuple[list[dict[str, str]], int, int]:
    """Child pages that their own section index never links to.

    ``has_children: true`` makes the theme list a page in the sidebar, so a section index can
    look complete while its body silently omits a child. That is how a landing page drifts
    behind the pages beneath it: the sidebar keeps working, the prose stops being true, and
    nothing fails. The sidebar is not the landing page — check what the page actually says.
    """
    missing: list[dict[str, str]] = []
    indexes = children = 0

    for index in sorted(ROOT.rglob("index.md")):
        if _is_excluded(index):
            continue
        fm = FRONT_MATTER.match(index.read_text(encoding="utf-8", errors="replace"))
        if not fm or not HAS_CHILDREN.search(fm.group(1)):
            continue
        indexes += 1
        linked = {_normalize(raw) for raw, _ in links_in(index)}
        linked |= {u.rstrip("/") for u in linked}

        for entry in sorted(index.parent.iterdir()):
            child = entry / "index.md" if entry.is_dir() else entry
            if not child.is_file() or child.suffix != ".md" or child == index:
                continue
            if not entry.is_dir() and child.name == "index.md":
                continue
            if _is_excluded(child):
                continue
            url = _published_url(child)
            if not url:
                continue
            children += 1
            if url not in linked and url.rstrip("/") not in linked:
                missing.append({
                    "index": index.relative_to(ROOT).as_posix(),
                    "child": child.relative_to(ROOT).as_posix(),
                    "url": url,
                })
    return missing, indexes, children


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    urls, _ = published_urls()
    broken, examined = check()
    missing, indexes, children = unlinked_children()

    if args.json:
        print(json.dumps({
            "published": len(urls),
            "examined": examined,
            "broken": broken,
            "indexes": indexes,
            "children": children,
            "unlinked_children": missing,
        }, indent=2))
        return 1 if broken or missing else 0

    # The count of links EXAMINED is part of the result. "0 broken" out of 4 links and out of
    # 1,400 are very different claims, and only one of them means the site is fine.
    print(f"{len(urls)} published URLs; {examined} internal links checked; {len(broken)} broken.")
    print(f"{indexes} section indexes; {children} child pages; {len(missing)} not linked from their index.\n")
    by_target: dict[str, list[dict[str, object]]] = {}
    for b in broken:
        by_target.setdefault(str(b["resolved"]), []).append(b)
    for target, hits in sorted(by_target.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        fix = str(hits[0].get("suggestion") or "")
        print(f"  {target}   <- {len(hits)} link(s)" + (f"   [page exists at {fix}]" if fix else ""))
        for h in hits[:6]:
            print(f"      {h['page']}:{h['line']}  [{h['link']}]")
        if len(hits) > 6:
            print(f"      ... and {len(hits) - 6} more")
    for m in missing:
        print(f"  unlinked: {m['child']}  ({m['url']})\n      not referenced by {m['index']}")
    return 1 if broken or missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
