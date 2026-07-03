"""Regression tests for the GitHub Copilot reasoning-model output-token floor.

Reasoning models (Opus 4.x, GPT-5, o-series, Sonnet 4) spend part of the output-token
budget on hidden reasoning before emitting any visible text. Without a floor, a small (or
unset) ``max_tokens`` can be entirely consumed by reasoning, returning an EMPTY completion
(the F-2 monitor bug: ``raw len=0`` -> downstream 422). The floor guarantees headroom.
"""
from __future__ import annotations

import pytest

from app.agent.github_copilot import (
    _REASONING_MIN_OUTPUT_TOKENS,
    _is_reasoning_model,
    _reasoning_floor,
)


@pytest.mark.parametrize(
    "model",
    ["claude-opus-4.8", "gpt-5", "o1", "o3-mini", "o4-mini", "claude-sonnet-4", "grok-4-thinking"],
)
def test_reasoning_models_detected(model):
    assert _is_reasoning_model(model) is True


@pytest.mark.parametrize("model", ["gpt-4.1", "gpt-4o", "claude-3-5-sonnet", "gpt-4o-mini", ""])
def test_non_reasoning_models_not_detected(model):
    assert _is_reasoning_model(model) is False


def test_floor_applied_when_unset():
    # The exact F-2 failure mode: caller passes no cap -> reasoning eats the default -> empty.
    assert _reasoning_floor("claude-opus-4.8", None) == _REASONING_MIN_OUTPUT_TOKENS


def test_floor_raises_small_cap():
    assert _reasoning_floor("claude-opus-4.8", 2000) == _REASONING_MIN_OUTPUT_TOKENS


def test_floor_keeps_larger_cap():
    # A generous caller cap must be preserved (this is a floor, not a ceiling).
    assert _reasoning_floor("claude-opus-4.8", 32000) == 32000


def test_non_reasoning_model_passes_through_unchanged():
    assert _reasoning_floor("gpt-4.1", None) is None
    assert _reasoning_floor("gpt-4.1", 2000) == 2000
    assert _reasoning_floor("gpt-4.1", 32000) == 32000
