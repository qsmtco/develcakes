"""SPEC-21 SP3: gated per-host HTTPS keep-alive behind urlopen_with_ssl_retry."""
from __future__ import annotations

import http.client
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent.llm import streaming
from agent.llm.streaming import (
    _POOL,
    _POOL_IDLE_S,
    _try_pooled,
    urlopen_with_ssl_retry,
)


def _certs(dirpath: Path) -> tuple[str, str]:
    cert = dirpath / "cert.pem"
    key = dirpath / "key.pem"
    subprocess.check_call(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=localhost",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return str(cert), str(key)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body_in = self.rfile.read(n)
        self.server.hits += 1
        path = self.path.split("?", 1)[0]
        if path == "/sse":
            payload = b'data: {"choices":[]}\n\ndata: [DONE]\n\n'
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n")
            self.wfile.flush()
            return
        if path == "/err":
            body = b"rate limited"
            self.send_response(429)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/redir":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        if path == "/slow":
            if getattr(self.server, "slow_once", False):
                self.server.slow_once = False
                time.sleep(3)
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(body)
            return
        echo = body_in or b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(echo)))
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(echo)

    def do_GET(self):
        self.do_POST()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler):
        super().__init__(addr, handler)
        self.connections = 0
        self.hits = 0
        self.slow_once = False

    def get_request(self):
        self.connections += 1
        return super().get_request()


@pytest.fixture
def https_server(tmp_path):
    cert, key = _certs(tmp_path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv = _Server(("127.0.0.1", 0), _Handler)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


@pytest.fixture
def keepalive_on(monkeypatch):
    monkeypatch.setenv("DEVELCAKES_HTTP_KEEPALIVE", "1")
    monkeypatch.delenv("CRABCAKES_HTTP_KEEPALIVE", raising=False)
    # This machine's sandbox exports HTTPS_PROXY. Spec skips the pool when
    # getproxies() has any https entry; tests that exercise the pool must
    # see a direct path. The proxy-bypass test re-patches this.
    monkeypatch.setattr(urllib.request, "getproxies", dict)
    _POOL.clear()
    yield
    _POOL.clear()


@pytest.fixture
def unverified_https(monkeypatch):
    monkeypatch.setattr(ssl, "_create_default_https_context", ssl._create_unverified_context)


def _post(url: str, data: bytes = b"{}"):
    return urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )


def test_gate_off_never_pools(monkeypatch, https_server):
    monkeypatch.delenv("DEVELCAKES_HTTP_KEEPALIVE", raising=False)
    monkeypatch.delenv("CRABCAKES_HTTP_KEEPALIVE", raising=False)
    called = []

    def fake(req, timeout=None):
        called.append(1)
        raise urllib.error.URLError("gate-off path")

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    req = _post(f"https://127.0.0.1:{https_server.server_address[1]}/ok")
    with pytest.raises(urllib.error.URLError):
        urlopen_with_ssl_retry(req, timeout=2)
    assert called == [1]
    assert _try_pooled(req, 2) is None


def test_reuse_one_connection(keepalive_on, unverified_https, https_server):
    port = https_server.server_address[1]
    url = f"https://127.0.0.1:{port}/ok"
    r1 = urlopen_with_ssl_retry(_post(url, b"one"), timeout=5)
    assert r1.read() == b"one"
    r1.close()
    r2 = urlopen_with_ssl_retry(_post(url, b"two"), timeout=5)
    assert r2.read() == b"two"
    r2.close()
    assert https_server.connections == 1


def test_sse_stop_at_done_reuses(keepalive_on, unverified_https, https_server):
    port = https_server.server_address[1]
    url = f"https://127.0.0.1:{port}/sse"
    resp = urlopen_with_ssl_retry(_post(url), timeout=5)
    saw_done = False
    for line in resp:
        if b"[DONE]" in line:
            saw_done = True
            break
    resp.close()
    assert saw_done
    resp2 = urlopen_with_ssl_retry(_post(url), timeout=5)
    resp2.read()
    resp2.close()
    assert https_server.connections == 1


