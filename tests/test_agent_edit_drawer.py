# tests/test_agent_edit_drawer.py
# The agent-edit drawer (ui/window.py `_build_agent_edit_drawer` +
# `_open_agent_builder` / `_close_agent_edit_drawer`) and the Delete-only
# right-click menu (ui/views/left_panel.py `_show_local_agent_menu`).
#
# Window-level behavior is exercised on the established
# MainWindow.__new__ shell pattern (test_window_settings_bar.py's `win`
# fixture): real widgets for the drawer path, MagicMock for everything
# the shell does not own. LeftPanel menu behavior constructs real
# widgets — runs under xvfb-run -a.
#
# Runs under xvfb-run -a (GTK4 widget construction needs a display; the
# conftest collector skips this module when none is available).

import inspect
from unittest.mock import MagicMock

import gi

gi.require_version('Gtk', '4.0')

from gi.repository import Gtk

import ui.views.left_panel as lp
import ui.window as wm
from ui.views.left_panel import LeftPanel

# ── Shared stubs ──────────────────────────────────────────────────────────

class StubBuilderHandler:
    """AgentBuilderHandler stub — canned load/create/save results."""

    def __init__(self, defs=None, save_result=(True, [])):
        self._defs = defs or {}
        self._save_result = save_result
        self.saved = []

    def load_for_edit(self, name):
        return self._defs.get(name)

    def create_new(self):
        return {
            "name": "",
            "emoji": "🤖",
            "role": "",
            "prompts": [],
            "tools": [],
            "provider": "",
            "model": "",
            "fallback_provider": None,
            "self_improvement": {},
        }

    def save(self, agent_def):
        self.saved.append(agent_def)
        return self._save_result

    def get_prompt_options(self):
        return []

    def get_tool_options(self):
        return []

    def get_provider_options(self):
        return [
            {"name": "openai", "base_url": "u", "default_model": "gpt-4o",
             "enabled": True, "api_key": "k"},
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


def _win_with_drawer(handler=None):
    """A MainWindow shell with the drawer built against real widgets."""
    w = wm.MainWindow.__new__(wm.MainWindow)
    w._main_content = MagicMock(name="main_content")
    w._agent_builder_handler = handler or StubBuilderHandler()
    w._build_agent_edit_drawer()
    return w


class _AgentDef(dict):
    """Minimal agent_def with the defaults the widget's _fill_form reads."""


def _def(name, role="coder"):
    return {"name": name, "role": role, "tools": [], "llm_name": "openai"}


# ── Drawer lifecycle ──────────────────────────────────────────────────────

class TestDrawerLifecycle:
    def test_drawer_starts_hidden(self):
        w = _win_with_drawer()
        assert w._agent_edit_drawer.get_reveal_child() is False
        assert w._agent_edit_drawer.get_visible() is False

    def test_drawer_is_a_revealer_sliding_down(self):
        w = _win_with_drawer()
        assert isinstance(w._agent_edit_drawer, Gtk.Revealer)
        assert (w._agent_edit_drawer.get_transition_type()
                == Gtk.RevealerTransitionType.SLIDE_DOWN)

    def test_parked_once_in_main_content_top_box(self):
        """add_drawer wires the revealer as a permanent row exactly once."""
        w = _win_with_drawer()
        w._main_content.add_drawer.assert_called_once_with(
            w._agent_edit_drawer)

        # Idempotent: a second call must not re-add. MainContent-shape fake
        # (add_drawer is a MainContent method, not Gtk.Box's — the earlier
        # bare-Box version of this test called a nonexistent method).
        class _FakeMC:
            _agent_edit_drawer = None
            def __init__(self):
                self._top_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                self.notebook = Gtk.Notebook()
                self._top_box.append(self.notebook)
            def add_drawer(self, drawer):
                if self._agent_edit_drawer is not None:
                    return
                self._agent_edit_drawer = drawer
                self._top_box.insert_child_after(drawer, None)
        mc = _FakeMC()
        drawer = Gtk.Revealer()
        mc.add_drawer(drawer)
        mc.add_drawer(Gtk.Revealer())  # no-op — already wired
        assert mc._agent_edit_drawer is drawer
        assert mc._top_box.get_first_child() is drawer
        assert drawer.get_next_sibling() is mc.notebook

    def test_drawer_body_shape_header_form_buttons(self):
        """Child = header row (title + ✕) + stable content box holding the
        form widget (whose own box carries the Cancel/Save row)."""
        w = _win_with_drawer()
        body = w._agent_edit_drawer.get_child()
        assert isinstance(body, Gtk.Box)
        header = body.get_first_child()
        assert isinstance(header, Gtk.Box)
        content = header.get_next_sibling()
        assert content is w._agent_edit_drawer_content
        form = content.get_first_child()
        assert form is w._agent_edit_drawer_widget
        # Cancel/Save row lives inside the widget's own box now (nested).
        def _all_buttons(wgt, acc):
            for child in wgt:
                if isinstance(child, Gtk.Button):
                    acc.append(child.get_label())
                elif isinstance(child, Gtk.Box):
                    _all_buttons(child, acc)
            return acc
        labels = _all_buttons(form, [])
        assert "Cancel" in labels
        assert "Save" in labels or "Create" in labels

    def test_header_has_close_button(self):
        w = _win_with_drawer()
        header = w._agent_edit_drawer.get_child().get_first_child()
        btns = [c for c in header if isinstance(c, Gtk.Button)]
        assert [b.get_label() for b in btns] == ["✕"]


class TestDrawerOpen:
    def test_edit_opens_drawer_revealed_with_title(self):
        w = _win_with_drawer(StubBuilderHandler(defs={"Coder": _def("Coder")}))
        w._open_agent_builder("Coder")
        assert w._agent_edit_drawer.get_reveal_child() is True
        assert w._agent_edit_drawer.get_visible() is True
        assert w._agent_edit_drawer_title.get_text() == "Edit: Coder"
        assert w._agent_edit_drawer_widget._is_edit is True
        assert w._agent_edit_drawer_widget._name_entry.get_text() == "Coder"
        assert w._agent_edit_drawer_widget._save_btn.get_label() == "Save"

    def test_create_opens_drawer_in_create_mode(self):
        w = _win_with_drawer()
        w._open_agent_builder(None)
        assert w._agent_edit_drawer.get_reveal_child() is True
        assert w._agent_edit_drawer.get_visible() is True
        assert w._agent_edit_drawer_title.get_text() == "New agent"
        assert w._agent_edit_drawer_widget._is_edit is False
        assert w._agent_edit_drawer_widget._save_btn.get_label() == "Create"

    def test_missing_agent_does_not_open(self):
        w = _win_with_drawer()
        w._open_agent_builder("ghost")
        assert w._agent_edit_drawer.get_reveal_child() is False


class TestDrawerClose:
    def test_close_hides_drawer(self):
        w = _win_with_drawer()
        w._open_agent_builder(None)
        w._close_agent_edit_drawer()
        assert w._agent_edit_drawer.get_reveal_child() is False

    def test_cancel_hides_drawer(self):
        w = _win_with_drawer()
        w._open_agent_builder(None)
        w._agent_edit_drawer_widget._do_cancel()
        assert w._agent_edit_drawer.get_reveal_child() is False

    def test_same_agent_while_open_closes(self):
        w = _win_with_drawer(StubBuilderHandler(defs={"Coder": _def("Coder")}))
        w._open_agent_builder("Coder")
        assert w._agent_edit_drawer.get_reveal_child() is True
        w._open_agent_builder("Coder")  # double-click again → close
        assert w._agent_edit_drawer.get_reveal_child() is False


class TestDrawerRepopulate:
    def test_different_agent_while_open_repopulates_and_stays_open(self):
        w = _win_with_drawer(StubBuilderHandler(defs={
            "Coder": _def("Coder"), "Qat": _def("Qat", role="debugger")}))
        w._open_agent_builder("Coder")
        first_widget = w._agent_edit_drawer_widget
        w._open_agent_builder("Qat")
        assert w._agent_edit_drawer.get_reveal_child() is True  # stayed open
        assert w._agent_edit_drawer_widget is not first_widget
        assert w._agent_edit_drawer_title.get_text() == "Edit: Qat"
        assert w._agent_edit_drawer_widget._name_entry.get_text() == "Qat"
        # The old form is gone from the drawer body.
        assert first_widget.get_parent() is None

    def test_create_after_edit_swaps_mode_and_title(self):
        w = _win_with_drawer(StubBuilderHandler(defs={"Coder": _def("Coder")}))
        w._open_agent_builder("Coder")
        w._open_agent_builder(None)
        assert w._agent_edit_drawer.get_reveal_child() is True
        assert w._agent_edit_drawer_title.get_text() == "New agent"
        assert w._agent_edit_drawer_widget._is_edit is False


class TestDrawerSave:
    def test_save_success_closes_drawer(self):
        w = _win_with_drawer(StubBuilderHandler(save_result=(True, [])))
        w._open_agent_builder(None)
        values = {"name": "new-agent", "llm_name": "openai"}
        w._on_builder_save(values)
        assert w._agent_builder_handler.saved == [values]
        assert w._agent_edit_drawer.get_reveal_child() is False

    def test_save_error_stays_visible_in_drawer(self):
        w = _win_with_drawer(
            StubBuilderHandler(save_result=(False, ["name is required"])))
        w._open_agent_builder(None)
        w._on_builder_save({"name": ""})
        assert w._agent_edit_drawer.get_reveal_child() is True  # still open
        widget = w._agent_edit_drawer_widget
        assert widget._error_label.get_visible() is True
        assert "name is required" in widget._error_label.get_text()


# ── LeftPanel: double-click → edit callback ───────────────────────────────

def _row(session_key="agent:coder", name="Coder", create_card=False):
    row = Gtk.ListBoxRow()
    if create_card:
        row._is_create_agent_card = True
    else:
        row._session_key = session_key
        row._agent_name = name
    return row


class TestLeftPanelDoubleClick:
    def test_double_click_agent_row_calls_edit(self):
        panel = LeftPanel()
        panel._on_edit_agent = MagicMock()
        panel._on_agent_row_activated(None, _row("agent:coder", "Coder"))
        panel._on_edit_agent.assert_called_once_with("Coder")

    def test_double_click_gateway_row_is_noop(self):
        panel = LeftPanel()
        panel._on_edit_agent = MagicMock()
        panel._on_agent_row_activated(None, _row("gw:x:main", "X"))
        panel._on_edit_agent.assert_not_called()

    def test_double_click_create_card_fires_create(self):
        panel = LeftPanel()
        panel._on_edit_agent = MagicMock()
        panel._on_create_agent = MagicMock()
        panel._on_agent_row_activated(None, _row(create_card=True))
        panel._on_create_agent.assert_called_once_with()
        panel._on_edit_agent.assert_not_called()

    def test_set_agents_takes_names_only(self):
        panel = LeftPanel()
        panel.set_agents({"agent:coder": "Coder"})
        assert panel._agent_names == {"agent:coder"}


# ── LeftPanel: right-click menu is Delete-only ────────────────────────────

class _SpyPopover(Gtk.Popover):
    """Records instances so tests can reach the menu _show_local_agent_menu
    builds (the popover itself is not returned). popup() is a no-op so the
    menu is never actually presented under the test display."""
    last = None

    def __init__(self):
        super().__init__()
        _SpyPopover.last = self

    def popup(self):
        pass


class TestLocalAgentMenuDeleteOnly:
    def setup_method(self):
        _SpyPopover.last = None
        self._orig = lp.Gtk.Popover
        lp.Gtk.Popover = _SpyPopover  # same module object as gi Gtk

    def teardown_method(self):
        lp.Gtk.Popover = self._orig

    def _menu_buttons(self):
        child = _SpyPopover.last.get_child()
        return list(child)

    def test_menu_shows_delete_only(self):
        panel = LeftPanel()
        panel._do_delete_agent = MagicMock()
        panel._show_local_agent_menu(Gtk.Box(), "Coder")
        buttons = [w for w in self._menu_buttons() if isinstance(w, Gtk.Button)]
        labels = [b.get_label() for b in buttons]
        assert labels == ["Delete"]
        assert "Edit" not in labels

    def test_menu_delete_button_dispatches(self):
        panel = LeftPanel()
        panel._do_delete_agent = MagicMock()
        panel._show_local_agent_menu(Gtk.Box(), "Coder")
        buttons = [w for w in self._menu_buttons() if isinstance(w, Gtk.Button)]
        assert len(buttons) == 1
        buttons[0].emit("clicked")
        panel._do_delete_agent.assert_called_once()
        # (popover, name) — popover first arg, the right-clicked name second.
        args = panel._do_delete_agent.call_args[0]
        assert args[1] == "Coder"
        assert isinstance(args[0], Gtk.Popover)


class TestRightClickStillWorks:
    def test_row_keeps_right_click_gesture(self):
        """The right-click gesture survives the Edit removal."""
        src = inspect.getsource(LeftPanel._build_agent_row)
        assert "_on_agent_right_click" in src
        assert "GestureClick" in src

    def test_right_click_dispatches_to_local_menu(self):
        panel = LeftPanel()
        panel._show_local_agent_menu = MagicMock()
        gesture = MagicMock()
        panel._on_agent_right_click(gesture, 1, 4.0, 4.0, "agent:coder", "Coder")
        panel._show_local_agent_menu.assert_called_once()
        args = panel._show_local_agent_menu.call_args[0]
        assert args[1] == "Coder"

    def test_right_click_gateway_row_has_no_menu(self):
        panel = LeftPanel()
        panel._show_local_agent_menu = MagicMock()
        panel._on_agent_right_click(MagicMock(), 1, 4.0, 4.0, "gw:x:main", "X")
        panel._show_local_agent_menu.assert_not_called()

    def test_no_edit_menu_remnant_in_left_panel(self):
        src = inspect.getsource(LeftPanel)
        assert "_do_edit_agent" not in src
        assert 'label="Edit"' not in src
