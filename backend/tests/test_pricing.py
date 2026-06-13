"""Tests for the token cost-estimation pricing table."""
from app.core.pricing import estimate_cost, is_priced, model_rate


def test_model_rate_exact_family():
    assert model_rate("gpt-4o-mini") == (0.15, 0.60)
    # "gpt-4o" must resolve to the gpt-4o tier, not the broader "gpt-4".
    assert model_rate("gpt-4o") == (2.50, 10.00)


def test_model_rate_longest_prefix_wins():
    # "claude-opus-4" is a longer (more specific) prefix than "claude".
    assert model_rate("claude-opus-4.7") == (15.00, 75.00)
    assert model_rate("claude-3-5-sonnet-20241022") == (3.00, 15.00)


def test_model_rate_handles_provider_prefixed_names():
    assert model_rate("openai/gpt-4o-mini") == (0.15, 0.60)
    assert model_rate("models/gemini-1.5-flash") == (0.075, 0.30)


def test_model_rate_unknown_falls_back_to_default():
    assert model_rate("some-brand-new-model") == (1.00, 3.00)
    assert model_rate(None) == (1.00, 3.00)
    assert model_rate("") == (1.00, 3.00)


def test_local_models_are_free():
    assert model_rate("ollama") == (0.0, 0.0)
    assert estimate_cost("ollama", 1_000_000, 1_000_000) == 0.0


def test_estimate_cost_per_million():
    # gpt-4o: $2.50 / 1M prompt, $10.00 / 1M completion.
    assert estimate_cost("gpt-4o", 1_000_000, 0) == 2.5
    assert estimate_cost("gpt-4o", 0, 1_000_000) == 10.0
    assert estimate_cost("gpt-4o", 0, 0) == 0.0


def test_estimate_cost_rounds_small_values():
    # Tiny token counts should still produce a small, non-negative cost.
    c = estimate_cost("gpt-4o-mini", 1000, 2000)
    assert c > 0
    assert round(c, 6) == c


def test_is_priced_flags_known_vs_default():
    assert is_priced("gpt-4o") is True
    assert is_priced("claude-opus-4.8") is True
    assert is_priced("totally-unknown-model") is False
    assert is_priced(None) is False


def test_estimate_cost_defensive_token_coercion():
    # Usage rows can be missing/garbage upstream — estimate_cost must never crash or
    # return a negative cost. None -> 0; negatives clamp to 0; floats truncate.
    assert estimate_cost("gpt-4o", None, 1_000_000) == 10.0  # None prompt treated as 0
    assert estimate_cost("gpt-4o", 1_000_000, None) == 2.5
    assert estimate_cost("gpt-4o", None, None) == 0.0
    # Negative counts must not flip the cost negative.
    assert estimate_cost("gpt-4o", -1_000_000, 1_000_000) == 10.0
    assert estimate_cost("gpt-4o", -5, -5) == 0.0
    # Float token counts are accepted (truncated to int).
    assert estimate_cost("gpt-4o", 1_000_000.9, 0) == 2.5


def test_is_priced_consistent_with_model_rate():
    # A near-miss like "gpt-4om" matches the "gpt-4o" prefix in BOTH functions — i.e.
    # is_priced() True iff model_rate() returns a non-default tier (no contradiction).
    for model in ["gpt-4o", "gpt-4om", "openai/gpt-4o", "claude-3-5-sonnet-x", "mystery-xyz"]:
        priced = is_priced(model)
        is_default = model_rate(model) == (1.00, 3.00)
        assert priced != is_default, f"inconsistent for {model!r}"

