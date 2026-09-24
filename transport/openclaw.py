# transport/openclaw.py — cleaned WebSocket transport core (SPEC-05 Sub-Phase 1).
#
# Extracted from gateway/client.py per SPEC-05 R1 gateway strip (decision #1:
# transport core retained and cleaned for reuse by the future Telegram bot;
# OpenClaw protocol and identity/auth removed; architecture ruling #7: new
# `transport/` package).
#
# KEPT from gateway/client.py (connection machinery only):
#   threaded async run loop, reconnect-with-backoff connect loop, message
#   framing (JSON req/res correlation with pending-RPC registry + timeouts),
#   keepalive tick loop, redacted log previews, pending drain/expire on
#   close, graceful stop.
#
# DELETED (OpenClaw protocol / auth — see the extraction report in
# tests/test_transport_package.py pins and the SP1 delivery report):
#   device identity loading (~/.openclaw/identity/device-auth.json,
#   key-pair load/sign), the v3 device-auth connect sequence, snapshot
#   validation, auth scopes, the OpenClaw event catalog (event names,
#   chat.send framing, snapshot handling), agent-manager coupling.
#
# DORMANT in Sub-Phase 1: zero callers. SP2 repoints send sites.

import asyncio
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from transport.base import Transport

_logger = logging.getLogger(__name__)

# Reconnect backoff (kept from client.py _connect_loop):
_BACKOFF_INITIAL_SEC = 1.0
_BACKOFF_MAX_SEC = 30.0
_BACKOFF_MULTIPLIER = 2.0

# RPC timeout (kept from client.py): per-request pending-callback deadline.
RPC_TIMEOUT_SEC = 30.0

# Keepalive tick interval (kept from client.py _tick_loop).
TICK_INTERVAL_SEC = 15.0

# FIX 3: bounded wait inside connect() for the socket to go live.
CONNECT_TIMEOUT_SEC = 10.0

# LOW-4 (kept): sensitive keys to redact from transport log previews.
_REDACT_KEYS = (
    "apiKey", "apikey", "api_key",
    "token", "deviceToken", "device_token",
    "password", "secret",
)


def redact_log_preview(raw: str) -> str:
    """Replace sensitive values with *** in a log preview.

    Kept verbatim in behavior from gateway/client.py
    (_redact_gateway_log_preview): JSON-style keys, URL query-string keys,
    and Authorization: Bearer headers.
    """
    out = raw
    for key in _REDACT_KEYS:
        json_pattern = re.compile(
            rf'("{re.escape(key)}"\s*:\s*)"?[^"\s,}}=&]+',
            re.IGNORECASE,
        )
        out = json_pattern.sub(r'\1"***"', out)
        qs_pattern = re.compile(
            rf'(?:([?&])({re.escape(key)})=[^&"\s]+)',
            re.IGNORECASE,
        )
        out = qs_pattern.sub(lambda m: m.group(1) + m.group(2) + '=***', out)
    bearer_pattern = re.compile(r'(Bearer\s+)([^\s"}]+)', re.IGNORECASE)
    out = bearer_pattern.sub(r'\1***', out)
    return out


