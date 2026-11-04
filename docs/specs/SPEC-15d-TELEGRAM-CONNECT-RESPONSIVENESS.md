# SPEC-15d: Telegram Connect Stays Responsive

**Date:** 2026-11-04
**Author:** Micah
**Status:** Draft — for implementation
**Implements:** investigation 2026-11-04 — Connect freezes the GTK window, the desktop shows the “not responding” dialog, the bridge paints Offline, then the same session delivers messages.
**Depends on:** SPEC-15 (Telegram Remote Bridge), SPEC-15b (hardening), SPEC-15c (Supervisor replies only). Does not reopen pairing, routing, or the reply mirror.
**Target branch:** main

> Architecture compliance: `transport/telegram.py` stays the owner of the poll thread and of every Telegram HTTP call. `TelegramBridgeHandler` stays the single writer of bridge state. `ui/window.py` stays the composition root and still does not let the bridge import window or `AgentRuntimeHandler`. No new module. `docs/ARCHITECTURE.md` §3.2a is the contract for this path and gains one paragraph (section 8). The strings `transport/telegram.py` and `TelegramBridgeHandler` stay in that section (`tests/test_window_telegram_bridge.py` `TestDocsPresence.test_architecture_documents_telegram`).

---

## Discovery (verified against source)

- Read `transport/telegram.py`: `CONNECT_TIMEOUT_SEC = 10.0` (line 49), `LONG_POLL_TIMEOUT_SEC = 50` (line 53). `async def connect(self) -> None` (150–196) posts `getMe` on the caller with the sync `httpx` client, starts `_run` on a daemon thread, then `await loop.run_in_executor(None, self._connected.wait, CONNECT_TIMEOUT_SEC)`. On timeout it calls `_dispatch(self.on_error, "connect: not connected after 10.0s", kind="on_error")` and does not stop the thread. `_connected` is set only inside `_poll_loop` after the first successful `getUpdates` (394–397). `_get_updates(self) -> list | None` (411) always sends `"timeout": self.poll_timeout` (50). `_default_client` (297–298) builds `httpx.Client(..., timeout=self.poll_timeout + 10)` so a socket can block the caller for 60 seconds. `async def disconnect(self) -> None` (198–216) sets `_stopping` and `_running` under `self._lock`, closes the client, and `thread.join(timeout=2.0)`. `_dispatch(self, callback, *args, kind: str)` (245) swallows callback exceptions. Raises: `TelegramAuthError` from `connect` (empty token, `getMe` exception, HTTP 401, or `ok` false). `_get_updates` raises `_Conflict` and `_RetryAfter`. `openclaw.py` has its own `CONNECT_TIMEOUT_SEC`; `tests/test_transport_package.py` line 59 asserts that one. Nothing imports `transport.telegram.CONNECT_TIMEOUT_SEC`.
- Read `ui/handlers/telegram_bridge_handler.py`: `start_bridge(self) -> None` (315) bumps `_generation`, calls `stop_bridge()`, reads config, refuses a missing token, a missing `chat_id`, a non-int `chat_id`, or an unregistered `agent:supervisor`, then constructs the transport and sets `BridgeState.CONNECTING` before `asyncio.run(self._transport.connect())` (399–408). `TelegramAuthError` and any other `Exception` set `ERROR` on the caller. The docstring at 319–320 says `connect()` runs on the transport thread. The call does not. `stop_bridge(self) -> None` (410) returns immediately when state is `disconnected` and `_transport is None`; otherwise it bumps `_generation` again, `asyncio.run(transport.disconnect())`, drops the transport, and sets `DISCONNECTED`. `_on_transport_connect(self, gen: int | None = None)` (434) sets `CONNECTED` with no prior-state check. `_on_transport_error` (449) sets `ERROR`. `_set_state` (261) ignores a repeat of the current state. Callbacks are `lambda m, g=gen: self._dispatch(...)`. Production `dispatch` is `GLib.idle_add` (`ui/window.py` 549).
- Read `ui/window.py` `_on_connect_clicked` (1591–1621): configured + not connected and not `connecting` → `start_bridge()`; connected or `connecting` → `stop_bridge()`. `_on_bridge_state_change` (1688) paints the pip and, on `"error"` with no pending specific notice, emits the card “Telegram bridge offline”.
- Read `ui/views/main_content.py` `_CONNECT_TIPS` (29–39) and `set_telegram_bridge_state` (342–356). `"connecting"` is already “Connecting…”. No new widget.
- Read `ui/handlers/telegram_settings_controller.py` `_pairing_run` (238–256): pairing already calls `asyncio.run(transport.connect())` on a daemon thread named `telegram-pairing`, and `on_error` ends pairing. A `connect()` that returns before `getMe` still works: the poll thread performs `getMe`, and a failure still arrives on `on_error`.
- Read `tests/test_telegram_transport.py`: fakes return `getUpdates` immediately, so the 10s-vs-50s race never happens. `test_connect_getme_ok_fires_on_connect` (151) asserts `t.is_connected()` in the same breath as `asyncio.run(t.connect())` — that becomes a race once `connect()` returns before the poll thread announces. `test_connect_getme_401_fail_closed_no_loop`, `test_connect_getme_not_ok_fail_closed`, `test_connect_auth_failure_closes_client`, and `test_connect_getme_exception_closes_client` expect `TelegramAuthError` from `asyncio.run(t.connect())`. `test_connect_no_token_raises` must keep expecting that raise. `test_on_connect_reannounced_after_soft_error` treats the second `getUpdates` (`calls["n"] == 2`) as the 502; the comment above that line says “first”. Leave the condition alone.
- Read `tests/test_telegram_bridge_handler.py`: `FakeTransport.connect` is `async def connect(self): pass` and does not touch the network. `test_deferred_stale_disconnect_does_not_clobber_error` and `test_restart_tears_down_previous_transport` require `start_bridge()` / `stop_bridge()` to finish the injected `connect()` / `disconnect()` before they return. Moving `asyncio.run` onto a worker thread breaks that order.
- Architecture owner: `docs/ARCHITECTURE.md` §3.2a. The Connect sentence (line 340) is the one this spec extends. The same section says inbound text goes to `send_to_special_agent("special:supervisor", …)`. The code constant is `SUPERVISOR_KEY = "agent:supervisor"` (handler line 35). This spec does not rewrite that sentence.
- Existing pattern to copy: the poll thread already owns `getUpdates`. `getMe` moves onto that same thread. The handler’s `asyncio.run` stays on the caller so the deferred-dispatch tests keep their order. `connect()` itself is what returns before any HTTP read.