def test_two_threads_do_not_share_connection(keepalive_on, unverified_https, https_server):
    port = https_server.server_address[1]
    url = f"https://127.0.0.1:{port}/ok"
    barrier = threading.Barrier(2)
    out = {}

    def worker(tag: bytes):
        barrier.wait()
        resp = urlopen_with_ssl_retry(_post(url, tag), timeout=5)
        out[tag] = resp.read()
        resp.close()

    t1 = threading.Thread(target=worker, args=(b"AAA",))
    t2 = threading.Thread(target=worker, args=(b"BBB",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert out[b"AAA"] == b"AAA"
    assert out[b"BBB"] == b"BBB"
    assert https_server.connections == 2


def test_http_429_raises_httperror_no_second_request(
    keepalive_on, unverified_https, https_server
):
    port = https_server.server_address[1]
    url = f"https://127.0.0.1:{port}/err"
    with pytest.raises(urllib.error.HTTPError) as ei:
        urlopen_with_ssl_retry(_post(url), timeout=5)
    assert ei.value.code == 429
    assert ei.value.read() == b"rate limited"
    assert https_server.hits == 1


def test_http_302_falls_back_to_urlopen(keepalive_on, unverified_https, https_server, monkeypatch):
    port = https_server.server_address[1]
    url = f"https://127.0.0.1:{port}/redir"
    called = []

    def fake(req, timeout=None):
        called.append(req.full_url)

        class _Resp:
            def read(self, *a):
                return b"followed"

            def close(self):
                pass

        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    resp = urlopen_with_ssl_retry(_post(url), timeout=5)
    assert called
    assert resp.read() == b"followed"


def test_idle_expiry_opens_fresh(keepalive_on, unverified_https, https_server, monkeypatch):
    clock = {"t": 100.0}
    monkeypatch.setattr(streaming.time, "monotonic", lambda: clock["t"])
    port = https_server.server_address[1]
    url = f"https://127.0.0.1:{port}/ok"
    r1 = urlopen_with_ssl_retry(_post(url, b"a"), timeout=5)
    r1.read()
    r1.close()
    clock["t"] = 100.0 + _POOL_IDLE_S + 1
    r2 = urlopen_with_ssl_retry(_post(url, b"b"), timeout=5)
    r2.read()
    r2.close()
    assert https_server.connections == 2


def test_cannot_send_request_on_reused_falls_back(
    keepalive_on, unverified_https, https_server, monkeypatch
):
    """CannotSendRequest is HTTPException, not OSError — must still stale-fallback."""
    slept = []
    monkeypatch.setattr(streaming.time, "sleep", lambda s: slept.append(s))
    port = https_server.server_address[1]
    origin = ("https", "127.0.0.1", port)

    class Dead(http.client.HTTPSConnection):
        def request(self, *a, **k):
            raise http.client.CannotSendRequest("Request-sent")

    dead = Dead("127.0.0.1", port, timeout=2)
    _POOL[origin] = [(dead, time.monotonic())]
    resp = urlopen_with_ssl_retry(_post(f"https://127.0.0.1:{port}/ok", b"z"), timeout=5)
    assert resp.read() == b"z"
    resp.close()
    assert slept == []


def test_stale_reused_socket_falls_back_without_sleep(
    keepalive_on, unverified_https, https_server, monkeypatch
):
    slept = []
    monkeypatch.setattr(streaming.time, "sleep", lambda s: slept.append(s))
    port = https_server.server_address[1]
    origin = ("https", "127.0.0.1", port)
    dead = http.client.HTTPSConnection("127.0.0.1", port, timeout=2)
    # Closed before use — checkout will treat it as reused and fail send.
    dead.close()
    _POOL[origin] = [(dead, time.monotonic())]
    resp = urlopen_with_ssl_retry(_post(f"https://127.0.0.1:{port}/ok", b"z"), timeout=5)
    assert resp.read() == b"z"
    resp.close()
    assert slept == []


def test_timeout_reaches_retry_loop(keepalive_on, unverified_https, https_server, monkeypatch):
    slept = []
    monkeypatch.setattr(streaming.time, "sleep", lambda s: slept.append(s))
    https_server.slow_once = True
    port = https_server.server_address[1]
    resp = urlopen_with_ssl_retry(
        _post(f"https://127.0.0.1:{port}/slow"), timeout=0.3
    )
    assert resp.read() == b"ok"
    resp.close()
    assert slept, "TimeoutError must enter the retry loop, not silent urlopen fallback"


def test_proxy_bypasses_pool(keepalive_on, monkeypatch):
    monkeypatch.setattr(
        urllib.request, "getproxies", lambda: {"https": "http://proxy.example:8080"}
    )
    req = _post("https://example.com/v1")
    assert _try_pooled(req, 2) is None


def test_non_https_and_get_never_enter_pool(keepalive_on):
    http_req = urllib.request.Request("http://127.0.0.1:9/x", data=b"{}", method="POST")
    get_req = urllib.request.Request("https://example.com/x", method="GET")
    assert _try_pooled(http_req, 2) is None
    assert _try_pooled(get_req, 2) is None
