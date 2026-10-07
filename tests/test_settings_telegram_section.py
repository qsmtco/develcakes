# tests/test_settings_telegram_section.py — SPEC-15 SP2 (settings section).
#
# The Settings dialog grows a sectioned layout: "Providers" (byte-identical
# legacy content) + "Telegram Bridge" (new). These tests pin the new section:
# the stack/switch, token save (0600), the off-thread getMe test result states,
# the reveal toggle, and pairing with a FAKE transport (no network).
#
# Threading: the controller runs its work in daemon threads. To keep the tests
# deterministic the GLib seam is a fake that QUEUES idle callbacks; the test
# runs them on its own (main) thread, exactly as production would.

import os
import stat
import time

import pytest

try:
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
    GTK_AVAILABLE = True
except (ImportError, ValueError):
    GTK_AVAILABLE = False

from models.providers import ProviderConfig
from ui.handlers.settings_handler import SettingsHandler
from ui.handlers.telegram_settings_controller import TelegramSettingsController
from ui.views.settings_dialog import SettingsDialog
from utils import telegram_store

pytestmark = pytest.mark.skipif(not GTK_AVAILABLE, reason="GTK not available")


# ── fakes ────────────────────────────────────────────────────────────────

class QueuingGLib:
    """idle_add QUEUES callbacks; the test drains them on the main thread."""

    def __init__(self):
        self.pending = []

    def idle_add(self, fn, *args, **kwargs):
        self.pending.append((fn, args, kwargs))
        return 1

    def run_pending(self):
        while self.pending:
            fn, args, kwargs = self.pending.pop(0)
            fn(*args, **kwargs)


def _wait(cond, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, resp):
        self._resp = resp
        self.closed = False
        self.posts = []

    def post(self, url, json=None):
        self.posts.append((url, json))
        return self._resp

    def close(self):
        self.closed = True


class FakePairingTransport:
    """Captures the on_update seam so the test can fire an inbound message."""

    def __init__(self, **kw):
        self.kw = kw
        self.disconnected = False
        self.connect_called = False

    async def connect(self):
        self.connect_called = True

    async def disconnect(self):
        self.disconnected = True


def _provider(name="test"):
    return ProviderConfig(
        name=name,
        base_url=f"https://api.{name}.example.com/v1",
        api_key="test-key",
        default_model=f"openai/{name}-model",
    )


def _controller(glib, *, resp=None, transport_factory=None, timeout=30.0):
    client_factory = (lambda: FakeClient(resp)) if resp is not None else None
    return TelegramSettingsController(
        GLib_module=glib,
        client_factory=client_factory,
        transport_factory=transport_factory,
        pairing_timeout_sec=timeout,
    )


# ── sectioned layout ─────────────────────────────────────────────────────

class TestSectionedLayout:
    def test_both_pages_exist(self, tmp_config_dir):
        glib = QueuingGLib()
        d = SettingsDialog(
            parent=None, handler=SettingsHandler(),
            telegram_controller=_controller(glib),
        )
        assert d._stack.get_child_by_name("providers") is not None
        assert d._stack.get_child_by_name("telegram") is not None

    def test_providers_is_default_page(self, tmp_config_dir):
        glib = QueuingGLib()
        d = SettingsDialog(
            parent=None, handler=SettingsHandler(),
            telegram_controller=_controller(glib),
        )
        assert d._stack.get_visible_child_name() == "providers"

    def test_switch_to_telegram(self, tmp_config_dir):
        glib = QueuingGLib()
        d = SettingsDialog(
            parent=None, handler=SettingsHandler(),
            telegram_controller=_controller(glib),
        )
        d._stack.set_visible_child_name("telegram")
        assert d._stack.get_visible_child_name() == "telegram"

    def test_constructor_still_source_compatible_without_controller(
        self, tmp_config_dir
    ):
        """window.py's existing call (no telegram_controller) must still work."""
        d = SettingsDialog(parent=None, handler=SettingsHandler())
        assert d._stack.get_child_by_name("providers") is not None
        assert d._stack.get_child_by_name("telegram") is not None

    def test_unwired_section_is_inert(self, tmp_config_dir):
        """No controller → honest, disabled controls (no dead buttons)."""
        d = SettingsDialog(parent=None, handler=SettingsHandler())
        assert d._telegram_pair_btn.get_sensitive() is False
        assert d._telegram_save_btn.get_sensitive() is False
        assert d._telegram_test_btn.get_sensitive() is False
        assert "not wired" in d._telegram_paired_label.get_text().lower()


