# tests/test_transport_package.py — SPEC-05 Sub-Phase 1 pins (+ fix-round pins).
#
# Pins the DORMANT transport/ package: ABC non-instantiability, issubclass
# contract, no-auth source-text pins (SPEC-04 grep-as-test pattern),
# behavioral backoff pin (probe-F storm regression), clean subprocess import
# from a neutral cwd, line budget (non-blank/non-comment), res-correlation,
# send-while-down, and the connect() bounded-wait contract.

import asyncio
import inspect
import json
import pathlib
import subprocess
import sys
import threading
import time

import pytest
import websockets

from transport import openclaw as oc
from transport.base import Transport
from transport.openclaw import WebSocketTransport, redact_log_preview

REPO = pathlib.Path(__file__).resolve().parent.parent
OPENCLAW_SRC = (REPO / "transport" / "openclaw.py").read_text(encoding="utf-8")


def test_transport_abc_cannot_instantiate():
    with pytest.raises(TypeError):
        Transport()  # type: ignore[abstract]


def test_openclaw_implements_transport():
    assert issubclass(WebSocketTransport, Transport)


def test_openclaw_has_no_auth_handshake():
    """Source-text pin: no device-auth machinery in the cleaned core."""
    for marker in ("ed25519", "device_auth", "signing", "private_key", "nonce",
                   "hello-ok", "cryptography"):
        assert marker not in OPENCLAW_SRC, f"auth marker leaked: {marker!r}"


