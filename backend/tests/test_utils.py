"""Tests for shared utilities: safe JSON parsing and error formatting."""
from app.core.utils import format_error, safe_json_parse


def test_safe_json_parse_valid_string():
    assert safe_json_parse('{"a": 1}') == {"a": 1}
    assert safe_json_parse("[1, 2, 3]") == [1, 2, 3]


def test_safe_json_parse_passes_through_already_parsed():
    assert safe_json_parse({"a": 1}) == {"a": 1}
    assert safe_json_parse([1, 2]) == [1, 2]


def test_safe_json_parse_invalid_returns_default():
    assert safe_json_parse("not json", default=[]) == []
    assert safe_json_parse("{bad", default={"x": 1}) == {"x": 1}
    assert safe_json_parse(None) is None


def test_safe_json_parse_non_string_non_container_returns_default():
    assert safe_json_parse(123, default="fallback") == "fallback"
    assert safe_json_parse(object(), default=None) is None


def test_safe_json_parse_bytes():
    assert safe_json_parse(b'{"a": 2}') == {"a": 2}


def test_format_error_uses_message():
    assert format_error(ValueError("boom")) == "boom"


def test_format_error_falls_back_to_class_name_when_blank():
    assert format_error(ValueError("")) == "ValueError"
    assert format_error(RuntimeError("   ")) == "RuntimeError"


def test_format_error_truncates():
    out = format_error(ValueError("x" * 1000), max_len=10)
    assert len(out) == 10
