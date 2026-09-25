# tests/test_chat_surface.py — SPEC-06 SP3 battery (xvfb; WebKit 6.0 present
# on this box, TextViewFallback is the environment-independent path).

import pytest

gi = pytest.importorskip("gi")
pytest.importorskip("gi.repository.Gtk")
GLib = pytest.importorskip("gi.repository.GLib")


from ui.views.chat_surface import (
    TextViewFallback,
    _cap_row_html,
    _document,
)

WebKit = None
try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit as _WebKit

    WebKit = _WebKit
except (ValueError, ImportError):  # pragma: no cover
    pass

xvfb_required = pytest.mark.usefixtures()


@pytest.fixture
def surface():
    """TextViewFallback drives the API-contract tests (env-independent)."""
    s = TextViewFallback()
    yield s
    s.destroy()


# ── Tests 1-3: fallback API contract (append / stream / pill) ────────────

class TestFallbackPath:
    def test_append_message_renders_plain_text(self, surface):
        surface.append_message("agent", "<p>hello <strong>world</strong></p>", "Coder")
        text = surface._text()
        assert "hello" in text and "world" in text
        assert "[Coder]" in text
        assert "<p>" not in text  # tags stripped in the raw-text view

    def test_stream_buffers_then_flushes_atomically(self, surface):
        surface.stream_delta("sk", "hel")
        surface.stream_delta("sk", "lo w")
        surface.stream_delta("sk", "orld")
        assert surface._text().strip() == ""  # nothing rendered while streaming
        surface.end_stream("sk", "Coder")
        assert "hello world" in surface._text()

    def test_end_stream_empty_is_noop(self, surface):
        surface.end_stream("never-started")
        assert surface._text().strip() == ""

    def test_pill_cycles_text_and_class(self, surface):
        for state in ("thinking", "tool", "error", "idle"):
            surface.set_activity_pill(state)
            assert surface._pill_label.get_text() == {
                "thinking": "Thinking…",
                "tool": "Tool running…",
                "error": "Error",
                "idle": "Idle",
            }[state]
        # Final state's class is present, the others are not.
        css = surface._pill_label.get_css_classes()
        assert "pill-idle" in css
        assert "pill-thinking" not in css

    def test_destroy_twice_safe(self, surface):
        surface.destroy()
        surface.destroy()  # must not raise


# ── Windowing (spec §6) ──────────────────────────────────────────────────

class TestWindowing:
    def test_window_is_fifo(self):
        """Bounded FIFO eviction, deque-level: maxlen drops the OLDEST rows.
        (The fallback's text view is append-only by design — the deque IS
        the window; this pins the window itself.)"""
        s = TextViewFallback(window_max=3)
        for i in range(7):
            s.append_message("agent", f"<p>m{i}</p>")
        roles = [row["html"] for row in s._rows]
        assert roles == ["<p>m4</p>", "<p>m5</p>", "<p>m6</p>"]
        s.destroy()

    def test_10k_appends_bounded_and_coalesced(self):
        """Spec §6: 10k appends → deque len == 500; coalesced rebuilds
        (WebKit path: one render per idle cycle, not per append)."""
        from ui.views.chat_surface import ChatSurface

        if WebKit is None:
            pytest.skip("WebKit unavailable — coalescing needs the real loader")
        s = ChatSurface()
        loads: list[str] = []
        s._load_html = lambda doc: loads.append(doc)  # type: ignore[method-assign]
        for i in range(10_000):
            s.append_message("agent", f"<p>m{i}</p>")
        assert len(s._rows) == 500
        s._drain_renders()
        assert s._render_pending is False
        assert s._rebuild_count == 1
        assert "m9999" in loads[-1] and "m4000" not in loads[-1]
        s.destroy()


# ── Huge-message cap (spec §7) ───────────────────────────────────────────

class TestHugeMessage:
    def test_truncation_marker(self):
        big = "<p>" + "x" * (512 * 1024 + 1000) + "</p>"
        capped = _cap_row_html(big)
        assert capped.endswith("[truncated]")
        assert len(capped.encode("utf-8")) <= 512 * 1024  # FIX D: exact byte cap

    def test_normal_rows_uncapped(self):
        assert _cap_row_html("<p>pre-fix</p>") == "<p>pre-fix</p>"


# ── WebKit path (skipif absent) ──────────────────────────────────────────

@pytest.mark.skipif(WebKit is None, reason="WebKit introspection unavailable")
class TestWebKitPath:
    def test_append_loads_escaped_sanitized_document(self, monkeypatch):
        from ui.views.chat_surface import ChatSurface

        s = ChatSurface()
        loads: list[str] = []
        monkeypatch.setattr(s, "_load_html", lambda doc: loads.append(doc))
        s.append_message("agent", "<p>hello <b>bold</b></p>", "Coder")
        s._drain_renders()
        assert len(loads) == 1
        doc = loads[0]
        assert "hello" in doc and "bold" in doc and "Coder" in doc
        assert "<style>" in doc and "message-row" in doc
        s.destroy()

    def test_javascript_disabled(self):
        from ui.views.chat_surface import ChatSurface

        s = ChatSurface()
        s._ensure_webview()
        settings = s._webview.get_settings()
        assert settings.get_enable_javascript() is False
        s.destroy()


