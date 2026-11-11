# tests/test_agent_builder_no_provider_keys.py
# Tests that the agent builder no longer requires api_key/provider_keys
# and that set_provider_options() works correctly.
#
# Phase C complete — all xfail markers removed.

import pytest

try:
    from gi.repository import Gtk
    GTK_AVAILABLE = True
except (ImportError, ValueError):
    GTK_AVAILABLE = False

from utils.agent_defs import validate_agent_def


pytestmark = pytest.mark.skipif(not GTK_AVAILABLE, reason="GTK not available")


class StubHandler:
    """Stub AgentBuilderHandler for testing the dialog without real I/O."""
    def get_prompt_options(self):
        return []
    def get_tool_options(self):
        return []
    def get_provider_options(self):
        return []
    def get_mcp_options(self):
        return []
    def get_role_options(self):
        return [
            ("Supervisor", "supervisor"),
            ("Coder", "coder"),
            ("Debugger", "debugger"),
        ]
    def save_role(self, name, prompt):
        return None, "stub save_role not implemented"


def _make_dlg(agent_def=None):
    from ui.views.agent_builder import AgentBuilderWidget
    return AgentBuilderWidget(StubHandler(), agent_def=agent_def)


class TestValidateAgentDef:
    """validate_agent_def should NOT require api_key or provider_keys (Phase 8)."""

    def _valid_def(self, **overrides) -> dict:
        base = {
            "name": "test-agent",
            "provider": "openai",
            "model": "gpt-4o",
            "prompts": ["default"],
            "tools": ["read"],
        }
        base.update(overrides)
        return base

    def test_no_api_key_is_ok(self):
        agent_def = self._valid_def()
        errors = validate_agent_def(agent_def)
        assert not any("api_key" in e.lower() for e in errors), \
            f"Unexpected api_key error: {errors}"

    def test_no_provider_keys_is_ok(self):
        agent_def = self._valid_def()
        errors = validate_agent_def(agent_def)
        assert not any("provider_keys" in e.lower() for e in errors), \
            f"Unexpected provider_keys error: {errors}"

    def test_with_api_key_still_validates(self):
        agent_def = self._valid_def(api_key="sk-test")
        errors = validate_agent_def(agent_def)
        assert not any("api_key" in e.lower() and "required" in e.lower() for e in errors)

    def test_missing_provider_still_errors(self, tmp_path, monkeypatch):
        """SPEC-62: llm_name is optional, but a NON-EMPTY unknown name still
        errors — and that branch only fires when at least one provider card
        is configured (utils/agent_defs.py display_names guard), so ONE card
        is seeded first. Both falsy spellings (absent key AND llm_name: "")
        yield no llm_name error."""
        # Config-dir isolation (conftest autouse does the same for HOME) +
        # one card, so `Unknown provider` is a real, non-vacuous assertion.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
        from models.providers import ProviderConfig
        from utils.providers_store import save_providers
        save_providers([
            ProviderConfig(name="openrouter", base_url="https://openrouter.ai/api/v1",
                           api_key="sk-test", default_model="deepseek/deepseek-v4-pro"),
        ])

        # Non-empty unknown llm_name → Unknown provider error.
        agent = self._valid_def(provider="not-a-provider", llm_name="not-a-provider")
        errors = validate_agent_def(agent)
        assert any("Unknown provider" in e for e in errors), (
            f"unknown non-empty llm_name must still error — got: {errors}"
        )

        # Falsy spelling 1: llm_name key ABSENT → no llm_name error.
        absent = self._valid_def(provider="")
        assert "llm_name" not in absent
        errors = validate_agent_def(absent)
        assert not any("llm_name" in e.lower() for e in errors), (
            f"absent llm_name must be optional — got: {errors}"
        )

        # Falsy spelling 2: llm_name == "" → also no llm_name error.
        empty = self._valid_def(provider="", llm_name="")
        errors = validate_agent_def(empty)
        assert not any("llm_name" in e.lower() for e in errors), (
            f'empty-string llm_name must be optional — got: {errors}'
        )

    def test_missing_name_still_errors(self):
        agent_def = self._valid_def(name="")
        errors = validate_agent_def(agent_def)
        assert any("name" in e.lower() for e in errors)


class TestAgentBuilderGetValuesPhaseC:
    """Phase C complete: verify the API key field and provider_keys are gone."""

    def test_get_values_does_not_include_provider_keys(self):
        dlg = _make_dlg()
        values = dlg.get_values()
        assert "provider_keys" not in values, \
            f"Expected provider_keys removed, but got: {list(values.keys())}"

    def test_api_key_field_removed(self):
        dlg = _make_dlg()
        assert not hasattr(dlg, "_api_key_entry"), \
            "Expected _api_key_entry removed from form"


class TestSetProviderOptions:
    """Tests for the new set_provider_options() method (Phase C)."""

    def test_set_provider_options_populates_providers(self):
        from models.providers import ProviderConfig
        providers = [
            ProviderConfig(name="p1", base_url="https://x", api_key="k", default_model="m1"),
            ProviderConfig(name="p2", base_url="https://y", api_key="k", default_model="m2"),
        ]
        dlg = _make_dlg()
        dlg.set_provider_options(providers)
        assert len(dlg._providers) == 2
        assert dlg._providers[0].name == "p1"

    def test_set_provider_options_handles_empty(self):
        dlg = _make_dlg()
        dlg.set_provider_options([])
        assert dlg._providers == []
        model = dlg._provider_dropdown.get_model()
        assert model.get_n_items() == 1  # "(no providers — open Settings)"

    def test_set_provider_options_rebuilds_dropdown(self):
        from models.providers import ProviderConfig
        dlg = _make_dlg()
        dlg.set_provider_options([
            ProviderConfig(name="alpha", base_url="x", api_key="k", default_model="a-m"),
            ProviderConfig(name="beta", base_url="y", api_key="k", default_model="b-m"),
        ])
        model = dlg._provider_dropdown.get_model()
        assert model.get_n_items() == 2
        assert model.get_string(0) == "alpha"
        assert model.get_string(1) == "beta"

    def test_set_provider_options_does_not_build_model_map(self):
        # Per SPEC-AGENT-FALLBACK-MODEL-DROPDOWN-REMOVAL.md (2026-06-15):
        # _provider_models was removed because the fallback model dropdown
        # that consumed it is gone. The primary provider dropdown selects
        # a provider name; the model is resolved at runtime from the card's
        # default_model. No internal model map is needed.
        from models.providers import ProviderConfig
        dlg = _make_dlg()
        dlg.set_provider_options([
            ProviderConfig(name="p1", base_url="x", api_key="k", default_model="model-a"),
        ])
        assert not hasattr(dlg, "_provider_models")

    def test_set_provider_options_replaces_previous(self):
        from models.providers import ProviderConfig
        dlg = _make_dlg()
        dlg.set_provider_options([
            ProviderConfig(name="old", base_url="x", api_key="k", default_model="m"),
        ])
        dlg.set_provider_options([
            ProviderConfig(name="new", base_url="y", api_key="k", default_model="m2"),
        ])
        assert len(dlg._providers) == 1
        assert dlg._providers[0].name == "new"
