"""SPEC-63 §2.1 — the 4-preset provider catalog (pure, stdlib only).

Static, offline-first: no imports from agent/*, ui/*, or gi; no network.
The caller taxonomy is deliberately NOT copied here (fix-round 1, Debugger
#15 / Flagger #14): enforcement is test-side only —
tests/test_provider_presets.py::TestPresetCallerInvariant asserts every
preset.caller against agent.runtime.get_valid_callers().
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderPreset:
    key: str                  # stable internal id, e.g. "openai"
    label: str                # menu text, e.g. "OpenAI"
    name: str                 # default ProviderConfig.name (user-editable)
    base_url: str
    default_model: str        # BARE or "<valid-caller>/<model>"
    caller: str               # must be in agent.runtime.get_valid_callers()
    api_key_url: str          # "Get a key" link target
    key_prefix_hint: str      # placeholder, e.g. "sk-…"
    context_window: int       # from models/providers.CALLER_DEFAULT_MAX_TOKENS
    supports_models_list: bool  # False for anthropic (no /models endpoint)


PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        key="openai", label="OpenAI", name="openai",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o", caller="openai",
        api_key_url="https://platform.openai.com/api-keys",
        key_prefix_hint="sk-…", context_window=128_000,
        supports_models_list=True,
    ),
    ProviderPreset(
        key="anthropic", label="Anthropic", name="anthropic",
        base_url="https://api.anthropic.com",  # NO /v1 — knowledge/providers.md:261
        default_model="claude-3-5-sonnet-20241022", caller="anthropic",
        api_key_url="https://console.anthropic.com/settings/keys",
        key_prefix_hint="sk-ant-…", context_window=200_000,
        supports_models_list=False,  # provider_test.py:237-238 skips /v1/models
    ),
    ProviderPreset(
        key="openrouter", label="OpenRouter", name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        default_model="openrouter/auto",  # deliberately prefixed (§2.1 note)
        caller="openrouter",
        api_key_url="https://openrouter.ai/keys",
        key_prefix_hint="sk-or-v1-…", context_window=128_000,
        supports_models_list=True,
    ),
    ProviderPreset(
        key="google", label="Google Gemini", name="google",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        default_model="openai/gemini-2.0-flash",  # V2 correction — foreign prefix deliberate
        caller="openai",  # serves via an OpenAI-compatible path
        api_key_url="https://aistudio.google.com",
        key_prefix_hint="AIza…", context_window=128_000,
        supports_models_list=True,
    ),
)


def get_preset(key: str) -> ProviderPreset | None:
    """Look up a preset by its stable key. None for unknown keys."""
    for preset in PRESETS:
        if preset.key == key:
            return preset
    return None


def preset_for(p) -> ProviderPreset | None:
    """Resolve a card's preset by base_url (NOT name — names are user-editable
    and renamed cards would otherwise lose their preset). None for Custom."""
    for preset in PRESETS:
        if p.base_url.rstrip("/") == preset.base_url.rstrip("/"):
            return preset
    return None


def preset_vendor_present(providers, preset: ProviderPreset) -> bool:
    """True when a stored card already serves this vendor — base_url match OR
    exact preset-name match. Used by create_from_env, startup_checks, and the
    env chip. SINGLE definition; all three sites MUST call this, not restate."""
    return any(
        p.base_url.rstrip("/") == preset.base_url.rstrip("/")
        or p.name == preset.name
        for p in providers
    )


def presets_from_env(env: dict[str, str]) -> list[ProviderPreset]:
    """Presets whose industry-standard env key is present and non-blank (§2.5).
    The key VALUE is never surfaced — see utils.env_provider_keys."""
    from utils.env_provider_keys import detect_env_keys
    out: list[ProviderPreset] = []
    for _env_name, preset_key, _value in detect_env_keys(env):
        preset = get_preset(preset_key)
        if preset is not None and preset not in out:
            out.append(preset)
    return out
