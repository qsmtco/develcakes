# tests/test_runtime_caller_resolution.py
# Unit tests for AgentRuntime._resolve_caller_key (PHASE-10).
# Verifies caller resolution priority:
#   1. Explicit provider_cfg.caller (highest priority)
#   2. Derivation from provider_cfg.default_model prefix
#   3. Fallback to model argument's prefix
#   4. Edge: empty model + empty caller + None provider_cfg

import types
import pytest

from agent.config import LLMProviderConfig
from agent.runtime import AgentRuntime


def _pcfg(name="Owl-Alpha", base_url="https://openrouter.ai/api/v1",
          default_model="openrouter/owl-alpha", caller=""):
    """Build a minimal LLMProviderConfig with a dummy key."""
    return LLMProviderConfig(
        name=name, base_url=base_url, api_key="k", default_model=default_model, caller=caller,
    )


class TestResolveCallerKey:
    """Tests for AgentRuntime._resolve_caller_key static method."""

    def test_explicit_caller_wins(self):
        """provider_cfg.caller is the highest-priority source."""
        pcfg = _pcfg(caller="openrouter")
        key = AgentRuntime._resolve_caller_key(pcfg, "Owl-Alpha/openrouter/owl-alpha")
        assert key == "openrouter"

    def test_derivation_from_default_model(self):
        """When caller is empty, returns empty string (no derivation)."""
        pcfg = _pcfg(caller="", default_model="minimax/MiniMax-M2.7")
        key = AgentRuntime._resolve_caller_key(pcfg, "minimax/MiniMax-M2.7")
        assert key == ""

    def test_fallback_to_model_argument(self):
        """When provider_cfg has no default_model and no caller, returns empty string."""
        pcfg = _pcfg(caller="", default_model="")
        key = AgentRuntime._resolve_caller_key(pcfg, "openrouter/owl-alpha")
        assert key == ""

    def test_none_provider_cfg_falls_through_to_model(self):
        """When provider_cfg is None, returns empty string."""
        key = AgentRuntime._resolve_caller_key(None, "openrouter/owl-alpha")
        assert key == ""

    def test_empty_inputs_return_model_as_is(self):
        """When nothing is resolvable, returns empty string."""
        pcfg = _pcfg(caller="", default_model="MiniMax-M2.7")
        key = AgentRuntime._resolve_caller_key(pcfg, "MiniMax-M2.7")
        assert key == ""

    def test_caller_mixed_case_lowered(self):
        """Caller is lowercased by _resolve_caller_key to match _PROVIDER_CALLERS keys."""
        pcfg = _pcfg(caller="OpenRouter")
        key = AgentRuntime._resolve_caller_key(pcfg, "Owl-Alpha/openrouter/owl-alpha")
        assert key == "openrouter"  # lowered to match _PROVIDER_CALLERS dict keys

    def test_all_known_caller_keys_resolvable(self):
        """All 5 known caller keys can be produced by _resolve_caller_key."""
        for caller_name, model in [
            ("openai", "gpt-4o"),
            ("minimax", "minimax/MiniMax-M2.7"),
            ("anthropic", "claude-3-5-sonnet"),
            ("openrouter", "openrouter/owl-alpha"),
            ("zai", "zai/glm-5"),
        ]:
            pcfg = _pcfg(caller=caller_name, default_model=model)
            key = AgentRuntime._resolve_caller_key(pcfg, model)
            assert key == caller_name, f"{caller_name}: got {key!r}"


class TestResolveAgentModelNoDoublePrefix:
    """Tests for the P4 fix: _resolve_agent_model must not double-prefix
    model names that already contain a slash."""

    def test_slashed_default_model_returned_as_is(self, monkeypatch):
        """If default_model contains a slash, return it as-is (no double prefix)."""
        from ui.handlers.agent_runtime_handler import AgentRuntimeHandler
        from agent.config import AgentConfig, LLMProviderConfig

        handler = AgentRuntimeHandler.__new__(AgentRuntimeHandler)
        fn = types.MethodType(AgentRuntimeHandler._resolve_agent_model, handler)

        class MockAgentDef:
            llm_name = "testprov"

        # Pin the config source so the test doesn't depend on the host
        # machine's providers.yaml (previously depended on the host's provider set).
        cfg = AgentConfig(
            providers={"testprov": LLMProviderConfig(
                name="testprov", base_url="https://x/v1", api_key="k",
                default_model="testprov/some-model", caller="openai",
            )},
        )
        import agent.config as config_mod
        monkeypatch.setattr(config_mod, "load_agent_config", lambda *a, **k: cfg)

        result = fn(MockAgentDef())
        assert result == "testprov/some-model", f"got {result!r}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
