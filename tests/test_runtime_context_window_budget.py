"""SPEC-38 Tests #4: the post-save 1M window reaches the runtime meter AND
the system-prompt budget.

Simulates the on-disk state produced by the SPEC-38 settings dual-write
(max_tokens == context_window == 1_000_000) and asserts the runtime resolves
1_000_000 for both _compute_model_max (compaction meter) and
_budget_window_and_mode (system-prompt budget) — for the CONVERSATION's
provider, never a raw max_tokens off the default provider.

Headless: __new__-built runtime (same pattern as tests/test_runtime_compaction),
no GTK, no network, no LLM calls.
"""
from __future__ import annotations

from agent.config import AgentConfig, LLMProviderConfig, _to_llm_provider
from agent.runtime import AgentRuntime
from models.conversation import Conversation
from models.providers import ProviderConfig
from tests.test_runtime_compaction import _make_runtime
from utils.providers_store import load_providers, save_providers


def _llm_provider(**kwargs) -> LLMProviderConfig:
    base = {
        "name": "openai",
        "base_url": "x",
        "api_key": "x",
        "default_model": "openai/gpt-4o",
        "caller": "openai",
        "max_tokens": 1_000_000,
        "context_window": 1_000_000,  # SPEC-38 dual-write end state
    }
    base.update(kwargs)
    return LLMProviderConfig(**base)


def _conv(model: str) -> Conversation:
    return Conversation(agent_name="Coder", model=model)


def test_compute_model_max_resolves_saved_context_window():
    rt = _make_runtime({"openai": _llm_provider()}, default_provider="openai")
    assert rt._compute_model_max(_conv("openai/gpt-4o")) == 1_000_000


def test_budget_window_and_mode_derive_saved_context_window():
    rt = _make_runtime({"openai": _llm_provider()}, default_provider="openai")
    window, mode = rt._budget_window_and_mode(_conv("openai/gpt-4o"))
    assert window == 1_000_000
    assert mode == "auto"


def test_budget_uses_resolver_not_raw_max_tokens_when_fields_disagree():
    """SPEC-38 mutation guard: when context_window != max_tokens, the budget
    must resolve context_window (1_000_000), NOT read raw max_tokens (64_000).
    A resolver-bypass mutation returns the smaller number and fails here."""
    rt = _make_runtime(
        {
            "openai": _llm_provider(
                max_tokens=64_000, context_window=1_000_000
            )
        },
        default_provider="openai",
    )
    assert rt._budget_window_and_mode(_conv("openai/gpt-4o"))[0] == 1_000_000
    assert rt._compute_model_max(_conv("openai/gpt-4o")) == 1_000_000


def test_conversation_provider_wins_over_default_provider():
    # SPEC-38 §4: the budget derives from the CONVERSATION's provider
    # (model prefix). The default provider's window must not leak in.
    rt = _make_runtime(
        {
            "openai": _llm_provider(),
            "minimax": _llm_provider(
                name="minimax",
                caller="minimax",
                default_model="minimax/m",
                max_tokens=128_000,
                context_window=0,
            ),
        },
        default_provider="minimax",
    )
    assert rt._compute_model_max(_conv("openai/gpt-4o")) == 1_000_000
    assert rt._compute_model_max(_conv("minimax/m")) == 128_000


def test_disk_round_trip_post_save_state(tmp_config_dir):
    """End-to-end: exactly the post-save state from the SPEC-38 settings test
    (both fields 1_000_000 on disk) reaches the runtime resolver."""
    save_providers([ProviderConfig(
        name="openai",
        base_url="https://example.com",
        api_key="k",
        default_model="openai/gpt-4o",
        caller="openai",
        max_tokens=1_000_000,
        context_window=1_000_000,
    )])
    stored = load_providers()[0]
    assert stored.max_tokens == 1_000_000
    assert stored.context_window == 1_000_000
    config = AgentConfig(
        providers={"openai": _to_llm_provider(stored)}, default_provider="openai",
    )
    rt = AgentRuntime.__new__(AgentRuntime)
    rt._config = config
    conv = _conv("openai/gpt-4o")
    assert rt._compute_model_max(conv) == 1_000_000
    assert rt._budget_window_and_mode(conv)[0] == 1_000_000


def test_unknown_provider_in_model_falls_back_to_128k():
    rt = _make_runtime({"openai": _llm_provider()}, default_provider="openai")
    assert rt._compute_model_max(_conv("ghost/m")) == 128_000


# ── Provider-card resolution (provider-card bug fix) ──────────────────────────
# Cards that share a provider prefix ('zai/…') must resolve to the RIGHT card
# via conv.provider (display name) or an exact default_model match — never via
# an ambiguous prefix (last-writer-wins across zai/* cards was the bug).

def _zai_cards() -> dict[str, LLMProviderConfig]:
    return {
        "GLM 5.3": LLMProviderConfig(
            name="GLM 5.3", base_url="x", api_key="k",
            default_model="zai/glm-5.3", caller="zai",
            max_tokens=128_000, context_window=262_144,
        ),
        "GLM 5.3 Flash": LLMProviderConfig(
            name="GLM 5.3 Flash", base_url="x", api_key="k",
            default_model="zai/glm-5.3-flash", caller="zai",
            max_tokens=128_000, context_window=32_768,
        ),
    }


class TestProviderCardResolution:
    def test_stored_card_name_selects_right_card_per_conversation(self):
        rt = _make_runtime(_zai_cards(), default_provider="zai")
        conv = _conv("zai/glm-5.3")
        conv.provider = "GLM 5.3"
        assert rt._compute_model_max(conv) == 262_144
        assert rt._budget_window_and_mode(conv)[0] == 262_144
        flash = _conv("zai/glm-5.3-flash")
        flash.provider = "GLM 5.3 Flash"
        assert rt._compute_model_max(flash) == 32_768

    def test_full_string_fallback_finds_card_after_display_name_deleted(self):
        # The stored card display name no longer resolves (card deleted /
        # renamed between save and load) — the exact default_model match must
        # still find the right card.
        rt = _make_runtime(_zai_cards(), default_provider="zai")
        conv = _conv("zai/glm-5.3")
        conv.provider = "GLM 9.9"  # not a configured display name
        assert rt._compute_model_max(conv) == 262_144

    def test_ambiguous_prefix_never_guesses(self):
        # Two cards share the 'zai' prefix, neither default_model equals the
        # conversation's model, and no card name is stored → prefix alone
        # cannot identify the card → resolver must return None (no guess) and
        # the meter falls back to 128_000.
        rt = _make_runtime(_zai_cards(), default_provider="zai")
        ambiguous = _conv("zai/glm-5.3-lite")  # no exact card, shared prefix
        assert rt._resolve_provider_cfg(ambiguous) is None
        assert rt._compute_model_max(ambiguous) == 128_000

    def test_create_conversation_stores_card_name(self):
        import threading
        rt = _make_runtime(_zai_cards(), default_provider="zai")
        rt._lock = threading.Lock()
        rt._conversations = {}
        sk = "agent:zai-test"
        rt.create_conversation(
            "Coder", sk, model="zai/glm-5.3",
            provider="GLM 5.3", defer_prompt_build=True,
        )
        conv = rt.get_conversation(sk)
        assert conv.provider == "GLM 5.3"
        assert conv.model == "zai/glm-5.3"