---

## 1. Overview

### Problem

Clicking Connect runs `asyncio.run(transport.connect())` on the GTK thread. `connect()` posts `getMe` on that thread, then waits up to 10 seconds for `_connected`. `_connected` is set only after the first `getUpdates` returns, and that request asks Telegram to hold an empty queue for 50 seconds. The main loop is unserviced for about 10 seconds plus the `getMe` round trip. The desktop then asks whether to shut the app down. Cancel only declines to kill the process, so the dialog comes back until `connect()` returns.

When the 10 seconds elapse, `connect()` fires `on_error` (“connect: not connected after 10.0s”) and leaves the poll thread running. The handler sets `error`. The pip goes Offline and the feed gets “Telegram bridge offline”. Up to 40 seconds later, or immediately if a phone message ends the long poll, the same thread fires `on_connect`. The pip turns on and the message is delivered. The offline card stays.

### Solution

`connect()` starts the poll thread and returns. That thread runs `getMe`, then one `getUpdates` with `timeout` 0, then the existing 50-second long poll. The toolbar can paint Connecting as soon as `start_bridge` returns. Connected arrives on `on_connect` after the short poll. A rejected token or a failed `getMe` arrives on `on_error`. Nothing in this path emits “not connected after 10.0s”.

### Scope

| In | Out |
|---|---|
| `TelegramTransport.connect` returns after `Thread.start()`, before any HTTP read | Moving `asyncio.run` in `start_bridge` / `stop_bridge` off the caller |
| `getMe` runs on the poll thread; failure closes the client, fires `on_error`, and does not enter the poll loop | `transport/openclaw.py` and its `CONNECT_TIMEOUT_SEC` |
| First `getUpdates` of an unannounced link uses `timeout` 0; announced links keep `poll_timeout` (50) | `send_message` / `edit_message_text` / `answer_callback_query` (still sync HTTP on whatever thread calls them) |
| Delete `transport.telegram.CONNECT_TIMEOUT_SEC` and the 10-second wait | Pairing UI, routing, `/status` `/stop` `/help`, the reply mirror |
| Docstring on `start_bridge`, one paragraph in §3.2a, transport tests | Retracting an offline feed card after a later recovery |
| | `ui/window.py`, `ui/views/main_content.py`, `telegram_settings_controller.py` |