# ── token save ───────────────────────────────────────────────────────────

class TestTokenSave:
    def test_save_persists_token_to_0600_yaml(self, tmp_config_dir):
        d = SettingsDialog(
            parent=None, handler=SettingsHandler(),
            telegram_controller=_controller(QueuingGLib()),
        )
        d._telegram_token_entry.set_text("123:abc")
        d._on_telegram_save_clicked(None)
        data = telegram_store.load_bridge_config()
        assert data["bot_token"] == "123:abc"
        path = os.path.join(str(tmp_config_dir.parent), "develcakes",
                            "telegram_bridge.yaml")
        assert os.path.isfile(path)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_reopen_loads_saved_token(self, tmp_config_dir):
        telegram_store.save_bridge_config(bot_token="555:saved")
        d = SettingsDialog(
            parent=None, handler=SettingsHandler(),
            telegram_controller=_controller(QueuingGLib()),
        )
        assert d._telegram_token_entry.get_text() == "555:saved"
        # Masked until the user reveals it.
        assert d._telegram_token_entry.get_visibility() is False

    def test_save_preserves_existing_pairing(self, tmp_config_dir):
        telegram_store.save_bridge_config(
            bot_token="123:abc", chat_id=42, paired_handle="@me"
        )
        d = SettingsDialog(
            parent=None, handler=SettingsHandler(),
            telegram_controller=_controller(QueuingGLib()),
        )
        d._telegram_token_entry.set_text("999:newtoken")
        d._on_telegram_save_clicked(None)
        data = telegram_store.load_bridge_config()
        assert data["bot_token"] == "999:newtoken"
        assert data["chat_id"] == 42  # pairing survives a token re-save
        assert data["paired_handle"] == "@me"


# ── reveal ───────────────────────────────────────────────────────────────

class TestReveal:
    def test_reveal_toggles_visibility(self, tmp_config_dir):
        d = SettingsDialog(
            parent=None, handler=SettingsHandler(),
            telegram_controller=_controller(QueuingGLib()),
        )
        assert d._telegram_token_entry.get_visibility() is False
        d._on_telegram_reveal_clicked(None)
        assert d._telegram_token_entry.get_visibility() is True
        d._on_telegram_reveal_clicked(None)
        assert d._telegram_token_entry.get_visibility() is False


# ── Test button (getMe) result states ────────────────────────────────────

class TestTestButton:
    def test_success_shows_bot_handle_green(self, tmp_config_dir):
        glib = QueuingGLib()
        resp = FakeResp(200, {"ok": True, "result": {"username": "mybot"}})
        d = SettingsDialog(
            parent=None, handler=SettingsHandler(),
            telegram_controller=_controller(glib, resp=resp),
        )
        d._telegram_token_entry.set_text("123:abc")
        d._on_telegram_test_clicked(None)
        assert _wait(lambda: len(glib.pending) > 0), "no result dispatched"
        glib.run_pending()
        text = d._telegram_status_label.get_text()
        assert "mybot" in text
        assert d._telegram_status_label.has_css_class("settings-status-ok")

    def test_failure_shows_error_red(self, tmp_config_dir):
        glib = QueuingGLib()
        resp = FakeResp(401, {"ok": False})
        d = SettingsDialog(
            parent=None, handler=SettingsHandler(),
            telegram_controller=_controller(glib, resp=resp),
        )
        d._telegram_token_entry.set_text("bad:token")
        d._on_telegram_test_clicked(None)
        assert _wait(lambda: len(glib.pending) > 0), "no result dispatched"
        glib.run_pending()
        assert "rejected" in d._telegram_status_label.get_text().lower()
        assert d._telegram_status_label.has_css_class("settings-status-fail")

    def test_result_never_contains_raw_token(self, tmp_config_dir):
        glib = QueuingGLib()
        secret = "999:SUPER-SECRET"
        resp = FakeResp(401, {"ok": False})
        d = SettingsDialog(
            parent=None, handler=SettingsHandler(),
            telegram_controller=_controller(glib, resp=resp),
        )
        d._telegram_token_entry.set_text(secret)
        d._on_telegram_test_clicked(None)
        assert _wait(lambda: len(glib.pending) > 0)
        glib.run_pending()
        assert "SUPER-SECRET" not in d._telegram_status_label.get_text()


