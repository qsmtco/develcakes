# tests/test_telegram_transport.py — SPEC-15 SP1 (TelegramTransport, pure).
#
# No network, no xvfb: every API call goes through an injected fake httpx
# client (the `client_factory` seam). The long-poll loop runs on its own
# daemon thread; tests wait on threading conditions, never on the poll
# thread's timing. Mirrors tests/test_transport_package.py's structure.

import asyncio
import logging
import time

import httpx
import pytest

from transport.base import Transport
from transport.telegram import (
    TelegramAuthError,
    TelegramTransport,
    redact_log_preview,
)

# ── Fake httpx client (records calls; handler decides each response) ──────


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {"ok": True, "result": []}
        self.headers = headers or {}

    def json(self):
        return self._json


class FakeClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.closed = False

    def post(self, path, json=None, **kw):
        self.calls.append({"http": "POST", "path": path, "json": json, **kw})
        return self.handler("POST", path, {"json": json})

    def get(self, path, params=None, **kw):
        self.calls.append({"http": "GET", "path": path, "params": params, **kw})
        return self.handler("GET", path, {"params": params})

    def close(self):
        self.closed = True


def _ok(result=None):
    return FakeResponse(200, {"ok": True, "result": result if result is not None else []})


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return False


TOKEN = "123456:ABCDEF-secret-token-xyz"


def _make(handler, *, token=TOKEN, chat_id=42, **kw):
    """Build a transport wired to a FakeClient; _sleep is a recorder by
    default (never actually sleeps in tests)."""
    client = FakeClient(handler)
    kw.setdefault("client_factory", lambda: client)
    ev = {"connect": [], "disconnect": [], "error": [], "update": []}
    t = TelegramTransport(
        token,
        on_connect=lambda: ev["connect"].append(1),
        on_disconnect=lambda r: ev["disconnect"].append(r),
        on_error=lambda m: ev["error"].append(m),
        on_update=lambda u: ev["update"].append(u),
        chat_id=chat_id,
        **kw,
    )
    sleeps = []
    t._sleep = lambda s: sleeps.append(s)
    return t, client, ev, sleeps


def _getme_ok_handler(update_fn=None):
    """getMe → ok; getUpdates → update_fn() (default: empty forever)."""
    def handler(http, path, kw):
        if path.endswith("/getMe"):
            return _ok({"id": 1, "username": "testbot", "is_bot": True})
        if path.endswith("/getUpdates"):
            return _ok(update_fn() if update_fn else [])
        return _ok()
    return handler


# ── ABC compliance ───────────────────────────────────────────────────────


def test_implements_transport():
    assert issubclass(TelegramTransport, Transport)


def test_status_signals_shape():
    t, _c, _ev, _s = _make(_getme_ok_handler())
    sigs = t.status_signals()
    assert len(sigs) == 3
    assert callable(sigs[0]) and callable(sigs[1]) and callable(sigs[2])


# ── connect / getMe fail-closed ──────────────────────────────────────────


def test_connect_no_token_raises():
    t, _c, _ev, _s = _make(_getme_ok_handler(), token="")
    with pytest.raises(TelegramAuthError):
        asyncio.run(t.connect())
    assert not t.is_connected()


def test_connect_getme_401_fail_closed_no_loop():
    def handler(http, path, kw):
        if path.endswith("/getMe"):
            return FakeResponse(401, {"ok": False, "description": "Unauthorized"})
        return _ok()

    t, client, ev, _s = _make(handler)
    with pytest.raises(TelegramAuthError):
        asyncio.run(t.connect())
    assert not t.is_connected()
    assert ev["connect"] == []          # no on_connect
    assert t._thread is None or not t._thread.is_alive()  # no poll loop
    # Only getMe was called — the loop never started.
    assert all("/getMe" in c["path"] for c in client.calls)


def test_connect_getme_not_ok_fail_closed():
    def handler(http, path, kw):
        if path.endswith("/getMe"):
            return FakeResponse(200, {"ok": False, "description": "bad token"})
        return _ok()

    t, _c, _ev, _s = _make(handler)
    with pytest.raises(TelegramAuthError):
        asyncio.run(t.connect())