### Principles

- The GTK thread does no Telegram HTTP on Connect. `httpx.Client()` construction is local and stays in `connect()` so `disconnect()` has a client to close.
- “Connected” still means the first `getUpdates` succeeded. An empty result (`[]`) is success. The short poll is what makes that true in one round trip instead of 50 seconds.
- Fail closed. A bad token never starts `getUpdates`. The signal moves from a raised `TelegramAuthError` (nobody is waiting on `connect()` anymore) to `on_error`.
- One poller. `start_bridge` still calls `stop_bridge()` before building the next transport. `stop_bridge` still joins for at most 2 seconds on the caller. That join is under the ~5 second desktop threshold and the deferred-dispatch tests depend on it finishing inline.
- `0` is a real timeout. The short-poll argument must be applied with `is None`, never with `timeout or self.poll_timeout`.

---

## 2. Changes by File

### 2.1 `transport/telegram.py`

Delete `CONNECT_TIMEOUT_SEC`. Add the drain constant next to the long-poll constant:

```python
# First getUpdates before the link is announced, and the first poll after a
# soft failure clears the latch. Telegram returns immediately. The 50s hold
# is only for a link that has already announced.
DRAIN_POLL_TIMEOUT_SEC = 0
```

`LONG_POLL_TIMEOUT_SEC = 50` stays. `connect` no longer imports a running loop and no longer waits.

Replace `connect` (signature stays `async def connect(self) -> None`). Empty token still raises `TelegramAuthError` before a thread exists. Every other failure is `on_error` from the poll thread.

```python
async def connect(self) -> None:
    """Start the poll thread and return.

    The caller (the GTK thread, via asyncio.run) does no HTTP. The poll
    thread runs getMe, then getUpdates. Empty token raises
    TelegramAuthError and starts nothing. A getMe failure or a poll
    failure is on_error from that thread. Idempotent while the thread
    is alive.
    """
    if self._thread is not None and self._thread.is_alive():
        return
    if not self.token:
        raise TelegramAuthError("connect: no bot token configured")
    self._client = self._client_factory()
    self._stopping = False
    self._running = True
    self._thread = threading.Thread(
        target=self._run, daemon=True, name="telegram-poll"
    )
    self._thread.start()
```

Return value is `None`. The caller ignores it. `asyncio.run` of this coroutine returns as soon as the thread has been started.

`_run` calls `_session` instead of `_poll_loop`:

```python
def _run(self) -> None:
    self._loop = asyncio.new_event_loop()
    asyncio.set_event_loop(self._loop)
    try:
        self._loop.run_until_complete(self._session())
    finally:
        self._loop.close()

async def _session(self) -> None:
    try:
        ok = self._authenticate()
    except Exception as e:
        self._close_client()
        self._fail_connect(
            f"connect: getMe failed: {redact_log_preview(str(e))}"
        )
        return
    if not ok or not self._running:
        return
    await self._poll_loop()
```

`_authenticate` is the old `getMe` block, moved off the caller. Same two outcomes: transport exception, or HTTP 401 / body not `ok`. Both close the client. Neither enters `_poll_loop`. A non-dict JSON body raises `AttributeError` on `data.get`; `_session` catches that `Exception`, closes the client, and `_fail_connect`s. That is the fail-closed path the old uncaught `AttributeError` did not have.

