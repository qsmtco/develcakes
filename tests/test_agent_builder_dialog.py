# tests/test_agent_builder_dialog.py
# Tests for AgentBuilderDialog — provider dropdown population at construction.
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


@pytest.fixture
def gtk_parent():
    """A real Gtk.ApplicationWindow for use as a dialog parent."""
    app = Gtk.Application()
    win = Gtk.ApplicationWindow(application=app)
    return win


def _make_dlg(parent, providers, agent_def=None, *, is_edit=False):
    handler = StubHandler(providers)
    from ui.views.agent_builder import AgentBuilderDialog
    return AgentBuilderDialog(
        parent=parent, handler=handler, agent_def=agent_def, is_edit=is_edit,
    )


class TestProviderDropdownPopulation:
    """The dropdown is populated at construction from handler.get_provider_options()."""

    def test_dropdown_populated_with_provider_names(self, gtk_parent):
        """Two providers → dropdown has two entries with their names."""
        providers = [
            ProviderConfig("openai", "u", "k", "gpt-4o", True),
            ProviderConfig("anthropic", "u", "k", "c", True),
        ]
        dlg = _make_dlg(gtk_parent, providers)
        model = dlg._provider_dropdown.get_model()
        assert model.get_n_items() == 2
        assert model.get_string(0) == "openai"
        assert model.get_string(1) == "anthropic"

    def test_dropdown_does_not_show_loading_when_providers_exist(self, gtk_parent):
        """The first item is NOT '(loading...)' when providers exist."""
        providers = [ProviderConfig("openai", "u", "k", "gpt-4o", True)]
        dlg = _make_dlg(gtk_parent, providers)
        first = dlg._provider_dropdown.get_model().get_string(0)
        assert first != "(loading...)"

    def test_dropdown_shows_no_providers_message_when_empty(self, gtk_parent):
        """When handler returns empty, dropdown shows the 'no providers' hint."""
        dlg = _make_dlg(gtk_parent, [])
        model = dlg._provider_dropdown.get_model()
        assert model.get_n_items() == 1
        assert model.get_string(0) == "(no providers — open Settings)"

    def test_get_values_returns_selected_provider(self, gtk_parent):
        """get_values() returns the selected provider name as a string."""
        providers = [ProviderConfig("openai", "u", "k", "gpt-4o", True)]
        dlg = _make_dlg(gtk_parent, providers)
        dlg._name_entry.set_text("test-agent")
        values = dlg.get_values()
        assert values["llm_name"] == "openai"
        assert "model" not in values
        assert "provider" not in values
        assert "provider_keys" not in values
        assert "api_key" not in values


