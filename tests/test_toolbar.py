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

    def test_stream_button_still_present(self):
        t = Toolbar()
        assert hasattr(t, "_stream_btn")
        assert "Stream" in t._stream_btn.get_label()

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
