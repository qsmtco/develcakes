# tests/test_window_telegram_bridge.py — SPEC-15 SP2 part D (window wiring).
#
# Coverage:
#   - _on_connect_clicked: unconfigured → honest gray label, no bridge start
#   - configured + disconnected → start_bridge(); connected → stop_bridge()
#   - connecting state → stop (toggle)
#   - _on_bridge_state_change drives the toolbar + emits an error feed card
#   - stop-all confirm branch also stops the bridge
#
# Strategy: connect-toggle tests use MainWindow.__new__ + MagicMock handlers
# (no real GTK widget tree — mirrors test_window_settings_bar.py). The stop-all
# hook reuses the real-MessageDialog harness pattern from
# test_window_stop_all_dialog.py.

import contextlib
import os
import sys
from unittest.mock import MagicMock

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ui.window import MainWindow
from utils import telegram_store


def test_build_wires_initial_bridge_toolbar_state():
    """BUG#6: _build must CALL _init_bridge_toolbar_state — otherwise the
    helper is dead code and the toolbar stays dishonest until a click.

    Source-shape guard (window.py can't be built headless): mirrors
    tests/test_window_project_created.py.
    """
    import pathlib

    src = pathlib.Path("ui/window.py").read_text()
    assert "self._init_bridge_toolbar_state()" in src, (
        "_build must call _init_bridge_toolbar_state() after constructing the "
        "toolbar (SPEC-15 SP2 BUG#6)"
    )


class FakeBridge:
    """Records start/stop; exposes controllable state."""

    def __init__(self, *, state="disconnected"):
        self.state = state
        self.started = 0
        self.stopped = 0

    def is_connected(self):
        return self.state == "connected"

    def start_bridge(self):
        self.started += 1
        self.state = "connecting"

    def stop_bridge(self):
        self.stopped += 1
        self.state = "disconnected"


@pytest.fixture
def win(tmp_config_dir):
    """Bare MainWindow with mocked toolbar/bridge/feed handlers."""
    w = MainWindow.__new__(MainWindow)
    w._toolbar = MagicMock()
    w._bridge_handler = FakeBridge()
    w._feed_handler = MagicMock()
    ph = MagicMock()
    ph.get_active_project_name.return_value = "Proj"
    w._project_handler = ph
    return w


# ── connect toggle ───────────────────────────────────────────────────────

class TestInitialToolbarState:
    """BUG#6 (SP2 audit): the toolbar must be honest BEFORE any click."""

    def test_fresh_unconfigured_store_sets_unconfigured_at_build(self, win):
        self._assert_initial(win, configured=False, expected="unconfigured")

    def test_configured_store_sets_disconnected_at_build(self, win):
        telegram_store.save_bridge_config(bot_token="123:abc", chat_id=42)
        self._assert_initial(win, configured=True, expected="disconnected")

    @staticmethod
    def _assert_initial(win, *, configured, expected):
        win._toolbar.reset_mock()
        win._init_bridge_toolbar_state()  # what _build() calls, no click
        win._toolbar.set_telegram_bridge_state.assert_called_once_with(expected)


class TestConnectUnconfigured:
    def test_unconfigured_sets_honest_label_and_starts_nothing(self, win):
        # No token/chat saved.
        win._on_connect_clicked()
        win._toolbar.set_telegram_bridge_state.assert_called_once_with("unconfigured")
        assert win._bridge_handler.started == 0
        assert win._bridge_handler.stopped == 0

    def test_token_only_still_unconfigured(self, win):
        telegram_store.save_bridge_config(bot_token="123:abc")  # no chat_id
        win._on_connect_clicked()
        win._toolbar.set_telegram_bridge_state.assert_called_once_with("unconfigured")
        assert win._bridge_handler.started == 0

    def test_no_bridge_wired_configured_is_safe(self, win):
        telegram_store.save_bridge_config(bot_token="123:abc", chat_id=42)
        win._bridge_handler = None
        win._on_connect_clicked()  # must not raise
        win._toolbar.set_telegram_bridge_state.assert_not_called()


class TestConnectConfigured:
    def _configure(self):
        telegram_store.save_bridge_config(bot_token="123:abc", chat_id=42)

    def test_disconnected_starts_bridge(self, win):
        self._configure()
        win._bridge_handler.state = "disconnected"
        win._on_connect_clicked()
        assert win._bridge_handler.started == 1
        assert win._bridge_handler.stopped == 0

    def test_connected_stops_bridge(self, win):
        self._configure()
        win._bridge_handler.state = "connected"
        win._on_connect_clicked()
        assert win._bridge_handler.stopped == 1
        assert win._bridge_handler.started == 0

    def test_connecting_toggles_to_stop(self, win):
        self._configure()
        win._bridge_handler.state = "connecting"
        win._on_connect_clicked()
        assert win._bridge_handler.stopped == 1

    def test_error_state_starts_again(self, win):
        self._configure()
        win._bridge_handler.state = "error"
        win._on_connect_clicked()
        assert win._bridge_handler.started == 1