def test_connect_getme_ok_fires_on_connect():
    t, _c, ev, _s = _make(_getme_ok_handler())
    asyncio.run(t.connect())
    try:
        assert t.is_connected()
        assert ev["connect"] == [1]      # fired from the poll thread
    finally:
        asyncio.run(t.disconnect())


# ── poll loop: updates + offset advance ──────────────────────────────────


def test_poll_delivers_updates():
    seen = []

    def updates():
        if not seen:
            seen.append(1)
            return [{"update_id": 100, "message": {"text": "hi"}}]
        return []

    t, _c, ev, _s = _make(_getme_ok_handler(updates))
    asyncio.run(t.connect())
    try:
        assert _wait(lambda: len(ev["update"]) >= 1), ev
        assert ev["update"][0]["update_id"] == 100
    finally:
        asyncio.run(t.disconnect())


def test_offset_advances_after_processing():
    calls = {"n": 0}

    def updates():
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"update_id": 100, "message": {"text": "a"}},
                    {"update_id": 101, "message": {"text": "b"}}]
        return []

    t, client, ev, _s = _make(_getme_ok_handler(updates))
    asyncio.run(t.connect())
    try:
        assert _wait(lambda: len(ev["update"]) >= 2), ev
        # Wait for a SECOND getUpdates call (the offset-bearing one).
        assert _wait(
            lambda: len([c for c in client.calls if c["path"].endswith("/getUpdates")]) >= 2
        ), client.calls
        gu = [c for c in client.calls if c["path"].endswith("/getUpdates")]
        # First call: no offset. Second: offset = last update_id + 1 = 102.
        assert gu[0]["json"].get("offset") in (None, 0)
        assert gu[1]["json"]["offset"] == 102
    finally:
        asyncio.run(t.disconnect())


# ── backoff: 429 / 5xx / network ─────────────────────────────────────────


def test_backoff_429_respects_retry_after():
    calls = {"n": 0}

    def handler(http, path, kw):
        if path.endswith("/getMe"):
            return _ok({"id": 1})
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(429, {
                "ok": False, "error_code": 429,
                "parameters": {"retry_after": 7},
            })
        return _ok([])

    t, _c, _ev, sleeps = _make(handler)
    asyncio.run(t.connect())
    try:
        assert _wait(lambda: sleeps), sleeps
        assert sleeps[0] == 7  # respected retry_after, not the default backoff
    finally:
        asyncio.run(t.disconnect())


def test_backoff_on_5xx():
    calls = {"n": 0}

    def handler(http, path, kw):
        if path.endswith("/getMe"):
            return _ok({"id": 1})
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(502, {"ok": False})
        return _ok([])

    t, _c, _ev, sleeps = _make(handler)
    asyncio.run(t.connect())
    try:
        assert _wait(lambda: sleeps), sleeps
        assert sleeps[0] >= 1.0  # initial backoff (not a hot retry)
    finally:
        asyncio.run(t.disconnect())


def test_backoff_on_network_error():
    calls = {"n": 0}

    def handler(http, path, kw):
        if path.endswith("/getMe"):
            return _ok({"id": 1})
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return _ok([])

    t, _c, ev, sleeps = _make(handler)
    asyncio.run(t.connect())
    try:
        assert _wait(lambda: sleeps), sleeps
        assert sleeps[0] >= 1.0
        assert ev["error"], "network error must fire on_error (redacted)"
    finally:
        asyncio.run(t.disconnect())


# ── send API surface ─────────────────────────────────────────────────────


def test_send_message_shape():
    t, client, _ev, _s = _make(_getme_ok_handler())
    resp = t.send_message("hello world")
    assert resp["ok"] is True
    call = [c for c in client.calls if c["path"].endswith("/sendMessage")][-1]
    assert call["json"]["chat_id"] == 42
    assert call["json"]["text"] == "hello world"
    assert "parse_mode" not in call["json"]  # plain text (our sanitizer is ours)


