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
from unittest.mock import MagicMock

from gi.repository import GLib, Gtk

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


# ── SPEC-06 SP5a: surface mount + session lifecycle ───────────────────────

class TestSurfaceMountLifecycle:
    """SP5a FIX ROUND 1 (audit BUGs #1/#2/#3): handler owns mounting with
    an IDEMPOTENT-RETRY contract — every _surface_for call retries the
    mount until the surface has a parent, so a None-getter at creation is
    recovered on the next render (P1). Project-routed replies mount by the
    RESOLVED box key (mount_key, FIX 2). The surface owns its scroll; the
    mount appends DIRECTLY — no wrapper (FIX 3).
    R2: main_content._close_tab / close_project_tab release the surface
    via close_session (SP3 destroy contract)."""

    def _wired_handler(self, monkeypatch, boxes: dict):
        """Handler whose factory yields SpySurfaces and whose getter reads
        a dict of real Gtk.Boxes (mount targets)."""
        created: list = []

        def factory():
            s = SpySurface()
            created.append(s)
            return s

        monkeypatch.setattr(crh_module, "create_chat_surface", factory)
        handler = ChatRenderHandler()
        handler.set_chat_container_getter(lambda sk: boxes.get(sk))
        return handler, created

    def _children(self, box):
        out = []
        child = box.get_first_child()
        while child is not None:
            out.append(child)
            child = child.get_next_sibling()
        return out

    def _mounted_in(self, box, surface) -> bool:
        """True if surface is mounted DIRECTLY in box (FIX 3: the surface
        owns its ScrolledWindow — the mount appends the surface itself,
        no wrapper; the surface's own scroll lives inside the surface)."""
        return surface.get_parent() is box

    def test_render_mounts_surface_into_session_chat_box(self, monkeypatch):
        """Test 1: render → surface IS in the session's chat box, mounted
        DIRECTLY (no ScrolledWindow wrapper — the surface owns its scroll)."""
        boxes = {"sk": Gtk.Box()}
        handler, created = self._wired_handler(monkeypatch, boxes)
        handler.render_sync("Agent", "hello", "sk")
        assert len(created) == 1
        surface = handler._surfaces["sk"]
        assert surface.get_parent() is not None  # mount happened
        assert self._mounted_in(boxes["sk"], surface)

    def test_mount_once_second_render_does_not_repack(self, monkeypatch):
        """Test 2: mount-once pin — CHILD IDENTITY, not count. A second
        render never repacks: the box's child is the SAME widget after
        further renders (a repack/rebuild would swap the child)."""
        boxes = {"sk": Gtk.Box()}
        handler, _created = self._wired_handler(monkeypatch, boxes)
        handler.render_sync("Agent", "one", "sk")
        first = boxes["sk"].get_first_child()
        handler.render_sync("Agent", "two", "sk")
        handler.render_sync("Agent", "three", "sk")
        assert boxes["sk"].get_first_child() is first  # identity, not count

    def test_late_box_recovery_after_none_at_create(self, monkeypatch):
        """FIX 1 (BUG #1) — the P1 shape: render BEFORE the box exists →
        surface works unmounted; then the box APPEARS (project tab opened)
        → the NEXT render MOUNTS it (idempotent-retry). The old one-shot
        skip left the surface permanently unmounted (blank window)."""
        boxes: dict = {}
        handler, created = self._wired_handler(monkeypatch, boxes)
        handler.render_sync("Agent", "early", "sk")
        assert created[0].get_parent() is None  # no box yet — works unmounted
        # Box appears (project tab opened / getter now resolves).
        boxes["sk"] = Gtk.Box()
        handler.render_sync("Agent", "late", "sk")
        assert created[0].get_parent() is boxes["sk"]  # RECOVERED — mounted
        assert self._mounted_in(boxes["sk"], created[0])

    def test_project_routed_reply_mounts_in_project_box(self, monkeypatch):
        """FIX 2 (BUG #2) — the headline-use-case pin: a project-routed
        reply renders with session_key=<agent sk> but mount_key=<resolved
        project box key> → the surface mounts in the project tab's box.
        Personal-tab reply (no mount_key) still mounts under its own key."""
        agent_box = Gtk.Box()   # no tab for the agent session itself
        project_box = Gtk.Box()  # the visible project group-chat tab
        boxes = {"project:alpha": project_box, "agent:sk": agent_box}
        handler, created = self._wired_handler(monkeypatch, boxes)
        handler.render_sync("Agent", "to project", "agent:sk",
                            mount_key="project:alpha")
        assert created[0].get_parent() is project_box
        assert agent_box.get_first_child() is None  # NOT in the agent box
        # Personal-tab control: no mount_key → own session key.
        handler.render_sync("Agent", "personal", "agent:other")
        other = handler._surfaces["agent:other"]
        assert other.get_parent() is None  # no box for that key — unmounted, fine
        # And a direct-tab session with no mount_key mounts under its own key.
        handler.render_sync("Agent", "direct", "agent:sk", mount_key=None)
        second = handler._surfaces["agent:sk"]
        assert second is created[0]   # same cached surface (cache stays session-keyed)
        assert second.get_parent() is project_box  # mount-once: NOT remounted

    def test_surface_cache_stays_session_keyed_with_mount_key(self, monkeypatch):
        """FIX 2 — surface CACHE keyed by session_key (streaming continuity)
        while MOUNT uses mount_key: two renders with different mount_keys
        still hit ONE cached surface for the session."""
        boxes = {"project:alpha": Gtk.Box(), "project:beta": Gtk.Box()}
        handler, created = self._wired_handler(monkeypatch, boxes)
        handler.render_sync("Agent", "one", "agent:sk", mount_key="project:alpha")
        handler.render_sync("Agent", "two", "agent:sk", mount_key="project:beta")
        assert len(created) == 1  # one surface, session-keyed cache
        assert handler._surfaces["agent:sk"] is created[0]

    def test_surface_for_box_identity_lookup(self, monkeypatch):
        """FIX 3 seam: surface_for_box resolves the surface mounted in a
        given chat box (parent identity); None for boxes without one."""
        boxes = {"sk": Gtk.Box()}
        handler, created = self._wired_handler(monkeypatch, boxes)
        handler.render_sync("Agent", "hello", "sk")
        assert handler.surface_for_box(boxes["sk"]) is created[0]
        assert handler.surface_for_box(Gtk.Box()) is None

    def test_textview_fallback_mounts_identically(self, monkeypatch):
        """Test 3: WebKit=None → the TextViewFallback mounts through the
        SAME path (Gtk.Box base — parity)."""
        import ui.views.chat_surface as cs_module

        monkeypatch.setattr(cs_module, "WebKit", None)
        boxes = {"sk": Gtk.Box()}
        handler, created = self._wired_handler(monkeypatch, boxes)
        handler.render_sync("Agent", "hello", "sk")
        assert len(created) == 1
        assert isinstance(created[0], TextViewFallback)
        assert self._mounted_in(boxes["sk"], created[0])

    def test_close_tab_releases_session_surface(self, monkeypatch):
        """Test 4 (R2): main_content._close_tab → close_session(sk) — the
        surfaces dict drains and the surface's destroy contract ran."""
        from ui.views.main_content import MainContent

        handler, _created = self._wired_handler(monkeypatch, {})
        mc = MainContent.__new__(MainContent)  # shell pattern: tab_switch tests
        mc._chat_notebook = MagicMock(spec=Gtk.Notebook)
        mc._chat_notebook.get_n_pages.return_value = 0
        mc._tab_sessions = {0: "sk"}
        mc._tab_chat_boxes = {0: Gtk.Box()}
        mc._tab_scrolls = {}
        mc._tab_overlays = {}
        mc._bulk_closing = False
        mc._chat_render_handler = handler

        handler._surface_for("sk")
        spy = handler._surfaces["sk"]
        destroyed: list = []
        spy.destroy = lambda: destroyed.append(True)

        assert "sk" in handler._surfaces
        mc._close_tab(0)
        assert destroyed == [True]           # SP3 destroy contract ran
        assert "sk" not in handler._surfaces  # session state released

    def test_close_project_tab_fans_out_leaves_other_sessions(self, monkeypatch):
        """Test 5 (R2): close_project_tab closes its session via the SAME
        _close_tab path (single-point wiring) and does NOT touch other
        sessions' surfaces."""
        from ui.views.main_content import MainContent

        handler, _created = self._wired_handler(monkeypatch, {})
        mc = MainContent.__new__(MainContent)
        mc._chat_notebook = MagicMock(spec=Gtk.Notebook)
        mc._chat_notebook.get_n_pages.return_value = 0
        mc._tab_sessions = {0: "project:alpha"}
        mc._tab_chat_boxes = {0: Gtk.Box()}
        mc._tab_scrolls = {}
        mc._tab_overlays = {}
        mc._bulk_closing = False
        mc._chat_render_handler = handler
        # Stub the notebook scan (MagicMock notebook has no real pages):
        # close_project_tab resolves its session to page 0.
        mc._find_page_by_session = (
            lambda sk: 0 if sk == "project:alpha" else None
        )

        project_spy = handler._surface_for("project:alpha")
        other_spy = handler._surface_for("agent:unrelated")
        p_destroyed: list = []
        o_destroyed: list = []
        project_spy.destroy = lambda: p_destroyed.append(True)
        other_spy.destroy = lambda: o_destroyed.append(True)

        mc.close_project_tab("alpha")
        assert p_destroyed == [True]
        assert o_destroyed == []                 # other session untouched
        assert set(handler._surfaces) == {"agent:unrelated"}

    def test_end_to_end_transcript_reaches_mounted_surface(self, monkeypatch):
        """Test 6 — THE closes-the-window pin: render_async (real pool,
        real composition) → pump the main loop → the session's chat box
        CONTAINS the surface, and the surface's DISPLAY STATE holds the
        sanitized row. Asserts the DOCUMENT (the rendered text buffer —
        what the user actually sees), not the SpySurface.appended spy
        (BUG #6 hardening: the spy can record while the display drops)."""
        boxes = {"sk": Gtk.Box()}
        handler, created = self._wired_handler(monkeypatch, boxes)
        results: list = []
        handler.render_async(
            "Agent", 'hello **world** <script>evil()</script>', "sk",
            on_bubble_ready=results.append,
        )
        ctx = GLib.MainContext.default()
        deadline = time.monotonic() + 5.0
        while not results and time.monotonic() < deadline:
            while ctx.pending():
                ctx.iteration(False)
            time.sleep(0.005)
        assert results == [None]  # R1: callback fires with None
        assert len(created) == 1
        # The box physically holds the surface (mount happened at create).
        assert self._mounted_in(boxes["sk"], created[0])
        # THE DOCUMENT: built from the surface's own row state (the real
        # _rows → _document chain the WebView loads — not the append spy).
        # Sanitized markdown survived (bold element); the script ELEMENT is
        # gone — only its ESCAPED (inert) text form remains.
        from ui.views.chat_surface import _document

        doc = _document(list(created[0]._rows))
        assert "hello" in doc and "<strong>world</strong>" in doc
        assert "<script" not in doc.lower()      # element neutralized
        assert "&lt;script&gt;" in doc           # inert escaped form only


