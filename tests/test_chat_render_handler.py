# tests/test_chat_render_handler.py
# Tests for ui/handlers/chat_render_handler.py — SPEC-06 SP4 surface repoint.
#
# SP4: append/stream paths route text → render_document (markdown → HTML →
# nh3 sanitize, ALWAYS in path) → per-session ChatSurface. Ruling R1: the
# surface owns the widget tree — render_sync returns None and
# on_bubble_ready fires with None (callers' `if bubble is not None` guards
# verified at chat_handler :228/:532 and agent_runtime_handler :2101/:2116/
# :2280/:2316/:2438). Spy surfaces (TextViewFallback instances pre-registered
# into handler._surfaces) keep these tests environment-independent.

import time

import gi

gi.require_version('Gtk', '4.0')
from gi.repository import Gtk

import ui.handlers.chat_render_handler as crh_module
from ui.handlers.chat_render_handler import ChatRenderHandler
from ui.views.chat_surface import TextViewFallback
from utils.escaping import escape_for_pango


class SpySurface(TextViewFallback):
    """TextViewFallback that records append_message calls (no WebKit needed)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.appended: list[dict] = []

    def append_message(self, role, html_fragment, agent_name=None):
        self.appended.append(
            {"role": role, "html": html_fragment, "agent": agent_name}
        )
        super().append_message(role, html_fragment, agent_name=agent_name)


class _SyncPool:
    """Inline executor: render_async's compose runs synchronously.

    The production pool is a shared 2-worker ThreadPoolExecutor — a drain
    barrier races against it (worker B can take the drain no-op while
    worker A is still composing). Unit pins for append/callback semantics
    don't need thread mechanics; the compose runs inline instead."""

    class _Done:
        def result(self, timeout=None):
            return None

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)
        return _SyncPool._Done()


def _spy_handler(**kw) -> tuple[ChatRenderHandler, SpySurface]:
    """Handler with a pre-registered spy surface for session_key 'sk'."""
    h = ChatRenderHandler(**kw)
    h._pool = _SyncPool()  # instance attr shadows the class-level executor
    spy = SpySurface()
    h._surfaces["sk"] = spy
    return h, spy


# ── SP4 core contract: surface append path ────────────────────────────────

class TestSurfaceAppendContract:
    """SP4: append/stream route through render_document → sanitized HTML."""

    def setup_method(self):
        self.handler, self.spy = _spy_handler()

    def test_render_sync_appends_sanitized_html(self):
        """Test 1: render_sync → surface.append_message with SANITIZED html —
        no raw <script>, link rel injected (composed entry, not bare md→html).
        NOTE: the probe uses an https link — the SP2 emitter renders
        javascript:-scheme links as plain <span> before nh3 ever runs."""
        out = self.handler.render_sync(
            "Agent",
            '[x](https://ok.example) and <script>evil()</script>',
            "sk",
        )
        assert out is None  # R1: surface owns the widget tree
        assert len(self.spy.appended) == 1
        html_arg = self.spy.appended[0]["html"]
        assert "<script" not in html_arg.lower()
        assert "<script" not in html_arg.lower()  # escaped, not executed
        assert 'rel="noopener noreferrer nofollow"' in html_arg  # sanitizer ran
        assert 'href="https://ok.example"' in html_arg

    def test_render_async_appends_sanitized_and_fires_none(self):
        """Test 2 (async path): off-thread compose → surface append;
        on_bubble_ready fires with None (R1 contract, see test below)."""
        results: list = []
        self.handler.render_async("Agent", "hello **world**", "sk",
                                  on_bubble_ready=results.append)
        assert len(self.spy.appended) == 1
        assert "<strong>world</strong>" in self.spy.appended[0]["html"]
        assert results == [None]  # R1: no bubble — the surface displayed it

    def test_on_bubble_ready_none_contract_sync_and_async(self):
        """Test 3: pin the R1 contract on both entries — callers append the
        return value / callback arg; None must be the guaranteed value."""
        async_results: list = []
        self.handler.render_async("You", "hi", "sk",
                                  on_bubble_ready=async_results.append)
        sync_result = self.handler.render_sync("Agent", "hi", "sk")
        assert async_results == [None]
        assert sync_result is None

    def test_role_mapping_and_agent_name_pass_through(self):
        """Roles map You→user/Agent→agent/System→system; agent_name forwards."""
        self.handler.render_sync("You", "a", "sk")
        self.handler.render_sync("Agent", "b", "sk", agent_name="Coder")
        self.handler.render_sync("System", "c", "sk")
        assert [a["role"] for a in self.spy.appended] == ["user", "agent", "system"]
        assert self.spy.appended[1]["agent"] == "Coder"

    def test_render_document_failure_falls_back_to_escaped_text(self, monkeypatch):
        """Test 6: composition raising must append ESCAPED raw text (still
        sanitized path — never raw markup, never a raise). Patches the
        HANDLER's binding (from-import copied the name at import time —
        patching render.html would miss the call site)."""

        def boom(text):
            raise RuntimeError("composition failed")

        monkeypatch.setattr(crh_module, "render_document", boom)
        self.handler.render_sync("Agent", "<b>raw & dangerous</b>", "sk")
        html_arg = self.spy.appended[0]["html"]
        assert "&lt;b&gt;raw &amp; dangerous&lt;/b&gt;" in html_arg
        assert "<b>" not in html_arg