def test_send_message_reply_markup_passthrough():
    t, client, _ev, _s = _make(_getme_ok_handler())
    markup = {"inline_keyboard": [[{"text": "OK", "callback_data": "x"}]]}
    t.send_message("q", reply_markup=markup)
    call = [c for c in client.calls if c["path"].endswith("/sendMessage")][-1]
    assert call["json"]["reply_markup"] == markup


def test_send_message_requires_chat_id():
    t, _c, _ev, _s = _make(_getme_ok_handler(), chat_id=None)
    with pytest.raises(ValueError):
        t.send_message("hi")


def test_answer_callback_query_shape():
    t, client, _ev, _s = _make(_getme_ok_handler())
    t.answer_callback_query("cbq-1", text="done")
    call = [c for c in client.calls if c["path"].endswith("/answerCallbackQuery")][-1]
    assert call["json"]["callback_query_id"] == "cbq-1"
    assert call["json"]["text"] == "done"


def test_edit_message_text_shape():
    t, client, _ev, _s = _make(_getme_ok_handler())
    t.edit_message_text(555, "edited")
    call = [c for c in client.calls if c["path"].endswith("/editMessageText")][-1]
    assert call["json"]["chat_id"] == 42
    assert call["json"]["message_id"] == 555
    assert call["json"]["text"] == "edited"


# ── redaction ────────────────────────────────────────────────────────────


def test_redact_log_preview_scrubs_bot_token_url():
    raw = f"POST https://api.telegram.org/bot{TOKEN}/getUpdates"
    out = redact_log_preview(raw)
    assert TOKEN not in out
    assert "***" in out


def test_token_never_appears_in_logs(caplog):
    """The token must never surface in logs OR on_error strings. A realistic
    httpx failure embeds the URL (which carries /bot<token>/), so the poll
    error path is the real leak vector."""
    state = {"ok_done": False}

    def handler(http, path, kw):
        if path.endswith("/getMe"):
            return _ok({"id": 1})
        if not state["ok_done"]:
            state["ok_done"] = True
            return _ok([])  # one healthy poll → connect completes fast
        raise httpx.ConnectError(
            f"failed to connect to https://api.telegram.org/bot{TOKEN}/getUpdates"
        )

    t, _c, ev, _s = _make(handler)
    with caplog.at_level(logging.DEBUG, logger="transport.telegram"):
        asyncio.run(t.connect())
        try:
            assert _wait(lambda: ev["error"]), ev
            time.sleep(0.05)
        finally:
            asyncio.run(t.disconnect())
    assert TOKEN not in caplog.text, "bot token leaked into logs"
    assert "ABCDEF-secret-token-xyz" not in caplog.text
    joined = " ".join(ev["error"])
    assert TOKEN not in joined, f"bot token leaked into on_error: {joined}"
    assert "ABCDEF-secret-token-xyz" not in joined


# ── disconnect: idempotent, no orphan thread ─────────────────────────────


def test_disconnect_idempotent():
    t, _c, _ev, _s = _make(_getme_ok_handler())
    asyncio.run(t.connect())
    asyncio.run(t.disconnect())
    asyncio.run(t.disconnect())  # second call must not raise
    assert not t.is_connected()


def test_disconnect_no_orphan_thread():
    t, _c, _ev, _s = _make(_getme_ok_handler())
    asyncio.run(t.connect())
    assert t._thread is not None and t._thread.is_alive()
    asyncio.run(t.disconnect())
    assert t._thread is not None and not t._thread.is_alive(), "poll thread orphaned"


def test_disconnect_fires_on_disconnect():
    t, _c, ev, _s = _make(_getme_ok_handler())
    asyncio.run(t.connect())
    asyncio.run(t.disconnect())
    assert ev["disconnect"], "on_disconnect must fire on teardown"


# ── BUG#3: connect() closes the client on auth failure ───────────────────


