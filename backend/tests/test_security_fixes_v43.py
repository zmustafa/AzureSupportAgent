"""v43: the SPA static fallback must use CONTAINMENT, not a string prefix.

`app/main.py::_spa_fallback` serves a real file when one exists under the bundled static
directory and otherwise hands back index.html. The containment check was:

    if candidate.is_file() and str(candidate).startswith(str(_STATIC_DIR)):

`str.startswith` is a PREFIX test. With ``_STATIC_DIR = /app/static`` it also accepts
``/app/static-backup/...`` and ``/app/static_old/...`` -- any sibling whose name begins
with the same characters. `.resolve()` defeats ``../`` traversal, so that part was fine;
this is the subtler sibling-prefix case.

Not exploitable in the shipped image today (no such sibling exists), which is precisely
why it survived review: it is one stray `COPY` away from being a real file read. Found
independently by CodeQL as `py/path-injection`.

These tests exercise the predicate directly against a temp tree, so they do not depend on
what happens to be inside the built image.
"""
from __future__ import annotations

from pathlib import Path


def _served(candidate: Path, static_dir: Path) -> bool:
    """The containment rule as `_spa_fallback` now applies it."""
    return candidate.is_file() and candidate.is_relative_to(static_dir)


def _served_old_buggy(candidate: Path, static_dir: Path) -> bool:
    """The previous prefix rule, kept so the tests below are demonstrably non-vacuous."""
    return candidate.is_file() and str(candidate).startswith(str(static_dir))


def test_a_sibling_directory_sharing_the_prefix_is_not_served(tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("spa", encoding="utf-8")

    # The attacker's target: a SIBLING whose name starts with "static".
    sibling = tmp_path / "static-backup"
    sibling.mkdir()
    secret = sibling / "secrets.env"
    secret.write_text("SECRETS_ENCRYPTION_KEY=would-be-leaked", encoding="utf-8")

    assert not _served(secret.resolve(), static), (
        "a sibling directory sharing the static prefix must not be served"
    )


def test_the_old_prefix_check_would_have_served_it(tmp_path):
    """Non-vacuity: proves the test above is testing something real.

    If this ever stops passing, the sibling case is no longer reachable by the old rule
    and the regression test above has lost its meaning.
    """
    static = tmp_path / "static"
    static.mkdir()
    sibling = tmp_path / "static-backup"
    sibling.mkdir()
    secret = sibling / "secrets.env"
    secret.write_text("x", encoding="utf-8")

    assert _served_old_buggy(secret.resolve(), static), (
        "expected the previous prefix check to accept the sibling -- if it does not, "
        "this regression test is no longer demonstrating the bug it was written for"
    )


def test_real_static_files_are_still_served(tmp_path):
    """The fix must not break the feature it guards."""
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    asset = static / "assets" / "index-abc123.js"
    asset.write_text("console.log(1)", encoding="utf-8")

    assert _served(asset.resolve(), static)


def test_classic_traversal_is_still_blocked(tmp_path):
    """`.resolve()` handled this before and must continue to."""
    static = tmp_path / "static"
    static.mkdir()
    outside = tmp_path / "etc_passwd"
    outside.write_text("root:x:0:0", encoding="utf-8")

    candidate = (static / ".." / "etc_passwd").resolve()
    assert not _served(candidate, static)


def test_main_no_longer_uses_the_prefix_check():
    """Pin the fix in the shipped code, not just in this file's helpers."""
    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(
        encoding="utf-8"
    )
    assert "is_relative_to(_STATIC_DIR)" in source
    assert "startswith(str(_STATIC_DIR))" not in source, (
        "the SPA fallback regressed to a string-prefix containment check"
    )
