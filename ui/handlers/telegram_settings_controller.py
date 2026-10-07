# ui/handlers/telegram_settings_controller.py — Telegram Bridge settings logic (SPEC-15 SP2).
#
# Pure logic — owns the Settings → "Telegram Bridge" section's non-widget work:
# token persistence, the off-thread getMe test, and the one-shot pairing poll.
# Mirrors ui/handlers/settings_handler.py's discipline: every async result is
# marshalled back to the main thread through an injected GLib seam
# (`GLib_module`; None = synchronous, for tests).
#
# Manifest:
#   - Reads:  <config_dir>/telegram_bridge.yaml (via utils.telegram_store)
#   - Writes: <config_dir>/telegram_bridge.yaml (atomic, chmod 0o600)
#   - Network: yes — getMe (Test) + getUpdates (Pairing), both off the UI thread
#   - Imports: stdlib + httpx + transport.telegram + utils.telegram_store
#   - Does NOT import gi.repository.Gtk — only optionally uses GLib for idle_add.
#
# Security: the bot token is a credential. It is NEVER logged, echoed, or
# placed in a user-facing message; every error string routes through
# transport.telegram.redact_log_preview (scrubs /bot<token> and token: forms).

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from transport.telegram import TelegramTransport, redact_log_preview
from utils import telegram_store

_logger = logging.getLogger(__name__)

_TELEGRAM_API_BASE = "https://api.telegram.org"

# ~60s pairing window (SPEC-15 §5 "Pairing code timeout"; the Supervisor's SP2
# instruction names ~60s). PM ruling (SP2 fix round): keep 60s — fast feedback
# beats a 5-minute honeypot window; 60s per SPEC-15 §5 — shorter window =
# smaller pairing-claim surface. The confirm dialog is the human safety gate —
# the first inbound message is OFFERED, never auto-paired.
_PAIRING_TIMEOUT_SEC = 60.0
# Bounded wait for the transport thread to observe the stop flag.
_PAIRING_TICK_SEC = 0.5