# ── Per-session surfaces ──────────────────────────────────────────────────

class TestPerSessionSurfaces:
    """SP4: per-session surfaces, lazy creation, destroy-on-close (SP3
    destroy contract honored via surface.destroy()).

    The lazy factory is patched to yield SpySurfaces — handler tests must
    not instantiate real WebKit widgets (crashes the combined gate run;
    real-WebKit behavior is test_chat_surface.py's job)."""

    def _handler_with_spy_factory(self, monkeypatch):
        created: list = []

        def factory():
            s = SpySurface()
            created.append(s)
            return s

        monkeypatch.setattr(crh_module, "create_chat_surface", factory)
        return ChatRenderHandler(), created

    def test_two_sessions_get_two_surfaces(self, monkeypatch):
        """Test 5a: distinct session keys → distinct surface instances."""
        handler, created = self._handler_with_spy_factory(monkeypatch)
        handler.render_sync("Agent", "one", "s1")
        handler.render_sync("Agent", "two", "s2")
        assert set(handler._surfaces) == {"s1", "s2"}
        assert handler._surfaces["s1"] is not handler._surfaces["s2"]
        assert len(created) == 2  # exactly one surface per session

    def test_close_session_destroys_only_that_surface(self, monkeypatch):
        """Test 5b: closing one session destroys ONLY its surface — the
        other stays live (SP3 destroy contract runs on the closed one)."""
        handler, _created = self._handler_with_spy_factory(monkeypatch)
        s1 = handler._surface_for("s1")
        s2 = handler._surface_for("s2")
        s1_destroyed, s2_destroyed = [], []
        s1.destroy = lambda: s1_destroyed.append(True)
        s2.destroy = lambda: s2_destroyed.append(True)

        handler.close_session("s1")
        assert s1_destroyed == [True]   # closed session destroyed
        assert s2_destroyed == []       # other session untouched
        assert "s1" not in handler._surfaces
        assert handler._surface_for("s1") is not s1  # re-lazily created

    def test_close_session_idempotent_and_unknown_safe(self):
        """close_session on unknown/twice-closed keys is a no-op."""
        handler = ChatRenderHandler()
        handler.close_session("never-existed")
        handler.close_session("never-existed")  # twice-safe

    def test_destroyed_surface_swallows_late_append(self, monkeypatch):
        """Append racing a close: close_session REMOVES the routing — the
        destroyed spy never sees the late row, and no raise escapes."""
        handler, _created = self._handler_with_spy_factory(monkeypatch)
        spy = SpySurface()
        handler._surfaces["s1"] = spy
        handler.render_sync("Agent", "first", "s1")
        assert len(spy.appended) == 1
        handler.close_session("s1")
        handler.render_sync("Agent", "late", "s1")  # must not raise
        assert len(spy.appended) == 1  # the destroyed surface saw nothing more


# ── Streaming lifecycle (buffered; atomic final row) ──────────────────────

