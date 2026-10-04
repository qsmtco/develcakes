# tests/test_review_bar_queues.py
# SPEC-10 SP3 — ReviewBar queue view (D4/D4b/D8) — sub-phase 5a.
#
# Coverage:
#   1. set_queue_view renders one chip per agent + 2 batch buttons (counts in labels)
#   2. set_queue_view([]) hides the region (sep + box)
#   3. Chip click selects; Accept All (agent) fires the callback with the right key
#   4. D4b pin: the bar's existing states (idle/reviewing/has_changes) unregressed
#
# Strategy: real ReviewBar widget under xvfb (mirrors test_window_stop_all_dialog.py's
# harness style — real widget, simulated signals).

import os
import sys

import gi
import pytest

gi.require_version('Gtk', '4.0')
from gi.repository import Gtk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ui.views.review_bar import ReviewBar


@pytest.fixture
def bar():
    b = ReviewBar(
        on_mode_changed=lambda _m: None,
        on_start_clicked=lambda: None,
        on_check_clicked=lambda: None,
    )
    yield b


def _chip_labels(widgets):
    labels = []
    for w in widgets:
        child = w.get_child()
        if isinstance(child, Gtk.Label):
            labels.append(child.get_text())
    return labels


def _visible_children(box):
    out = []
    child = box.get_first_child()
    while child is not None:
        out.append(child)
        child = child.get_next_sibling()
    return out


def _click(button):
    button.emit("clicked")


class TestQueueViewRegion:
    def test_set_queue_view_renders_chips(self, bar):
        """2 agents → 2 chips + 2 batch buttons, visible, counts in labels."""
        bar.set_queue_callbacks(
            on_accept_agent=lambda ak: None,
            on_accept_all=lambda: None,
        )
        bar.set_queue_view([("special:coder", 3), ("special:debugger", 1)])
        assert bar._queue_box.get_visible()
        assert bar._queue_sep.get_visible()
        children = _visible_children(bar._queue_box)
        texts = _chip_labels(children)
        assert "special:coder (3)" in texts
        assert "special:debugger (1)" in texts
        # batch buttons present and labeled
        btn_labels = [w.get_child().get_text() for w in children
                      if isinstance(w, Gtk.Button)
                      and isinstance(w.get_child(), Gtk.Label)
                      and "Accept All" in (w.get_child().get_text() or "")]
        assert any("agent" in l for l in btn_labels)
        assert any("everyone" in l for l in btn_labels)

    def test_set_queue_view_empty_hides(self, bar):
        """[] → region hidden (sep + box invisible)."""
        bar.set_queue_callbacks(
            on_accept_agent=lambda ak: None,
            on_accept_all=lambda: None,
        )
        bar.set_queue_view([("special:coder", 1)])
        assert bar._queue_box.get_visible()
        bar.set_queue_view([])
        assert not bar._queue_box.get_visible()
        assert not bar._queue_sep.get_visible()

    def test_queue_callbacks_fire(self, bar):
        """Click chip → selects that agent; Accept All (agent) → callback
        receives the SELECTED key; Accept All (everyone) → zero-arg callback."""
        got_agent = []
        got_all = []
        bar.set_queue_callbacks(
            on_accept_agent=lambda ak: got_agent.append(ak),
            on_accept_all=lambda: got_all.append(True),
        )
        bar.set_queue_view([("special:coder", 2), ("special:debugger", 1)])
        buttons = [w for w in _visible_children(bar._queue_box)
                   if isinstance(w, Gtk.Button)]
        batch = [b for b in buttons
                 if isinstance(b.get_child(), Gtk.Label)
                 and "everyone" in (b.get_child().get_text() or "")]
        per_agent = [b for b in buttons
                     if isinstance(b.get_child(), Gtk.Label)
                     and "Accept All (agent)" in (b.get_child().get_text() or "")]
        # selection defaults to the first chip; per-agent accept targets it
        _click(per_agent[0])
        assert got_agent == ["special:coder"]
        # select the second chip explicitly, then per-agent accept targets it
        chips = [b for b in buttons if "special:debugger" in
                 (b.get_child().get_text() or "")]
        _click(chips[0])
        _click(per_agent[0])
        assert got_agent == ["special:coder", "special:debugger"]
        _click(batch[0])
        assert got_all == [True]

    def test_pm_chip_never_targeted_by_batch(self, bar):
        """D8: a 'pm' chip renders (with read-only affordance) but Accept
        All (agent) never fires with key 'pm'."""
        got_agent = []
        bar.set_queue_callbacks(
            on_accept_agent=lambda ak: got_agent.append(ak),
            on_accept_all=lambda: None,
        )
        bar.set_queue_view([("pm", 2), ("special:coder", 1)])
        buttons = [w for w in _visible_children(bar._queue_box)
                   if isinstance(w, Gtk.Button)]
        per_agent = [b for b in buttons
                     if isinstance(b.get_child(), Gtk.Label)
                     and "Accept All (agent)" in (b.get_child().get_text() or "")]
        # select the pm chip directly, then per-agent accept — must NOT target pm
        pm_chip = [b for b in buttons if b.get_child().get_text() == "pm (2)"]
        assert pm_chip, "pm chip missing"
        _click(pm_chip[0])
        _click(per_agent[0])
        assert "pm" not in got_agent
        assert got_agent == ["special:coder"]  # selection fell back to an agent

    def test_existing_states_unregressed(self, bar):
        """D4b pin: idle/reviewing/has_changes visibility semantics unchanged;
        the queue region does not interfere."""
        bar.set_state_idle()
        assert bar._btn_start.get_visible() and not bar._btn_check.get_visible()
        bar.set_state_reviewing("abc123")
        assert bar._btn_check.get_visible() and not bar._btn_start.get_visible()
        bar.set_state_has_changes(2, 10, 5)
        assert bar._btn_accept.get_visible() and bar._btn_reject.get_visible()
        # queue region coexists independently
        bar.set_queue_callbacks(
            on_accept_agent=lambda ak: None,
            on_accept_all=lambda: None,
        )
        bar.set_queue_view([("special:coder", 1)])
        assert bar._queue_box.get_visible()
        bar.set_state_idle()
        assert bar._btn_start.get_visible()
        assert bar._queue_box.get_visible()  # queue state persists across state changes


