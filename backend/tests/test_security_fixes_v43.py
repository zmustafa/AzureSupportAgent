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


# --------------------------------------------------------------------------------------
# py/polynomial-redos: eleven patterns that took seconds on ~40k characters of input.
#
# Each test below feeds the *shipped* callable a worst-case string and asserts it returns
# inside BUDGET_S. These are deliberately not micro-benchmarks: the fixed forms all finish
# in around a millisecond while every original took 1.7-10.5 SECONDS on the same input, so
# a one-second budget separates them by three orders of magnitude and will not flake on a
# slow runner. If someone reintroduces a backtracking form, the test hangs then fails.
#
# The accompanying behavior tests matter just as much. Two "obvious" rewrites attempted
# during this work were fast but silently WRONG (`\s+` collapsed intra-line spaces inside
# quoted KQL literals), and three others were equivalent but slower than what they
# replaced. Speed alone is not the property being pinned here.
# --------------------------------------------------------------------------------------

BUDGET_S = 1.0
ATTACK_N = 40_000


def _under_budget(fn, payload):
    import time

    start = time.perf_counter()
    fn(payload)
    return time.perf_counter() - start


def test_kql_comment_stripping_is_linear():
    from app.alerts_manager.advisory import _strip_comments

    # Many `/*` openers and no `*/` anywhere: the old lazy `.*?` rescanned to end-of-
    # string from each one. 1983ms before.
    elapsed = _under_budget(_strip_comments, "/* " * ATTACK_N)
    assert elapsed < BUDGET_S, f"_strip_comments took {elapsed:.2f}s"


def test_kql_comment_stripping_still_strips_both_styles():
    from app.alerts_manager.advisory import _strip_comments

    assert _strip_comments("Heartbeat // note").strip() == "Heartbeat"
    assert _strip_comments("Heartbeat /* a\nb */ | take 5").split() == ["Heartbeat", "|", "take", "5"]
    # Order matters: a `//` INSIDE a block comment must not win. Handling the two comment
    # styles in separate passes gets this wrong.
    assert _strip_comments("/* // */").strip() == ""
    # An unterminated block comment is left alone, exactly as the regex did.
    assert _strip_comments("Heartbeat /* oops") == "Heartbeat /* oops"


def test_promql_semantic_key_is_linear():
    from app.alerts_manager.advisory import _promql_semantic_key

    elapsed = _under_budget(_promql_semantic_key, "up > " + " " * ATTACK_N)
    assert elapsed < BUDGET_S, f"_promql_semantic_key took {elapsed:.2f}s"


def test_promql_semantic_key_still_ignores_the_threshold():
    """The whole point of the key: same expression, different threshold -> same key."""
    from app.alerts_manager.advisory import _promql_semantic_key

    assert _promql_semantic_key("up > 5") == _promql_semantic_key("up > 99")
    assert _promql_semantic_key("up>5") == _promql_semantic_key("up >= 0.5")
    assert _promql_semantic_key("up > 5") != _promql_semantic_key("down > 5")


def test_tag_question_parsing_is_linear():
    from app.tagintel.ask import _MISSING_RE, _VALUES_RE

    # `question` reaches these unbounded and entirely user-supplied. 3050ms before.
    for name, rx, payload in (
        ("_VALUES_RE", _VALUES_RE, "values for " + " " * ATTACK_N + "!"),
        ("_MISSING_RE", _MISSING_RE, "missing " + " " * ATTACK_N + "!"),
    ):
        elapsed = _under_budget(rx.search, payload)
        assert elapsed < BUDGET_S, f"{name} took {elapsed:.2f}s"


def test_tag_question_parsing_still_captures_the_key():
    from app.tagintel.ask import _MISSING_RE, _VALUES_RE

    assert _VALUES_RE.search("distinct values for cost center").group(2) == "cost center"
    assert _VALUES_RE.search("values of Owner").group(3) == "Owner"
    assert _MISSING_RE.search("which resources are missing app-name").group(1) == "app-name"
    assert _MISSING_RE.search("nothing here") is None


def test_disallowed_kql_check_is_linear():
    from app.alerts_manager.rules import _DISALLOWED_KQL

    # The 8,000-character guard only *reports* an oversize query; this search still runs.
    elapsed = _under_budget(_DISALLOWED_KQL.search, "x;" + "\n" * ATTACK_N)
    assert elapsed < BUDGET_S, f"_DISALLOWED_KQL took {elapsed:.2f}s"


def test_disallowed_kql_check_still_catches_control_commands():
    """A security control -- this must keep matching, including across newline runs."""
    from app.alerts_manager.rules import _DISALLOWED_KQL

    for blocked in (
        ".show tables",
        "Heartbeat;.show version",
        "Heartbeat;\n\n\n   .drop table",  # the newline-run case the rewrite had to keep
        "Heartbeat\n\t.show",
        "externaldata(x:string)",
        "EXTERNALDATA(x:string)",
        "let a = external_table('t')",
        "evaluate   python(typeof(x))",
    ):
        assert _DISALLOWED_KQL.search(blocked), f"no longer blocked: {blocked!r}"

    for allowed in ("Heartbeat | take 5", "requests | project a.b", "Heartbeat | where ok"):
        assert not _DISALLOWED_KQL.search(allowed), f"false positive: {allowed!r}"


def test_email_validation_is_linear():
    from app.alerts_manager.service import _EMAIL_RE

    elapsed = _under_budget(_EMAIL_RE.fullmatch, "a@" + "a." * (ATTACK_N // 2) + "!")
    assert elapsed < BUDGET_S, f"_EMAIL_RE took {elapsed:.2f}s"


def test_email_validation_accepts_real_addresses_and_rejects_consecutive_dots():
    from app.alerts_manager.service import _EMAIL_RE

    for good in ("a@b.co", "first.last@example.com", "a+b@sub.domain.co.uk", "A@B.CO"):
        assert _EMAIL_RE.fullmatch(good), f"rejected a valid address: {good!r}"

    for bad in ("a@b", "@b.co", "a@.co", "a b@c.co"):
        assert not _EMAIL_RE.fullmatch(bad), f"accepted an invalid address: {bad!r}"

    # Behavior CHANGE, pinned deliberately: the old pattern let the literal dot and the
    # dot inside the character class share a character, so `a@b..co` validated.
    assert not _EMAIL_RE.fullmatch("a@b..co"), "consecutive dots must not validate"


def test_query_line_joining_is_linear():
    from app.exec.command_runner import _join_wrapped_lines

    elapsed = _under_budget(_join_wrapped_lines, "Heartbeat" + "\n \t" * ATTACK_N)
    assert elapsed < BUDGET_S, f"_join_wrapped_lines took {elapsed:.2f}s"


def test_query_line_joining_preserves_spacing_inside_literals():
    """The fast-but-wrong rewrite (`\\s+`) corrupted this and nothing else caught it."""
    from app.exec.command_runner import _join_wrapped_lines

    assert _join_wrapped_lines('Heartbeat\n| where x == "a  b"') == 'Heartbeat | where x == "a  b"'
    assert _join_wrapped_lines("Heartbeat\n\n|  take 5") == "Heartbeat |  take 5"
    assert _join_wrapped_lines("  Heartbeat  ") == "Heartbeat"
    assert _join_wrapped_lines("") == ""
