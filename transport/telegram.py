# transport/telegram.py — Telegram long-poll transport (SPEC-15 SP1).
#
# The Transport ABC's FIRST real second implementation (base.py; SPEC-05
# decision #1 pre-ruled the reuse). Telegram is HTTP long-polling, NOT
# WebSocket: this mirrors transport/openclaw.py's PATTERNS (thread + own
# event loop, reconnect-with-backoff, redacted log previews, drain-on-close,
# signal-thread discipline) WITHOUT importing or reusing its WebSocket
# internals.
#
# Scope (SPEC-15 §3 SP1): pure transport, no UI. connect() validates the bot
# token via getMe (fail-closed: bad token → TelegramAuthError, NO poll loop).
# The poll loop long-polls getUpdates (timeout=50, allowed_updates
# message/callback_query), tracks the offset (last update_id + 1), and backs
# off on 429 (respecting retry_after), 5xx, and network errors. send_message /
# answer_callback_query / edit_message_text send PLAIN text — parse_mode is
# deliberately NOT set (HTML rendering is OUR surface's job; the bridge strips
# to text per SPEC-15 §5, the transport sends verbatim).
#
# Threading: the poll loop runs on a daemon thread; status_signals callbacks
# (on_connect/on_disconnect/on_error) and on_update fire FROM THAT THREAD.
# Consumers marshal to the UI thread themselves (same discipline as
# openclaw.py — the GLib.idle_add coupling was removed there and is NOT
# reintroduced here).
#
# Testability: ALL network I/O goes through the injected httpx client
# (`client_factory`, default builds httpx.Client(base_url=...)). `_sleep` is
# an overridable seam so backoff tests do not actually sleep.

import asyncio
import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from transport.base import Transport

_logger = logging.getLogger(__name__)

# Reconnect backoff (mirrors openclaw.py's shape/values).
_BACKOFF_INITIAL_SEC = 1.0
_BACKOFF_MAX_SEC = 30.0
_BACKOFF_MULTIPLIER = 2.0

# Bounded wait inside connect() for the poll thread to go live.
CONNECT_TIMEOUT_SEC = 10.0

# Telegram long-poll: the getUpdates `timeout` param (seconds the server may
# hold the request open) — SPEC-15 §2.
LONG_POLL_TIMEOUT_SEC = 50

# getUpdates is limited to the two update kinds the bridge consumes.
ALLOWED_UPDATES = ("message", "callback_query")

_TELEGRAM_API_BASE = "https://api.telegram.org"

# SPEC-15 §2: the bot token is a credential — it must NEVER appear in logs or
# exceptions. It rides in the URL PATH (`/bot<token>/method`), which the
# openclaw redactor does not know about, so we scrub it explicitly here.
#
# SPEC-15b A3: this helper scrubs APP-AUTHORED lines only. The httpx/httpcore
# LIBRARIES log the request URL themselves at INFO/DEBUG (the raw-token leak
# observed in a live session). That path is covered by utils/log_redaction
# (a RedactingFilter attached to httpx/httpcore by main.py) plus main.py's
# level floor for those loggers — so the §2 guarantee holds library-side too.
_TOKEN_IN_PATH_RE = re.compile(r"/bot[^/\s]+")


def redact_log_preview(raw: str) -> str:
    """Scrub the bot token (and the openclaw sensitive keys) from a log line.

    The token is embedded in the Telegram URL path as `/bot<token>` — replaced
    with `/bot***`. The openclaw key/qs/bearer scrubbing is mirrored so the
    same helper shape serves both transports. YAML/plain `key: value` forms
    (e.g. a PyYAML parse-error dump quoting the source file) are scrubbed too
    — the error text can echo the raw file, token included.
    """
    out = _TOKEN_IN_PATH_RE.sub("/bot***", raw)
    for key in ("apiKey", "apikey", "api_key", "token", "bot_token",
                "password", "secret"):
        out = re.sub(
            rf'("{re.escape(key)}"\s*:\s*)"?[^"\s,}}=&]+',
            r'\1"***"', out, flags=re.IGNORECASE,
        )
        out = re.sub(
            rf'([?&]{re.escape(key)}=)[^&"\s]+',
            r'\1***', out, flags=re.IGNORECASE,
        )
        # YAML / bare `key: value` (incl. indented lines from error dumps).
        out = re.sub(
            rf'(^\s*{re.escape(key)}\s*:\s*)\S+',
            r'\1***', out, flags=re.IGNORECASE | re.MULTILINE,
        )
    out = re.sub(r"(Bearer\s+)[^\s\"}]+", r"\1***", out, flags=re.IGNORECASE)
    return out


