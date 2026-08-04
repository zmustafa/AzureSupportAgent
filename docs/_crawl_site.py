"""Crawl the built _site/ for internal hrefs and srcs that resolve to nothing.

Complements _check_links.py: that one reads the markdown sources, this one reads what Jekyll
actually published, so a link broken by a layout, an include or a permalink collision is caught
too. Temporary validation helper; safe to delete.

Usage (from docs/, after `bundle exec jekyll build`):

    python _crawl_site.py

Exit code 1 when anything is broken.
"""
from __future__ import annotations

import collections
import pathlib
import re
import sys
import urllib.parse

BASEURL = "/AzureSupportAgent"
SITE = pathlib.Path(__file__).resolve().parent / "_site"
LINK = re.compile(r"""(?:href|src)=["']([^"']+)["']""")


def published() -> set[str]:
    out: set[str] = set()
    for f in SITE.rglob("*"):
        if not f.is_file():
            continue
        rel = "/" + f.relative_to(SITE).as_posix()
        out.add(rel)
        if f.name == "index.html":
            out.add(rel[: -len("index.html")])
    return out


def main() -> int:
    if not SITE.is_dir():
        print("no _site/ — build first")
        return 1
    files = published()
    broken: collections.Counter[tuple[str, str]] = collections.Counter()
    checked = 0
    for page in SITE.rglob("*.html"):
        html = page.read_text(encoding="utf-8", errors="ignore")
        for raw in LINK.findall(html):
            if not raw.startswith(BASEURL + "/"):
                continue
            target = urllib.parse.unquote(urllib.parse.urlsplit(raw).path)[len(BASEURL):]
            checked += 1
            if target in files or target + "index.html" in files or target + "/" in files:
                continue
            broken[(target, page.relative_to(SITE).as_posix())] += 1
    print(f"internal hrefs/srcs checked: {checked}")
    print(f"broken: {len(broken)}")
    for (target, src), _n in sorted(broken)[:50]:
        print(f"  {target}  <- {src}")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