# ── Document assembly helpers ────────────────────────────────────────────

class TestDocumentAssembly:
    def test_document_contains_rows_and_css(self):
        rows = [{"role": "agent", "html": "<p>x</p>", "agent": "Coder"}]
        doc = _document(rows)
        assert "<style>" in doc and "message-row" in doc and "<p>x</p>" in doc

    def test_document_agent_name_escaped(self):
        rows = [{"role": "agent", "html": "<p>x</p>", "agent": '<b>evil</b>'}]
        doc = _document(rows)
        assert "<b>evil</b>" not in doc
        assert "&lt;b&gt;evil&lt;/b&gt;" in doc


# ── SP2-discovery pin: classes survive the composed render ───────────────

class TestClassSurvival:
    def test_tok_and_lang_classes_survive_composed_render(self):
        """THE SP2 discovery, now green: fenced python keeps lang-/tok-
        classes through sanitize (SP3 class= ruling)."""
        from render.html import render_document

        out = render_document("```python\ndef f(): pass\n```")
        assert 'class="lang-python"' in out
        assert 'class="tok-kw"' in out

    def test_unknown_class_still_stripped(self):
        """Closed allowlist: arbitrary class values die at the sanitizer
        (sanitize-level — raw HTML input, bypassing the markdown escaper)."""
        from render.sanitize import sanitize_html

        out = sanitize_html('<p><code class="evil">x</code></p>')
        assert 'class="evil"' not in out
        assert "<code>x</code>" in out
        out2 = sanitize_html('<span class="tok-kw evil other">x</span>')
        assert out2 == '<span class="tok-kw">x</span>'


# ── SP3 audit fixes ──────────────────────────────────────────────────────

class TestSanitizePanicPin:
    def test_baseexception_derived_yields_empty(self, monkeypatch):
        """FIX 1 (BUG #1): a BaseException-derived failure (pyo3
        PanicException is not Exception-derived) must yield "" — the
        widest-net catch is the fail-closed contract."""
        from render import sanitize as san

        class SyntheticPanic(BaseException):
            pass

        def boom(*a, **k):  # kwarg-proof — nh3.clean passes 6 kwargs
            raise SyntheticPanic("simulated Rust panic")

        monkeypatch.setattr(san.nh3, "clean", boom)
        assert san.sanitize_html("<p>hi</p>") == ""


class TestDestroyCancelsPendingRender:
    """FIX A (SP3 audit r2): the destroy-race defense is FOUR layers, and
    every single-layer revert must be KILLED by an assertion here:

      R1 destroy skips _destroyed=True      → killed by the post-destroy
        state assert (test 1) — and by test 2's contract (append must be
        inert) — R1 lets a post-destroy append re-arm the pipeline.
      R2 destroy skips source_remove        → killed by test 1's
        find_source_by_id assert taken IMMEDIATELY post-destroy (before any
        drain: a fired callback self-removes, so the drain alone is blind
        to R2 — that was the old pin's blind spot).
      R3 destroy skips the pending/dirty    → killed by test 1's state
        resets                               asserts (_render_pending/
                                              _dirty must be False).
      R4 _do_render drops its guard         → killed by test 2: the flag is
                                              set MANUALLY (destroy NOT
                                              called), so _dirty is still
                                              True from the append — the
                                              guard is the only defense.

    The main-loop drain in test 1 additionally proves the real loop delivers
    no rebuild post-destroy (the original probe's resurrection path), and
    test 2 is Debugger's already-dispatched simulation (callback fired
    BEFORE destroy → only the guard remains).
    """

    def _loads_recorder(self, s) -> list:
        loads: list = []
        s._load_html = lambda doc: loads.append(doc)  # type: ignore[method-assign]
        return loads

    def test_destroy_cancels_queued_source_and_resets_state(self):
        """Prong (i): real queued idle + destroy + main-loop drain. The
        source must be GONE immediately post-destroy (R2), transient render
        state must be reset (R3), _destroyed must be set (R1), and the drain
        must deliver nothing (end state)."""
        from ui.views.chat_surface import ChatSurface

        if WebKit is None:
            pytest.skip("WebKit unavailable")
        s = ChatSurface()
        loads = self._loads_recorder(s)
        s.append_message("agent", "<p>before</p>")
        assert s._render_source is not None  # idle callback genuinely queued
        ctx = GLib.MainContext.default()
        sid = s._render_source  # capture BEFORE destroy (destroy nulls the field)
        assert ctx.find_source_by_id(sid) is not None  # queued
        s.destroy()
        # R2: source_remove ran — the source is gone from the loop NOW.
        assert ctx.find_source_by_id(sid) is None
        # R1 + R3: destroy's state contract.
        assert s._destroyed is True
        assert s._render_pending is False
        assert s._dirty is False
        # Full drain: the real loop delivers no rebuild (no resurrection).
        while ctx.pending():
            ctx.iteration(False)
        assert loads == []
        assert s._webview is None
        assert s._rebuild_count == 0

    def test_already_dispatched_render_is_inert(self):
        """Prong (ii): the callback fired BEFORE destroy (destroy found no
        source to cancel). _destroyed is set MANUALLY — destroy() is NOT
        called, so _dirty is still True from the append and _do_render's
        guard is the ONLY remaining defense (kills R4)."""
        from ui.views.chat_surface import ChatSurface

        if WebKit is None:
            pytest.skip("WebKit unavailable")
        s = ChatSurface()
        loads = self._loads_recorder(s)
        s.append_message("agent", "<p>before</p>")
        s._render_source = None  # already fired — destroy's remove is a no-op
        s._destroyed = True  # manual: destroy NOT called, _dirty stays True
        assert s._dirty is True  # precondition: only the guard can stop it
        s._do_render()  # direct dispatch simulation
        assert loads == []
        assert s._webview is None
        assert s._rebuild_count == 0

    def test_append_after_destroy_is_fully_inert(self):
        """R1 companion: post-destroy append must neither row nor re-arm the
        render source (kills an R1 revert that re-enables scheduling)."""
        from ui.views.chat_surface import ChatSurface

        if WebKit is None:
            pytest.skip("WebKit unavailable")
        s = ChatSurface()
        loads = self._loads_recorder(s)
        s.destroy()
        s.append_message("agent", "<p>after</p>")
        assert len(s._rows) == 0
        assert s._render_source is None
        assert loads == []