# ── state-change driver ──────────────────────────────────────────────────

class TestBridgeStateChange:
    def test_each_state_reaches_toolbar(self, win):
        for state in ("connecting", "connected", "disconnected", "unconfigured"):
            win._on_bridge_state_change(state)
        calls = [c.args[0] for c in win._toolbar.set_telegram_bridge_state.call_args_list]
        assert calls == ["connecting", "connected", "disconnected", "unconfigured"]

    def test_error_state_emits_feed_card(self, win):
        win._on_bridge_state_change("error")
        win._feed_handler.add_card.assert_called_once()
        card = win._feed_handler.add_card.call_args.args[0]
        assert card.card_type == "system"
        assert card.metadata["origin"] == "telegram-bridge"
        assert "offline" in card.title.lower()

    def test_non_error_state_emits_no_card(self, win):
        win._on_bridge_state_change("connected")
        win._feed_handler.add_card.assert_not_called()

    def test_notice_card_emitter_uses_given_title_body(self, win):
        """BUG#1: the bridge's specific refusal notice must reach the feed
        with ITS title/body, not the generic 'offline' text."""
        win._emit_bridge_notice_card(
            "Telegram bridge: Supervisor agent not registered", "add it"
        )
        win._feed_handler.add_card.assert_called_once()
        card = win._feed_handler.add_card.call_args.args[0]
        assert "not registered" in card.title.lower()
        assert card.metadata["origin"] == "telegram-bridge"

    def test_specific_notice_suppresses_generic_error_card(self, win):
        """BUG#1 dedup: a specific refusal notice + the ERROR transition it
        triggers must produce exactly ONE card (the specific one)."""
        win._emit_bridge_notice_card("Supervisor not registered", "add it")
        win._on_bridge_state_change("error")
        win._feed_handler.add_card.assert_called_once()
        # A subsequent genuine error (no pending notice) still emits the
        # generic card.
        win._on_bridge_state_change("error")
        assert win._feed_handler.add_card.call_count == 2

    def test_error_card_survives_missing_feed_handler(self, win):
        win._feed_handler = None
        win._on_bridge_state_change("error")  # must not raise

    def test_error_card_survives_feed_handler_exception(self, win):
        win._feed_handler.add_card.side_effect = RuntimeError("boom")
        win._on_bridge_state_change("error")  # must not raise


# ── stop-all hook (real MessageDialog harness) ────────────────────────────

_app = Gtk.Application(application_id="test.develcakes.telegram_bridge")


class _StopAllHarness(Gtk.ApplicationWindow):
    def __init__(self):
        super().__init__(application=_app)
        self.set_default_size(400, 300)
        self._agent_runtime_handler = MagicMock()
        self._bridge_handler = FakeBridge(state="connected")
        self._on_stop_all_clicked = (
            lambda *a: MainWindow._on_stop_all_clicked(self, *a)
        )


@pytest.fixture
def stop_harness():
    return _StopAllHarness()


@pytest.fixture(autouse=True)
def cleanup_dialogs():
    yield
    import gc
    gc.collect()
    for w in list(Gtk.Window.list_toplevels()):
        if isinstance(w, Gtk.MessageDialog):
            with contextlib.suppress(Exception):
                w.destroy()


def _find_dialog():
    matches = [w for w in Gtk.Window.list_toplevels()
               if isinstance(w, Gtk.MessageDialog)]
    if not matches:
        raise AssertionError("No dialog — did _on_stop_all_clicked run?")
    return matches[-1]


class TestStopAllDropsBridge:
    def test_confirm_stops_bridge(self, stop_harness):
        stop_harness._on_stop_all_clicked()
        _find_dialog().emit("response", Gtk.ResponseType.OK)
        assert stop_harness._bridge_handler.stopped == 1

    def test_cancel_does_not_stop_bridge(self, stop_harness):
        stop_harness._on_stop_all_clicked()
        _find_dialog().emit("response", Gtk.ResponseType.CANCEL)
        assert stop_harness._bridge_handler.stopped == 0

    def test_no_bridge_wired_confirm_is_safe(self, stop_harness):
        stop_harness._bridge_handler = None
        stop_harness._on_stop_all_clicked()
        _find_dialog().emit("response", Gtk.ResponseType.OK)  # must not raise


# ── SP3b: ARH response-slot composition ──────────────────────────────────

