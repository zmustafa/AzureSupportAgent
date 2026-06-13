"""Pytest bootstrap: make the `app` package importable when tests run from any CWD."""
import os
import sys

# backend/ (the dir that contains the `app` package) — one level up from tests/.
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