# ── SP3 5c: window confirm dialog (mirrors the Stop-All pattern) ─────────────

import contextlib

from ui.window import MainWindow

_app = Gtk.Application(application_id="test.crabcakes.batch_accept_dialog")

_app = Gtk.Application(application_id="test.crabcakes.batch_accept_dialog")


class _MainWindowTestHarness(Gtk.ApplicationWindow):
    """Real Gtk.ApplicationWindow proxying the production confirm method."""

    def __init__(self):
        super().__init__(application=_app)
        self._agent_runtime_handler = None
        self._confirm_batch_accept = (
            lambda *a: MainWindow._confirm_batch_accept(self, *a))


@pytest.fixture
def win_harness():
    return _MainWindowTestHarness()


@pytest.fixture(autouse=True)
def _cleanup_batch_dialogs():
    yield
    import gc
    gc.collect()
    for w in list(Gtk.Window.list_toplevels()):
        if isinstance(w, Gtk.MessageDialog):
            with contextlib.suppress(Exception):
                w.destroy()


def _find_batch_dialog():
    matches = [w for w in Gtk.Window.list_toplevels()
               if isinstance(w, Gtk.MessageDialog)]
    assert matches, "No Gtk.MessageDialog — did _confirm_batch_accept run?"
    return matches[-1]


class TestBatchAcceptConfirmDialog:
    def test_dialog_constructs_and_shows_n(self, win_harness):
        win_harness._confirm_batch_accept("proj", 3, lambda: None)
        dialog = _find_batch_dialog()
        assert dialog.get_property("message-type") == Gtk.MessageType.WARNING

    def test_ok_fires_on_confirm_once(self, win_harness):
        fired = []
        win_harness._confirm_batch_accept("proj", 2, lambda: fired.append(1))
        dialog = _find_batch_dialog()
        dialog.response(Gtk.ResponseType.OK)
        dialog.response(Gtk.ResponseType.OK)  # cascade — guard must suppress
        assert fired == [1]

    def test_cancel_never_fires(self, win_harness):
        fired = []
        win_harness._confirm_batch_accept("proj", 2, lambda: fired.append(1))
        dialog = _find_batch_dialog()
        dialog.response(Gtk.ResponseType.CANCEL)
        assert fired == []


class TestPmOnlyButtonSensitivity:
    """SP3 fix round SUGG#3: pm-only queue must disable 'Accept All (agent)'
    (previously enabled + _selected_agent=None = silent no-op)."""

    def _agent_batch_button(self, bar):
        for w in _visible_children(bar._queue_box):
            if (isinstance(w, Gtk.Button) and isinstance(w.get_child(), Gtk.Label)
                    and "Accept All (agent)" in (w.get_child().get_text() or "")):
                return w
        raise AssertionError("per-agent batch button missing")

    def test_pm_only_disables_agent_batch_button(self, bar):
        bar.set_queue_callbacks(on_accept_agent=lambda ak: None,
                                on_accept_all=lambda: None)
        bar.set_queue_view([("pm", 2)])
        assert not self._agent_batch_button(bar).get_sensitive(), (
            "pm-only: per-agent button enabled = silent no-op")
        # one agent arrives → sensitive again
        bar.set_queue_view([("pm", 2), ("special:coder", 1)])
        assert self._agent_batch_button(bar).get_sensitive()

    def test_agents_only_keeps_button_sensitive(self, bar):
        bar.set_queue_callbacks(on_accept_agent=lambda ak: None,
                                on_accept_all=lambda: None)
        bar.set_queue_view([("special:coder", 1)])
        assert self._agent_batch_button(bar).get_sensitive()
