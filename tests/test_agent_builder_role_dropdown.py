# tests/test_agent_builder_role_dropdown.py
# SPEC-AGENT-ID-AND-ROLES §2.5/§2.6 — the Role dropdown and Create-role
# dialog in ui/views/agent_builder.py.
#
# Runs under xvfb-run -a (GTK4 widget construction needs a display; the
# conftest collector skips this module when none is available).

import gi
gi.require_version('Gtk', '4.0')

import pytest
from gi.repository import Gtk

from models.providers import ProviderConfig

BUILTIN_OPTIONS = [
    ("Supervisor", "supervisor"),
    ("Coder", "coder"),
    ("Debugger", "debugger"),
]


class StubHandler:
    """AgentBuilderHandler stub with canned role options and save_role.

    Allows the dialog tests to exercise the Role dropdown end-to-end
    without shipping role files to a real config dir.
    """

    def __init__(self, roles=(), save_result=("my-role", None), tool_options=()):
        self._roles = list(roles)
        self._save_result = save_result
        self._tool_options = list(tool_options)
        self.saved = []

    def get_prompt_options(self):
        return []

    def get_tool_options(self):
        return self._tool_options

    def get_provider_options(self):
        return [
            {"name": "openai", "base_url": "u", "default_model": "gpt-4o",
             "enabled": True, "api_key": "k"},
            {"name": "anthropic", "base_url": "u", "default_model": "c",
             "enabled": True, "api_key": "k"},
        ]

    def get_mcp_options(self):
        return []

    def get_role_options(self):
        return BUILTIN_OPTIONS + [(r["name"], r["slug"]) for r in self._roles]

    def save_role(self, name, prompt):
        self.saved.append((name, prompt))
        return self._save_result


@pytest.fixture
def gtk_parent():
    app = Gtk.Application()
    return Gtk.ApplicationWindow(application=app)


def _make_dlg(parent, *, roles=(), agent_def=None, is_edit=False,
              save_result=("my-role", None), tool_options=()):
    handler = StubHandler(roles=roles, save_result=save_result,
                          tool_options=tool_options)
    from ui.views.agent_builder import AgentBuilderDialog
    dlg = AgentBuilderDialog(
        parent=parent, handler=handler, agent_def=agent_def, is_edit=is_edit,
    )
    return dlg, handler


def _builtin_agent_def(role=""):
    return {"name": "", "role": role, "prompts": [], "tools": []}


class TestRoleControlIsDropdown:
    """§2.5 — the Role control is a Gtk.DropDown, not a Gtk.Entry."""

    def test_role_is_dropdown_not_entry(self, gtk_parent):
        dlg, _ = _make_dlg(gtk_parent)
        assert isinstance(dlg._role_dropdown, Gtk.DropDown)
        assert not hasattr(dlg, "_role_entry")

    def test_rows_builtins_then_user_roles_then_sentinel(self, gtk_parent):
        roles = [
            {"name": "Architect", "slug": "architect"},
            {"name": "Reviewer", "slug": "reviewer"},
        ]
        dlg, _ = _make_dlg(gtk_parent, roles=roles)
        assert dlg._role_labels == [
            "Supervisor", "Coder", "Debugger",
            "Architect", "Reviewer", "Create role…",
        ]
        assert dlg._role_values == [
            "supervisor", "coder", "debugger",
            "architect", "reviewer", "",
        ]

    def test_new_agent_defaults_to_coder(self, gtk_parent):
        # agent_def=None (create mode without template)
        dlg, _ = _make_dlg(gtk_parent)
        assert dlg._get_selected_role_value() == "coder"

        # create mode with the handler template (role "")
        dlg, _ = _make_dlg(gtk_parent, agent_def=_builtin_agent_def(""))
        assert dlg._get_selected_role_value() == "coder"


class TestGetValuesWritesValue:
    """§2.5 — get_values() writes the dropdown's value, not its label."""

    def test_returns_value_not_label(self, gtk_parent):
        roles = [{"name": "Reviewer", "slug": "reviewer"}]
        dlg, _ = _make_dlg(gtk_parent, roles=roles)
        reviewer_idx = dlg._role_values.index("reviewer")
        dlg._role_dropdown.set_selected(reviewer_idx)
        dlg._name_entry.set_text("TestAgent")
        assert dlg.get_values()["role"] == "reviewer"

    def test_returns_builtin_value(self, gtk_parent):
        dlg, _ = _make_dlg(gtk_parent)
        dlg._role_dropdown.set_selected(0)  # Supervisor
        assert dlg.get_values()["role"] == "supervisor"


