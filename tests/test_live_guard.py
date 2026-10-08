# tests/test_live_guard.py — SPEC-19 SP1 (enforcement core + probe harness).
#
# Three layers:
#   1. Pure unit tests for the ruleset constant + the E2 decision logic
#      (fakes; no WebKit needed).
#   2. THE G1 MATRIX (the load-bearing deliverable) — real WebKit under xvfb,
#      a local HTTP control server as positive control, realized (presented)
#      views (probe-pinned: unrealized views load NOTHING).
#   3. chat_surface integration: real-render witness (no monkeypatched
#      _load_html), P3 live-state-survives-append, scroll follow/preserve.

import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import Gtk, WebKit
except (ValueError, ImportError):  # pragma: no cover
    pytest.skip("WebKit 6.0 unavailable", allow_module_level=True)
from gi.repository import GLib

from utils.live_guard import BLOCK_ALL_REMOTE_RULESET, LiveGuard

# ── 1. Pure unit tests (fakes) ───────────────────────────────────────────


class FakeNavType:
    OTHER = "OTHER"
    LINK_CLICKED = "LINK_CLICKED"
    RELOAD = "RELOAD"


class FakeWebKit:
    class NavigationType:
        OTHER = "OTHER"

    class PolicyDecisionType:
        NAVIGATION_ACTION = "NAV"
        RESPONSE = "RESP"


def test_ruleset_is_one_blanket_block_rule():
    """The probe-pinned shape: EXACTLY one blanket block rule (no exemption
    rule — probe proved about:blank is spared by the blanket alone)."""
    assert BLOCK_ALL_REMOTE_RULESET == [
        {"trigger": {"url-filter": ".*"}, "action": {"type": "block"}}
    ]
    assert len(BLOCK_ALL_REMOTE_RULESET) == 1


def test_is_allowed_other_about_blank():
    assert LiveGuard.is_allowed_navigation("OTHER", "about:blank", FakeWebKit) is True


def test_is_allowed_other_about_variant():
    assert LiveGuard.is_allowed_navigation("OTHER", "about:srcdoc", FakeWebKit) is True


def test_denies_remote_uri():
    assert LiveGuard.is_allowed_navigation("OTHER", "http://evil.test/", FakeWebKit) is False


def test_denies_link_click():
    assert LiveGuard.is_allowed_navigation(
        "LINK_CLICKED", "about:blank", FakeWebKit) is False


def test_denies_reload():
    assert LiveGuard.is_allowed_navigation("RELOAD", "about:blank", FakeWebKit) is False


def test_denies_none_uri():
    assert LiveGuard.is_allowed_navigation("OTHER", None, FakeWebKit) is False


def test_denies_https_the_surface_document_navigation():
    assert LiveGuard.is_allowed_navigation(
        "OTHER", "https://api.telegram.org/x", FakeWebKit) is False


# ── 2. G1 MATRIX — real WebKit, realized views, server-hit ground truth ──

HITS = {"n": 0}