class TelegramAuthError(Exception):
    """Raised when connect() cannot authenticate the bot token (fail-closed)."""


class TelegramTransport(Transport):
    """Telegram long-polling transport (the ABC's second implementation).

    Args:
        token: bot token (`<id>:<secret>`); the credential.
        on_connect/on_disconnect/on_error: status_signals callbacks — fired
            FROM THE POLL THREAD (consumers marshal).
        on_update: called with each raw Telegram update dict (from the poll
            thread).
        chat_id: the paired chat; send_message uses it as the default target.
        client_factory: () -> httpx.Client-like (injected for tests; default
            builds a real httpx.Client at the Telegram API base).
        poll_timeout: getUpdates long-poll timeout seconds.
    """

    def __init__(
        self,
        token: str,
        on_connect: Callable[[], None] | None = None,
        on_disconnect: Callable[[str], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_update: Callable[[dict], None] | None = None,
        chat_id: int | None = None,
        client_factory: Callable[[], Any] | None = None,
        poll_timeout: int = LONG_POLL_TIMEOUT_SEC,
    ) -> None:
        self.token = token
        self.on_connect = on_connect if on_connect is not None else lambda: None
        self.on_disconnect = on_disconnect if on_disconnect is not None else lambda r: None
        self.on_error = on_error if on_error is not None else lambda m: None
        self.on_update = on_update if on_update is not None else lambda u: None
        self.chat_id = chat_id
        self.poll_timeout = poll_timeout
        self._client_factory = client_factory if client_factory is not None else self._default_client
        self._client: Any | None = None
        self._running = False
        self._stopping = False
        self._connected = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset: int | None = None
        self._sleep: Callable[[float], None] = time.sleep
        self._lock = threading.Lock()

    # ── Transport ABC ────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Validate the token (getMe) then start the long-poll loop.

        Fail-closed: no token or a failed getMe → TelegramAuthError, NO poll
        loop started. Bounded wait for the thread to go live (CONNECT_TIMEOUT
        _SEC); on timeout fires on_error and returns (not connected).
        """
        if self._thread is not None and self._thread.is_alive():
            return  # already running — idempotent
        if not self.token:
            raise TelegramAuthError("connect: no bot token configured")
        self._client = self._client_factory()
        # getMe validation runs synchronously on the caller's thread (it is a
        # one-shot, non-blocking-relative-to-poll check) — fail-closed BEFORE
        # any thread/loop is spawned.
        client: Any = self._client
        try:
            resp = client.post(f"/bot{self.token}/getMe", json={})
            data = resp.json()
        except Exception as e:  # any transport failure is a connect failure
            # BUG#3 fix: close the just-created client before raising so a
            # failed connect does not leak an httpx connection pool.
            self._close_client()
            raise TelegramAuthError(
                f"connect: getMe failed: {redact_log_preview(str(e))}"
            ) from e
        if resp.status_code == 401 or not data.get("ok"):
            # BUG#3 fix: same cleanup on the auth-rejection path.
            self._close_client()
            raise TelegramAuthError(
                "connect: getMe rejected the token "
                f"(status={resp.status_code})"
            )
        self._stopping = False
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        loop = asyncio.get_running_loop()
        connected = await loop.run_in_executor(
            None, self._connected.wait, CONNECT_TIMEOUT_SEC
        )
        if not connected:
            self._dispatch(  # fires on transport thread — consumers marshal
                self.on_error,
                f"connect: not connected after {CONNECT_TIMEOUT_SEC}s",
                kind="on_error",
            )

    async def disconnect(self) -> None:
        """Stop the poll loop and close the client (idempotent).

        Stops cleanly — the poll thread is joined (bounded) so no orphaned
        loop survives; pending updates drain via the loop's own teardown.
        """
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
            self._running = False
        self._connected.clear()
        # Wake the long-poll promptly: close the client so an in-flight
        # getUpdates returns/raises and the loop sees _running False.
        self._close_client()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._dispatch(self.on_disconnect, "disconnected", kind="on_disconnect")

    async def send(
        self,
        payload: dict,
        *,
        on_response: Callable[[dict], None] | None = None,
    ) -> None:
        """Send one JSON payload via the Telegram `method` in the payload.

        SPEC-15 SP1 keeps the ABC shape: `payload` carries `method` + `params`
        (or a raw callable path). Prefer the explicit helpers
        (send_message / answer_callback_query / edit_message_text); this is
        the ABC-compliant generic entry.
        """
        method = payload.get("method")
        params = payload.get("params", {})
        result = self._api_call(method, params) if method else {"ok": False}
        if on_response is not None:
            self._dispatch(on_response, result, kind="on_response")

    def status_signals(self) -> tuple:
        return (self.on_connect, self.on_disconnect, self.on_error)

    # ── Introspection ────────────────────────────────────────────────────

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def _dispatch(self, callback: Callable, *args: Any, kind: str) -> None:
        """Fire a consumer callback in isolation (BUG#4 fix).

        A consumer callback (bridge handler, feed-card writer, logger) is
        arbitrary code; if it raises, the exception must NOT escape into the
        poll loop and kill the thread. Log and continue — the transport's
        liveness is independent of any single consumer.
        """
        try:
            callback(*args)
        except Exception as e:  # noqa: BLE001 — one bad consumer must not kill the loop
            _logger.error("%s callback raised: %s", kind, redact_log_preview(str(e)))

    # ── Send API surface (plain text; parse_mode NOT set) ────────────────

    def send_message(self, text: str, reply_markup: dict | None = None,
                     chat_id: int | None = None) -> dict:
        """sendMessage — plain text to the paired chat.

        F3 (audit): ``chat_id`` optionally overrides the target chat so the
        bridge can deliver a foreign-chat refusal to the FOREIGN id (None →
        the paired chat, preserving every existing caller). Raises ValueError
        when neither is available. reply_markup passes through verbatim
        (inline-keyboard approvals are built by the SP3 bridge).
        """
        target = chat_id if chat_id is not None else self.chat_id
        if target is None:
            raise ValueError("send_message: no chat_id paired")
        params: dict = {"chat_id": target, "text": text}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        return self._api_call("sendMessage", params)

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> dict:
        """answerCallbackQuery — acknowledge an inline-button tap."""
        params: dict = {"callback_query_id": callback_query_id}
        if text is not None:
            params["text"] = text
        return self._api_call("answerCallbackQuery", params)

    def edit_message_text(self, message_id: int, text: str,
                          reply_markup: dict | None = None) -> dict:
        """editMessageText — resolve an approval message in place."""
        if self.chat_id is None:
            raise ValueError("edit_message_text: no chat_id paired")
        params: dict = {"chat_id": self.chat_id, "message_id": message_id, "text": text}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        return self._api_call("editMessageText", params)

    # ── Internals ────────────────────────────────────────────────────────

    def _default_client(self):
        return httpx.Client(base_url=_TELEGRAM_API_BASE, timeout=self.poll_timeout + 10)

    def _close_client(self) -> None:
        """Best-effort close of the current HTTP client (idempotent).

        BUG#3 fix: connect() calls this on every auth-failure path so a
        failed connect does not leak the connection pool. disconnect() uses
        it too — single close site.
        """
        client = self._client
        if client is None:
            return
        try:
            client.close()
        except Exception as e:  # noqa: BLE001 — best-effort close
            _logger.debug("Error closing Telegram client: %s", redact_log_preview(str(e)))

    def _api_call(self, method: str, params: dict) -> dict:
        """One Telegram API POST; returns the parsed body (fail-quiet)."""
        client = self._client if self._client is not None else self._client_factory()
        self._client = client
        try:
            resp = client.post(f"/bot{self.token}/{method}", json=params)
            return resp.json()
        except Exception as e:  # noqa: BLE001 — callers get a structured failure
            _logger.warning("Telegram %s failed: %s", method, redact_log_preview(str(e)))
            return {"ok": False, "error": redact_log_preview(str(e))}

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._poll_loop())
        finally:
            self._loop.close()

    async def _poll_loop(self) -> None:
        """Long-poll getUpdates with offset tracking + backoff.

        On the FIRST successful getUpdates the link is live → on_connect fires
        and _connected is set. Failures (429/5xx/network) back off and fire
        on_error (redacted); 429 respects the server's retry_after.
        """
        retry_delay = _BACKOFF_INITIAL_SEC
        announced = False
        while self._running:
            try:
                updates = self._get_updates()
            except _Conflict:
                # SPEC-15b B2: a second getUpdates consumer holds the bot.
                # Surface it DISTINCTLY (the generic soft-failure path was
                # silent → permanent "Connecting…"). Name the likely cause:
                # a stale pairing poller or another app using the same token.
                announced = False  # link is down — re-announce on recovery
                self._dispatch(
                    self.on_error,
                    "getUpdates conflict (409) — another consumer is polling "
                    "this bot; close other apps or a stale pairing poller",
                    kind="on_error",
                )
                self._sleep(retry_delay)
                retry_delay = min(retry_delay * _BACKOFF_MULTIPLIER, _BACKOFF_MAX_SEC)
                continue
            except _RetryAfter as ra:
                # F1b (audit): a soft error means the link is DOWN; re-arm the
                # announce latch so the next good getUpdates re-fires on_connect
                # (otherwise the handler stays ERROR forever while we recover).
                announced = False
                self._dispatch(
                    self.on_error,
                    f"rate limited — retrying in {ra.seconds}s",
                    kind="on_error",
                )
                self._sleep(ra.seconds)
                continue
            except Exception as e:  # noqa: BLE001 — poll loop must survive
                announced = False  # F1b: link lost — re-announce on recovery
                self._dispatch(
                    self.on_error,
                    f"poll error: {redact_log_preview(str(e))}",
                    kind="on_error",
                )
                self._sleep(retry_delay)
                retry_delay = min(retry_delay * _BACKOFF_MULTIPLIER, _BACKOFF_MAX_SEC)
                continue
            if updates is None:
                # 5xx / soft failure — back off, keep the loop alive.
                announced = False  # F1b: link lost — re-announce on recovery
                self._dispatch(
                    self.on_error,
                    "getUpdates soft failure — backing off",
                    kind="on_error",
                )
                self._sleep(retry_delay)
                retry_delay = min(retry_delay * _BACKOFF_MULTIPLIER, _BACKOFF_MAX_SEC)
                continue
            if not announced:
                announced = True
                self._connected.set()
                self._dispatch(self.on_connect, kind="on_connect")
            retry_delay = _BACKOFF_INITIAL_SEC  # healthy → reset backoff
            for update in updates:
                uid = update.get("update_id")
                if uid is None:
                    _logger.warning(
                        "malformed update (no update_id): %s",
                        redact_log_preview(str(update)[:200]),
                    )
                    continue
                self._dispatch(self.on_update, update, kind="on_update")
                with self._lock:
                    self._offset = int(uid) + 1  # confirmed after processing

    def _get_updates(self) -> list | None:
        """One getUpdates call. Raises _RetryAfter on 429; returns None on 5xx
        / non-ok soft failure; returns the update list on success."""
        params: dict = {
            "timeout": self.poll_timeout,
            "allowed_updates": list(ALLOWED_UPDATES),
        }
        if self._offset is not None:
            params["offset"] = self._offset
        client = self._client
        if client is None:
            return None
        resp = client.post(f"/bot{self.token}/getUpdates", json=params)
        if resp.status_code == 409:
            # SPEC-15b B1: another getUpdates consumer holds this bot.
            raise _Conflict(
                "getUpdates 409 Conflict — another consumer is polling this bot"
            )
        if resp.status_code == 429:
            body = resp.json()
            retry_after = (
                body.get("parameters", {}).get("retry_after")
                if isinstance(body, dict) else None
            )
            raise _RetryAfter(int(retry_after) if retry_after else _BACKOFF_INITIAL_SEC)
        if resp.status_code >= 500:
            return None
        body = resp.json()
        if not isinstance(body, dict) or not body.get("ok"):
            # SPEC-15b B1: a 200-wrapped conflict body (ok:false,
            # error_code 409) is the same case as HTTP 409.
            if isinstance(body, dict) and body.get("error_code") == 409:
                raise _Conflict(
                    "getUpdates 409 Conflict (body) — another consumer is polling"
                )
            # REGISTER (SP2/SP3): stale offset + persistent 4xx could loop —
            # defer until error codes are modeled (BUG#5, theoretical only:
            # the offset is always int(update_id)+1, monotonically increasing).
            return None
        result = body.get("result", [])
        return result if isinstance(result, list) else []


class _RetryAfter(Exception):
    """Internal: 429 with a server-supplied retry_after (seconds)."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"retry_after={seconds}")
        self.seconds = seconds


class _Conflict(Exception):
    """Internal: getUpdates 409 — another consumer is polling this bot.

    Telegram permits ONE getUpdates consumer per bot. A second poller gets
    HTTP 409 (and/or a connection reset). Mirrors _RetryAfter: raised inside
    _get_updates, surfaced distinctly by _poll_loop.
    """