class TelegramSettingsController:
    """Settings-section logic for the Telegram bridge.

    Security note (BUG#4, SP2 audit — pairing claim race): pairing accepts the
    FIRST message that arrives during the pairing window as the paired chat —
    whoever messages the bot first wins. There is no cryptographic identity
    check; the human confirm dialog is the only gate. Start pairing only in a
    private setting, and treat a bot token as a credential: an attacker who
    learns it within the window could claim the bridge.

    REGISTER (SPEC-15 follow-up): a full identity check — a challenge/response
    or a pre-shared pairing code echoed back to the phone — is DEFERRED. When
    it lands, it belongs here in start_pairing/_on_pairing_update, gating the
    candidate before it is offered to the confirm dialog.

    Args:
        GLib_module: gi.repository.GLib — for idle_add dispatch of async
            results. None → callbacks fire synchronously (test mode).
        transport_factory: (**kwargs) -> TelegramTransport-like. Injected so
            tests drive pairing without network. Default constructs the real
            transport.
        client_factory: () -> httpx.Client-like. Injected for the getMe test.
            Default builds a real client at the Telegram API base.
        pairing_timeout_sec: pairing window length (tests shorten it).
    """

    def __init__(
        self,
        *,
        GLib_module: Any = None,
        transport_factory: Callable[..., Any] | None = None,
        client_factory: Callable[[], Any] | None = None,
        pairing_timeout_sec: float = _PAIRING_TIMEOUT_SEC,
    ) -> None:
        self._GLib = GLib_module
        self._transport_factory = transport_factory or (
            lambda **kw: TelegramTransport(**kw)
        )
        self._client_factory = client_factory or self._default_client_factory
        self._pairing_timeout_sec = pairing_timeout_sec
        self._pairing_transport: Any | None = None
        self._pairing_stop = threading.Event()
        self._pairing = False

    # ── state / reads ────────────────────────────────────────────────────

    def load(self) -> dict[str, Any]:
        """Current stored bridge config (never raises, never logs the token)."""
        return telegram_store.load_bridge_config()

    def paired_label(self, data: dict[str, Any] | None = None) -> str:
        """Human label for the paired-chat row: '@handle (chat_id)' or
        'Not paired'."""
        cfg = data if data is not None else self.load()
        chat_id = cfg.get("chat_id")
        if chat_id is None:
            return "Not paired"
        handle = (cfg.get("paired_handle") or "").strip()
        return f"{handle} ({chat_id})" if handle else f"({chat_id})"

    def has_saved_token(self, data: dict[str, Any] | None = None) -> bool:
        """True when a non-empty token is persisted (gates Start Pairing)."""
        cfg = data if data is not None else self.load()
        return bool((cfg.get("bot_token") or "").strip())

    # ── save ─────────────────────────────────────────────────────────────

    def save_token(self, token: str) -> None:
        """Persist the token, PRESERVING any existing pairing (chat_id/handle).

        Saving a token must never silently unpair a configured bridge. The
        token is written atomically with 0600 perms by the store.
        """
        cfg = self.load()
        telegram_store.save_bridge_config(
            bot_token=(token or "").strip(),
            chat_id=cfg.get("chat_id"),
            paired_handle=cfg.get("paired_handle") or "",
        )

    # ── Test (getMe) ─────────────────────────────────────────────────────

    def test_token(
        self, token: str, on_result: Callable[[bool, str], None]
    ) -> None:
        """Validate a token via getMe in a daemon thread; dispatch (ok, message).

        Mirrors SettingsHandler.test_provider. The message NEVER contains the
        raw token — success shows the bot handle, failure shows a redacted
        error.
        """
        def _worker() -> None:
            cleaned = (token or "").strip()
            if not cleaned:
                self._dispatch_result(on_result, False, "Enter a bot token first.")
                return
            ok, message = self._get_me(cleaned)
            self._dispatch_result(on_result, ok, message)

        threading.Thread(
            target=_worker, daemon=True, name="telegram-settings-test"
        ).start()

    def _get_me(self, token: str) -> tuple[bool, str]:
        """One getMe call → (ok, display_message). Never returns the token."""
        try:
            client = self._client_factory()
        except Exception as e:  # noqa: BLE001 — client construction is external
            return False, f"Could not start HTTP client: {redact_log_preview(str(e))}"
        try:
            resp = client.post(f"/bot{token}/getMe", json={})
            data = resp.json()
        except Exception as e:  # noqa: BLE001 — network failure is expected
            return False, f"Could not reach Telegram: {redact_log_preview(str(e))}"
        finally:
            try:
                client.close()
            except Exception as e:  # noqa: BLE001 — best-effort close
                _logger.debug("error closing getMe client: %s", redact_log_preview(str(e)))
        if getattr(resp, "status_code", 200) == 401 or not (
            isinstance(data, dict) and data.get("ok")
        ):
            return False, "Token rejected by Telegram (invalid or revoked)."
        result = data.get("result")
        username = ""
        if isinstance(result, dict):
            username = str(result.get("username") or result.get("first_name") or "")
        if username:
            return True, f"Token valid — bot @{username}"
        return True, "Token valid — bot authenticated."

    def _dispatch_result(
        self, on_result: Callable[[bool, str], None], ok: bool, message: str
    ) -> None:
        def _run() -> None:
            try:
                on_result(ok, message)
            except Exception:  # a UI callback must not kill the worker
                _logger.exception("telegram test on_result callback raised")

        self._dispatch(_run)

    # ── Pairing ──────────────────────────────────────────────────────────

    def is_pairing(self) -> bool:
        return self._pairing

    def start_pairing(
        self,
        on_candidate: Callable[[int, str], None],
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """Enter pairing mode: poll for the first inbound message.

        A throwaway TelegramTransport long-polls getUpdates for
        `pairing_timeout_sec`; the first message's chat id + sender handle is
        dispatched to on_candidate (main thread) for a human confirm. Fail-closed
        when no token is saved. No-op if already pairing.
        """
        if self._pairing:
            return  # already pairing — second click is a no-op
        cfg = self.load()
        token = (cfg.get("bot_token") or "").strip()
        if not token:
            self._dispatch_error(on_error, "Save a bot token first.")
            return
        self._pairing = True
        self._pairing_stop.clear()
        try:
            transport = self._transport_factory(
                token=token,
                on_connect=lambda: None,
                on_disconnect=lambda _reason: None,
                on_error=lambda message: self._on_pairing_error(message, on_error),
                on_update=lambda update: self._on_pairing_update(update, on_candidate),
            )
        except Exception as e:  # noqa: BLE001 — transport construction is external
            self._pairing = False
            self._dispatch_error(
                on_error, f"Pairing setup failed: {redact_log_preview(str(e))}"
            )
            return
        self._pairing_transport = transport
        threading.Thread(
            target=self._pairing_run,
            args=(transport, on_error),
            daemon=True,
            name="telegram-pairing",
        ).start()

    def _pairing_run(
        self, transport: Any, on_error: Callable[[str], None] | None
    ) -> None:
        """Transport thread: connect, then hold the window until a candidate
        arrives, the user cancels, or the timeout expires."""
        try:
            asyncio.run(transport.connect())
        except Exception as e:  # noqa: BLE001 — any connect failure ends pairing
            self._finish_pairing()
            self._dispatch_error(
                on_error, f"Pairing failed: {redact_log_preview(str(e))}"
            )
            return
        deadline = time.monotonic() + self._pairing_timeout_sec
        while self._pairing and time.monotonic() < deadline:
            time.sleep(_PAIRING_TICK_SEC)
        if self._pairing:  # window expired with no message
            self._finish_pairing()
            self._dispatch_error(on_error, "Pairing timed out — no message received.")

    def _on_pairing_update(
        self, update: Any, on_candidate: Callable[[int, str], None]
    ) -> None:
        """Transport-thread callback: capture the first English-agnostic
        message's chat id + handle, stop polling, and offer the confirm."""
        if not self._pairing or not isinstance(update, dict):
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return  # only plain messages pair (callback_query/etc. ignored)
        chat = message.get("chat")
        if not isinstance(chat, dict) or chat.get("id") is None:
            return
        try:
            chat_id = int(chat["id"])
        except (TypeError, ValueError):
            return
        sender = message.get("from")
        sender = sender if isinstance(sender, dict) else {}
        username = sender.get("username") or chat.get("username") or ""
        handle = f"@{username}" if username else ""
        self._pairing = False
        self._pairing_stop.set()
        self._teardown_async()
        self._dispatch(lambda: on_candidate(chat_id, handle))

    def _on_pairing_error(
        self, message: str, on_error: Callable[[str], None] | None
    ) -> None:
        if not self._pairing:
            return
        self._finish_pairing()
        self._dispatch_error(
            on_error, f"Pairing error: {redact_log_preview(str(message))}"
        )

    def confirm_pairing(self, chat_id: int, handle: str) -> bool:
        """Persist the confirmed pairing (token preserved). False if no token."""
        cfg = self.load()
        token = (cfg.get("bot_token") or "").strip()
        if not token:
            return False
        telegram_store.save_bridge_config(
            bot_token=token, chat_id=int(chat_id), paired_handle=handle or ""
        )
        return True

    def cancel_pairing(self) -> None:
        """Leave pairing mode and tear the throwaway transport down. Idempotent
        and safe to call from the dialog's close-request handler."""
        self._pairing = False
        self._pairing_stop.set()
        self._teardown_async()

    def _finish_pairing(self) -> None:
        self._pairing = False
        self._pairing_stop.set()
        self._teardown_async()

    def _teardown_async(self) -> None:
        """Disconnect the throwaway transport on a fresh daemon thread.

        Never called on the transport's own poll thread (asyncio.run there
        would nest event loops) and never blocks the caller (dialog close).
        """
        transport = self._pairing_transport
        self._pairing_transport = None
        if transport is None:
            return

        def _run() -> None:
            try:
                asyncio.run(transport.disconnect())
            except Exception as e:  # noqa: BLE001 — teardown is best-effort
                _logger.debug(
                    "pairing teardown failed: %s", redact_log_preview(str(e))
                )

        threading.Thread(
            target=_run, daemon=True, name="telegram-pairing-teardown"
        ).start()

    # ── internals ────────────────────────────────────────────────────────

    def _dispatch(self, fn: Callable[[], None]) -> None:
        if self._GLib is not None and hasattr(self._GLib, "idle_add"):
            self._GLib.idle_add(fn)
        else:
            fn()  # test path: synchronous

    def _dispatch_error(
        self, on_error: Callable[[str], None] | None, message: str
    ) -> None:
        if on_error is None:
            return
        self._dispatch(lambda: on_error(message))

    @staticmethod
    def _default_client_factory() -> Any:
        return httpx.Client(base_url=_TELEGRAM_API_BASE, timeout=15.0)
