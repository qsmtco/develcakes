# tests/test_window_agent_selected.py — SPEC-12 SP5 (R3).
#
# Window-shell unit pins for `_on_agent_selected`: a member click opens the
# ACTIVE project's group tab; non-member / no-project / pre-build clicks are
# no-ops. Uses the established MainWindow.__new__ shell pattern
# (test_window_settings_bar.py's `win` fixture) — no GTK widget build.

from unittest.mock import MagicMock

import ui.window as wm


def _shell():
    """A MainWindow shell with only the attributes _on_agent_selected reads."""
    w = wm.MainWindow.__new__(wm.MainWindow)
    w._main_content = MagicMock()
    return w


def test_member_click_opens_project_tab():
    """R3: a project member's Chat click opens the ACTIVE project's group
    tab (project:<name>, not a private agent tab)."""
    w = _shell()
    ph = MagicMock()
    ph.get_active_project_name.return_value = "alpha"
    ph.get_project_members.return_value = ["agent:q1", "special:coder"]
    w._project_handler = ph

    w._on_agent_selected("special:coder", "Coder")

    w._main_content.create_chat_tab.assert_called_once_with(
        "project:alpha", "alpha")


def test_non_member_click_is_noop():
    """R3: a non-member click is a no-op (the '+' toggle is the add path)."""
    w = _shell()
    ph = MagicMock()
    ph.get_active_project_name.return_value = "alpha"
    ph.get_project_members.return_value = ["agent:q1"]
    w._project_handler = ph

    w._on_agent_selected("special:coder", "Coder")

    w._main_content.create_chat_tab.assert_not_called()


def test_no_active_project_click_is_noop():
    """R3: no active project → nothing to open → no-op."""
    w = _shell()
    ph = MagicMock()
    ph.get_active_project_name.return_value = None
    w._project_handler = ph

    w._on_agent_selected("special:coder", "Coder")

    w._main_content.create_chat_tab.assert_not_called()


def test_pre_build_click_is_noop_not_crash():
    """getattr guard: `_project_handler` is assigned mid-_build — a click
    before that (or on a partially-built window) must not raise."""
    w = _shell()  # no _project_handler attribute at all

    w._on_agent_selected("special:coder", "Coder")  # must not raise

    w._main_content.create_chat_tab.assert_not_called()