# ── pairing (fake transport, no network) ─────────────────────────────────

class TestPairing:
    def test_start_without_token_reports_error(self, tmp_config_dir):
        glib = QueuingGLib()
        ctrl = _controller(glib)
        errors = []
        ctrl.start_pairing(lambda *a: None, errors.append)
        glib.run_pending()
        assert errors and "token" in errors[0].lower()
        assert ctrl.is_pairing() is False

    def test_first_message_is_offered_as_candidate(self, tmp_config_dir):
        """The controller's poll → candidate seam, fake transport."""
        glib = QueuingGLib()
        holder = {}

        def factory(**kw):
            t = FakePairingTransport(**kw)
            holder["t"] = t
            return t

        ctrl = _controller(glib, transport_factory=factory)
        telegram_store.save_bridge_config(bot_token="123:abc")
        candidates = []
        ctrl.start_pairing(lambda cid, handle: candidates.append((cid, handle)))
        assert ctrl.is_pairing() is True
        assert "t" in holder
        # Fire the first inbound message the way the transport thread would.
        holder["t"].kw["on_update"](
            {"message": {"chat": {"id": 42}, "from": {"username": "me"}}}
        )
        assert _wait(lambda: len(glib.pending) > 0)
        glib.run_pending()
        assert candidates == [(42, "@me")]
        assert ctrl.is_pairing() is False

    def test_non_message_update_is_ignored(self, tmp_config_dir):
        glib = QueuingGLib()
        holder = {}

        def factory(**kw):
            t = FakePairingTransport(**kw)
            holder["t"] = t
            return t

        ctrl = _controller(glib, transport_factory=factory)
        telegram_store.save_bridge_config(bot_token="123:abc")
        candidates = []
        ctrl.start_pairing(lambda cid, handle: candidates.append((cid, handle)))
        holder["t"].kw["on_update"]({"callback_query": {"id": "x"}})
        time.sleep(0.05)
        assert candidates == []
        assert ctrl.is_pairing() is True  # still waiting
        ctrl.cancel_pairing()  # stop the (daemon) poll thread before teardown
        assert _wait(lambda: holder["t"].disconnected)

    def test_dialog_pairing_happy_path_saves_chat(self, tmp_config_dir, monkeypatch):
        """Full dialog path: Start Pairing → inbound message → confirm → saved."""
        glib = QueuingGLib()
        holder = {}

        def factory(**kw):
            t = FakePairingTransport(**kw)
            holder["t"] = t
            return t

        ctrl = _controller(glib, transport_factory=factory)
        telegram_store.save_bridge_config(bot_token="123:abc")

        # Auto-confirm the pairing MessageDialog (YES).
        class FakeMsgDialog:
            def __init__(self, **kw):
                self._cb = None

            def set_property(self, *a, **k):
                pass

            def connect(self, _sig, cb):
                self._cb = cb

            def close(self):
                pass

            def show(self):
                self._cb(self, Gtk.ResponseType.YES)

        monkeypatch.setattr(
            "ui.views.settings_dialog.Gtk.MessageDialog", FakeMsgDialog
        )

        d = SettingsDialog(
            parent=None, handler=SettingsHandler(), telegram_controller=ctrl
        )
        d._on_telegram_pair_clicked(None)
        assert "t" in holder
        holder["t"].kw["on_update"](
            {"message": {"chat": {"id": 42}, "from": {"username": "me"}}}
        )
        assert _wait(lambda: len(glib.pending) > 0)
        glib.run_pending()

        data = telegram_store.load_bridge_config()
        assert data["chat_id"] == 42
        assert data["paired_handle"] == "@me"
        assert "paired" in d._telegram_status_label.get_text().lower()

    def test_pairing_confirm_dialog_states_exclusive_identity_warning(
        self, tmp_config_dir, monkeypatch
    ):
        """BUG#4 (SP2 audit): pairing accepts whoever messages first, so the
        confirm dialog must state the identity + exclusivity warning
        explicitly — the human is the only gate."""
        glib = QueuingGLib()
        holder = {}
        captured = {}

        def factory(**kw):
            t = FakePairingTransport(**kw)
            holder["t"] = t
            return t

        ctrl = _controller(glib, transport_factory=factory)
        telegram_store.save_bridge_config(bot_token="123:abc")

        class CapturingDialog:
            def __init__(self, **kw):
                self._cb = None

            def set_property(self, name, value):
                captured[name] = value

            def connect(self, _sig, cb):
                self._cb = cb

            def close(self):
                pass

            def show(self):
                self._cb(self, Gtk.ResponseType.NO)  # decline — no save

        monkeypatch.setattr(
            "ui.views.settings_dialog.Gtk.MessageDialog", CapturingDialog
        )

        d = SettingsDialog(
            parent=None, handler=SettingsHandler(), telegram_controller=ctrl
        )
        d._on_telegram_pair_clicked(None)
        holder["t"].kw["on_update"](
            {"message": {"chat": {"id": 42}, "from": {"username": "me"}}}
        )
        assert _wait(lambda: len(glib.pending) > 0)
        glib.run_pending()

        secondary = captured.get("secondary-text", "")
        assert "only" in secondary.lower()
        assert "supervisor" in secondary.lower()
        assert "confirm only if this is you" in secondary.lower()

    def test_cancel_pairing_tears_down_transport(self, tmp_config_dir):
        glib = QueuingGLib()
        holder = {}

        def factory(**kw):
            t = FakePairingTransport(**kw)
            holder["t"] = t
            return t

        ctrl = _controller(glib, transport_factory=factory)
        telegram_store.save_bridge_config(bot_token="123:abc")
        ctrl.start_pairing(lambda *a: None)
        assert ctrl.is_pairing() is True
        ctrl.cancel_pairing()
        assert ctrl.is_pairing() is False
        # Teardown runs on its own thread; wait for the disconnect.
        assert _wait(lambda: holder["t"].disconnected)

    def test_close_request_cancels_pairing(self, tmp_config_dir):
        glib = QueuingGLib()
        holder = {}

        def factory(**kw):
            t = FakePairingTransport(**kw)
            holder["t"] = t
            return t

        ctrl = _controller(glib, transport_factory=factory)
        telegram_store.save_bridge_config(bot_token="123:abc")
        d = SettingsDialog(
            parent=None, handler=SettingsHandler(), telegram_controller=ctrl
        )
        ctrl.start_pairing(lambda *a: None)
        assert ctrl.is_pairing() is True
        d._on_close_request()
        assert ctrl.is_pairing() is False