```python
def _authenticate(self) -> bool:
    """getMe on the poll thread. False → do not poll. Does not raise
    for the two handled outcomes. Closes the client on failure.
    """
    client: Any = self._client
    try:
        resp = client.post(f"/bot{self.token}/getMe", json={})
        data = resp.json()
    except Exception as e:
        self._close_client()
        self._fail_connect(
            f"connect: getMe failed: {redact_log_preview(str(e))}"
        )
        return False
    if resp.status_code == 401 or not data.get("ok"):
        self._close_client()
        self._fail_connect(
            "connect: getMe rejected the token "
            f"(status={resp.status_code})"
        )
        return False
    return True

def _fail_connect(self, message: str) -> None:
    """Record a connect failure. No poll loop. No callback during disconnect.

    Return value is None. _running is cleared so _session and _poll_loop
    stop. disconnect() sets _stopping before it closes the client; a
    getMe aborted by that close must not surface as a bridge error.
    """
    self._running = False
    if self._stopping:
        return
    self._dispatch(self.on_error, message, kind="on_error")
```

`_dispatch` already catches a raising consumer. `_fail_connect` does not catch `_dispatch`.

`_get_updates` gains one optional argument. Existing direct callers (`t._get_updates()` in the 409 tests) pass nothing and keep `self.poll_timeout`.

```python
def _get_updates(self, timeout: int | None = None) -> list | None:
    """One getUpdates. timeout None → self.poll_timeout (the 50s hold).

    A passed 0 must be sent as 0. `timeout or self.poll_timeout` is wrong:
    0 is the drain poll.
    """
    params: dict = {
        "timeout": self.poll_timeout if timeout is None else timeout,
        "allowed_updates": list(ALLOWED_UPDATES),
    }
```

Only that signature, that docstring, and the `"timeout"` expression change. From `if self._offset is not None:` through the final `return` the function stays as it is: 409 raises `_Conflict`, 429 raises `_RetryAfter`, status >= 500 returns `None`, a non-ok body returns `None` (409 in the body raises `_Conflict`), and a successful body returns `result` when it is a list.

The only call inside `_poll_loop` becomes:

```python
updates = self._get_updates(
    timeout=DRAIN_POLL_TIMEOUT_SEC if not announced else None
)
```

`announced` starts `False`, so the first request is `timeout` 0. On `[]` or a non-empty list the existing block sets `_connected`, fires `on_connect` once, and sets `announced = True`. The next iteration passes `timeout=None` and the body sends 50. Every failure branch already sets `announced = False` before sleeping, so the retry after a 409, 429, 5xx, or network error is a drain poll again, then a long poll after it announces. Do not change those branches.

Update the module docstring (lines 10–13) and the `_poll_loop` docstring (334–339). `connect()` still validates the token via `getMe` before any `getUpdates`. A `getMe` failure is `on_error` from the poll thread, not `TelegramAuthError` raised out of `connect()`, except the empty-token raise which happens before the thread. The long poll is 50 seconds only after the link has announced.

No new import. `threading`, `asyncio`, and `httpx` are already imported.

Line count: about +55 / −25. Risk: medium. This is the whole behavior change.

### 2.2 `ui/handlers/telegram_bridge_handler.py`

Replace only the two false sentences in the `start_bridge` docstring (lines 319–320). Leave the body, including both `asyncio.run` calls, the `TelegramAuthError` handler, and the generic `except Exception`, exactly as they are.

```python
def start_bridge(self) -> None:
    """Validate the store, construct the transport, and connect.

    Fail-closed: no token or no paired chat_id → ERROR state, no transport.
    ``connect()`` starts the poll thread and returns. getMe and getUpdates
    run on that thread, so this call does not block on the network.
    CONNECTING is set before ``connect()`` returns. CONNECTED arrives
    later on on_connect. A getMe or poll failure arrives on on_error.

    F1a (audit): ...
    """
```