class TestWebKitlessBox:
    def test_fallback_is_the_surface_when_webkit_none(self, monkeypatch):
        """FIX 3+4 (BUGs #3/#4): WebKit=None → the fallback IS the surface;
        append+drain must not raise and text must land."""
        import ui.views.chat_surface as cs

        monkeypatch.setattr(cs, "WebKit", None)
        s = cs.create_chat_surface()
        s.append_message("agent", "<p>hello</p>", "Coder")
        assert "hello" in s._text()
        s.destroy()


class TestByteBudgetCap:
    def test_multibyte_cap_is_byte_exact(self):
        """FIX 5 (BUG #5): the cap is a BYTE budget — multi-byte rows are
        sliced on bytes and re-decoded, never over budget."""
        row = "<p>" + "\u2603" * 200_000 + "</p>"  # snowman = 3 bytes UTF-8
        capped = _cap_row_html(row)
        assert len(capped.encode("utf-8")) <= 512 * 1024
        assert capped.endswith("[truncated]")


class TestFallbackBufferBound:
    def test_text_buffer_bounded_under_load(self):
        """FIX 7 (BUG #7, P11): the fallback TextBuffer trims from the top —
        bounded memory after heavy appends."""
        s = TextViewFallback(window_max=50)
        for i in range(1000):
            s.append_message("agent", f"<p>line {i}</p>")
        buf = s._view.get_buffer()
        assert buf.get_line_count() <= 55  # window_max + small slack
        assert "line 999" in s._text()  # newest content present
        s.destroy()


class TestImportTimeAlias:
    def test_module_source_aliases_chat_surface_when_webkit_none(self):
        """FIX 4 mechanism pin: the module's import block BINDS the alias —
        on a genuinely WebKit-less box, ChatSurface IS TextViewFallback.
        Source-structure pin (import-once semantics make exec/reload pins
        untestable here; falsifier = remove the alias line)."""
        import ui.views.chat_surface as cs

        with open(cs.__file__, encoding="utf-8") as f:
            src = f.read()
        # The alias lives at the module BOTTOM (after all class defs) — pin
        # the full source: the guarded alias + factory must both exist, and
        # the alias must be the module-level ChatSurface binding for
        # WebKit-less imports (import-once semantics keep this untestable
        # at runtime; falsifier = delete either line).
        # FIX E / rider-a (SP3 audit r2): EXACT-INDENT match — the alias is
        # a top-level statement and must match exactly 4 spaces. A dead-def
        # 8-space binding or a commented-out line both FAIL this pin (no
        # lstrip — stripping is what made the old pin evadable).
        code_lines = [
            ln.split("#", 1)[0].rstrip() for ln in src.splitlines()
        ]
        alias_lines = [
            ln for ln in code_lines
            if ln == "    ChatSurface = TextViewFallback"
        ]
        assert len(alias_lines) == 1, f"alias must appear exactly once uncommented: {alias_lines!r}"
        assert "def create_chat_surface" in src
        # And the factory (runtime path) resolves the fallback for real.
        monkey = __import__("unittest.mock", fromlist=["patch"])
        with monkey.patch(f"{cs.__name__}.WebKit", None):
            s = cs.create_chat_surface()
            assert type(s) is cs.TextViewFallback
            s.destroy()