# ── controller helpers ───────────────────────────────────────────────────

class TestControllerHelpers:
    def test_paired_label_unpaired(self, tmp_config_dir):
        ctrl = _controller(QueuingGLib())
        telegram_store.save_bridge_config(bot_token="123:abc")
        assert ctrl.paired_label() == "Not paired"

    def test_paired_label_paired(self, tmp_config_dir):
        ctrl = _controller(QueuingGLib())
        telegram_store.save_bridge_config(
            bot_token="123:abc", chat_id=42, paired_handle="@me"
        )
        assert ctrl.paired_label() == "@me (42)"

    def test_has_saved_token_gates_pairing(self, tmp_config_dir):
        ctrl = _controller(QueuingGLib())
        assert ctrl.has_saved_token() is False
        telegram_store.save_bridge_config(bot_token="123:abc")
        assert ctrl.has_saved_token() is True

    def test_token_never_logged_on_test(self, tmp_config_dir, caplog):
        import logging as _logging
        glib = QueuingGLib()
        secret = "999:LEAKY-TOKEN"
        resp = FakeResp(401, {"ok": False})
        ctrl = _controller(glib, resp=resp)
        with caplog.at_level(_logging.DEBUG,
                             logger="ui.handlers.telegram_settings_controller"):
            ctrl.test_token(secret, lambda *a: None)
            assert _wait(lambda: len(glib.pending) > 0)
            glib.run_pending()
        assert "LEAKY-TOKEN" not in caplog.text