class TestAgentResponseComposition:
    def test_composes_command_handler_and_bridge(self, win):
        """SP3b: the single ARH response slot must call BOTH the agent-command
        handler AND the bridge's Supervisor mirror."""
        win._agent_command_handler = MagicMock()
        win._bridge_handler = MagicMock()
        win._on_agent_response("special:supervisor", "hi", "Proj")
        win._agent_command_handler.on_agent_response.assert_called_once_with(
            "special:supervisor", "hi", "Proj")
        win._bridge_handler.on_supervisor_reply.assert_called_once_with(
            "special:supervisor", "hi")

    def test_bridge_fault_never_breaks_pipeline(self, win):
        win._agent_command_handler = MagicMock()
        win._bridge_handler = MagicMock()
        win._bridge_handler.on_supervisor_reply.side_effect = RuntimeError("boom")
        win._on_agent_response("special:supervisor", "hi", "Proj")  # no raise
        win._agent_command_handler.on_agent_response.assert_called_once()

    def test_missing_bridge_is_safe(self, win):
        win._agent_command_handler = MagicMock()
        win._bridge_handler = None
        win._on_agent_response("special:supervisor", "hi", "Proj")  # no raise
        win._agent_command_handler.on_agent_response.assert_called_once()

    def test_build_registers_composed_callback(self):
        """Source-shape guard: _build must register _on_agent_response (not the
        bare command handler) on the ARH — otherwise the phone mirror is dead."""
        import pathlib

        src = pathlib.Path("ui/window.py").read_text()
        assert (
            "self._agent_runtime_handler.set_on_agent_response(self._on_agent_response)"
            in src
        ), "ARH response slot must be composed via _on_agent_response (SPEC-15 SP3b)"
        assert "def _on_agent_response(" in src


# ── SP4: remote stop-all + status injection ──────────────────────────────

class TestRemoteStopAll:
    def test_runs_arh_and_stops_bridge(self, win):
        win._agent_runtime_handler = MagicMock()
        win._remote_stop_all()
        win._agent_runtime_handler.stop_all_agents.assert_called_once()
        assert win._bridge_handler.stopped == 1

    def test_missing_arh_still_stops_bridge(self, win):
        win._agent_runtime_handler = None
        win._remote_stop_all()  # no raise
        assert win._bridge_handler.stopped == 1

    def test_build_injects_stop_all_and_status(self):
        import pathlib

        src = pathlib.Path("ui/window.py").read_text()
        assert "set_stop_all_handler(self._remote_stop_all)" in src
        assert "set_status_provider(self._remote_status_summary)" in src


class TestCloseRequestStopsBridge:
    """SPEC-15 §5: 'App quit with bridge up → disconnect in shutdown path'."""

    def test_close_request_stops_bridge(self, win):
        assert win._on_close_request() is False  # default close still allowed
        win._feed_handler.shutdown_persist_writer.assert_called_once()
        assert win._bridge_handler.stopped == 1

    def test_close_request_without_bridge_is_safe(self, win):
        win._bridge_handler = None
        assert win._on_close_request() is False  # must not raise
        win._feed_handler.shutdown_persist_writer.assert_called_once()

    def test_close_request_bridge_fault_does_not_break_close(self, win):
        win._bridge_handler.stop_bridge = MagicMock(side_effect=RuntimeError("boom"))
        assert win._on_close_request() is False  # must not raise
        win._feed_handler.shutdown_persist_writer.assert_called_once()

    def test_build_wires_close_request_hook(self):
        import pathlib

        src = pathlib.Path("ui/window.py").read_text()
        assert 'connect("close-request", self._on_close_request)' in src
        # The close hook must stop the bridge (SPEC-15 §5).
        idx = src.index("def _on_close_request")
        body = src[idx:idx + 1200]
        assert "stop_bridge()" in body, (
            "_on_close_request must stop the bridge (SPEC-15 §5)"
        )


class TestRemoteStatusSummary:
    def test_no_active_project_is_honest(self, win):
        win._project_handler.get_active_project_name.return_value = None
        assert "no active project" in win._remote_status_summary().lower()

    def test_no_project_handler_is_honest(self, win):
        win._project_handler = None
        assert "no project data" in win._remote_status_summary().lower()

    def test_summary_includes_project_and_buckets(self, win, monkeypatch):
        win._project_handler.get_project_members.return_value = ["special:coder"]
        monkeypatch.setattr("models.work_store.list_all", list)
        out = win._remote_status_summary()
        assert "Proj" in out
        assert "Work units:" in out


# ── SP4 B5: documentation presence (source-shape guards) ─────────────────

class TestDocsPresence:
    def test_architecture_documents_telegram(self):
        import pathlib

        src = pathlib.Path("docs/ARCHITECTURE.md").read_text()
        assert "transport/telegram.py" in src
        assert "TelegramBridgeHandler" in src

    def test_spec05_register_closed(self):
        import pathlib

        src = pathlib.Path("docs/specs/SPEC-05-R1-GATEWAY-STRIP.md").read_text()
        assert "CLOSED" in src
        assert "SPEC-15" in src

    def test_readme_has_remote_in_bullet(self):
        import pathlib

        src = pathlib.Path("README.md").read_text()
        assert "Remote-In from Your Phone" in src
        assert "thin client" in src
