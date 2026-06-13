"""Best-effort token cost estimation.

We don't have per-call billing from providers, but token counts ARE recorded per
request (`Usage`), so we can estimate spend with a static per-model price table.
Prices are USD per 1,000,000 tokens (prompt / completion), matched by longest model
prefix so families (e.g. "gpt-4o", "gpt-4o-mini") resolve to the right tier. Unknown
models fall back to a conservative default rather than reporting $0 (which would hide
real spend). This is an ESTIMATE for governance/visibility — not a billing source.
"""
from __future__ import annotations

# (prompt_per_1m, completion_per_1m) in USD. Ordered most-specific-first is not
# required — lookup picks the longest matching prefix.
_PRICES: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-5.5": (5.00, 15.00),
    "gpt-5": (5.00, 15.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-4": (30.00, 60.00),
    "gpt-3.5": (0.50, 1.50),
    "o4-mini": (1.10, 4.40),
    "o3-mini": (1.10, 4.40),
    "o3": (2.00, 8.00),
    "o1-mini": (1.10, 4.40),
    "o1": (15.00, 60.00),
    # Anthropic Claude
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-3-haiku": (0.25, 1.25),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-7-sonnet": (3.00, 15.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-opus-4": (15.00, 75.00),
    "claude-3-opus": (15.00, 75.00),
    "claude": (3.00, 15.00),
    # Google Gemini
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-1.5-pro": (1.25, 5.00),
    "gemini-1.5-flash": (0.075, 0.30),
    "gemini": (0.10, 0.40),
    # Mistral
    "mistral-large": (2.00, 6.00),
    "mistral-small": (0.20, 0.60),
    "mistral": (0.20, 0.60),
    # xAI Grok
    "grok-2": (2.00, 10.00),
    "grok": (2.00, 10.00),
    # Local / self-hosted — no marginal cost.
    "ollama": (0.0, 0.0),
    "lmstudio": (0.0, 0.0),
    "llama": (0.0, 0.0),
}

# Conservative fallback for models we don't recognise (rough mid-tier estimate) so
# spend is visible rather than silently $0.
_DEFAULT_PRICE = (1.00, 3.00)


def model_rate(model: str | None) -> tuple[float, float]:
    """Return (prompt, completion) USD-per-1M-token rate for a model name."""
    if not model:
        return _DEFAULT_PRICE
    key = model.strip().lower()
    best: tuple[float, float] | None = None
    best_len = -1
    for prefix, rate in _PRICES.items():
        if key.startswith(prefix) and len(prefix) > best_len:
            best = rate
            best_len = len(prefix)
    if best is not None:
        return best
    # Also catch provider-prefixed names like "openai/gpt-4o" or "models/gemini-...".
    tail = key.split("/")[-1]
    if tail != key:
        return model_rate(tail)
    return _DEFAULT_PRICE


def estimate_cost(model: str | None, prompt_tokens: int, completion_tokens: int) -> float:
    """Estimated USD cost for a number of prompt/completion tokens on `model`.

    Token counts are coerced defensively (None -> 0, negatives clamped to 0, floats
    truncated) so a missing/garbage Usage row can never crash or produce a negative cost.
    """
    def _safe(n: object) -> int:
        try:
            return max(0, int(n or 0))
        except (TypeError, ValueError):
            return 0

    p_rate, c_rate = model_rate(model)
    pt, ct = _safe(prompt_tokens), _safe(completion_tokens)
    cost = (pt / 1_000_000.0) * p_rate + (ct / 1_000_000.0) * c_rate
    return round(cost, 6)


def is_priced(model: str | None) -> bool:
    """Whether `model` matched a known price tier (vs. the default fallback)."""
    if not model:
        return False
    key = model.strip().lower()
    tail = key.split("/")[-1]
    return any(key.startswith(p) or tail.startswith(p) for p in _PRICES)