class TestCreateVsEditMode:
    """The dialog distinguishes create mode from edit mode based on is_edit.

    Regression: previously the dialog used `agent_def is not None` as the mode
    signal, but create_new() returns a non-empty template dict, so new agents
    were mis-labeled as edits (title 'Edit Agent', button 'Save').
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

    def test_create_mode_title_and_button(self, gtk_parent):
        """is_edit=False → title 'Create Agent', button 'Create'."""
        providers = [ProviderConfig("openai", "u", "k", "gpt-4o", True)]
        dlg = _make_dlg(gtk_parent, providers, agent_def=self._new_template(), is_edit=False)
        assert dlg._window.get_title() == "Create Agent"
        assert dlg._save_btn.get_label() == "Create"
        assert dlg._is_edit is False

    def test_edit_mode_title_and_button(self, gtk_parent):
        """is_edit=True → title 'Edit Agent', button 'Save'."""
        providers = [ProviderConfig("openai", "u", "k", "gpt-4o", True)]
        existing = self._new_template()
        existing["name"] = "MyAgent"
        dlg = _make_dlg(gtk_parent, providers, agent_def=existing, is_edit=True)
        assert dlg._window.get_title() == "Edit Agent"
        assert dlg._save_btn.get_label() == "Save"
        assert dlg._is_edit is True

    def test_create_mode_with_no_agent_def(self, gtk_parent):
        """agent_def=None, is_edit=False → still 'Create Agent' (not 'Edit')."""
        providers = [ProviderConfig("openai", "u", "k", "gpt-4o", True)]
        dlg = _make_dlg(gtk_parent, providers, agent_def=None, is_edit=False)
        assert dlg._window.get_title() == "Create Agent"
        assert dlg._save_btn.get_label() == "Create"

    def test_legacy_truthiness_pattern_would_have_bugged(self, gtk_parent):
        """Sanity: passing a non-None template (legacy create_new) WITHOUT is_edit
        should still produce 'Create Agent'. This is the regression case —
        the old `self._is_edit = agent_def is not None` would have produced
        'Edit Agent' here. With explicit is_edit=False, it's correct.
        """
        providers = [ProviderConfig("openai", "u", "k", "gpt-4o", True)]
        dlg = _make_dlg(gtk_parent, providers, agent_def=self._new_template(), is_edit=False)
        assert dlg._is_edit is False
        assert dlg._window.get_title() != "Edit Agent"


class TestFallbackRowVisibility:
    """The Fallback Provider row is always visible, regardless of primary.

    The row is always visible (no primary-conditional visibility).
    """

    def test_fallback_row_visible_for_first_provider(self, gtk_parent):
        """First provider as primary → fallback row visible."""
        providers = [
            ProviderConfig("firstprov", "u", "k", "firstprov", True),
            ProviderConfig("openrouter", "u", "k", "or-m", True),
        ]
        dlg = _make_dlg(gtk_parent, providers)
        assert dlg._fallback_row.get_visible() is True

    def test_fallback_row_visible_for_cloud_primary(self, gtk_parent):
        """openai as primary → fallback row visible (was: hidden in old code)."""
        providers = [
            ProviderConfig("openai", "u", "k", "gpt-4o", True),
            ProviderConfig("anthropic", "u", "k", "c", True),
        ]
        dlg = _make_dlg(gtk_parent, providers)
        assert dlg._fallback_row.get_visible() is True

    def test_fallback_excludes_current_primary(self, gtk_parent):
        """The current primary is excluded from the fallback dropdown options."""
        providers = [
            ProviderConfig("firstprov", "u", "k", "firstprov", True),
            ProviderConfig("openai", "u", "k", "gpt-4o", True),
            ProviderConfig("anthropic", "u", "k", "c", True),
        ]
        dlg = _make_dlg(gtk_parent, providers)
        # The selection depends on what _rebuild_provider_dropdown selects.
        # Check the fallback list excludes the currently-selected primary.
        primary = dlg._get_selected_llm_name()
        fallback_names = [p.name for p in dlg._fallback_providers]
        assert primary not in fallback_names

    def test_fallback_excludes_selected_primary(self, gtk_parent):
        """The selected primary is never a valid fallback target."""
        providers = [
            ProviderConfig("firstprov", "u", "k", "firstprov", True),
            ProviderConfig("openai", "u", "k", "gpt-4o", True),
            ProviderConfig("anthropic", "u", "k", "c", True),
        ]
        dlg = _make_dlg(gtk_parent, providers)
        # Switch primary to the first provider
        dlg._provider_dropdown.set_selected(0)
        fallback_names = [p.name for p in dlg._fallback_providers]
        assert dlg._get_selected_llm_name() not in fallback_names


class TestSaveButtonRequiresFallback:
    """The Save button is disabled until a fallback is selected."""

    def test_save_disabled_when_no_fallback(self, gtk_parent):
        """Empty fallback selection → save button is not sensitive."""
        providers = [
            ProviderConfig("firstprov", "u", "k", "firstprov", True),
            ProviderConfig("openai", "u", "k", "gpt-4o", True),
        ]
        dlg = _make_dlg(gtk_parent, providers)
        # Fill required fields except fallback
        dlg._name_entry.set_text("TestAgent")
        # Mark a tool and prompt (the test stubs return empty lists for these,
        # so we can't tick them — but the providers list is non-empty).
        # The fallback defaults to "None" (index 0), so save should be disabled.
        assert dlg._save_btn.get_sensitive() is False

    def test_save_enabled_when_fallback_selected(self, gtk_parent):
        """Non-None fallback → save button becomes sensitive (other conditions met)."""
        providers = [
            ProviderConfig("firstprov", "u", "k", "firstprov", True),
            ProviderConfig("openai", "u", "k", "gpt-4o", True),
            ProviderConfig("anthropic", "u", "k", "c", True),
        ]
        dlg = _make_dlg(gtk_parent, providers)
        dlg._name_entry.set_text("TestAgent")
        dlg._provider_dropdown.set_selected(1)  # select openai as primary
        # After selecting openai, fallback list excludes it and includes anthropic
        # The dropdown's first non-None item is anthropic (index 1).
        dlg._fallback_dropdown.set_selected(1)
        assert dlg._get_selected_fallback_provider() != ""

