# tests/test_chat_surface.py — SPEC-06 SP3 battery (xvfb; WebKit 6.0 present
# on this box, TextViewFallback is the environment-independent path).

import pytest

gi = pytest.importorskip("gi")
pytest.importorskip("gi.repository.Gtk")


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
        assert len(capped) < 512 * 1024 + 100  # marker fits inside the budget

    def test_normal_rows_uncapped(self):
        assert _cap_row_html("<p>hi</p>") == "<p>hi</p>"


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