A real `getMe` rejection no longer hits `except TelegramAuthError` in `start_bridge`, because `connect()` does not raise it. The transport fires `on_error`, the existing lambda dispatches `_on_transport_error`, and that sets `ERROR`. The window’s existing error card is the user-visible result. The log line moves from “bridge connect failed (auth)” to “bridge error: connect: getMe rejected the token …”. Keep the `except` blocks: an injected transport can still raise, and `test_deferred_stale_disconnect_does_not_clobber_error` patches `connect` to raise `ValueError`.

`stop_bridge` is unchanged. `disconnect()` still runs to completion on the caller, joins up to 2.0 seconds, and queues `on_disconnect` before `stop_bridge` returns.

Line count: docstring only, about +4 / −2. Risk: low.

### 2.3 `tests/test_telegram_transport.py`

`test_connect_no_token_raises` stays. Empty token still raises `TelegramAuthError`, `is_connected()` is false, and no thread is started.

`test_connect_getme_ok_fires_on_connect`: after `asyncio.run(t.connect())`, wait. A synchronous `assert t.is_connected()` races the poll thread.

```python
def test_connect_getme_ok_fires_on_connect():
    t, _c, ev, _s = _make(_getme_ok_handler())
    asyncio.run(t.connect())
    try:
        assert _wait(lambda: t.is_connected()), ev
        assert ev["connect"] == [1]
    finally:
        asyncio.run(t.disconnect())
```

`_wait` is already in this file (`timeout=5.0`).

Rewrite the four tests that expect `TelegramAuthError` from a failed `getMe`. `asyncio.run(t.connect())` returns. The thread then fails, closes the client, fires `on_error`, and exits. No `getUpdates` call.

```python
def test_connect_getme_401_fail_closed_no_loop():
    def handler(http, path, kw):
        if path.endswith("/getMe"):
            return FakeResponse(401, {"ok": False, "description": "Unauthorized"})
        return _ok()

    t, client, ev, _s = _make(handler)
    asyncio.run(t.connect())  # does not raise
    assert _wait(lambda: ev["error"]), ev
    assert not t.is_connected()
    assert ev["connect"] == []
    assert _wait(lambda: t._thread is not None and not t._thread.is_alive())
    assert all("/getMe" in c["path"] for c in client.calls)
    assert client.closed is True
```

Keep these test names. Change each body to the no-raise shape above:

- `test_connect_getme_not_ok_fail_closed` — `getMe` body `{"ok": False}`. `on_error` fired, `client.closed is True`, no path ending in `/getUpdates`, `is_connected()` is false.
- `test_connect_auth_failure_closes_client` — same 401 fixture as the test above. Assert `client.closed is True` after `on_error` has fired. Do not expect `TelegramAuthError`.
- `test_connect_getme_exception_closes_client` — `getMe` raises `httpx.ConnectError("boom")`. `on_error` text contains `getMe failed`. `client.closed is True`. No raise. No `/getUpdates` call.

Add two tests.

```python
def test_connect_returns_while_getme_is_blocked():
    """Connect must return to the GTK caller before getMe finishes."""
    release = threading.Event()
    entered = threading.Event()

    def handler(http, path, kw):
        if path.endswith("/getMe"):
            entered.set()
            release.wait(timeout=2.0)
            return _ok({"id": 1, "username": "testbot", "is_bot": True})
        return _ok([])

    t, _c, ev, _s = _make(handler)
    started = time.monotonic()
    asyncio.run(t.connect())
    elapsed = time.monotonic() - started
    assert elapsed < 0.5, elapsed
    assert _wait(lambda: entered.is_set())
    release.set()
    try:
        assert _wait(lambda: t.is_connected()), ev
        assert ev["error"] == []
    finally:
        asyncio.run(t.disconnect())
```

`release.wait(timeout=2.0)` is required. If an implementer leaves `getMe` on the caller, `connect()` blocks inside that wait, `elapsed` is about 2 seconds, and the assertion fails. The timeout keeps the test from hanging when the bug is present.

