# tests/test_agent_builder_no_model_dropdown.py
# Tests that the Agent Builder form has no Model dropdown, no Manual entry,
# no API key field, and Save enables without an API key.
#
# Refs: docs/specs/SPEC-AGENT-BUILDER-PROVIDER-DROPDOWN.md §2.5

import gi
gi.require_version('Gtk', '4.0')

import pytest
from gi.repository import Gtk

from models.providers import ProviderConfig


class StubHandler:
    def __init__(self, providers):
        self._providers = providers

    def get_prompt_options(self):
        return [
            {"name": "prompt1", "filepath": "/p1.md"},
        ]

    def get_tool_options(self):
        return [
            {"name": "read_file", "description": "Read a file"},
        ]

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


@pytest.fixture
def gtk_parent():
    app = Gtk.Application()
    win = Gtk.ApplicationWindow(application=app)
    return win


def _make_dlg(parent, providers, *, agent_def=None, is_edit=False):
    handler = StubHandler(providers)
    from ui.views.agent_builder import AgentBuilderDialog
    return AgentBuilderDialog(
        parent=parent, handler=handler, agent_def=agent_def, is_edit=is_edit,
    )


class TestNoModelDropdown:
    """The form has no Model dropdown widget."""

    def test_no_model_dropdown_attribute(self, gtk_parent):
        providers = [ProviderConfig("openai", "u", "k", "gpt-4o", True)]
        dlg = _make_dlg(gtk_parent, providers)
        assert not hasattr(dlg, "_model_dropdown")
        assert not hasattr(dlg, "_model_labeled")
        assert not hasattr(dlg, "_build_model_dropdown")
        assert not hasattr(dlg, "_rebuild_model_dropdown")
        assert not hasattr(dlg, "_get_selected_model")


class TestNoManualEntry:
    """The form has no Manual provider/model entry widgets."""

    def test_no_manual_widgets(self, gtk_parent):
        providers = [ProviderConfig("openai", "u", "k", "gpt-4o", True)]
        dlg = _make_dlg(gtk_parent, providers)
        assert not hasattr(dlg, "_manual_provider_entry")
        assert not hasattr(dlg, "_manual_model_entry")
        assert not hasattr(dlg, "_manual_toggle")
        assert not hasattr(dlg, "_manual_mode")
        assert not hasattr(dlg, "_on_manual_toggled")


class TestNoApiKeyField:
    """The form has no API key entry."""

    def test_no_api_key_entry(self, gtk_parent):
        providers = [ProviderConfig("openai", "u", "k", "gpt-4o", True)]
        dlg = _make_dlg(gtk_parent, providers)
        assert not hasattr(dlg, "_api_key_entry")
        assert not hasattr(dlg, "_provider_keys")


class TestSaveButtonEnablesWithoutApiKey:
    """Save enables when name + prompts + tools + provider + fallback are set (no API key)."""

    def test_save_disabled_when_only_name_set(self, gtk_parent):
        providers = [ProviderConfig("openai", "u", "k", "gpt-4o", True)]
        dlg = _make_dlg(gtk_parent, providers)
        dlg._name_entry.set_text("agent")
        dlg._update_save_button()
        assert dlg._save_btn.get_sensitive() is False

    def test_save_enabled_when_all_required_fields_set(self, gtk_parent):
        """name + prompts + tools + provider + fallback → Save sensitive, no API key entered."""
        providers = [
            ProviderConfig("openai", "u", "k", "gpt-4o", True),
            ProviderConfig("openrouter", "u", "k", "auto", True),
        ]
        dlg = _make_dlg(gtk_parent, providers)
        dlg._name_entry.set_text("agent")
        # Select primary provider (index 0 = openai)
        dlg._provider_dropdown.set_selected(0)
        # Simulate user checking a prompt
        if dlg._prompt_checks:
            first_prompt = list(dlg._prompt_checks.values())[0]
            first_prompt.set_active(True)
        # Simulate user checking a tool
        if dlg._tool_checks:
            first_tool = list(dlg._tool_checks.values())[0]
            first_tool.set_active(True)
        # Selecting primary provider populates the fallback dropdown;
        # pick the second entry (the only fallback candidate since KB is excluded).
        if len(dlg._fallback_providers) >= 1:
            dlg._fallback_dropdown.set_selected(1)
        dlg._update_save_button()
        assert dlg._save_btn.get_sensitive() is True