def test_connect_auth_failure_closes_client():
    """BUG#3: a failed getMe must close the just-created httpx client — no
    leaked connection pool on repeated connect attempts with a bad token."""
    def handler(http, path, kw):
        if path.endswith("/getMe"):
            return FakeResponse(401, {"ok": False, "description": "Unauthorized"})
        return _ok()

    t, client, _ev, _s = _make(handler)
    with pytest.raises(TelegramAuthError):
        asyncio.run(t.connect())
    assert client.closed is True, "auth-failure connect must close the client"


def test_connect_getme_exception_closes_client():
    """BUG#3 (transport-error path): a getMe network failure also closes the
    client before raising."""
    def handler(http, path, kw):
        if path.endswith("/getMe"):
            raise httpx.ConnectError("boom")
        return _ok()

    t, client, _ev, _s = _make(handler)
    with pytest.raises(TelegramAuthError):
        asyncio.run(t.connect())
    assert client.closed is True, "getMe exception must close the client"


def test_connect_success_does_not_close_client():
    """Control: a successful connect keeps the client open (the poll loop
    needs it)."""
    t, client, _ev, _s = _make(_getme_ok_handler())
    asyncio.run(t.connect())
    try:
        assert client.closed is False
    finally:
        asyncio.run(t.disconnect())


# ── BUG#4: a raising consumer must not kill the poll loop ────────────────


def test_on_error_raise_does_not_kill_poll():
    """BUG#4: if a consumer's on_error raises, the poll loop must survive
    (log + continue) — otherwise the bridge's error handler could kill the
    transport."""
    def handler(http, path, kw):
        if path.endswith("/getMe"):
            return _ok({"id": 1})
        raise httpx.ConnectError("boom")  # forces on_error each iteration

    client = FakeClient(handler)
    calls: list = []

    def raising_on_error(_m):
        calls.append(_m)
        raise RuntimeError("consumer bug")

    t = TelegramTransport(
        TOKEN, on_error=raising_on_error, chat_id=42,
        client_factory=lambda: client,
    )
    sleeps = []
    t._sleep = lambda s: sleeps.append(s)
    asyncio.run(t.connect())
    try:
        # The loop keeps polling (recorded sleeps prove it did not die on the
        # first on_error exception).
        assert _wait(lambda: len(sleeps) >= 2), sleeps
        assert t._thread is not None and t._thread.is_alive()
    finally:
        asyncio.run(t.disconnect())


def test_on_update_raise_does_not_kill_poll():
    """BUG#4 (sibling): a raising on_update must not kill the loop; the
    offset still advances past the processed update."""
    seen = []

    def updates():
        if not seen:
            seen.append(1)
            return [{"update_id": 100, "message": {"text": "hi"}}]
        return []

    def raising_on_update(_u):
        raise RuntimeError("consumer bug")

    client = FakeClient(_getme_ok_handler(updates))
    t = TelegramTransport(
        TOKEN, on_update=raising_on_update, chat_id=42,
        client_factory=lambda: client,
    )
    t._sleep = lambda s: None
    asyncio.run(t.connect())
    try:
        # A second getUpdates call proves the loop survived on_update raising.
        assert _wait(
            lambda: len([c for c in client.calls if c["path"].endswith("/getUpdates")]) >= 2
        ), client.calls
        gu = [c for c in client.calls if c["path"].endswith("/getUpdates")]
        assert gu[1]["json"]["offset"] == 101  # advanced despite the raise
    finally:
        asyncio.run(t.disconnect())


def test_send_on_response_raise_does_not_propagate():
    """BUG#4 (sibling): a raising on_response passed to send() must be
    swallowed by the dispatch guard — the caller's send() must not raise."""
    response = {"ok": True, "result": {"message_id": 1}}

    def handler(http, path, kw):
        if path.endswith("/getMe"):
            return _ok({"id": 1})
        return FakeResponse(200, response)

    def raising_on_response(_r):
        raise RuntimeError("consumer bug")

    client = FakeClient(handler)
    t = TelegramTransport(
        TOKEN, chat_id=42, client_factory=lambda: client,
    )
    t._sleep = lambda s: None
    # Must not raise despite the consumer raising.
    asyncio.run(t.send({"method": "sendMessage", "params": {}},
                       on_response=raising_on_response))