class TestStreamingSurfaceLifecycle:
    """SP4: start/update buffer only; end_streaming renders ONE atomic row."""

    def setup_method(self):
        self.handler, self.spy = _spy_handler()

    def test_is_streaming_false_initially(self):
        assert self.handler.is_streaming("sk") is False

    def test_start_then_update_renders_nothing(self):
        """Test 7 (buffer half): deltas buffer — no surface rows mid-stream."""
        self.handler.start_streaming("sk")
        self.handler.update_streaming("sk", "chunk one ")
        self.handler.update_streaming("sk", "chunk two")
        assert self.handler.is_streaming("sk") is True
        assert self.spy.appended == []  # nothing rendered while streaming
        assert self.handler.get_streaming_text("sk") == "chunk two"  # FULL replace

    def test_end_streaming_renders_single_atomic_sanitized_row(self):
        """Test 7 (end half): buffered text → ONE sanitized row (SP3 stream
        contract: atomic, sanitized at the composed entry)."""
        self.handler.start_streaming("sk")
        self.handler.update_streaming("sk", "use <script>x</script> please")
        self.handler.end_streaming("sk", agent_name="Coder")
        assert self.handler.is_streaming("sk") is False
        assert len(self.spy.appended) == 1
        html_arg = self.spy.appended[0]["html"]
        assert "<script" not in html_arg.lower()
        assert "&lt;script&gt;" in html_arg
        assert self.spy.appended[0]["agent"] == "Coder"

    def test_end_streaming_without_start_is_noop(self):
        """Test: end_streaming() is safe to call when no stream exists."""
        self.handler.end_streaming("sk")
        assert self.spy.appended == []

    def test_restart_finalizes_previous_stream(self):
        """start_streaming on an active session finalizes it first (no
        buffer loss — old v1 semantics preserved)."""
        self.handler.start_streaming("sk")
        self.handler.update_streaming("sk", "first stream")
        self.handler.start_streaming("sk")
        assert len(self.spy.appended) == 1
        assert "first stream" in self.spy.appended[0]["html"]
        assert self.handler.is_streaming("sk") is True  # new session live
        assert self.handler.get_streaming_text("sk") == ""

    def test_stream_role_carried_to_final_row(self):
        """FIX 6 (SP4 audit): start_streaming(role=...) is carried to the
        final row's role — You streams land as user rows, not agent rows."""
        self.handler.start_streaming("sk", role="You")
        self.handler.update_streaming("sk", "hello")
        self.handler.end_streaming("sk")
        assert len(self.spy.appended) == 1
        assert self.spy.appended[0]["role"] == "user"

    def test_stream_role_defaults_to_agent(self):
        """FIX 6: no role arg → the final row stays an agent row."""
        self.handler.start_streaming("sk")
        self.handler.update_streaming("sk", "hello")
        self.handler.end_streaming("sk")
        assert self.spy.appended[0]["role"] == "agent"

    def test_end_streaming_render_false_drops_buffer(self):
        """render=False (ARH non-streaming finalize path): buffer dropped,
        NO row — the caller renders final text via render_sync itself."""
        self.handler.start_streaming("sk")
        self.handler.update_streaming("sk", "will be re-rendered")
        self.handler.end_streaming("sk", render=False)
        assert self.spy.appended == []
        assert self.handler.is_streaming("sk") is False

    def test_end_streaming_clears_buffer_for_new_session(self):
        """A NEW stream with identical text still renders (no stale-skip
        inheritance — preserves the old B4 contract)."""
        self.handler.start_streaming("sk")
        self.handler.update_streaming("sk", "repeated across sessions")
        self.handler.end_streaming("sk")
        self.handler.start_streaming("sk")
        self.handler.update_streaming("sk", "repeated across sessions")
        self.handler.end_streaming("sk")
        assert len(self.spy.appended) == 2

    def test_skip_diagnostic_uses_logger_not_print(self, capsys):
        """update_streaming for unknown key: debug log only — stdout clean."""
        self.handler.update_streaming("agent:missing", "text")
        captured = capsys.readouterr()
        assert captured.out == ""


class TestStreamingReplaceContract:
    """set_streaming_text (ARH crabcard-cleaning dependency) must REPLACE
    the pending buffer — why the handler buffers instead of using
    surface.stream_delta (append-only by contract)."""

    def setup_method(self):
        self.handler, self.spy = _spy_handler()

    def test_set_streaming_text_overwrites_pending_buffer(self):
        self.handler.start_streaming("sk")
        self.handler.update_streaming("sk", "with crabcard blocks")
        assert self.handler.set_streaming_text("sk", "cleaned") is True
        self.handler.end_streaming("sk")
        assert self.spy.appended[0]["html"].count("cleaned") == 1
        assert "crabcard" not in self.spy.appended[0]["html"]

    def test_set_and_get_streaming_text_offline_keys(self):
        assert self.handler.set_streaming_text("nope", "x") is False
        assert self.handler.get_streaming_text("nope") is None