class WebSocketTransport(Transport):
    """Threaded async WebSocket transport — the cleaned connection core.

    This is the retained/generic half of GatewayClient: connection
    lifecycle, reconnect-with-backoff, JSON framing with req/res
    correlation, keepalive tick, and graceful shutdown. All OpenClaw
    protocol (auth sequence, event catalog, snapshot, chat.send framing)
    is gone — payloads are opaque JSON dicts.

    Interface adaptations vs gateway/client.py (documented per SP1
    instructions):
      1. Implements the Transport ABC: `connect()`/`disconnect()`/`send()`
         are now async public methods (client.py's start()/stop()/
         send_message() were sync, thread-kicked). A thread + own event
         loop still drive the connection; the async methods schedule onto
         it.
      2. `status_signals()` returns (on_connect, on_disconnect, on_error)
         callbacks — client.py passed on_connect/on_error into __init__ and
         had no on_disconnect (only error-strings on ConnectionClosed);
         on_disconnect is now a first-class callback fired when the socket
         drops.
      3. `send(payload)` takes an opaque JSON dict instead of
         send_message(session_key, text) — OpenClaw chat.send framing
         removed. Returns via on_response correlation, not on_sent.
      4. Callbacks are plain callables invoked on the transport thread
         (client.py marshaled via GLib.idle_add to the GTK main thread —
         a UI coupling removed; consumers marshal themselves).
    """

    def __init__(
        self,
        url: str,
        on_connect: Callable[[], None],
        on_disconnect: Callable[[str], None],
        on_error: Callable[[str], None],
        on_response: Callable[[str, dict[str, Any]], None] | None = None,
        on_tick: Callable[[], None] | None = None,
    ) -> None:
        self.url = url
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.on_error = on_error
        self.on_response = on_response
        self.on_tick = on_tick if on_tick is not None else lambda: None
        self._running = False
        self._stopping = False
        self._tick_task: asyncio.Task | None = None
        self._thread: threading.Thread | None = None
        self._connected = threading.Event()
        self._ws: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: dict[str, dict[str, Any]] = {}
        self._pending_lock = threading.Lock()

    # ── Transport ABC ────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Schedule the connection and wait (bounded) for it to go live.

        Idempotent while running. FIX 3: waits up to CONNECT_TIMEOUT_SEC
        for the socket to come up; on timeout fires on_error and returns
        (not connected). See also: on_connect signal, is_connected().
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping = False
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        loop = asyncio.get_running_loop()
        # Await the connected event WITHOUT blocking the caller's loop
        # (threading.Event.wait() would freeze it — seen in SP1 fix probes).
        connected = await loop.run_in_executor(
            None, self._connected.wait, CONNECT_TIMEOUT_SEC
        )
        if not connected:
            self.on_error(  # fires on transport thread — consumers marshal
                f"connect: not connected after {CONNECT_TIMEOUT_SEC}s"
            )

    async def disconnect(self) -> None:
        """Stop retrying and close the WebSocket (graceful shutdown)."""
        if self._stopping:
            return
        self._stopping = True
        self._running = False
        self._connected.clear()
        self._drain_pending("connection closed")
        if self._ws is not None:
            try:
                if self._loop is not None:
                    asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
            except Exception as e:  # noqa: BLE001 — best-effort close, any failure is non-fatal
                _logger.debug("Error closing WebSocket during disconnect: %s", e)

    async def send(
        self,
        payload: dict,
        *,
        on_response: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Send one JSON payload (thread-safe), with optional res correlation.

        FIX 2: two correlation channels, both live. A per-request
        on_response keyword is registered in the pending registry
        (timeout-protected) and fired exactly once for the matching res.
        The constructor on_response fires for EVERY res carrying an id
        (client.py set_on_res semantics) — with or without a pending
        entry.
        """
        self._send(payload, on_response=on_response)

    def status_signals(self) -> tuple:
        return (self.on_connect, self.on_disconnect, self.on_error)

    # ── Introspection (client.py parity) ─────────────────────────────────

    def is_connected(self) -> bool:
        return self._connected.is_set()

    # ── Internals (kept from client.py, auth stripped) ───────────────────

    def _send(self, payload: dict, on_response: Callable[[dict[str, Any]], None] | None = None) -> None:
        """Send a JSON payload on the WebSocket (thread-safe)."""
        self._expire_pending()
        if self._ws is None or self._loop is None:
            # FIX 4: restore client.py semantics — a send while down is
            # surfaced, never silently dropped (probe H). Checked BEFORE
            # pending registration so nothing is left dangling.
            self.on_error("Not connected")  # fires on transport thread — consumers marshal
            return
        req_id = payload.get("id")
        if req_id and on_response:
            with self._pending_lock:
                self._pending[req_id] = {
                    "callback": on_response,
                    "deadline": time.monotonic() + RPC_TIMEOUT_SEC,
                }
        asyncio.run_coroutine_threadsafe(
            self._ws.send(json.dumps(payload)), self._loop
        )

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connect_loop())

    async def _connect_loop(self) -> None:
        """Reconnecting connection loop with exponential backoff."""
        import websockets

        retry_delay = _BACKOFF_INITIAL_SEC
        retry_count = 0
        while self._running:
            try:
                async with websockets.connect(self.url) as ws:
                    self._ws = ws
                    self._connected.set()
                    retry_delay = _BACKOFF_INITIAL_SEC
                    loop = self._loop
                    assert loop is not None
                    self._tick_task = loop.create_task(self._tick_loop())
                    self.on_connect()  # fires on transport thread — consumers marshal
                    await self._listen()
            except websockets.exceptions.ConnectionClosed as e:
                self._connected.clear()
                self._cancel_tick()
                self._drain_pending(f"Connection closed: {e}")
                self.on_disconnect(f"Connection closed: {e}")  # fires on transport thread — consumers marshal
                if self._running:
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(
                        retry_delay * _BACKOFF_MULTIPLIER, _BACKOFF_MAX_SEC
                    )
            except Exception as e:  # noqa: BLE001 — reconnect loop must survive any failure
                self._connected.clear()
                self._cancel_tick()
                self._drain_pending(str(e))
                retry_count += 1
                _logger.info(
                    "Connection failed (attempt %d): %s — retrying in %.0fs",
                    retry_count, e, retry_delay,
                )
                self.on_error(f"Reconnecting in {int(retry_delay)}s…")  # fires on transport thread — consumers marshal
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * _BACKOFF_MULTIPLIER, _BACKOFF_MAX_SEC)
            else:
                # FIX 1 (clean-return path): _listen() returned normally —
                # a server-side clean close where no ConnectionClosed
                # exception escapes the async-for. Treat exactly like a
                # dropped socket: clear state, signal, respect backoff —
                # otherwise the loop spins hot (probe F: 63 accepts/2s,
                # zero on_disconnect).
                self._connected.clear()
                self._cancel_tick()
                self._drain_pending("connection closed")
                self.on_disconnect("connection closed")  # fires on transport thread — consumers marshal
                if self._running and not self._stopping:
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(
                        retry_delay * _BACKOFF_MULTIPLIER, _BACKOFF_MAX_SEC
                    )

    def _cancel_tick(self) -> None:
        if self._tick_task is not None and not self._tick_task.done():
            self._tick_task.cancel()
            self._tick_task = None

    async def _tick_loop(self) -> None:
        """Fire on_tick every TICK_INTERVAL_SEC while connected."""
        while self._running and not self._stopping:
            await asyncio.sleep(TICK_INTERVAL_SEC)
            if self._connected.is_set() and not self._stopping:
                self.on_tick()  # fires on transport thread — consumers marshal

    async def _listen(self) -> None:
        """Pump the WebSocket — dispatch JSON messages to consumers."""
        assert self._ws is not None, "_ws must be set before _listen"
        async for raw in self._ws:
            self._expire_pending()
            _logger.debug("[transport>>] %s", redact_log_preview(raw[:300]))
            try:
                msg = json.loads(raw)
                msg_type = msg.get("type")
                req_id = msg.get("id")
                with self._pending_lock:
                    entry = self._pending.pop(req_id, None) if req_id else None
                if entry:
                    # FIX 2: per-request correlation — the pending entry's
                    # callback fires exactly once for the matching res.
                    entry["callback"](msg.get("payload", {}))
                if req_id and self.on_response is not None:
                    # FIX 2: constructor on_response fires for EVERY res
                    # with an id — with or without a pending entry
                    # (client.py set_on_res semantics).
                    self.on_response(str(req_id), msg.get("payload", {}))
                if msg_type not in ("event", "res", "req"):
                    _logger.debug(
                        "passing through unknown message type: %r", msg_type
                    )
            except json.JSONDecodeError:
                _logger.warning(
                    "Malformed JSON (first 80 chars redacted): %s",
                    redact_log_preview(raw[:80]),
                )
            except Exception as exc:  # noqa: BLE001 — per-message isolation
                _logger.error("Unexpected error processing message: %s", exc)

    def _expire_pending(self) -> None:
        """Fire timeout callbacks for expired pending requests."""
        now = time.monotonic()
        with self._pending_lock:
            expired = [
                req_id
                for req_id, entry in list(self._pending.items())
                if entry.get("deadline", 0) <= now
            ]
            entries_to_fire = []
            for req_id in expired:
                entries_to_fire.append(self._pending.pop(req_id))
        for entry in entries_to_fire:
            entry["callback"](
                {"ok": False, "error": {"message": "request timed out"}}
            )

    def _drain_pending(self, reason: str) -> None:
        """Fire all remaining pending callbacks with an error payload."""
        with self._pending_lock:
            entries_to_fire = list(self._pending.items())
            self._pending.clear()
        for _, entry in entries_to_fire:
            entry["callback"](
                {"ok": False, "error": {"message": f"request cancelled: {reason}"}}
            )