# ── SPEC-06 SP5a FIX ROUND 2: #7 streaming mount, #9 allocation, ───────────
#    #10+#4 mount-relationship lifecycle, #5 bulk drain, #11 caps + O(1) idx


class TestStreamingMountLifecycle:
    """Round 2: FIX 7 (mount_key through the STREAMING path — production
    ALWAYS streams, so round-1's render_sync-only wiring never fired on the
    real path), FIX 10/#4 (project close kills agent surfaces mounted
    there; tombstones drop late renders), FIX 11 (unmountable surfaces
    evicted after _MOUNT_MISS_LIMIT consecutive getter misses)."""

    def _wired(self, monkeypatch, boxes):
        """Real create-path handler wired to a dict of chat boxes; returns
        (handler, created_surfaces)."""
        created: list = []

        def factory():
            s = TextViewFallback()
            created.append(s)
            return s

        monkeypatch.setattr(crh_module, "create_chat_surface", factory)
        handler = ChatRenderHandler()
        handler.set_chat_container_getter(lambda sk: boxes.get(sk))
        return handler, created

    def test_streaming_end_mounts_surface_in_project_box(self, monkeypatch):
        """FIX 7 — THE headline pin: start → update → end_streaming with a
        PROJECT-ONLY getter (no personal tab) mounts the final row's surface
        in the project box. Falsifier: drop the mount_key threading (or the
        ARH caller) → the surface lands unmounted and this fails."""
        project_box = Gtk.Box()
        boxes = {"project:alpha": project_box}  # NO box for "agent:sk"
        handler, created = self._wired(monkeypatch, boxes)
        handler.start_streaming("agent:sk", role="Agent")
        handler.update_streaming("agent:sk", "final **answer**")
        handler.end_streaming("agent:sk", agent_name="Coder",
                              mount_key="project:alpha")
        assert len(created) == 1
        assert created[0].get_parent() is project_box
        from ui.views.chat_surface import _document
        assert "final" in _document(list(created[0]._rows))
        assert "<strong>answer</strong>" in _document(list(created[0]._rows))

    def test_close_project_kills_agent_surface_mounted_there(self, monkeypatch):
        """FIX 10 — closing the PROJECT session destroys the AGENT-keyed
        surface mounted in the project box and tombstones the agent key."""
        project_box = Gtk.Box()
        boxes = {"project:alpha": project_box}
        handler, created = self._wired(monkeypatch, boxes)
        handler.render_sync("Agent", "routed reply", "agent:sk",
                            mount_key="project:alpha")
        surface = created[0]
        assert surface.get_parent() is project_box
        destroyed: list = []
        surface.destroy = lambda: destroyed.append(True)
        handler.close_session("project:alpha")
        assert destroyed == [True]                     # mounted surface died
        assert "agent:sk" not in handler._surfaces     # agent key released
        assert handler._closed_sessions.get("agent:sk") is True

    def test_reopen_remounts_after_project_close(self, monkeypatch):
        """FIX 10 — reopen path: re-wiring (new getter) clears tombstones;
        the next render legitimately creates a FRESH surface and mounts it
        in the new project box."""
        project_box = Gtk.Box()
        handler, created = self._wired(monkeypatch, {"project:alpha": project_box})
        handler.render_sync("Agent", "first life", "agent:sk",
                            mount_key="project:alpha")
        handler.close_session("project:alpha")
        new_box = Gtk.Box()  # reopened tab = new widget tree
        handler.set_chat_container_getter(lambda sk: {("project:alpha"): new_box}.get(sk))
        handler.render_sync("Agent", "second life", "agent:sk",
                            mount_key="project:alpha")
        assert len(created) == 2                       # fresh surface, not the old one
        assert created[1].get_parent() is new_box

    def test_late_render_after_close_does_not_resurrect(self, monkeypatch):
        """FIX #4 — a render in flight when close_session lands must be
        DROPPED, not resurrect an unmounted orphan surface. Falsifier:
        remove the tombstone guard → a second surface is created+mounted."""
        box = Gtk.Box()
        handler, created = self._wired(monkeypatch, {"sk": box})
        handler.render_sync("Agent", "before close", "sk")
        assert len(created) == 1
        handler.close_session("sk")
        handler.render_sync("Agent", "late render", "sk")  # compose landed late
        assert len(created) == 1                        # NO new surface
        assert "sk" not in handler._surfaces
        assert box.get_first_child() is None            # nothing remounted

    def test_unmountable_surface_evicted_after_miss_cap(self, monkeypatch):
        """FIX 11 — a getter that returns None forever cannot accumulate
        surfaces: after _MOUNT_MISS_LIMIT consecutive misses the surface is
        EVICTED and recreation is LAZY (this render drops — SP3's destroyed-
        surface contract already swallows late appends). Six HARDCODED
        dead-getter renders → exactly TWO creations (one per cap window),
        _surfaces drains to empty, no crash. The count is deliberately NOT
        derived from _MOUNT_MISS_LIMIT — a falsifier that scales with the
        constant under test can never fail (that variant PASSED under a
        limit=999 mutation; hardcoding kills it).
        Falsifier: remove the cap → 6 creations."""
        handler, created = self._wired(monkeypatch, {})  # getter → always None
        for i in range(6):
            handler.render_sync("Agent", f"msg {i}", "sk-doomed")  # must not raise
        assert len(created) == 2                        # bounded: 1 per cap window
        assert handler._surfaces == {}                  # lazy — nothing lingers


