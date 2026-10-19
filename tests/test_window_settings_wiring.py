# tests/test_window_settings_wiring.py
# Tests for ui/wiring.py — verifies that the SettingsHandler's callbacks
# are correctly wired to the toolbar and the settings dialog factory.
# No GTK widgets required — we use simple stubs.

import pytest

from ui.handlers.settings_handler import SettingsHandler
from ui.wiring import wire_settings_handler

from models.providers import ProviderConfig


class StubToolbar:
    """Captures set_settings_status calls."""
    def __init__(self):
        self.status_calls: list[bool] = []
    def set_settings_status(self, has_verified: bool) -> None:
        self.status_calls.append(has_verified)


class StubDialog:
    """Captures refresh_providers calls."""
    def __init__(self):
        self.refresh_calls: list[list] = []
    def refresh_providers(self, providers: list) -> None:
        self.refresh_calls.append(list(providers))


def _make_provider(name: str = "test", **overrides) -> ProviderConfig:
    defaults = dict(
        name=name,
        base_url=f"https://api.{name}.example.com/v1",
        api_key="test-key",
        default_model=f"openai/{name}-model",
    )
    defaults.update(overrides)
    return ProviderConfig(**defaults)


class TestWireSettingsHandler:
    def test_returns_the_handler(self, tmp_config_dir):
        h = SettingsHandler()
        t = StubToolbar()
        result = wire_settings_handler(h, t)
        assert result is h

    def test_sets_initial_toolbar_status(self, tmp_config_dir):
        h = SettingsHandler()
        t = StubToolbar()
        wire_settings_handler(h, t)
        # No providers → status is False
        assert t.status_calls == [False]

    def test_initial_status_true_when_verified_provider_exists(self, tmp_config_dir, monkeypatch):
        from ui.handlers import settings_handler as sh
        from utils.provider_test import TestResult
        monkeypatch.setattr(sh, "test_connection", lambda **kw:
            TestResult(ok=True, latency_ms=1, error=None, model_used=kw["model"]))
        h = SettingsHandler()
        p = _make_provider("p")
        h.add_or_update(p)
        # Now test the provider to set last_verified_at
        import threading
        event = threading.Event()
        h.test_provider(p, lambda r: event.set())
        assert event.wait(timeout=2.0)
        t = StubToolbar()
        wire_settings_handler(h, t)
        assert t.status_calls == [True]


class TestOnStatusChanged:
    def test_add_fires_status_changed(self, tmp_config_dir):
        h = SettingsHandler()
        t = StubToolbar()
        wire_settings_handler(h, t)
        t.status_calls.clear()
        h.add_or_update(_make_provider("p"))
        # After add, status should be False (no verified yet)
        assert t.status_calls[-1] is False

    def test_remove_fires_status_changed(self, tmp_config_dir):
        h = SettingsHandler()
        h.add_or_update(_make_provider("p"))
        t = StubToolbar()
        wire_settings_handler(h, t)
        t.status_calls.clear()
        h.remove("p")
        assert t.status_calls[-1] is False


class TestOnProvidersChanged:
    def test_add_fires_providers_changed_with_factory(self, tmp_config_dir):
        h = SettingsHandler()
        t = StubToolbar()
        dlg = StubDialog()
        factory = lambda: dlg
        wire_settings_handler(h, t, settings_dialog_factory=factory)
        h.add_or_update(_make_provider("p1"))
        # StubDialog should have received the new list
        assert len(dlg.refresh_calls) == 1
        assert len(dlg.refresh_calls[0]) == 1
        assert dlg.refresh_calls[0][0].name == "p1"

    def test_remove_fires_providers_changed_with_factory(self, tmp_config_dir):
        h = SettingsHandler()
        h.add_or_update(_make_provider("p1"))
        t = StubToolbar()
        dlg = StubDialog()
        factory = lambda: dlg
        wire_settings_handler(h, t, settings_dialog_factory=factory)
        h.remove("p1")
        assert len(dlg.refresh_calls) >= 1
        # Last refresh should have empty list
        assert dlg.refresh_calls[-1] == []

    def test_no_factory_is_safe(self, tmp_config_dir):
        h = SettingsHandler()
        t = StubToolbar()
        wire_settings_handler(h, t, settings_dialog_factory=None)
        # Adding a provider should not crash even with no dialog factory
        h.add_or_update(_make_provider("p"))
        # Status callback should still fire
        assert t.status_calls[-1] is False

    def test_factory_returning_none_is_safe(self, tmp_config_dir):
        h = SettingsHandler()
        t = StubToolbar()
        factory = lambda: None  # dialog not open
        wire_settings_handler(h, t, settings_dialog_factory=factory)
        h.add_or_update(_make_provider("p"))
        # Should not crash; status callback should still fire
        assert t.status_calls[-1] is False

    def test_on_providers_changed_updates_agent_builder(self, tmp_config_dir):
        """When providers change and the agent builder is open, set_provider_options is called."""
        from unittest.mock import MagicMock

        builder = MagicMock()
        h = SettingsHandler()
        h = wire_settings_handler(
            h, MagicMock(),
            settings_dialog_factory=lambda: None,
            agent_builder_factory=lambda: builder,
        )

        providers = [{"name": "openai", "base_url": "u", "default_model": "gpt-4o"}]
        h._on_providers_changed(providers)

        builder.set_provider_options.assert_called_once()
        args, _ = builder.set_provider_options.call_args
        assert len(args) == 1
        # First arg is the providers list — passed through as-is from the handler
        assert len(args[0]) == 1
        assert args[0][0]["name"] == "openai"


# ── GTK availability for lifecycle tests ──────────────────────────────
try:
    import gi
    gi.require_version('Gtk', '4.0')
    from gi.repository import Gtk as _Gtk
    _GTK_AVAILABLE = True
except (ImportError, ValueError):
    _GTK_AVAILABLE = False


@pytest.mark.skipif(not _GTK_AVAILABLE, reason="GTK not available")
class TestSettingsPaneLifecycle:
    """The settings surface is one pane for the window, not a fresh dialog per open."""

    def test_window_hosts_one_pane(self, tmp_config_dir):
        import pathlib
        from unittest.mock import MagicMock

        from ui.handlers.settings_handler import SettingsHandler
        from ui.views.settings_dialog import SettingsDialog

        src = pathlib.Path("ui/window.py").read_text()
        assert "set_settings_pane(" in src
        assert "def _open_settings" not in src

        pane = SettingsDialog(parent=None, handler=SettingsHandler())
        assert pane.widget() is not None
        assert not hasattr(pane, "_window")
        pane.release()

        controller = MagicMock()
        controller.load.return_value = {
            "bot_token": "", "chat_id": None, "paired_handle": "",
        }
        controller.paired_label.return_value = "Not paired"
        controller.has_saved_token.return_value = False
        wired = SettingsDialog(
            parent=None,
            handler=SettingsHandler(),
            telegram_controller=controller,
        )
        wired.release()
        controller.cancel_pairing.assert_called_once()
