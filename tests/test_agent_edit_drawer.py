# tests/test_agent_edit_drawer.py
# The agent-edit drawer now lives in the LEFT PANEL, under the agent row
# (file-tree-drawer shape) — NOT in the chat pane.
#
# Contract: .crabcakes/workorder-agent-drawer-leftpanel.md
#   - ui/views/left_panel.py owns the drawer API:
#       set_agent_drawer_factory(cb) / open_agent_drawer(name, row=None)
#       close_agent_drawer() / agent_drawer_widget
#   - the revealer is ONE lazily-created Gtk.Revealer (SLIDE_DOWN, 150 ms)
#     reparented between rows' `_drawer_container` slots with Gtk.Box.remove
#     per child (never unparent() on a live widget — prior SEGFAULT)
#   - MainContent.add_drawer is gone (the drawer is not a chat-pane child)
#   - the Delete-only right-click menu is unchanged
#
# Window-level behavior (factory → form → save) is exercised on the
# established MainWindow.__new__ shell pattern (test_window_settings_bar.py's
# `win` fixture): real widgets for the drawer path, MagicMock for everything
# the shell does not own. LeftPanel behavior constructs real widgets — runs
# under xvfb-run -a.
#
# Runs under xvfb-run -a (GTK4 widget construction needs a display; the
# conftest collector skips this module when none is available).

import atexit
import inspect
import os
import shutil
import subprocess
import sys
from unittest.mock import MagicMock

# ── Local display bootstrap (headless runs) ──────────────────────────────
#
# GTK4 has NO headless backend: constructing a widget without a display
# SEGFAULTS (see tests/conftest.py — that is why the collector skips
# widget-constructing modules when no display is reachable). The repo's
# supported way to run the GUI suites is `xvfb-run -a`, and under it this
# bootstrap does nothing (DISPLAY is already set).
#
# When this module is run on its own WITHOUT a display, it starts a private
# Xvfb server for the duration of the run and points DISPLAY at it, so the
# module's own assertions really execute instead of being skipped. The
# server is torn down at interpreter exit.
#
# Guard: the bootstrap only arms when (a) DISPLAY is unset AND (b) this
# module is the suite being run. A full headless `pytest` run therefore
# keeps its current semantics exactly — no display, every widget-constructing
# module skipped by tests/conftest.py — and only THIS module opts in.
_ARM_BOOTSTRAP = (
    not os.environ.get("DISPLAY")
    and not os.environ.get("WAYLAND_DISPLAY")
    and any("test_agent_edit_drawer" in arg for arg in sys.argv)
)

_XVFB_PROC = None


