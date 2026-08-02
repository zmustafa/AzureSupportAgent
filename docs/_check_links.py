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

Usage (from docs/):

    python _check_links.py            # report
    python _check_links.py --json     # machine-readable

Exit code 1 when anything is broken, so it can gate a docs change.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Mirrors `exclude:` in _config.yml — these are not published, so links into them are broken and
# links FROM them are irrelevant.
EXCLUDED_DIRS = {
    "_journey_render", "deck-assets", "improvement-plans", "test-findings", "usecase-render",
    "_site", "_sass", ".jekyll-cache",
}
EXCLUDED_FILES = {
    "ARCHITECTURES_FEATURE.md", "ARCHITECTURES_TEST_PLAN.md", "BUG_HUNTING_PLAN.md",
    "DATA_RETENTION_PLAN.md", "GRAPH_TEST_PLAN.md", "INVENTORY_TEST_PLAN.md",
    "UI_TEST_PLAN.md", "UX_ADVANCED_PLAN.md",
}

FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
PERMALINK = re.compile(r"^permalink:\s*(.+?)\s*$", re.MULTILINE)
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
    if rel.name in EXCLUDED_FILES:
        return True
    return any(part in EXCLUDED_DIRS for part in rel.parts)


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    urls, _ = published_urls()
    broken, examined = check()

    if args.json:
        print(json.dumps({"published": len(urls), "examined": examined, "broken": broken}, indent=2))
        return 1 if broken else 0

    # The count of links EXAMINED is part of the result. "0 broken" out of 4 links and out of
    # 1,400 are very different claims, and only one of them means the site is fine.
    print(f"{len(urls)} published URLs; {examined} internal links checked; {len(broken)} broken.\n")
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
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
