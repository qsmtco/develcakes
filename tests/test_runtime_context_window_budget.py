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