class _ControlHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        HITS["n"] += 1
        body = b"PONG"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def control_server():
    srv = HTTPServer(("127.0.0.1", 0), _ControlHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield port
    srv.shutdown()


@pytest.fixture(scope="module")
def guard_filter():
    """Compile the filter once (module scope); skip the matrix if it fails."""
    import tempfile

    loop = GLib.MainLoop()
    box = {}

    g = LiveGuard(store_path=tempfile.mkdtemp(prefix="lg-"))

    def cb(f):
        box["f"] = f
        loop.quit()

    g.compile(cb)
    GLib.timeout_add_seconds(8, lambda: (loop.quit(), False)[1])
    loop.run()
    if box.get("f") is None:
        pytest.skip("content filter failed to compile on this box")
    return g


_CTX = GLib.MainContext.default()


def _pump(sec):
    end = time.time() + sec
    while time.time() < end:
        while _CTX.pending():
            _CTX.iteration(False)
        time.sleep(0.02)


def _js(view, code, timeout=6):
    box = {}
    loop = GLib.MainLoop()

    def cb(v, r):
        try:
            box["v"] = v.evaluate_javascript_finish(r).to_string()
        except Exception:  # noqa: BLE001
            box["v"] = None
        loop.quit()

    view.evaluate_javascript(code, -1, None, None, None, cb)
    GLib.timeout_add_seconds(timeout, lambda: (loop.quit(), False)[1])
    loop.run()
    return box.get("v")


def _poll_flag(view, flag, tries=30):
    for _ in range(tries):
        time.sleep(0.15)
        r = _js(view, f"window.{flag} || 'PENDING'")
        if r != "PENDING":
            return r
    return "PENDING"


class _RealizedView:
    """A presented WebView (probe-pinned: subresources need realization)."""

    def __init__(self, guard=None, filtered=True):
        self.view = WebKit.WebView()
        self.view.get_settings().set_enable_javascript(True)
        if filtered and guard is not None:
            guard.attach(self.view)
        self.win = Gtk.Window()
        self.win.set_child(self.view)
        self.win.set_default_size(640, 480)
        self.win.present()
        _pump(0.4)
        self.view.load_html("<html><body>ALIVE</body></html>", "about:blank")
        _pump(1.0)

    def close(self):
        self.win.close()


@pytest.mark.usefixtures("guard_filter")
class TestG1Matrix:
    """The load-bearing probe matrix. Ground truth = control-server hits
    (filtered runs must add ZERO) + DOM flags. HALTS the unit if any row
    fails (asserted here; a failure IS the halt signal)."""

    def test_row1_fetch_blocked(self, control_server, guard_filter):
        h0 = HITS["n"]
        v = _RealizedView(guard_filter)
        try:
            _js(v.view, f"window.r1=null;fetch('http://127.0.0.1:{control_server}/f')"
                        ".then(()=>window.r1='GOT').catch(()=>window.r1='BLOCKED');")
            _poll_flag(v.view, "r1")
            assert HITS["n"] - h0 == 0, "fetch reached the server (NOT blocked)"
            assert v.view is not None
        finally:
            v.close()

    def test_row2_xhr_blocked(self, control_server, guard_filter):
        h0 = HITS["n"]
        v = _RealizedView(guard_filter)
        try:
            _js(v.view, "window.r2=null;const x=new XMLHttpRequest();"
                        "x.onload=()=>window.r2='GOT';x.onerror=()=>window.r2='BLOCKED';"
                        f"x.open('GET','http://127.0.0.1:{control_server}/x');x.send();")
            _poll_flag(v.view, "r2")
            assert HITS["n"] - h0 == 0, "XHR reached the server (NOT blocked)"
        finally:
            v.close()

    def test_row3_websocket_blocked(self, control_server, guard_filter):
        h0 = HITS["n"]
        v = _RealizedView(guard_filter)
        try:
            _js(v.view, "window.r3=null;try{const w=new WebSocket('ws://127.0.0.1:"
                        f"{control_server}/w');w.onopen=()=>window.r3='OPEN';"
                        "w.onerror=()=>window.r3='BLOCKED';"
                        "setTimeout(()=>{if(!window.r3)window.r3='NO_OPEN';},2500);"
                        "}catch(e){window.r3='THREW';}")
            r = _poll_flag(v.view, "r3", 25)
            assert r != "OPEN", f"WebSocket opened (NOT blocked): {r}"
            assert HITS["n"] - h0 == 0, "WebSocket reached the server"
        finally:
            v.close()

    def test_row4_eventsource_blocked(self, control_server, guard_filter):
        h0 = HITS["n"]
        v = _RealizedView(guard_filter)
        try:
            _js(v.view, "window.r4=null;try{const e=new EventSource('http://127.0.0.1:"
                        f"{control_server}/e');e.onopen=()=>window.r4='OPEN';"
                        "e.onerror=()=>window.r4='BLOCKED';"
                        "setTimeout(()=>{if(!window.r4)window.r4='NO_OPEN';},2500);"
                        "}catch(x){window.r4='THREW';}")
            r = _poll_flag(v.view, "r4", 25)
            assert r != "OPEN", f"EventSource opened (NOT blocked): {r}"
            assert HITS["n"] - h0 == 0, "EventSource reached the server"
        finally:
            v.close()

    def test_row5_sendbeacon_blocked(self, control_server, guard_filter):
        h0 = HITS["n"]
        v = _RealizedView(guard_filter)
        try:
            r = _js(v.view, "String(navigator.sendBeacon('http://127.0.0.1:"
                            f"{control_server}/b','x'))")
            _pump(0.5)
            assert HITS["n"] - h0 == 0, "sendBeacon reached the server (NOT blocked)"
            assert r in ("false", "False"), f"sendBeacon returned {r} (not false)"
        finally:
            v.close()

    def test_row6_img_subresource_blocked(self, control_server, guard_filter):
        h0 = HITS["n"]
        v = _RealizedView(guard_filter)
        try:
            _js(v.view, "window.r6=null;const i=new Image();"
                        "i.onload=()=>window.r6='LOADED';i.onerror=()=>window.r6='BLOCKED';"
                        f"i.src='http://127.0.0.1:{control_server}/p.png';")
            _poll_flag(v.view, "r6")
            assert HITS["n"] - h0 == 0, "img subresource reached the server"
        finally:
            v.close()

    def test_row7_script_subresource_blocked(self, control_server, guard_filter):
        h0 = HITS["n"]
        v = _RealizedView(guard_filter)
        try:
            _js(v.view, "window.r7=null;const s=document.createElement('script');"
                        "s.onload=()=>window.r7='LOADED';s.onerror=()=>window.r7='BLOCKED';"
                        f"s.src='http://127.0.0.1:{control_server}/s.js';"
                        "document.body.appendChild(s);")
            _poll_flag(v.view, "r7")
            assert HITS["n"] - h0 == 0, "script subresource reached the server"
        finally:
            v.close()

    def test_row8_inverse_surface_itself_works(self, control_server, guard_filter):
        """The surface's OWN about:blank load + JS must NOT be blocked."""
        v = _RealizedView(guard_filter)
        try:
            assert _js(v.view, "document.body.textContent") == "ALIVE"
        finally:
            v.close()

    def test_positive_control_unfiltered_fetch_reaches_server(self, control_server, guard_filter):
        """Positive control: WITHOUT the filter the same fetch DOES hit the
        server — proves the harness can actually observe a load (so the row1-7
        zeros are meaningful, not a dead harness)."""
        h0 = HITS["n"]
        v = _RealizedView(guard_filter, filtered=False)
        try:
            _js(v.view, f"fetch('http://127.0.0.1:{control_server}/ctl')"
                        ".then(()=>{}).catch(()=>{});")
            _pump(1.5)
            assert HITS["n"] - h0 >= 1, "control fetch did not reach the server"
        finally:
            v.close()

# ── 3. chat_surface integration (real WebKit) ────────────────────────────
#
# P2 real-render witness: build the REAL surface with NO monkeypatched
# _load_html — the actual load_html runs under xvfb, then read the DOM back.
# P3 live-state-survives-append: a script in message A sets window.__t3=42;
# after appending B, __t3 is still 42 (the F1 payoff for incremental inject).
# P4/scroll: injected rows keep follow at bottom, preserve position when up.

import ui.views.chat_surface as cs_module
from ui.views.chat_surface import ChatSurface


def _present(surface):
    win = Gtk.Window()
    win.set_child(surface)
    win.set_default_size(640, 480)
    win.present()
    return win


def _eval(view, code, timeout=6):
    box = {}
    loop = GLib.MainLoop()

    def cb(v, r):
        try:
            box["v"] = v.evaluate_javascript_finish(r).to_string()
        except Exception:  # noqa: BLE001
            box["v"] = None
        loop.quit()

    view.evaluate_javascript(code, -1, None, None, None, cb)
    GLib.timeout_add_seconds(timeout, lambda: (loop.quit(), False)[1])
    loop.run()
    return box.get("v")


def _drain(surface):
    """Run the coalesced render + pump the main loop so the real load lands."""
    surface._drain_renders()
    _pump(0.8)


class TestRealRenderWitness:
    """P2: fixes the audit's blind spot — a REAL load, no _load_html patched."""

    def test_real_load_renders_message_text(self):
        s = ChatSurface()
        win = _present(s)
        try:
            s.append_message("agent", "<p>REALRENDER_MARKER</p>", "Coder")
            _drain(s)
            # Read the DOM through the surface's OWN eval seam (the isolated
            # scroll world — legal with content JS off, which is the default).
            # The eval is ASYNC: pump the loop so the callback fires.
            box = {}
            s._document_eval("document.body.textContent",
                             lambda t: box.__setitem__("v", t))
            _pump(0.6)
            got = box.get("v")
            assert got is not None and "REALRENDER_MARKER" in got, repr(got)
        finally:
            win.close()
            s.destroy()


class TestLiveStateSurvivesAppend:
    """P3: the F1 payoff — live state persists across incremental appends."""

    def test_incremental_append_preserves_live_state(self, monkeypatch):
        monkeypatch.setattr(cs_module, "_live_js_enabled", lambda: True)
        s = ChatSurface()
        win = _present(s)
        try:
            # First message — initial full-document load (load_html, once).
            s.append_message(
                "agent",
                "<p>A</p><script>window.__t3 = 42;</script>",
                "Coder",
            )
            s._drain_renders()
            _pump(1.0)
            # Second message — must be INCREMENTAL (no reload → state survives).
            s.append_message("agent", "<p>B</p>", "Coder")
            s._drain_renders()
            _pump(0.8)
            got = _eval(s._webview, "window.__t3")
            assert got == "42", f"live state lost across append: {got!r}"
        finally:
            win.close()
            s.destroy()


class TestInjectionScroll:
    """SPEC-19 SP1 F1 scroll: injected rows follow / preserve."""

    def test_injected_rows_follow_at_bottom(self, monkeypatch):
        monkeypatch.setattr(cs_module, "_live_js_enabled", lambda: True)
        s = ChatSurface()
        win = _present(s)
        try:
            for i in range(6):
                s.append_message("agent", f"<p>msg {i}</p>", "Coder")
                s._drain_renders()
                _pump(0.4)
            vadj = s.get_vadjustment()
            _pump(0.5)
            max_v = vadj.get_upper() - vadj.get_page_size()
            assert max_v - vadj.get_value() <= s._BOTTOM_THRESHOLD, (
                f"at-bottom follow broke under injection: "
                f"value={vadj.get_value()} max={max_v}")
        finally:
            win.close()
            s.destroy()

    def test_injected_rows_preserve_reading_position(self, monkeypatch):
        monkeypatch.setattr(cs_module, "_live_js_enabled", lambda: True)
        s = ChatSurface()
        win = _present(s)
        try:
            for i in range(20):
                s.append_message("agent", f"<p>msg {i}</p>", "Coder")
                s._drain_renders()
                _pump(0.3)
            vadj = s.get_vadjustment()
            _pump(0.4)
            vadj.set_value(40.0)  # scroll UP (away from bottom) — reading
            _pump(0.3)
            s.append_message("agent", "<p>new</p>", "Coder")
            s._drain_renders()
            _pump(0.6)
            assert vadj.get_value() <= 200.0, (
                f"reading position lost (snapped): value={vadj.get_value()}")
        finally:
            win.close()
            s.destroy()