class TestSentinelOpensCreateDialog:
    """§2.5 — choosing 'Create role…' restores the previous selection and
    opens the modal Create-role dialog."""

    def test_sentinel_restores_previous_and_opens_dialog(self, gtk_parent):
        dlg, _ = _make_dlg(gtk_parent)
        dlg._role_dropdown.set_selected(2)  # Debugger
        assert dlg._role_dropdown.get_selected() == 2
        sentinel_idx = len(dlg._role_values) - 1
        dlg._role_dropdown.set_selected(sentinel_idx)
        # Selection snapped back to the previous non-sentinel row.
        assert dlg._role_dropdown.get_selected() == 2
        assert dlg._get_selected_role_value() == "debugger"
        # And the create dialog opened.
        assert dlg._create_role_window is not None
        assert dlg._create_role_window.get_title() == "Create role"
        assert dlg._create_role_window.get_modal() is True

    def test_sentinel_restores_to_coder_when_no_previous(self, gtk_parent):
        dlg, _ = _make_dlg(gtk_parent)
        # No prior non-sentinel change — previous defaults to Coder.
        sentinel_idx = len(dlg._role_values) - 1
        dlg._role_dropdown.set_selected(sentinel_idx)
        assert dlg._get_selected_role_value() == "coder"
        assert dlg._create_role_window is not None


class TestCreateDialogSubmit:
    """§2.5 — Create calls handler.save_role; success refreshes the dropdown
    and closes the dialog, error stays open with the message shown."""

    def test_success_refreshes_dropdown_and_closes(self, gtk_parent):
        role = {"name": "Reviewer", "slug": "reviewer"}
        dlg, handler = _make_dlg(gtk_parent, roles=[role],
                                 save_result=("reviewer", None))
        dlg._role_dropdown.set_selected(len(dlg._role_values) - 1)  # sentinel
        dlg._create_role_name.set_text("Reviewer")
        dlg._create_role_prompt.get_buffer().set_text("Do careful reviews.")
        dlg._on_create_role_submit()
        assert handler.saved == [("Reviewer", "Do careful reviews.")]
        assert dlg._create_role_window is None
        assert dlg._get_selected_role_value() == "reviewer"
        # The new role row is present and sits before the sentinel.
        assert dlg._role_values[-2] == "reviewer"
        assert dlg._role_values[-1] == ""

    def test_error_keeps_dialog_open_and_shows_message(self, gtk_parent):
        dlg, handler = _make_dlg(gtk_parent,
                                 save_result=(None, "Role name cannot be empty."))
        dlg._role_dropdown.set_selected(len(dlg._role_values) - 1)  # sentinel
        assert dlg._create_role_window is not None
        dlg._create_role_name.set_text("")
        dlg._on_create_role_submit()
        assert handler.saved == [("", "")]
        assert dlg._create_role_window is not None  # still open
        assert dlg._create_role_error.get_visible() is True
        assert "cannot be empty" in dlg._create_role_error.get_text()
        # Selection never changed to the sentinel.
        assert dlg._get_selected_role_value() == "coder"


class TestFillFormRole:
    """§2.5 — _fill_form selects the stored role; an absent stored role is
    inserted before the sentinel so edits never silently retarget."""

    def test_selects_stored_role(self, gtk_parent):
        role = {"name": "Reviewer", "slug": "reviewer"}
        dlg, _ = _make_dlg(gtk_parent, roles=[role],
                           agent_def={"name": "A", "role": "reviewer"},
                           is_edit=True)
        assert dlg._get_selected_role_value() == "reviewer"

    def test_inserts_unknown_role_before_sentinel(self, gtk_parent):
        dlg, _ = _make_dlg(gtk_parent,
                           agent_def={"name": "A", "role": "vanished"},
                           is_edit=True)
        assert dlg._get_selected_role_value() == "vanished"
        assert dlg._role_values[-2] == "vanished"
        assert dlg._role_values[-1] == ""


class TestSaveButtonRoleCountsAsPrompt:
    """§2.5 — a non-sentinel role satisfies has_prompts even with an empty
    System Prompts checklist."""

    def test_role_alone_satisfies_has_prompts(self, gtk_parent):
        tool_opts = [{"name": "read_file", "description": "Read a file"}]
        dlg, _ = _make_dlg(gtk_parent, tool_options=tool_opts)
        # Selected role defaults to coder (non-sentinel).
        assert dlg._get_selected_role_value() == "coder"
        assert dlg._prompt_checks == {}  # no prompt checkboxes available
        dlg._name_entry.set_text("Agent")
        first_tool = list(dlg._tool_checks.values())[0]
        first_tool.set_active(True)
        dlg._provider_dropdown.set_selected(1)
        dlg._fallback_dropdown.set_selected(1)
        dlg._update_save_button()
        assert dlg._save_btn.get_sensitive() is True