```python
def test_first_get_updates_drains_then_long_polls():
    """An empty queue must not cost a 50s hold or a connect-timeout error."""
    from transport.telegram import DRAIN_POLL_TIMEOUT_SEC, LONG_POLL_TIMEOUT_SEC

    t, client, ev, _s = _make(_getme_ok_handler())
    asyncio.run(t.connect())
    try:
        assert _wait(
            lambda: len(
                [c for c in client.calls if c["path"].endswith("/getUpdates")]
            ) >= 2
        ), client.calls
        gu = [c for c in client.calls if c["path"].endswith("/getUpdates")]
        assert gu[0]["json"]["timeout"] == DRAIN_POLL_TIMEOUT_SEC
        assert gu[1]["json"]["timeout"] == LONG_POLL_TIMEOUT_SEC
        assert ev["connect"] == [1]
        assert not any("not connected after" in m for m in ev["error"])
    finally:
        asyncio.run(t.disconnect())
```

`threading` and `time` are already imported in this file.

Leave `test_on_connect_reannounced_after_soft_error` and the direct `_get_updates()` 409 tests unchanged. After this change the first of those reannounce calls is a drain poll (success, announce), the second is the 502 (long poll, because `announced` is true), and the third is a drain poll again. The test only counts `on_connect` and `on_error`.

Line count: about +80. Risk: low.

### 2.4 `docs/ARCHITECTURE.md`

In §3.2a, keep the sentence that the Connect button toggles the Telegram bridge. Add this paragraph immediately after it. Do not edit the `special:supervisor` sentence.

```markdown
`TelegramTransport.connect()` starts the poll thread and returns; the GTK
thread does no Telegram HTTP on that call. The poll thread runs `getMe`,
then one `getUpdates` with `timeout` 0, then long-polls at 50 seconds.
The toolbar stays on Connecting until that short poll succeeds
(`on_connect`) or `getMe` / `getUpdates` fails (`on_error`).
```

`transport/telegram.py` and `TelegramBridgeHandler` remain in the section.

Line count: +5. Risk: low.

### Files NOT changed

- `ui/window.py` — `_on_connect_clicked` and `_on_bridge_state_change` already paint Connecting and turn `on_error` into the offline card. They start working once `connect()` returns.
- `ui/views/main_content.py` — `"connecting"`, `"connected"`, and `"error"` tips already exist.
- `ui/handlers/telegram_settings_controller.py` — pairing already runs `connect()` off the GTK thread. The new `connect()` returns sooner; `on_error` still ends pairing.
- `ui/handlers/telegram_bridge_handler.py` body — `asyncio.run` stays on the caller. `stop_bridge` still joins for at most 2 seconds. Moving either call onto a daemon thread breaks `DeferredDispatch` (the stale `on_disconnect` would not be queued before `start_bridge` returns).
- `transport/openclaw.py` — its own `CONNECT_TIMEOUT_SEC` and the wait in `OpenClawTransport.connect` stay. `tests/test_transport_package.py` asserts `oc.CONNECT_TIMEOUT_SEC == 10.0`.
- `tests/test_telegram_bridge_handler.py` — fakes never block. No assertion changes.
- `agent/runtime.py`, feed cards, `send_message` — a slow reply to the phone can still block the GTK thread later (client timeout 60 seconds). That is the 2026-10-07 post-mortem follow-up, not this click.

---

## 3. Data Flow

Healthy Connect, empty Telegram queue:

