# tests/test_agent_builder_handler.py
# Tests for ui/handlers/agent_builder_handler.py — agent builder form logic.
#
# Tests validation, save/load round-trip, and option retrieval.
# No GTK imports — pure handler logic.

import os
import tempfile
import shutil

import pytest

import utils.agent_defs as ad
from ui.handlers.agent_builder_handler import AgentBuilderHandler


@pytest.fixture
def tmp_agents_dir(monkeypatch, tmp_path):
    """Redirect agent defs to a temp directory."""
    agents_dir = str(tmp_path / "agents")
    monkeypatch.setattr(ad, "_get_agents_dir", lambda: agents_dir)
    # Also redirect default source so no seeding interferes
    monkeypatch.setattr(ad, "_get_default_agents_src", lambda: str(tmp_path / "no_defaults"))
    return agents_dir


@pytest.fixture
def handler():
    saved = []
    deleted = []
    h = AgentBuilderHandler(
        on_agent_saved=lambda name: saved.append(name),
        on_agent_deleted=lambda name: deleted.append(name),
    )
    return h, saved, deleted


class TestCreateNew:
    def test_returns_template(self, handler):
        h, _, _ = handler
        template = h.create_new()
        assert template["name"] == ""
        assert template["emoji"] == "🤖"
        assert "read_file" in template["tools"]
        assert template["self_improvement"]["bug_journal"] is True
        assert template["self_improvement"]["enforcement"] is False

    def test_template_is_fresh(self, handler):
        h, _, _ = handler
        t1 = h.create_new()
        t1["name"] = "modified"
        t2 = h.create_new()
        assert t2["name"] == ""


