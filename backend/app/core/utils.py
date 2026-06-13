"""Small shared utilities used across the app: safe JSON parsing and error
formatting. Centralizes patterns that were previously duplicated in several modules.
"""
from __future__ import annotations

import json
from typing import Any


def safe_json_parse(value: Any, default: Any = None) -> Any:
    """Parse a JSON string into a Python object, returning ``default`` on failure.

    Accepts values that are already parsed (dict/list) and returns them unchanged, so
    callers iterating over heterogeneous tool-result blocks don't need their own
    try/except around every ``json.loads``.
    """
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, (str, bytes, bytearray)):
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        return default


def format_error(exc: BaseException, max_len: int = 500) -> str:
    """Render an exception as a clean, bounded message for logs/UI.

    Falls back to the exception class name when ``str(exc)`` is empty (common for
    some library exceptions), and truncates to ``max_len`` to keep payloads sane.
    """
    detail = str(exc).strip() or exc.__class__.__name__
    return detail[:max_len]