1. User clicks the project-bar connect icon. `MainContent._on_transport_connect_clicked` → `MainWindow._on_connect_clicked`.
2. Store has a token and a `chat_id`. Bridge state is not `connected` or `connecting`. `TelegramBridgeHandler.start_bridge()`.
3. Generation bumps. `stop_bridge()` returns immediately (state `disconnected`, `_transport is None`). Config checks pass. Supervisor is registered.
4. `TelegramTransport(...)` is stored. Callbacks close over the current generation and call `dispatch` (`GLib.idle_add` in the app).
5. `_set_state("connecting")` runs `MainWindow._on_bridge_state_change` → `set_telegram_bridge_state("connecting")`. The pip is “Connecting…”.
6. `asyncio.run(transport.connect())` builds `httpx.Client` locally, sets `_running`, starts thread `telegram-poll`, and returns `None`. The GTK main loop runs again. The pip paints.
7. Poll thread `_run` → `_session` → `_authenticate` posts `/bot<token>/getMe`. Body `ok` is true. `_poll_loop` posts `getUpdates` with `{"timeout": 0, "allowed_updates": ["message", "callback_query"]}` and no `offset`.
8. Telegram returns `{"ok": true, "result": []}`. `announced` was false, so the thread sets `_connected` and calls `on_connect`. The handler lambda idle-adds `_on_transport_connect(gen)`. Generation matches. State becomes `connected`. The pip tooltip becomes “Disconnect the Telegram bridge”.
9. The loop posts `getUpdates` again with `"timeout": 50`. That hold is on the poll thread. The window stays usable. A later message is `on_update` → `_handle_update` → `_handle_message` → `send_to_special_agent("agent:supervisor", text)`.

Rejected token:

1. Steps 1–6 are the same. The pip paints Connecting.
2. `getMe` returns HTTP 401 or `ok` false. `_authenticate` closes the client, `_fail_connect` sets `_running = False` and dispatches `on_error` with `connect: getMe rejected the token (status=…)`. `_session` returns. The thread ends. No `getUpdates`.
3. `_on_transport_error` sets `error`. The window emits “Telegram bridge offline” and the pip tooltip becomes “Offline — check the bot token and pairing in Settings”.

Phone message during what used to be the 50-second hold:

1. The first `getUpdates` already returned in step 8, so the bridge is `connected` before the user types.
2. The in-flight long poll returns that update. `on_update` routes it once. There is no preceding `error` state and no offline card.

Disconnect during `getMe`:

1. State is `connecting`, so the click calls `stop_bridge`.
2. Generation bumps. `disconnect()` sets `_stopping` and `_running = False` under `_lock`, closes the client (this unblocks `getMe`), joins up to 2 seconds, and dispatches `on_disconnect`.
3. The poll thread’s `getMe` raises. `_fail_connect` sees `_stopping` and does not dispatch `on_error`.
4. `stop_bridge` sets `disconnected`. The stale `on_disconnect` is dropped because its captured generation does not match.

---

## 4. File Change Summary

| File | Change | Lines | Risk |
|---|---|---|---|
| `transport/telegram.py` | `connect` returns after thread start; `getMe` and drain poll move onto that thread; delete the 10s wait | +55 / −25 | Medium |
| `tests/test_telegram_transport.py` | Auth failures assert `on_error`; two new timing tests; one `is_connected` wait | +80 | Low |
| `ui/handlers/telegram_bridge_handler.py` | `start_bridge` docstring only | +4 / −2 | Low |
| `docs/ARCHITECTURE.md` | One paragraph in §3.2a | +5 | Low |

---

## 5. Implementation Order

1. `transport/telegram.py` — constant, `connect`, `_session`, `_authenticate`, `_fail_connect`, `_get_updates` argument, `_poll_loop` call, docstrings. Do not add a thread around `asyncio.run` in the handler.
2. Run `python -m pytest tests/test_telegram_transport.py -q`. Update the five tests in §2.3 until this file is green. `test_connect_no_token_raises` must still raise.
3. Add `test_connect_returns_while_getme_is_blocked` and `test_first_get_updates_drains_then_long_polls`. Re-run the same file.
4. Docstring in `start_bridge`. Paragraph in `docs/ARCHITECTURE.md` §3.2a.
5. Run:

```bash
python -m pytest tests/test_telegram_transport.py tests/test_telegram_bridge_handler.py tests/test_window_telegram_bridge.py tests/test_transport_package.py -q
```

`test_transport_package.py` must still see `openclaw.CONNECT_TIMEOUT_SEC == 10.0`.

Verification after step 1, before declaring the transport done: `grep -n "CONNECT_TIMEOUT_SEC\|not connected after" transport/telegram.py` returns nothing. `grep -n "CONNECT_TIMEOUT_SEC" transport/openclaw.py` still finds the OpenClaw constant.

