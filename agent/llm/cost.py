"""Cost calculation for LLM API calls.

Extracted from agent/runtime.py (Phase B1). Pure functions — no network,
no GTK, no state. The cost tables are USD per 1M tokens.
"""

from __future__ import annotations


# ── Cost tables (USD per 1M tokens) ─────────────────────────────────────────

OPENAI_COST = {"prompt": 2.5, "completion": 10.0}    # GPT-4o
MINIMAX_COST = {"prompt": 0.5, "completion": 1.0}    # MiniMax-M2
ANTHROPIC_COST = {"prompt": 3.0, "completion": 15.0}  # Claude 3.5

PROVIDER_COSTS: dict[str, dict[str, float]] = {
    "openai": OPENAI_COST,
    "minimax": MINIMAX_COST,
    "anthropic": ANTHROPIC_COST,
    "openrouter": OPENAI_COST,  # varies by model, using openai as fallback
    "zai": OPENAI_COST,        # free tier, no cost
}


def model_id(model: str) -> str:
    """Strip the provider prefix, returning the model ID sent to the API.

    'minimax/MiniMax-M2.7'       -> 'MiniMax-M2.7'
    'openrouter/deepseek/deepseek-v4-pro' -> 'deepseek/deepseek-v4-pro'
    """
    parts = model.split("/", 1)
    return parts[1] if len(parts) > 1 else parts[0]


def cost_for_model(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Compute cost in USD for a model call."""
    provider = model.split("/")[0] if "/" in model else model
    # Prefer a per-card (full model string) cost entry — two cards sharing a
    # provider prefix may have different pricing. Falls back to the prefix
    # table, then the openai default.
    costs = (
        PROVIDER_COSTS.get(model)
        or PROVIDER_COSTS.get(provider)
        or OPENAI_COST
    )
    return (prompt_tokens / 1_000_000 * costs["prompt"] +
            completion_tokens / 1_000_000 * costs["completion"])
