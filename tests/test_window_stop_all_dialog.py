# tests/test_window_stop_all_dialog.py
# SPEC-09 SP3 — MainWindow._on_stop_all_clicked confirm dialog.
#
# Coverage:
#   - Dialog constructs (GTK4 secondary_text= kwarg — the Phase 5-4 regression class)
#   - OK response → ARH.stop_all_agents() called EXACTLY once
#   - Cancel response → stop_all_agents NEVER called
#   - Response cascade: close() fires a second response — one-shot guard
#     suppresses it (Bug A pattern from _show_auto_accept_warning)
#   - Default response is CANCEL (destructive action confirms deliberately)
#
# Strategy: same harness as test_window_auto_accept_warning.py — a real
# Gtk.ApplicationWindow proxying the production method, dialog found via
# Gtk.Window.list_toplevels(), user click simulated by emitting "response".

import contextlib
import os
import sys
from unittest.mock import MagicMock

import gi
import pytest

gi.require_version('Gtk', '4.0')
from gi.repository import Gtk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ui.window import MainWindow

_app = Gtk.Application(application_id="test.crabcakes.stop_all_dialog")


class _MainWindowTestHarness(Gtk.ApplicationWindow):
    """Real Gtk.ApplicationWindow proxying the production stop-all method."""

    def __init__(self):
        super().__init__(application=_app)
        self.set_default_size(400, 300)
        self._agent_runtime_handler = None
        # Bind the production method to this instance (unbound call idiom).
        self._on_stop_all_clicked = (
            lambda *a: MainWindow._on_stop_all_clicked(self, *a)
        )


@pytest.fixture
def harness():
    h = _MainWindowTestHarness()
    return h


@pytest.fixture(autouse=True)
def cleanup_dialogs():
    yield
    import gc
    gc.collect()
    for w in list(Gtk.Window.list_toplevels()):
        if isinstance(w, Gtk.MessageDialog):
            with contextlib.suppress(Exception):
                w.destroy()


def _find_dialog(window):
    matches = [w for w in Gtk.Window.list_toplevels()
               if isinstance(w, Gtk.MessageDialog)]
    if not matches:
        raise AssertionError(
            "No Gtk.MessageDialog found — did _on_stop_all_clicked run?"
        )
    return matches[-1]


class TestStopAllConfirmDialog:
    def test_dialog_constructs_with_warning_type(self, harness):
        harness._on_stop_all_clicked()
        dialog = _find_dialog(harness)
        # GTK4 MessageDialog: message-type is a construct property (no getter).
        assert dialog.get_property("message-type") == Gtk.MessageType.WARNING

    def test_default_response_is_cancel(self, harness):
        """Destructive action: default is Cancel (confirm is deliberate)."""
        harness._on_stop_all_clicked()
        dialog = _find_dialog(harness)
        cancel_widget = dialog.get_widget_for_response(Gtk.ResponseType.CANCEL)
        assert cancel_widget is not None
        assert cancel_widget.has_default()

    def test_ok_confirms_calls_stop_all_agents_once(self, harness):
        """OK → ARH.stop_all_agents() EXACTLY once (cascade guard)."""
        arh = MagicMock()
        harness._agent_runtime_handler = arh
        harness._on_stop_all_clicked()
        dialog = _find_dialog(harness)
        dialog.emit("response", Gtk.ResponseType.OK)
        assert arh.stop_all_agents.call_count == 1, (
            f"expected exactly one stop_all_agents call; "
            f"got {arh.stop_all_agents.call_count}"
        )

    def test_cancel_never_stops_agents(self, harness):
        arh = MagicMock()
        harness._agent_runtime_handler = arh
        harness._on_stop_all_clicked()
        dialog = _find_dialog(harness)
        dialog.emit("response", Gtk.ResponseType.CANCEL)
        assert arh.stop_all_agents.call_count == 0

    def test_ok_then_close_cascade_fires_only_once(self, harness):
        """Bug A regression class: close() after dispatch triggers a second
        response(DELETE_EVENT) — the one-shot guard must suppress it."""
        arh = MagicMock()
        harness._agent_runtime_handler = arh
        harness._on_stop_all_clicked()
        dialog = _find_dialog(harness)
        dialog.emit("response", Gtk.ResponseType.OK)
        dialog.emit("response", Gtk.ResponseType.DELETE_EVENT)  # cascade
        assert arh.stop_all_agents.call_count == 1

    def test_no_handler_wired_no_crash_on_confirm(self, harness):
        """_agent_runtime_handler None (handler not built yet) — confirm
        must log-and-no-op, never raise."""
        harness._agent_runtime_handler = None
        harness._on_stop_all_clicked()
        dialog = _find_dialog(harness)
        dialog.emit("response", Gtk.ResponseType.OK)  # must not raise
