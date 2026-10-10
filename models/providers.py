# models/providers.py
# Provider configuration dataclass — pure Python, no GTK, no network, no I/O.
#
# Manifest:
#   - Reads: nothing
#   - Writes: nothing
#   - Network: none
#   - Imports: only stdlib dataclasses

from dataclasses import dataclass


# Caller-specific default context windows. Used as a fallback when the
# configured max_tokens is missing/zero OR the /v1/models probe returns no
# context_window. Values verified against each provider's published docs:
#   - openai:      128_000 (gpt-4o, gpt-4-turbo)
#   - anthropic:   200_000 (claude-3+, claude-4)
#   - minimax:     1_048_576 (MiniMax-M2.7, MiniMax-M3 — per published docs)
#   - openrouter:  128_000 (most models; outliers discoverable via /v1/models probe)
#   - zai:         128_000 (GLM-4.5+, glm-5 series)
CALLER_DEFAULT_MAX_TOKENS: dict[str, int] = {
    "openai": 128_000,
    "anthropic": 200_000,
    "minimax": 1_048_576,
    "openrouter": 128_000,
    "zai": 128_000,
}


def caller_default_max_tokens(caller: str) -> int:
    """Return the default context window for a caller key, or 128_000 fallback.

    Used by agent/runtime._compute_model_max when provider_cfg.max_tokens is
    missing/zero AND by tests asserting the fallback behavior.
    """
    return CALLER_DEFAULT_MAX_TOKENS.get(caller.lower(), 128_000)


@dataclass
class ProviderConfig:
    """Configuration for a single LLM API provider."""
    name: str
    base_url: str
    api_key: str
    default_model: str
    caller: str = ""                    # API caller key (openai|minimax|anthropic|openrouter|zai)
    enabled: bool = True
    supports_tools: bool = True
    supports_streaming: bool = True
    max_tokens: int = 128_000
    default_max_tokens: int = 0
    compaction_threshold: float = 0.80  # fraction of max_tokens that triggers compaction
    last_verified_at: str | None = None
    last_error: str | None = None
    context_mode: str = "auto"          # "auto" | "preload" | "jit" | "hybrid"
    reasoning_effort: str = "off"       # "off" | "low" | "medium" | "high"
    supports_reasoning: bool = False    # send-side guard; card checkbox


# ── Context mode validation ───────────────────────────────────────────────────

_VALID_CONTEXT_MODES = frozenset({"auto", "preload", "jit", "hybrid"})
_VALID_REASONING_LEVELS = frozenset({"off", "low", "medium", "high"})


def validate_provider_context_mode(mode: str) -> str:
    """Validate and normalize a context_mode string. Raises ValueError on bad input."""
    if not isinstance(mode, str):
        raise ValueError(f"context_mode must be a string, got {type(mode).__name__}")
    normalized = mode.lower().strip()
    if normalized not in _VALID_CONTEXT_MODES:
        raise ValueError(
            f"Invalid context_mode: {mode!r}. Must be one of {sorted(_VALID_CONTEXT_MODES)}."
        )
    return normalized


def validate_provider_reasoning_effort(level: object) -> str:
    """Coerce a reasoning_effort value to off|low|medium|high.

    Missing, empty, non-string, or unrecognized values become ``"off"``.
    The adapter must never see a level outside the closed set — a bad
    value forwarded on the wire can 400 the request.
    """
    if not isinstance(level, str):
        return "off"
    normalized = level.strip().lower()
    if normalized not in _VALID_REASONING_LEVELS:
        return "off"
    return normalized
