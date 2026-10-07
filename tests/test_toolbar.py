# tests/test_toolbar.py
# Tests for ui/toolbar.py — Settings button + red status dot.
#
# These tests construct the Toolbar widget directly. No window parent needed
# for construction; tests only inspect widget properties and click handlers.

import pytest

# gtk may not be importable on all CI environments — skip the module if not
try:
    from gi.repository import Gtk
    GTK_AVAILABLE = True
except (ImportError, ValueError):
    GTK_AVAILABLE = False

from ui.toolbar import Toolbar


pytestmark = pytest.mark.skipif(not GTK_AVAILABLE, reason="GTK not available")


class TestToolbarConstruction:
    def test_constructs_without_crash(self):
        t = Toolbar()
        assert t is not None

    def test_has_settings_button(self):
        t = Toolbar()
        assert hasattr(t, "_settings_btn")
        assert t._settings_btn.get_label() == "⚙ Settings"
        assert "settings-toolbar-btn" in t._settings_btn.get_css_classes()

    def test_status_dot_starts_hidden(self):
        t = Toolbar()
        assert hasattr(t, "_status_dot")
        assert t._status_dot.get_visible() is False


class TestSettingsClickCallback:
    def test_callback_fires_on_click(self):
        fired = []
        t = Toolbar(on_settings_clicked=lambda: fired.append(True))
        t._on_settings_click(None)  # simulate click
        assert fired == [True]

    def test_no_callback_no_crash(self):
        t = Toolbar()  # no on_settings_clicked
        t._on_settings_click(None)  # must not raise
        assert True


class TestConnectClickDoesNotClobberState:
    """SPEC-15 SP2: _on_connect_click must not overwrite the label the
    window's callback just set (the old unconditional '● No transport'
    post-click markup clobbered the 'connecting' state)."""

    def test_callback_state_survives_click(self):
        def _on_connect():
            t.set_telegram_bridge_state("connecting")

        t = Toolbar(on_connect_clicked=_on_connect)
        t._on_connect_click(None)
        assert "Connecting" in t._connect_btn.get_label()
        assert "Connecting" in t._status_label.get_text()

    def test_click_delegates_and_no_callback_is_safe(self):
        fired = []
        t = Toolbar(on_connect_clicked=lambda: fired.append(True))
        t._on_connect_click(None)
        assert fired == [True]
        Toolbar()._on_connect_click(None)  # must not raise


class TestSetSettingsStatus:
    def test_unverified_shows_dot(self):
        t = Toolbar()
        t.set_settings_status(False)
        assert t._status_dot.get_visible() is True

    def test_verified_hides_dot(self):
        t = Toolbar()
        t.set_settings_status(True)
        assert t._status_dot.get_visible() is False

    def test_toggle_back_and_forth(self):
        t = Toolbar()
        t.set_settings_status(False)  # show
        assert t._status_dot.get_visible() is True
        t.set_settings_status(True)   # hide
        assert t._status_dot.get_visible() is False
        t.set_settings_status(False)  # show again
        assert t._status_dot.get_visible() is True


class TestExistingBehaviorPreserved:
    """Make sure the new button didn't break the old ones."""

    def test_connect_button_still_present(self):
        t = Toolbar()
        assert t._connect_btn.get_label() == "Connect"

    def test_status_label_still_present(self):
        t = Toolbar()
        assert hasattr(t, "_status_label")


# ═══════════════ SPEC-09 SP3: ■ Stop All button ══════════════════════════════

class TestStopAllButton:
    """■ Stop All — destructive styling, callback wiring (steel Rule 4/5:
    the click handler is exercised, not just the attribute's existence)."""

    def test_stop_all_button_present_and_destructive(self):
        t = Toolbar()
        assert hasattr(t, "_stop_all_btn")
        assert t._stop_all_btn.get_label() == "■ Stop All"
        assert "destructive-action" in t._stop_all_btn.get_css_classes()

    def test_stop_all_click_fires_callback(self):
        fired = []
        t = Toolbar(on_stop_all_clicked=lambda: fired.append(True))
        t._on_stop_all_click(None)  # simulate the click signal
        assert fired == [True]

    def test_stop_all_no_callback_no_crash(self):
        t = Toolbar()  # no callback wired
        t._on_stop_all_click(None)  # must not raise

    def test_other_buttons_still_work_alongside(self):
        fired = []
        t = Toolbar(on_settings_clicked=lambda: fired.append("settings"),
                    on_stop_all_clicked=lambda: fired.append("stop"))
        t._on_settings_click(None)
        t._on_stop_all_click(None)
        assert fired == ["settings", "stop"]


# ═══════════════ SPEC-15 SP2: Telegram bridge toolbar states ══════════════════

class TestTelegramBridgeState:
    """set_telegram_bridge_state drives the button label + status markup."""

    def _label(self, t):
        return t._connect_btn.get_label()

    def test_unconfigured(self):
        t = Toolbar()
        t.set_telegram_bridge_state("unconfigured")
        assert self._label(t) == "Connect"
        assert "No transport" in t._status_label.get_text()
        assert "suggested-action" in t._connect_btn.get_css_classes()

    def test_connecting(self):
        t = Toolbar()
        t.set_telegram_bridge_state("connecting")
        assert "Connecting" in self._label(t)

    def test_connected(self):
        t = Toolbar()
        t.set_telegram_bridge_state("connected")
        assert self._label(t) == "Disconnect"
        assert "Telegram bridge" in t._status_label.get_text()
        assert "destructive-action" in t._connect_btn.get_css_classes()

    def test_error(self):
        t = Toolbar()
        t.set_telegram_bridge_state("connected")
        t.set_telegram_bridge_state("error")
        assert self._label(t) == "Connect"
        assert "Offline" in t._status_label.get_text()
        assert "destructive-action" not in t._connect_btn.get_css_classes()

    def test_disconnected_default(self):
        t = Toolbar()
        t.set_telegram_bridge_state("disconnected")
        assert self._label(t) == "Connect"