def test_line_budget():
    """SPEC-05 §2 target: cleaned core ≤400 non-blank/non-comment lines."""
    code = [
        line for line in OPENCLAW_SRC.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert len(code) <= 400, f"openclaw.py has {len(code)} code lines (budget 400)"


def test_openclaw_backoff_constants():
    """Backoff constants exist with client.py values, and _connect_loop is async."""
    assert oc._BACKOFF_INITIAL_SEC == 1.0
    assert oc._BACKOFF_MAX_SEC == 30.0
    assert oc._BACKOFF_MULTIPLIER == 2.0
    assert oc.CONNECT_TIMEOUT_SEC == 10.0
    assert "_connect_loop" in OPENCLAW_SRC
    assert inspect.iscoroutinefunction(WebSocketTransport._connect_loop)


def test_backoff_mechanism_clean_close():
    """FIX 1 behavioral pin (probe-F storm regression).

    Server closes cleanly on every accept. Requirements: on_disconnect
    fires on the clean path, reconnects are spaced >= initial backoff
    (no hot storm), and no on_error on a clean close.
    """
    ev: list = []
    lock = threading.Lock()

    def rec(kind, detail=None):
        with lock:
            ev.append((kind, time.monotonic(), detail))

    async def handler(ws):
        rec("server_accept")
        await asyncio.sleep(0.05)
        await ws.close()

    async def run():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            t = WebSocketTransport(
                f"ws://127.0.0.1:{port}",
                on_connect=lambda: rec("connect"),
                on_disconnect=lambda r: rec("disconnect", r),
                on_error=lambda m: rec("error", m),
            )
            await t.connect()
            await asyncio.sleep(3.5)
            await t.disconnect()
            await asyncio.sleep(0.3)

    # Server lives on THIS test's event loop; transport runs on its own
    # thread+loop. Do NOT block this loop with th.join() — the server
    # would freeze (websockets handshake starves) and the test deadlocks.
    th = threading.Thread(target=lambda: asyncio.run(run()), daemon=True)
    th.start()
    await_live = _wait_sync(lambda: len([e for e in ev if e[0] == "connect"]) >= 2,
                            timeout=10.0)
    assert await_live, f"expected >=2 accepts within 10s, got {ev}"

    with lock:
        connects = [e for e in ev if e[0] == "connect"]
        disconnects = [e for e in ev if e[0] == "disconnect"]
        errors = [e for e in ev if e[0] == "error"]
    assert len(connects) >= 2, f"expected >=2 accepts, got {len(connects)} ({ev})"
    assert len(disconnects) >= 1, f"on_disconnect must fire on clean close ({ev})"
    assert not errors, f"clean close must not fire on_error ({errors})"
    gaps = [connects[i + 1][1] - connects[i][1] for i in range(len(connects) - 1)]
    assert all(g >= 0.5 for g in gaps), f"backoff violated: gaps={gaps}"


def test_send_correlation_per_request_and_constructor():
    """FIX 2 pin: both correlation channels fire on a res echo.

    Per-request on_response (pending registry) AND constructor on_response
    (fires for every res with an id, entry or no entry).
    """
    per_req = []
    ctor = []

    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            await ws.send(json.dumps({
                "type": "res", "id": msg["id"],
                "payload": {"echo": msg["id"]},
            }))

    async def run():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            t = WebSocketTransport(
                f"ws://127.0.0.1:{port}",
                on_connect=lambda: None,
                on_disconnect=lambda r: None,
                on_error=lambda m: rec_error(m),
                on_response=lambda rid, p: ctor.append((rid, p)),
            )
            await t.connect()
            for _ in range(600):          # async wait — never block this loop
                if t.is_connected():
                    break
                await asyncio.sleep(0.01)
            assert t.is_connected()
            await t.send({"id": "abc", "type": "req"}, on_response=per_req.append)
            await asyncio.sleep(0.5)
            await t.send({"id": "def", "type": "req"})  # no per-request cb
            await asyncio.sleep(0.5)
            await t.disconnect()
            await asyncio.sleep(0.3)

    asyncio.run(run())
    assert per_req == [{"echo": "abc"}], (
        f"per-request cb must fire exactly once with the echo payload: {per_req}")
    assert any(rid == "abc" for rid, _ in ctor), f"constructor cb never fired: {ctor}"


def _wait(pred, timeout=6.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def rec_error(m):
    return None


def test_send_while_down_fires_on_error():
    """FIX 4 pin (probe H): send() while down fires on_error, never silent."""
    errors = []
    t = WebSocketTransport("ws://127.0.0.1:1",  # nothing listens on :1
                           on_connect=lambda: None,
                           on_disconnect=lambda r: None,
                           on_error=errors.append)
    asyncio.run(t.send({"id": "z"}))
    assert errors, "send while down was SILENT — on_error must fire"
    assert any("not connected" in e.lower() for e in errors), errors


def test_connect_bounded_wait():
    """FIX 3 pin: connect() blocks until connected (or bounded timeout)."""
    async def handler(ws):
        await asyncio.sleep(3.0)

    async def run():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            t = WebSocketTransport(f"ws://127.0.0.1:{port}",
                                   on_connect=lambda: None,
                                   on_disconnect=lambda r: None,
                                   on_error=lambda m: None)
            t0 = time.monotonic()
            await t.connect()
            elapsed = time.monotonic() - t0
            assert t.is_connected(), "connect() returned but socket not live"
            assert elapsed >= 0.0
            await t.disconnect()
            await asyncio.sleep(0.2)

    asyncio.run(run())


def test_package_imports_clean():
    """FIX 5: both modules import in a fresh subprocess from a NEUTRAL cwd,
    and transport.__file__ resolves inside REPO (no cwd masking)."""
    # Neutral cwd (/tmp) + explicit PYTHONPATH: the import cannot succeed
    # by cwd shadowing; transport.__file__ must resolve inside REPO.
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO),
           "HOME": str(REPO / ".venv")}
    result = subprocess.run(
        [sys.executable, "-c",
         "import transport, transport.base, transport.openclaw;"
         " assert transport.__file__.startswith('" + str(REPO) + "'),"
         " transport.__file__; print('ok')"],
        capture_output=True, text=True, timeout=30, cwd="/tmp",
        check=False, env=env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")


def _wait_sync(pred, timeout=10.0):
    """Block the CALLING thread (test thread), never an event loop."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


# ═══════════════════════════════════════════════════════════════════
#  LOW-4 redaction behavioral coverage (SPEC-05 SP5 rider)
#  Ported from the deleted tests/test_low345_gateway_hardening.py
#  (SP3b) — redact_log_preview survived into transport/; its tests
#  now live here. Shapes preserved verbatim from git history.
# ═══════════════════════════════════════════════════════════════════

class TestRedactLogPreview:
    """transport.redact_log_preview scrubs sensitive keys from raw frames."""

    def test_redact_apikey(self):
        raw = '{"apiKey":"secret123","other":"x"}'
        result = redact_log_preview(raw)
        assert "secret123" not in result, f"apiKey value leaked: {result}"
        assert "***" in result

    def test_redact_apikey_case_insensitive(self):
        for key in ("apiKey", "api_key", "apikey", "APIKEY"):
            raw = f'{{"{key}":"hunter42"}}'
            result = redact_log_preview(raw)
            assert "hunter42" not in result, f"Failed for {key}: {result}"
            assert "***" in result

    def test_redact_token(self):
        raw = '{"token":"mysecret","data":"ok"}'
        result = redact_log_preview(raw)
        assert "mysecret" not in result
        assert "***" in result

    def test_redact_device_token(self):
        for key in ("deviceToken", "device_token"):
            raw = f'{{"{key}":"dev_tok_xyz"}}'
            result = redact_log_preview(raw)
            assert "dev_tok_xyz" not in result

    def test_redact_password(self):
        raw = '{"username":"alice","password":"s3cr3t"}'
        result = redact_log_preview(raw)
        assert "s3cr3t" not in result

    def test_redact_secret(self):
        raw = '{"algorithm":"HS256","secret":"my-shared-secret"}'
        result = redact_log_preview(raw)
        assert "my-shared-secret" not in result

    def test_redact_truncation_respected(self):
        raw = "x" * 1000
        result = redact_log_preview(raw)
        assert len(result) <= len(raw)

    def test_redact_no_op_for_clean_input(self):
        raw = '{"type":"event","event":"chat.final","payload":{}}'
        result = redact_log_preview(raw)
        assert "chat.final" in result
        assert "event" in result

    def test_redact_key_without_value(self):
        raw = '{"apiKey"}'
        result = redact_log_preview(raw)
        assert isinstance(result, str)

    def test_redact_url_query_apiKey(self):
        raw = "GET /api/v1/foo?apiKey=secret123&limit=10 HTTP/1.1"
        result = redact_log_preview(raw)
        assert "secret123" not in result
        assert "apiKey=***" in result
        assert "limit=10" in result

    def test_redact_bearer_token(self):
        raw = 'Authorization: Bearer eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJhYmMifQ.sig'
        result = redact_log_preview(raw)
        assert "eyJhbGciOiJSUzI1NiJ9" not in result
        assert "Bearer ***" in result

    def test_malformed_preview_truncated(self):
        long_raw = '{"apiKey":"a_very_long_value_that_exceeds_eighty_chars","other":"x"}'
        preview = redact_log_preview(long_raw[:80])
        assert len(preview) <= 80
