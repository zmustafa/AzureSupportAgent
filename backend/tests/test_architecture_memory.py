"""Tests for the architecture memory store + Markdown rendering + investigation cap."""
from app.architectures import memory as mem


def _sections(*pairs):
    return [{"key": k, "label": mem.section_label(k), "content": c} for k, c in pairs]


def test_catalog_and_defaults_are_consistent():
    keys = {s["key"] for s in mem.SECTION_CATALOG}
    # Every default + priority key must exist in the catalog.
    assert set(mem.DEFAULT_SECTION_KEYS).issubset(keys)
    for k in mem.INVESTIGATION_PRIORITY_KEYS:
        assert k in keys


def test_section_label_falls_back_for_custom_keys():
    assert mem.section_label("overview") == "Overview"
    assert mem.section_label("custom_my_thing") == "Custom My Thing"


def test_default_sections_shape():
    secs = mem.default_sections()
    assert len(secs) == len(mem.DEFAULT_SECTION_KEYS)
    assert all(s["content"] == "" and s["key"] and s["label"] for s in secs)


def test_render_markdown_skips_empty_sections():
    memory = {
        "title": "Portal Memory",
        "sections": _sections(
            ("overview", "A customer portal."),
            ("known_gaps", ""),  # empty → skipped
            ("diagnostic_hints", "Check Front Door first."),
        ),
    }
    md = mem.render_markdown(memory, "Portal", "Customer Portal")
    assert "# Portal Memory" in md
    assert "> **Linked workload:** Customer Portal" in md
    assert "## Overview" in md
    assert "A customer portal." in md
    assert "## Diagnostic hints" in md
    # The empty known_gaps section must not appear.
    assert "## Known gaps" not in md


def test_render_markdown_default_title():
    md = mem.render_markdown({"sections": _sections(("overview", "x"))}, "MyArch", "")
    assert md.startswith("# MyArch — Memory")


def test_render_for_investigation_prioritizes_key_sections():
    memory = {
        "title": "T",
        "sections": _sections(
            ("overview", "ov"),
            ("expected_flow", "User -> FD -> App"),
            ("diagnostic_hints", "check fd"),
            ("known_gaps", "no failover"),
        ),
    }
    out = mem.render_for_investigation(memory, "Arch", "WL", max_chars=4000)
    # Priority sections (expected_flow, diagnostic_hints, known_gaps) come before overview.
    assert out.index("Expected flow") < out.index("Overview")
    assert out.index("Diagnostic hints") < out.index("Overview")


def test_render_for_investigation_respects_cap():
    big = "x" * 5000
    memory = {"title": "T", "sections": _sections(("expected_flow", big), ("overview", "tail"))}
    out = mem.render_for_investigation(memory, "Arch", "WL", max_chars=500)
    assert len(out) <= 520  # cap + small header/ellipsis slack


def test_clean_sections_drops_keyless_entries():
    cleaned = mem._clean_sections([
        {"key": "overview", "content": "a"},
        {"key": "", "content": "ignored"},
        {"content": "no key"},
        "not a dict",
    ])
    assert len(cleaned) == 1
    assert cleaned[0]["key"] == "overview"
    assert cleaned[0]["label"] == "Overview"


# --- merge_ai_sections (full "Generate with AI" merge) -----------------------------------

def test_merge_ai_sections_overwrites_filled_sections():
    # Regression: a fully-populated memory used to silently keep its old content because the
    # merge only filled EMPTY sections. A full AI draft must overwrite.
    existing = _sections(("overview", "OLD overview"), ("expected_flow", "OLD flow"))
    merged = mem.merge_ai_sections(existing, {"overview": "NEW overview", "expected_flow": "NEW flow"})
    by_key = {s["key"]: s["content"] for s in merged}
    assert by_key["overview"] == "NEW overview"
    assert by_key["expected_flow"] == "NEW flow"


def test_merge_ai_sections_keeps_existing_when_ai_omits_section():
    # A partial AI draft must never wipe sections it didn't return content for.
    existing = _sections(("overview", "keep me"), ("expected_flow", "keep me too"))
    merged = mem.merge_ai_sections(existing, {"overview": "fresh"})
    by_key = {s["key"]: s["content"] for s in merged}
    assert by_key["overview"] == "fresh"
    assert by_key["expected_flow"] == "keep me too"


def test_merge_ai_sections_empty_ai_content_does_not_overwrite():
    existing = _sections(("overview", "keep me"))
    merged = mem.merge_ai_sections(existing, {"overview": "   "})  # whitespace = empty
    assert merged[0]["content"] == "keep me"


def test_merge_ai_sections_appends_new_catalog_keys():
    existing = _sections(("overview", "ov"))
    merged = mem.merge_ai_sections(existing, {"overview": "ov2", "diagnostic_hints": "check fd"})
    keys = [s["key"] for s in merged]
    assert keys == ["overview", "diagnostic_hints"]
    assert merged[-1]["label"] == "Diagnostic hints"
    assert merged[-1]["content"] == "check fd"


def test_merge_ai_sections_clears_needs_review_on_overwrite():
    existing = [{"key": "overview", "label": "Overview", "content": "old", "needs_review": True}]
    merged = mem.merge_ai_sections(existing, {"overview": "new"})
    assert merged[0]["content"] == "new"
    assert "needs_review" not in merged[0]


def test_merge_ai_sections_seeds_defaults_when_existing_is_none():
    merged = mem.merge_ai_sections(None, {"overview": "from AI"})
    keys = [s["key"] for s in merged]
    # Must seed the full default catalog and fill overview from the AI payload.
    assert keys == list(mem.DEFAULT_SECTION_KEYS)
    by_key = {s["key"]: s["content"] for s in merged}
    assert by_key["overview"] == "from AI"
    # Sections the AI didn't return stay empty.
    for k in mem.DEFAULT_SECTION_KEYS:
        if k != "overview":
            assert by_key[k] == ""
