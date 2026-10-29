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

    def test_missing_provider_still_errors(self):
        agent_def = self._valid_def(provider="")
        errors = validate_agent_def(agent_def)
        assert any("llm_name" in e.lower() for e in errors)

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