---

## 6. Acceptance Criteria

- [ ] Clicking Connect with a saved token and a paired chat paints Connecting and the window accepts other input while `getMe` and the first `getUpdates` are in flight. The desktop “not responding” dialog does not appear on a normal round trip.
- [ ] An idle bot (empty update queue) goes Connecting → Connected. It does not go through Offline. The feed does not get “Telegram bridge offline” for that click.
- [ ] A phone message sent after Connected shows up in the Supervisor chat once.
- [ ] A 401 or `ok: false` from `getMe` ends on Offline with the existing token card, no `getUpdates` request, and the HTTP client closed.
- [ ] `connect()` with an empty token still raises `TelegramAuthError` and starts no thread.
- [ ] After the link is up, `getUpdates` requests use `timeout` 50. The request that first announces the link uses `timeout` 0.
- [ ] Disconnect during `getMe` ends on Disconnected and does not then flip to Offline from the aborted `getMe`.
- [ ] `tests/test_telegram_bridge_handler.py` passes without edits, including the deferred-dispatch reconnect tests and `test_restart_tears_down_previous_transport`.
- [ ] `transport/openclaw.py` is unchanged.
- [ ] `docs/ARCHITECTURE.md` still contains `transport/telegram.py` and `TelegramBridgeHandler`.

---

## 7. Edge Cases

| Case | Expected behavior |
|---|---|
| Empty update queue | Drain poll returns `[]`, `on_connect` fires, state `connected`. Next poll uses `timeout` 50. No `on_error`. |
| Updates already waiting | Drain poll returns them, `on_connect` fires, then each update is `on_update`. Offset advances the same way it does today (`update_id + 1` after the consumer returns). |
| `getMe` raises (`httpx.ConnectError`, timeout, non-dict JSON) | Client closed. `on_error` text starts with `connect: getMe failed:`. Thread exits. State `error`. No `getUpdates`. |
| `getMe` HTTP 401 or `ok` false | Client closed. `on_error` is `connect: getMe rejected the token (status=…)`. No `getUpdates`. |
| `getMe` aborted because the user disconnected | `_stopping` is true. No `on_error`. `stop_bridge` sets `disconnected`. |
| First drain poll returns 409 | Existing conflict `on_error`, backoff, `announced` stays false, retry is another drain poll. No second poller is started by this spec. |
| Drain poll returns 5xx, then succeeds | `on_error` (“getUpdates soft failure — backing off”), then `on_connect`. Handler goes `error` then `connected`. This recovery already exists (`test_on_connect_reannounced_after_soft_error`). |
| Drain poll’s socket hangs | The poll thread blocks until the httpx timeout (`poll_timeout + 10`, 60 seconds). The GTK thread does not. The pip stays Connecting until `on_error` or success. There is no 10-second “not connected after” error. |
| `connect()` called twice on a live thread | Second call returns immediately. One thread. |
| User clicks Connect while state is `connecting` | Window calls `stop_bridge`, not a second `start_bridge`. |
| User clicks Connect again from `error` after a dead poll thread | `start_bridge` → `stop_bridge` joins a dead thread (no wait), drops it, builds a new transport. One poller. |
| Injected `connect` raises `ValueError` | `start_bridge`’s `except Exception` sets `error` before it returns. Deferred stale `on_disconnect` still loses to that `error`. |
| `timeout=0` passed to `_get_updates` | JSON body contains `"timeout": 0`, not 50. |
| Pairing | `connect()` returns on the pairing thread before `getMe`. A `getMe` failure still calls the pairing `on_error` and ends pairing. The first `getUpdates` is a drain; the following ones long-poll for the pairing message. |
| Supervisor reply mirrored to the phone | Unchanged. `send_message` can still block the GTK thread. Out of scope. |

---

## 8. ARCHITECTURE.md Updates Required

§3.2a, the paragraph added in §2.4. No other section. Do not change the `special:supervisor` wording in that section; it is outside this spec and the code constant is `agent:supervisor`.
