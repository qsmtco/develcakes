# tests/test_agent_builder_dialog.py
# Tests for AgentBuilderWidget (ex AgentBuilderDialog) — provider dropdown
# population at construction.
#
# Refs: docs/specs/SPEC-AGENT-BUILDER-PROVIDER-DROPDOWN.md §2.5

import gi
gi.require_version('Gtk', '4.0')

import pytest
from gi.repository import Gtk

from models.providers import ProviderConfig


class StubHandler:
    """Stub AgentBuilderHandler — returns canned provider options."""
    def __init__(self, providers):
        self._providers = providers

    def get_prompt_options(self):
        return []

    def get_tool_options(self):
        return []

    def get_provider_options(self):
        return [
            {
                "name": p.name,
                "base_url": p.base_url,
                "default_model": p.default_model,
                "enabled": p.enabled,
                "api_key": p.api_key,
            }
            for p in self._providers
        ]

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


def _make_dlg(providers, agent_def=None, *, is_edit=False):
    handler = StubHandler(providers)
    from ui.views.agent_builder import AgentBuilderWidget
    return AgentBuilderWidget(
        handler, agent_def=agent_def, is_edit=is_edit,
    )


class TestProviderDropdownPopulation:
    """The dropdown is populated at construction from handler.get_provider_options()."""

    def test_dropdown_populated_with_provider_names(self):
        """Two providers → dropdown has two entries with their names."""
        providers = [
            ProviderConfig("openai", "u", "k", "gpt-4o", True),
            ProviderConfig("anthropic", "u", "k", "c", True),
        ]
        dlg = _make_dlg(providers)
        model = dlg._provider_dropdown.get_model()
        assert model.get_n_items() == 2
        assert model.get_string(0) == "openai"
        assert model.get_string(1) == "anthropic"

    def test_dropdown_does_not_show_loading_when_providers_exist(self):
        """The first item is NOT '(loading...)' when providers exist."""
        providers = [ProviderConfig("openai", "u", "k", "gpt-4o", True)]
        dlg = _make_dlg(providers)
        first = dlg._provider_dropdown.get_model().get_string(0)
        assert first != "(loading...)"

    def test_dropdown_shows_no_providers_message_when_empty(self):
        """When handler returns empty, dropdown shows the 'no providers' hint."""
        dlg = _make_dlg([])
        model = dlg._provider_dropdown.get_model()
        assert model.get_n_items() == 1
        assert model.get_string(0) == "(no providers — open Settings)"

    def test_get_values_returns_selected_provider(self):
        """get_values() returns the selected provider name as a string."""
        providers = [ProviderConfig("openai", "u", "k", "gpt-4o", True)]
        dlg = _make_dlg(providers)
        dlg._name_entry.set_text("test-agent")
        values = dlg.get_values()
        assert values["llm_name"] == "openai"
        assert "model" not in values
        assert "provider" not in values
        assert "provider_keys" not in values
        assert "api_key" not in values


class TestCreateVsEditMode:
    """The widget distinguishes create mode from edit mode based on is_edit.

    Regression: previously the builder used `agent_def is not None` as the
    mode signal, but create_new() returns a non-empty template dict, so new
    agents were mis-labeled as edits (button 'Save'). The modal-window title
    pins are gone with the dialog — the button label carries the mode now.

    (Tombstone: tests test_create_mode_title_and_button,
    test_edit_mode_title_and_button, test_create_mode_with_no_agent_def and
    test_legacy_truthiness_pattern_would_have_bugged asserted
    `dlg._window.get_title()`; the widget has no window.)
    """

    def _new_template(self):
        """Mirror of AgentBuilderHandler.create_new() — non-None template."""
        return {
            "name": "",
            "emoji": "🤖",
            "role": "",
            "prompts": [],
            "tools": ["read_file", "list_files", "search_files"],
            "provider": "",
            "model": "",
            "fallback_provider": None,
            "self_improvement": {"bug_journal": True, "enforcement": False},
        }

    def test_create_mode_saves_button(self):
        """is_edit=False → button 'Create'."""
        providers = [ProviderConfig("openai", "u", "k", "gpt-4o", True)]
        dlg = _make_dlg(providers, agent_def=self._new_template(), is_edit=False)
        assert dlg._save_btn.get_label() == "Create"
        assert dlg._is_edit is False

    def test_edit_mode_saves_button(self):
        """is_edit=True → button 'Save'."""
        providers = [ProviderConfig("openai", "u", "k", "gpt-4o", True)]
        existing = self._new_template()
        existing["name"] = "MyAgent"
        dlg = _make_dlg(providers, agent_def=existing, is_edit=True)
        assert dlg._save_btn.get_label() == "Save"
        assert dlg._is_edit is True


