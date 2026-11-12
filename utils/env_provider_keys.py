"""SPEC-63 §2.5 — industry-standard provider env-key detection (pure).

Deliberate deviation from utils.config.get_env(): that helper requires the
DEVELCAKES_/CRABCAKES_ prefix (utils/config.py:44-57) and would look for
DEVELCAKES_OPENAI_API_KEY. The whole point here is honoring the
industry-standard names users already have, so bare names are read.
Presence rule: a variable set to "" is treated as ABSENT (matches
utils.config.get_env's presence semantics, but a usable value is required).

Security invariant: the key VALUE never appears in any string this module
returns — callers put the value directly into ProviderConfig.api_key; UI
and startup warnings show only the env var NAME and the preset label.
"""

from __future__ import annotations

ENV_KEY_MAP: dict[str, str] = {
    "OPENAI_API_KEY": "openai",
    "ANTHROPIC_API_KEY": "anthropic",
    "OPENROUTER_API_KEY": "openrouter",
}


def detect_env_keys(env: dict[str, str]) -> list[tuple[str, str, str]]:
    """Return (env_var_name, preset_key, key_value) for present, non-blank vars.

    No os.environ read inside — pass os.environ (or a dict) in from the
    caller for testability. Order is deterministic: ENV_KEY_MAP insertion
    order (openai, anthropic, openrouter).
    """
    out: list[tuple[str, str, str]] = []
    for env_name, preset_key in ENV_KEY_MAP.items():
        value = env.get(env_name, "")
        if isinstance(value, str) and value.strip():
            out.append((env_name, preset_key, value))
    return out