class TestSurfaceAllocationAndCaps:
    """Round 2: FIX 9 (realized surface fills its viewport — the 69px
    sliver), FIX 5 (bulk close drains surfaces), FIX 11 (O(1) box index)."""

    def _wired(self, monkeypatch, boxes):
        created: list = []

        def factory():
            s = TextViewFallback()
            created.append(s)
            return s

        monkeypatch.setattr(crh_module, "create_chat_surface", factory)
        handler = ChatRenderHandler()
        handler.set_chat_container_getter(lambda sk: boxes.get(sk))
        return handler, created

    def test_realized_surface_fills_viewport(self):
        """FIX 9 — REALIZED chain: a mounted surface is ~viewport-height.
        With the old valign=END the box shrink-wrapped to the child minimum
        (69px in a 317px viewport — Debugger's sliver). Needs a display:
        runs under xvfb-run like the rest of the GUI suite."""
        from ui.views.main_content import MainContent

        mc = MainContent()
        win = Gtk.Window()
        win.set_default_size(800, 600)
        win.set_child(mc)
        # Wire the handler FIRST — production order (window._build wires
        # handlers; tabs are created later, at runtime, when agents
        # connect). create_chat_tab consults the wiring to suppress the
        # welcome bubble on surface tabs.
        orig = crh_module.create_chat_surface
        crh_module.create_chat_surface = TextViewFallback
        try:
            handler = ChatRenderHandler()
            mc.set_chat_render_handler(handler)
            win.present()
            mc.create_chat_tab("agent:alloc", "Alloc")
            box = mc._tab_chat_boxes[0]
            # The alignment pin (falsifier: restore END → this fails).
            assert box.get_valign() == Gtk.Align.FILL
            handler.render_sync("Agent", "hello", "agent:alloc")
            surface = handler._surfaces["agent:alloc"]
            ctx = GLib.MainContext.default()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                while ctx.pending():
                    ctx.iteration(False)
                time.sleep(0.005)
            viewport_h = mc._tab_scrolls[0].get_height()
            surface_h = surface.get_height()
            assert viewport_h > 100, f"viewport not realized ({viewport_h}px)"
            assert surface_h >= viewport_h * 0.8, (
                f"surface is a sliver: {surface_h}px in {viewport_h}px viewport")
        finally:
            crh_module.create_chat_surface = orig
            win.destroy()

    def test_close_tabs_drains_surfaces(self, monkeypatch):
        """FIX 5 — bulk close routes through _close_tab per index, so EVERY
        closed tab fires close_session: the handler's surfaces DRAIN
        (round 1 leaked them — close_tabs replicated dict-pops only)."""
        from unittest.mock import MagicMock

        from ui.views.main_content import MainContent

        handler, _created = self._wired(monkeypatch, {})
        mc = MainContent.__new__(MainContent)
        mc._chat_notebook = MagicMock(spec=Gtk.Notebook)
        mc._chat_notebook.get_n_pages.return_value = 0
        mc._tab_sessions = {0: "sk-a", 1: "sk-b", 2: "sk-c"}
        mc._tab_chat_boxes = {0: Gtk.Box(), 1: Gtk.Box(), 2: Gtk.Box()}
        mc._tab_scrolls = {}
        mc._tab_overlays = {}
        mc._bulk_closing = False
        mc._chat_render_handler = handler
        for sk in ("sk-a", "sk-b", "sk-c"):
            handler._surface_for(sk)
        assert len(handler._surfaces) == 3
        mc.close_tabs([0, 1, 2])
        assert handler._surfaces == {}                  # drained

    def test_surface_for_box_index_roundtrip(self, monkeypatch):
        """FIX 11 — the id(box) index is maintained at mount and the lookup
        returns the SAME surface as the authoritative scan (O(1) sanity)."""
        box = Gtk.Box()
        handler, created = self._wired(monkeypatch, {"sk": box})
        handler.render_sync("Agent", "hello", "sk")
        assert id(box) in handler._surfaces_by_parent   # index kept at mount
        assert handler.surface_for_box(box) is created[0]
        assert handler.surface_for_box(Gtk.Box()) is None