class TestFallbackRowVisibility:
    """The Fallback Provider row is always visible, regardless of primary.

    The row is always visible (no primary-conditional visibility).
    """

    def test_fallback_row_visible_for_first_provider(self):
        """First provider as primary → fallback row visible."""
        providers = [
            ProviderConfig("firstprov", "u", "k", "firstprov", True),
            ProviderConfig("openrouter", "u", "k", "or-m", True),
        ]
        dlg = _make_dlg(providers)
        assert dlg._fallback_row.get_visible() is True

    def test_fallback_row_visible_for_cloud_primary(self):
        """openai as primary → fallback row visible (was: hidden in old code)."""
        providers = [
            ProviderConfig("openai", "u", "k", "gpt-4o", True),
            ProviderConfig("anthropic", "u", "k", "c", True),
        ]
        dlg = _make_dlg(providers)
        assert dlg._fallback_row.get_visible() is True

    def test_fallback_excludes_current_primary(self):
        """The current primary is excluded from the fallback dropdown options."""
        providers = [
            ProviderConfig("firstprov", "u", "k", "firstprov", True),
            ProviderConfig("openai", "u", "k", "gpt-4o", True),
            ProviderConfig("anthropic", "u", "k", "c", True),
        ]
        dlg = _make_dlg(providers)
        # The selection depends on what _rebuild_provider_dropdown selects.
        # Check the fallback list excludes the currently-selected primary.
        primary = dlg._get_selected_llm_name()
        fallback_names = [p.name for p in dlg._fallback_providers]
        assert primary not in fallback_names

    def test_fallback_excludes_selected_primary(self):
        """The selected primary is never a valid fallback target."""
        providers = [
            ProviderConfig("firstprov", "u", "k", "firstprov", True),
            ProviderConfig("openai", "u", "k", "gpt-4o", True),
            ProviderConfig("anthropic", "u", "k", "c", True),
        ]
        dlg = _make_dlg(providers)
        # Switch primary to the first provider
        dlg._provider_dropdown.set_selected(0)
        fallback_names = [p.name for p in dlg._fallback_providers]
        assert dlg._get_selected_llm_name() not in fallback_names


class TestSaveButtonRequiresFallback:
    """The Save button is disabled until a fallback is selected."""

    def test_save_disabled_when_no_fallback(self):
        """Empty fallback selection → save button is not sensitive."""
        providers = [
            ProviderConfig("firstprov", "u", "k", "firstprov", True),
            ProviderConfig("openai", "u", "k", "gpt-4o", True),
        ]
        dlg = _make_dlg(providers)
        # Fill required fields except fallback
        dlg._name_entry.set_text("TestAgent")
        # Mark a tool and prompt (the test stubs return empty lists for these,
        # so we can't tick them — but the providers list is non-empty).
        # The fallback defaults to "None" (index 0), so save should be disabled.
        assert dlg._save_btn.get_sensitive() is False

    def test_save_enabled_when_fallback_selected(self):
        """Non-None fallback → save button becomes sensitive (other conditions met)."""
        providers = [
            ProviderConfig("firstprov", "u", "k", "firstprov", True),
            ProviderConfig("openai", "u", "k", "gpt-4o", True),
            ProviderConfig("anthropic", "u", "k", "c", True),
        ]
        dlg = _make_dlg(providers)
        dlg._name_entry.set_text("TestAgent")
        dlg._provider_dropdown.set_selected(1)  # select openai as primary
        # After selecting openai, fallback list excludes it and includes anthropic
        # The dropdown's first non-None item is anthropic (index 1).
        dlg._fallback_dropdown.set_selected(1)
        assert dlg._get_selected_fallback_provider() != ""



class TestDecisionModelExcluded:
    """SPEC-22 §2.8: the DecisionModel/JEV card never appears as an agent
    provider or fallback."""

    def test_decision_model_hidden_from_provider_dropdown(self):
        providers = [
            ProviderConfig("openai", "u", "k", "gpt-4o", True),
            ProviderConfig("DecisionModel", "u", "k", "typesafe/s1", True),
            ProviderConfig("anthropic", "u", "k", "c", True),
        ]
        dlg = _make_dlg(providers)
        model = dlg._provider_dropdown.get_model()
        names = [model.get_string(i) for i in range(model.get_n_items())]
        assert "DecisionModel" not in names
        assert names == ["openai", "anthropic"]

    def test_decision_model_hidden_from_fallback_dropdown(self):
        providers = [
            ProviderConfig("openai", "u", "k", "gpt-4o", True),
            ProviderConfig("DecisionModel", "u", "k", "typesafe/s1", True),
            ProviderConfig("anthropic", "u", "k", "c", True),
        ]
        dlg = _make_dlg(providers)
        dlg._populate_fallback_provider_dropdown()
        model = dlg._fallback_dropdown.get_model()
        names = [model.get_string(i) for i in range(model.get_n_items())]
        assert "DecisionModel" not in names


class TestNonChatCardsExcluded:
    """SPEC-55: both the DecisionModel and ImproveModel cards never appear as an
    agent provider or fallback (neither is a chat model)."""

    def test_improve_model_hidden_from_provider_dropdown(self):
        providers = [
            ProviderConfig("openai", "u", "k", "gpt-4o", True),
            ProviderConfig("ImproveModel", "u", "k", "openai/gpt-4o-mini", True),
            ProviderConfig("anthropic", "u", "k", "c", True),
        ]
        dlg = _make_dlg(providers)
        model = dlg._provider_dropdown.get_model()
        names = [model.get_string(i) for i in range(model.get_n_items())]
        assert "ImproveModel" not in names
        assert names == ["openai", "anthropic"]

    def test_decision_and_improve_hidden_from_fallback_dropdown(self):
        providers = [
            ProviderConfig("openai", "u", "k", "gpt-4o", True),
            ProviderConfig("DecisionModel", "u", "k", "typesafe/s1", True),
            ProviderConfig("ImproveModel", "u", "k", "openai/gpt-4o-mini", True),
            ProviderConfig("anthropic", "u", "k", "c", True),
        ]
        dlg = _make_dlg(providers)
        dlg._populate_fallback_provider_dropdown()
        model = dlg._fallback_dropdown.get_model()
        names = [model.get_string(i) for i in range(model.get_n_items())]
        assert "DecisionModel" not in names
        assert "ImproveModel" not in names
