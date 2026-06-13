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