def _start_private_xvfb():
    """Start a private Xvfb server; return (display_name, proc) or None."""
    xvfb = shutil.which("Xvfb")
    if xvfb is None:
        return None
    try:
        read_fd, write_fd = os.pipe()
    except OSError:
        return None
    try:
        proc = subprocess.Popen(
            [xvfb, "-displayfd", str(write_fd), "-screen", "0", "1024x768x24",
             "-nolisten", "tcp"],
            pass_fds=(write_fd,),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None
    finally:
        try:
            os.close(write_fd)
        except OSError:
            pass
    try:
        number = os.read(read_fd, 32).decode().strip()
    except OSError:
        number = ""
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass
    if not number:
        proc.kill()
        proc.wait()
        return None
    return f":{number}", proc


def _kill_private_xvfb():
    global _XVFB_PROC
    proc, _XVFB_PROC = _XVFB_PROC, None
    if proc is None:
        return
    try:
        proc.kill()
        proc.wait(timeout=10)
    except Exception:  # pragma: no cover — best-effort cleanup
        pass


if _ARM_BOOTSTRAP:
    _started = _start_private_xvfb()
    if _started is not None:
        _display, _XVFB_PROC = _started
        os.environ["DISPLAY"] = _display
        atexit.register(_kill_private_xvfb)

import gi

gi.require_version('Gtk', '4.0')

from gi.repository import Gtk

import ui.views.left_panel as lp
import ui.window as wm
from ui.views.left_panel import LeftPanel
from ui.views.main_content import MainContent

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
    """A MainWindow shell whose agent drawer lives in the LEFT PANEL.

    The window no longer builds a drawer widget of its own — it wires a
    factory into the panel and lets the panel own the revealer.
    """
    w = wm.MainWindow.__new__(wm.MainWindow)
    w._main_content = MagicMock(name="main_content")
    w._agent_builder_handler = handler or StubBuilderHandler()
    panel = LeftPanel()
    panel.set_agent_drawer_factory(w._build_agent_form)
    w._left_panel = panel
    return w


def _def(name, role="coder"):
    return {"name": name, "role": role, "tools": [], "llm_name": "openai"}


def _panel_with_agents(names=("Coder", "Qat"), factory=None):
    """A LeftPanel with real agent rows and a recording drawer factory.

    Returns (panel, rows) where rows maps agent name → its ListBoxRow.
    """
    panel = LeftPanel()
    built = []

    def _factory(name):
        widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        widget._built_for = name
        built.append((name, widget))
        return widget

    panel.set_agent_drawer_factory(factory or _factory)
    panel.set_agents(list(names))
    rows = {}
    child = panel._agents_list_box.get_first_child()
    while child is not None:
        agent_name = getattr(child, "_agent_name", None)
        if agent_name:
            rows[agent_name] = child
        child = child.get_next_sibling()
    return panel, rows, built


# ── The drawer lives in the left panel, not the chat pane ────────────────

class TestDrawerLeftPanelOwnership:
    def test_main_content_has_no_add_drawer(self):
        """The chat pane no longer hosts the drawer."""
        assert not hasattr(MainContent, "add_drawer")

    def test_left_panel_exposes_the_drawer_api(self):
        for name in ("set_agent_drawer_factory", "open_agent_drawer",
                     "close_agent_drawer", "agent_drawer_widget"):
            assert hasattr(LeftPanel, name), f"LeftPanel missing {name}"

    def test_window_has_no_drawer_builder(self):
        """The window-side drawer builder is gone."""
        assert not hasattr(wm.MainWindow, "_build_agent_edit_drawer")

    def test_drawer_is_not_parented_to_main_content(self):
        """Opening the drawer never touches the chat pane."""
        w = _win_with_drawer(StubBuilderHandler(defs={"Coder": _def("Coder")}))
        w._left_panel.set_agents(["Coder"])
        w._open_agent_builder("Coder")
        w._main_content.add_drawer.assert_not_called()


# ── Drawer lifecycle ──────────────────────────────────────────────────────

class TestDrawerLifecycle:
    def test_drawer_starts_hidden(self):
        panel, _rows, _built = _panel_with_agents()
        assert panel._agent_drawer is None  # lazily created, nothing built yet

    def test_drawer_is_a_revealer_sliding_down(self):
        panel, rows, _built = _panel_with_agents()
        panel.open_agent_drawer("Coder", row=rows["Coder"])
        revealer = panel._agent_drawer
        assert isinstance(revealer, Gtk.Revealer)
        assert (revealer.get_transition_type()
                == Gtk.RevealerTransitionType.SLIDE_DOWN)
        assert revealer.get_transition_duration() == 150

    def test_one_revealer_is_reused_across_agents(self):
        panel, rows, _built = _panel_with_agents()
        panel.open_agent_drawer("Coder", row=rows["Coder"])
        first = panel._agent_drawer
        panel.open_agent_drawer("Qat", row=rows["Qat"])
        assert panel._agent_drawer is first

    def test_drawer_body_shape_header_form(self):
        """Child = header row (title + ✕) + the form widget."""
        panel, rows, built = _panel_with_agents()
        panel.open_agent_drawer("Coder", row=rows["Coder"])
        body = panel._agent_drawer.get_child()
        assert isinstance(body, Gtk.Box)
        header = body.get_first_child()
        assert isinstance(header, Gtk.Box)
        form = header.get_next_sibling()
        assert form is built[0][1]
        assert panel._agent_drawer_title.get_text() == "Edit: Coder"

    def test_header_has_close_button(self):
        panel, rows, _built = _panel_with_agents()
        panel.open_agent_drawer("Coder", row=rows["Coder"])
        header = panel._agent_drawer.get_child().get_first_child()
        btns = [c for c in header if isinstance(c, Gtk.Button)]
        assert [b.get_label() for b in btns] == ["✕"]

    def test_css_class_sits_on_the_revealer(self):
        """The hairline belongs to the drawer (under the row), not the form."""
        panel, rows, _built = _panel_with_agents()
        panel.open_agent_drawer("Coder", row=rows["Coder"])
        assert panel._agent_drawer.has_css_class("agent-edit-drawer")


# ── Open: the drawer is INSIDE the row's own container ───────────────────

class TestDrawerOpensInsideRow:
    def test_double_click_agent_row_reveals_drawer_in_that_row(self):
        """The revealer's parent chain reaches row._drawer_container."""
        panel, rows, built = _panel_with_agents()
        row = rows["Coder"]
        panel._on_agent_row_activated(None, row)

        revealer = panel._agent_drawer
        assert revealer is not None
        assert revealer.get_reveal_child() is True
        assert revealer.get_parent() is row._drawer_container
        # …and the container is the row's own child (inside the row).
        assert row._drawer_container.get_parent() is row.get_child()

    def test_drawer_is_not_in_the_chat_pane(self):
        panel, rows, _built = _panel_with_agents()
        panel._on_agent_row_activated(None, rows["Coder"])
        revealer = panel._agent_drawer
        # The revealer must not be a direct child of the panel's list box.
        assert revealer.get_parent() is not panel._agents_list_box
        assert revealer.get_parent() is rows["Coder"]._drawer_container

    def test_row_owns_a_drawer_container_slot(self):
        panel = LeftPanel()
        row = panel._build_agent_row("agent:coder", "Coder")
        assert hasattr(row, "_drawer_container")
        assert isinstance(row._drawer_container, Gtk.Box)
        # Empty + invisible until the drawer is attached.
        assert row._drawer_container.get_first_child() is None
        assert row._drawer_container.get_visible() is False

    def test_create_agent_row_owns_a_drawer_container_slot(self):
        panel = LeftPanel()
        row = panel._build_create_agent_row()
        assert hasattr(row, "_drawer_container")
        assert row._drawer_container.get_first_child() is None
        assert row._drawer_container.get_visible() is False

    def test_open_without_factory_returns_false(self):
        panel = LeftPanel()
        row = panel._build_agent_row("agent:coder", "Coder")
        assert panel.open_agent_drawer("Coder", row=row) is False

    def test_open_with_unresolvable_name_returns_false(self):
        panel, _rows, _built = _panel_with_agents()
        assert panel.open_agent_drawer("ghost") is False

    def test_open_by_name_finds_the_row(self):
        """open_agent_drawer(name) without a row resolves the row itself."""
        panel, rows, _built = _panel_with_agents()
        assert panel.open_agent_drawer("Qat") is True
        assert panel._agent_drawer.get_parent() is rows["Qat"]._drawer_container


# ── Moving between rows / toggling ───────────────────────────────────────

class TestDrawerMovesBetweenRows:
    def test_opening_a_different_agent_moves_the_revealer(self):
        panel, rows, built = _panel_with_agents()
        panel.open_agent_drawer("Coder", row=rows["Coder"])
        first = panel._agent_drawer
        first_container = rows["Coder"]._drawer_container

        panel.open_agent_drawer("Qat", row=rows["Qat"])

        # Still open, but now parented into the OTHER row's container.
        assert first.get_reveal_child() is True
        assert first.get_parent() is rows["Qat"]._drawer_container
        assert first.get_parent() is not first_container
        assert first_container.get_first_child() is None
        assert first_container.get_visible() is False

    def test_opening_a_different_agent_builds_a_fresh_form(self):
        panel, rows, built = _panel_with_agents()
        panel.open_agent_drawer("Coder", row=rows["Coder"])
        first_widget = built[0][1]
        panel.open_agent_drawer("Qat", row=rows["Qat"])
        assert built[1][0] == "Qat"
        assert built[1][1] is not first_widget
        assert first_widget.get_parent() is None

    def test_same_agent_again_closes_it(self):
        panel, rows, _built = _panel_with_agents()
        panel.open_agent_drawer("Coder", row=rows["Coder"])
        assert panel._agent_drawer.get_reveal_child() is True
        panel.open_agent_drawer("Coder", row=rows["Coder"])
        assert panel._agent_drawer.get_reveal_child() is False

    def test_same_agent_via_double_click_closes_it(self):
        panel, rows, _built = _panel_with_agents()
        panel._on_agent_row_activated(None, rows["Coder"])
        assert panel._agent_drawer.get_reveal_child() is True
        panel._on_agent_row_activated(None, rows["Coder"])
        assert panel._agent_drawer.get_reveal_child() is False

    def test_create_mode_toggles_closed_on_repeat(self):
        panel, rows, _built = _panel_with_agents()
        create_row = panel._agents_list_box.get_first_child()
        panel.open_agent_drawer(None, row=create_row)
        assert panel._agent_drawer.get_reveal_child() is True
        panel.open_agent_drawer(None, row=create_row)
        assert panel._agent_drawer.get_reveal_child() is False


# ── Close paths ───────────────────────────────────────────────────────────

class TestDrawerClose:
    def test_close_hides_drawer(self):
        panel, rows, _built = _panel_with_agents()
        panel.open_agent_drawer("Coder", row=rows["Coder"])
        panel.close_agent_drawer()
        assert panel._agent_drawer.get_reveal_child() is False
        assert rows["Coder"]._drawer_container.get_visible() is False

    def test_close_button_closes_drawer(self):
        panel, rows, _built = _panel_with_agents()
        panel.open_agent_drawer("Coder", row=rows["Coder"])
        header = panel._agent_drawer.get_child().get_first_child()
        close_btn = [c for c in header if isinstance(c, Gtk.Button)][0]
        close_btn.emit("clicked")
        assert panel._agent_drawer.get_reveal_child() is False

    def test_cancel_closes_drawer(self):
        """The form's Cancel button routes to close_agent_drawer()."""
        w = _win_with_drawer()
        w._left_panel.set_agents(["Coder"])
        w._open_agent_builder(None)
        assert w._left_panel._agent_drawer.get_reveal_child() is True
        w._left_panel.agent_drawer_widget._do_cancel()
        assert w._left_panel._agent_drawer.get_reveal_child() is False

    def test_window_close_delegates_to_panel(self):
        w = _win_with_drawer()
        w._left_panel.set_agents(["Coder"])
        w._open_agent_builder(None)
        w._close_agent_edit_drawer()
        assert w._left_panel._agent_drawer.get_reveal_child() is False


# ── Create Agent card ─────────────────────────────────────────────────────

class TestCreateAgentCard:
    def test_create_card_opens_drawer_in_create_mode(self):
        panel = LeftPanel()
        built = []

        def _factory(name):
            widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            widget._built_for = name
            built.append((name, widget))
            return widget

        panel.set_agent_drawer_factory(_factory)
        panel.set_agents(["Coder"])

        create_row = panel._agents_list_box.get_first_child()
        assert getattr(create_row, "_is_create_agent_card", False) is True

        panel._on_agent_row_activated(None, create_row)

        assert panel._agent_drawer.get_reveal_child() is True
        assert panel._agent_drawer.get_parent() is create_row._drawer_container
        assert built[0][0] is None  # create mode
        assert panel._agent_drawer_title.get_text() == "New agent"

    def test_create_card_click_opens_drawer(self):
        """The single-click gesture on the card opens the drawer too."""
        panel = LeftPanel()
        panel.set_agent_drawer_factory(lambda name: Gtk.Box())
        panel.set_agents(["Coder"])
        create_row = panel._agents_list_box.get_first_child()
        panel._on_create_agent_click(row=create_row)
        assert panel._agent_drawer.get_reveal_child() is True
        assert panel._agent_drawer.get_parent() is create_row._drawer_container

    def test_create_card_falls_back_to_callback_without_factory(self):
        panel = LeftPanel()
        panel._on_create_agent = MagicMock()
        panel.set_agents(["Coder"])
        create_row = panel._agents_list_box.get_first_child()
        panel._on_agent_row_activated(None, create_row)
        panel._on_create_agent.assert_called_once_with()


# ── Window-side form wiring (factory → form → save) ──────────────────────

class TestWindowFormFactory:
    def test_edit_builds_form_in_edit_mode(self):
        w = _win_with_drawer(StubBuilderHandler(defs={"Coder": _def("Coder")}))
        w._left_panel.set_agents(["Coder"])
        w._open_agent_builder("Coder")
        widget = w._left_panel.agent_drawer_widget
        assert widget._is_edit is True
        assert widget._name_entry.get_text() == "Coder"
        assert widget._save_btn.get_label() == "Save"

    def test_create_builds_form_in_create_mode(self):
        w = _win_with_drawer()
        w._left_panel.set_agents(["Coder"])
        w._open_agent_builder(None)
        widget = w._left_panel.agent_drawer_widget
        assert widget._is_edit is False
        assert widget._save_btn.get_label() == "Create"

    def test_missing_agent_does_not_open(self):
        w = _win_with_drawer()
        w._left_panel.set_agents(["Coder"])
        w._open_agent_builder("ghost")
        assert w._left_panel._agent_drawer is None

    def test_save_success_closes_drawer(self):
        w = _win_with_drawer(StubBuilderHandler(save_result=(True, [])))
        w._left_panel.set_agents(["Coder"])
        w._open_agent_builder(None)
        values = {"name": "new-agent", "llm_name": "openai"}
        w._on_builder_save(values)
        assert w._agent_builder_handler.saved == [values]
        assert w._left_panel._agent_drawer.get_reveal_child() is False

    def test_save_error_keeps_drawer_open_and_shows_the_error(self):
        w = _win_with_drawer(
            StubBuilderHandler(save_result=(False, ["name is required"])))
        w._left_panel.set_agents(["Coder"])
        w._open_agent_builder(None)
        w._on_builder_save({"name": ""})
        assert w._left_panel._agent_drawer.get_reveal_child() is True
        widget = w._left_panel.agent_drawer_widget
        assert widget._error_label.get_visible() is True
        assert "name is required" in widget._error_label.get_text()


# ── Refresh resets drawer state ───────────────────────────────────────────

class TestRefreshResetsDrawer:
    def test_refresh_drops_stale_drawer_state(self):
        panel, rows, _built = _panel_with_agents()
        panel.open_agent_drawer("Coder", row=rows["Coder"])
        assert panel._agent_drawer is not None

        panel._refresh_agents_list()

        assert panel._agent_drawer is None
        assert panel._agent_drawer_row is None
        assert panel._agent_drawer_edit_name is None
        assert panel.agent_drawer_widget is None

    def test_refreshed_rows_get_fresh_drawer_containers(self):
        panel, rows, _built = _panel_with_agents()
        panel.open_agent_drawer("Coder", row=rows["Coder"])
        old_container = rows["Coder"]._drawer_container

        panel._refresh_agents_list()

        new_row = panel._find_agent_row("Coder")
        assert new_row is not None
        assert new_row._drawer_container is not old_container
        assert new_row._drawer_container.get_first_child() is None


# ── LeftPanel: double-click routing ──────────────────────────────────────

class TestLeftPanelDoubleClick:
    def test_double_click_agent_row_opens_drawer(self):
        panel = LeftPanel()
        panel.set_agent_drawer_factory(lambda name: Gtk.Box())
        row = panel._build_agent_row("agent:coder", "Coder")
        panel._on_agent_row_activated(None, row)
        assert panel._agent_drawer.get_reveal_child() is True
        assert panel._agent_drawer.get_parent() is row._drawer_container

    def test_double_click_gateway_row_is_noop(self):
        panel = LeftPanel()
        panel.set_agent_drawer_factory(lambda name: Gtk.Box())
        row = Gtk.ListBoxRow()
        row._session_key = "gw:x:main"
        row._agent_name = "X"
        panel._on_agent_row_activated(None, row)
        assert panel._agent_drawer is None

    def test_double_click_falls_back_to_edit_callback_without_factory(self):
        panel = LeftPanel()
        panel._on_edit_agent = MagicMock()
        row = panel._build_agent_row("agent:coder", "Coder")
        panel._on_agent_row_activated(None, row)
        panel._on_edit_agent.assert_called_once_with("Coder")

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