class TestSaveValidation:
    def test_save_valid_agent(self, handler, tmp_agents_dir):
        h, saved, _ = handler
        agent = {
            "name": "TestAgent",
            "emoji": "🔬",
            "role": "tester",
            "prompts": ["system/coder.md"],
            "tools": ["read_file", "list_files"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
        }
        ok, errors = h.save(agent)
        assert ok
        assert errors == []
        assert "TestAgent" in saved

    def test_save_rejects_invalid(self, handler, tmp_agents_dir):
        h, _, _ = handler
        ok, errors = h.save({"name": ""})
        assert not ok
        assert len(errors) > 0

    def test_save_rejects_missing_fallback(self, handler, tmp_agents_dir):
        """Every agent must have a fallback_provider configured."""
        h, _, _ = handler
        ok, errors = h.save({
            "name": "NoFallback",
            "prompts": ["system/coder.md"],
            "tools": ["read_file"],
            "llm_name": "openrouter",
        })
        assert not ok
        assert any("fallback_provider" in e for e in errors)

    def test_save_fires_callback(self, handler, tmp_agents_dir):
        h, saved, _ = handler
        h.save({
            "name": "CallbackTest",
            "prompts": ["system/coder.md"],
            "tools": ["read_file"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
            "api_key": "sk-test-123",
        })
        assert saved == ["CallbackTest"]


class TestLoadForEdit:
    def test_load_existing(self, handler, tmp_agents_dir):
        h, _, _ = handler
        h.save({
            "name": "Editable",
            "prompts": ["system/coder.md"],
            "tools": ["read_file"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
            "api_key": "sk-test-edit",
        })
        loaded = h.load_for_edit("Editable")
        assert loaded is not None
        assert loaded["name"] == "Editable"

    def test_load_nonexistent(self, handler, tmp_agents_dir):
        h, _, _ = handler
        assert h.load_for_edit("Ghost") is None


class TestDelete:
    def test_delete_existing(self, handler, tmp_agents_dir):
        h, _, deleted = handler
        h.save({
            "name": "Deletable",
            "prompts": ["system/coder.md"],
            "tools": ["read_file"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
            "api_key": "sk-test-del",
        })
        assert h.delete("Deletable") is True
        assert "Deletable" in deleted

    def test_delete_nonexistent(self, handler, tmp_agents_dir):
        h, _, _ = handler
        assert h.delete("Ghost") is False

    def test_delete_fires_callback(self, handler, tmp_agents_dir):
        h, _, deleted = handler
        h.save({
            "name": "ToDelete",
            "prompts": ["system/coder.md"],
            "tools": ["read_file"],
            "llm_name": "openrouter",
            "fallback_provider": "openrouter",
            "api_key": "sk-test-del2",
        })
        h.delete("ToDelete")
        assert deleted == ["ToDelete"]


class TestOptions:
    def test_tool_options(self, handler):
        h, _, _ = handler
        tools = h.get_tool_options()
        assert len(tools) > 0
        assert all("name" in t for t in tools)

    def test_prompt_options(self, handler):
        h, _, _ = handler
        prompts = h.get_prompt_options()
        assert len(prompts) > 0

    def test_provider_options(self, handler, monkeypatch, tmp_path):
        # get_provider_options reads from providers.yaml via get_available_providers
        config_dir = str(tmp_path / ".config" / "crabcakes")
        os.makedirs(config_dir, exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        from utils.providers_store import save_providers
        from models.providers import ProviderConfig
        save_providers([
            ProviderConfig(name="minimax", base_url="https://api.minimax.chat/v1",
                           api_key="sk-test", default_model="MiniMax-M2.7"),
        ])
        h, _, _ = handler
        providers = h.get_provider_options()
        assert len(providers) > 0
        assert all("name" in p for p in providers)

    def test_save_provider_writes_to_yaml_not_agent_json(self, handler, tmp_config_dir):
        """A-5: save_provider writes to providers.yaml, NOT agent.json."""
        from utils.providers_store import load_providers
        import utils.config

        h, _, _ = handler
        # Use a "<vendor>/<model>" default_model so the caller
        # auto-detect path can derive a valid caller. (Prior versions
        # of save_provider were a data-sink; the PROV-CALLER-CONSISTENCY
        # fix added validation that requires a slash.)
        h.save_provider("testprov", {
            "base_url": "https://api.test.com/v1",
            "api_key": "sk-testkey",
            "default_model": "openai/test-model",
            "supports_tools": True,
            "supports_streaming": True,
            "max_tokens": 128_000,
        })

        # Provider should be in providers.yaml
        yaml_providers = load_providers()
        names = [p.name for p in yaml_providers]
        assert "testprov" in names

        # Provider should NOT be in agent.json
        agent_json = os.path.join(utils.config.get_config_dir(), "agent.json")
        if os.path.exists(agent_json):
            import json
            with open(agent_json) as f:
                raw = json.load(f)
            assert "providers" not in raw or "testprov" not in raw.get("providers", {})


class TestSaveProviderPrefixConsistency:
    """Sonnet-5 regression for agent_builder.save_provider: this path
    previously bypassed validation entirely (it called add_provider
    directly, not add_or_update). Now it must mirror the prefix-correction
    logic so the bypass cannot reintroduce a wrong caller.
    """

    def test_sonnet5_regression_caller_corrected(self, handler, tmp_config_dir):
        """The exact Sonnet 5 case: base_url=openrouter.ai, model=
        openrouter/claude-sonnet-5, caller=anthropic. After save,
        caller must be openrouter.
        """
        h, _, _ = handler
        h.save_provider("Sonnet 5", {
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-v1-fake",
            "default_model": "openrouter/claude-sonnet-5",
            "caller": "anthropic",        # the wrong-but-valid value
        })
        from utils.providers_store import load_providers
        providers = load_providers()
        assert any(p.name == "Sonnet 5" and p.caller == "openrouter" for p in providers)

    def test_auto_detects_caller_from_model_prefix(self, handler, tmp_config_dir):
        """When caller is empty and model has a slash, auto-detect.
        Mirrors settings_handler.add_or_update.
        """
        h, _, _ = handler
        h.save_provider("autoprovider", {
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "k",
            "default_model": "openrouter/some-model",
            "caller": "",
        })
        from utils.providers_store import load_providers
        providers = [p for p in load_providers() if p.name == "autoprovider"]
        assert providers[0].caller == "openrouter"

    def test_no_correction_when_caller_matches_prefix(self, handler, tmp_config_dir):
        """Idempotent on matching caller+prefix."""
        h, _, _ = handler
        h.save_provider("p-matched", {
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "k",
            "default_model": "openrouter/foo",
            "caller": "openrouter",
        })
        from utils.providers_store import load_providers
        providers = [p for p in load_providers() if p.name == "p-matched"]
        assert providers[0].caller == "openrouter"

    def test_no_correction_when_model_has_no_slash(self, handler, tmp_config_dir):
        """Anthropic native: model='claude-sonnet-4-5' (no slash).
        Caller stays as user set.
        """
        h, _, _ = handler
        h.save_provider("p-anthro", {
            "base_url": "https://api.anthropic.com",
            "api_key": "k",
            "default_model": "claude-sonnet-4-5",
            "caller": "anthropic",
        })
        from utils.providers_store import load_providers
        providers = [p for p in load_providers() if p.name == "p-anthro"]
        assert providers[0].caller == "anthropic"

    def test_no_correction_when_prefix_not_in_taxonomy(self, handler, tmp_config_dir):
        """Custom prefix not in the valid-caller set — block must not fire."""
        h, _, _ = handler
        h.save_provider("p-custom", {
            "base_url": "https://mycompany.example.com",
            "api_key": "k",
            "default_model": "mycompany/foo",
            "caller": "openai",
        })
        from utils.providers_store import load_providers
        providers = [p for p in load_providers() if p.name == "p-custom"]
        assert providers[0].caller == "openai"

    def test_raises_on_empty_caller_no_slash_model(self, handler, tmp_config_dir):
        """BUG #1: when caller is empty and default_model has no slash,
        save_provider must raise ValueError — same contract as
        settings_handler.add_or_update. Silently saving caller=""
        reintroduces the 'No streaming caller' runtime failure.
        """
        h, _, _ = handler
        with pytest.raises(ValueError, match="Cannot auto-detect caller"):
            h.save_provider("broken", {
                "base_url": "https://api.openai.com",
                "api_key": "sk-test",
                "default_model": "gpt-4o",   # no slash
                "caller": "",                # empty
            })

    def test_normalizes_none_caller_to_empty(self, handler, tmp_config_dir):
        """BUG #3: dict.get('caller', '') returns None for YAML null
        values (default only fires on MISSING keys). save_provider
        must treat None as empty so the empty-caller guard fires
        rather than leaking caller=None into ProviderConfig.
        """
        h, _, _ = handler
        # caller=None + no slash on model → must raise (same path as
        # empty string), not silently accept None.
        with pytest.raises(ValueError, match="Cannot auto-detect caller"):
            h.save_provider("nullcaller", {
                "base_url": "https://api.openai.com",
                "api_key": "sk-test",
                "default_model": "gpt-4o",
                "caller": None,
            })

    def test_raises_on_invalid_caller_value(self, handler, tmp_config_dir):
        """Mirror of settings_handler.add_or_update: a hand-set caller
        that is NOT in the valid taxonomy must raise ValueError. The
        correction block (which fires on prefix-in-taxonomy mismatches)
        does not pre-empt this — use a non-taxonomy prefix so the
        correction branch skips, then the taxonomy guard fires.
        """
        h, _, _ = handler
        with pytest.raises(ValueError, match="Invalid caller 'foo'"):
            h.save_provider("bad-caller", {
                "base_url": "https://x",
                "api_key": "k",
                "default_model": "mycompany/foo",  # not in taxonomy
                "caller": "foo",                    # not in taxonomy
            })


class TestProviderTaxonomyDuplicationInvariant:
    """The agent_builder_handler must use the LIVE get_valid_callers()
    taxonomy, not a hardcoded set literal that can drift out of sync
    when a new adapter is added to agent.runtime._PROVIDER_CALLERS.

    The existing TestValidCallersDuplicationInvariant only guards
    utils/providers_store._VALID_CALLERS. This class guards the
    behavioral contract: a brand-new caller in the runtime taxonomy
    must be honored by the agent_builder correction gate. We
    simulate the drift by monkeypatching get_valid_callers and
    asserting save_provider's behavior reflects the new set.
    """

    def test_correction_uses_live_taxonomy_not_hardcoded_set(
        self, handler, tmp_config_dir, monkeypatch
    ):
        """Add a fake caller to the live taxonomy and confirm that
        agent_builder.save_provider corrects caller to the new
        taxonomy prefix. With a hardcoded set literal, the new
        caller would NOT be in the gate and the wrong caller
        would persist.
        """
        from agent import runtime

        h, _, _ = handler

        # Simulate a new adapter added to _PROVIDER_CALLERS
        fake_callers = runtime.get_valid_callers() | {"groq"}
        monkeypatch.setattr(
            runtime, "get_valid_callers", lambda: fake_callers
        )

        h.save_provider("groq-prov", {
            "base_url": "https://api.groq.com",
            "api_key": "k",
            "default_model": "groq/llama-4",
            "caller": "anthropic",   # wrong but valid
        })
        from utils.providers_store import load_providers
        providers = [p for p in load_providers() if p.name == "groq-prov"]
        # With the live taxonomy, 'groq' is in the gate → correction
        # fires → caller becomes 'groq'. With a hardcoded set literal,
        # 'groq' would NOT be in the gate → correction skipped →
        # caller stays 'anthropic'.
        assert providers[0].caller == "groq", (
            "agent_builder_handler is using a hardcoded set literal "
            "instead of get_valid_callers(); the new caller was not "
            "honored by the correction gate."
        )