# ── Reentrancy (kept from v1 — unchanged contract) ────────────────────────

class TestReentrancyGuard:
    """Reentrancy guard survives the repoint (concurrent renders skipped)."""

    def setup_method(self):
        self.handler, self.spy = _spy_handler()

    def test_async_blocks_duplicate_session_key(self, monkeypatch):
        """Second render_async for an in-flight key is skipped — first wins.

        Uses the REAL pool + a gate: render_async adds the key to the guard
        SYNCHRONOUSLY before submitting, so the second call is deterministically
        blocked while the first compose is parked. (With the inline test pool
        the whole render completes synchronously and the guard is released by
        the time render_async returns — unobservable without real threads.)"""
        import threading

        gate = threading.Event()
        real_compose = crh_module.render_document  # capture BEFORE patching

        def gated_compose(text):
            gate.wait(timeout=5)  # first render parks here — in-flight
            return real_compose(text)

        monkeypatch.setattr(crh_module, "render_document", gated_compose)
        handler = ChatRenderHandler()  # REAL shared pool
        spy = SpySurface()
        handler._surfaces["sk"] = spy
        results: list = []
        handler.render_async("Agent", "first", "sk", on_bubble_ready=results.append)
        handler.render_async("Agent", "second", "sk", on_bubble_ready=results.append)
        gate.set()
        deadline = time.monotonic() + 5
        while len(results) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert results == [None]
        assert len(spy.appended) == 1
        assert "first" in spy.appended[0]["html"]

    def test_reentrancy_released_after_render(self):
        """The guard releases after completion — the NEXT render succeeds."""
        self.handler.render_sync("Agent", "one", "sk")
        self.handler.render_sync("Agent", "two", "sk")
        assert len(self.spy.appended) == 2

    def test_reentrancy_set_basics(self):
        guard = self.handler._reentrancy
        assert "k" not in guard
        assert guard.add("k") is True
        assert guard.add("k") is False  # in-flight
        assert "k" in guard
        guard.remove("k")
        assert "k" not in guard
        guard.remove("k")  # no-op on missing


# ── Event cards: UNCHANGED Pango path (R3 — not transcript sites) ─────────

class TestPhase4EventCards:
    """render_event_card stays Pango (ruling R2/R3 — cards are not chat
    transcript sites and are not in the pango catalog)."""

    def setup_method(self):
        self.handler = ChatRenderHandler(GLib_module=None)
        self.fake_box = FakeChatBox()

    def test_file_read_card(self):
        self.handler.render_event_card("file_read", self.fake_box,
                                      file_path="src/main.py",
                                      snippet="print('hello')",
                                      line_range="1-3")
        assert len(self.fake_box._children) == 1
        assert self.fake_box._children[0].get_halign() == Gtk.Align.START

    def test_edit_proposal_card(self):
        self.handler.render_event_card("edit_proposal", self.fake_box,
                                      file_path="src/main.py",
                                      diff="- old\n+ new")
        assert len(self.fake_box._children) == 1

    def test_tool_call_card(self):
        self.handler.render_event_card("tool_call", self.fake_box,
                                      tool_name="ReadFile",
                                      detail="path=README.md")
        assert len(self.fake_box._children) == 1

    def test_error_bubble(self):
        self.handler.render_event_card("error", self.fake_box,
                                      error_msg="File not found")
        assert len(self.fake_box._children) == 1

    def test_unknown_event_type_silent(self):
        self.handler.render_event_card("unknown_type", self.fake_box)
        assert self.fake_box._children == []


class FakeChatBox:
    """Minimal Gtk.Box stand-in for testing card append/remove."""

    def __init__(self):
        self._children = []

    def append(self, widget):
        self._children.append(widget)

    def remove(self, widget):
        self._children.remove(widget)

    def __contains__(self, widget):
        return widget in self._children

    def get_first_child(self):
        return self._children[0] if self._children else None


# ── Legacy escape semantics (kept: escape util itself unchanged) ──────────

class TestEscapeUtil:
    """The escape util's own semantics are unchanged by SP4 (the surface
    pipeline uses render_document; this pins the util for remaining Pango
    sites — event cards and unconverted surfaces)."""

    def test_xss_prevention(self):
        escaped = escape_for_pango("<script>evil()</script>")
        assert "&lt;script&gt;" in escaped
