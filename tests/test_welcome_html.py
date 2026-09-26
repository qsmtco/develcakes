# tests/test_welcome_html.py — SPEC-06 SP5c-1.
# SUCCESSOR to tests/test_welcome_bubble.py (retired this round): the welcome
# is now the first HTML-native surface content, emitted by the render handler
# through the SAME fail-closed pipeline as agent content (render_document →
# sanitize_html). Locator discipline carried over from the Pango pin: find
# the row by its stable CSS class (welcome-row), never by position or index.
#
# Survey verdict pinned here (brief constraint 1): the welcome is TEXT-ONLY.
# The logo cannot pass the sanitizer (src is http(s)-only; render/html emits
# no <img> at all) and the policy must NOT be weakened for it.
#
# Environment-independent: SpySurface-style rows (no WebKit); the sanitizer
# pins import render/ directly.

import pytest

gi = pytest.importorskip("gi")

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

import ui.handlers.chat_render_handler as crh_module
from ui.handlers.chat_render_handler import (
    _WELCOME_MARKDOWN,
    ChatRenderHandler,
)
from ui.views.chat_surface import TextViewFallback


class SpySurface(TextViewFallback):
    """Records append_message calls — no WebKit required."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.appended: list[dict] = []

    def append_message(self, role, html_fragment, agent_name=None):
        self.appended.append({"role": role, "html": html_fragment, "agent": agent_name})
        super().append_message(role, html_fragment, agent_name=agent_name)


def _handler_with_spy() -> tuple[ChatRenderHandler, SpySurface]:
    h = ChatRenderHandler()
    spy = SpySurface()
    h._surfaces["sk"] = spy
    return h, spy


def _welcome_rows(spy: SpySurface) -> list[dict]:
    """Rows carrying the stable welcome-row class (CSS-class discipline)."""
    return [a for a in spy.appended if 'class="welcome-row"' in a["html"]]


# ── Gate 1: the HTML pin (content present after emission, class-located) ──


class TestWelcomeHtmlPin:
    def test_welcome_emitted_with_class_and_identity(self):
        """THE pin (gate 1; successor to the Pango title pin): the welcome
        lands in the surface's rows, carries the stable welcome-row class,
        and the DevelCakes identity survives composition AND sanitize.
        Falsifier: drop the emission (or the class token) → zero rows /
        no class in the document."""
        h, spy = _handler_with_spy()
        h.render_welcome("sk")
        rows = _welcome_rows(spy)
        assert len(rows) == 1
        assert rows[0]["role"] == "system"
        assert 'class="welcome-row"' in rows[0]["html"]
        assert "DevelCakes" in rows[0]["html"]
        assert "Project Development Environment" in rows[0]["html"]

    def test_welcome_survives_to_document_state(self):
        """Content is part of the surface's document state: the fallback's
        rows (the deque that feeds _document) contain the class too."""
        h, spy = _handler_with_spy()
        h.render_welcome("sk")
        doc_rows = [r["html"] for r in spy._rows]
        assert any('class="welcome-row"' in html for html in doc_rows)

    def test_pipeline_passes_sanitized_content_through(self):
        """The composed welcome markdown goes through render_document, the
        stable class is stamped on the emitted block node, and the emitted
        row is EXACTLY the sanitized form of that stamped fragment (no raw
        pass-through, no policy weakening — the text survives nh3
        untouched). Mirrors the production construction in render_welcome."""
        from render.html import markdown_to_html
        from render.sanitize import sanitize_html

        expected = sanitize_html(
            markdown_to_html(_WELCOME_MARKDOWN).replace(
                "<p>", f'<p class="{crh_module._WELCOME_CLASS}">', 1
            )
        )
        h, spy = _handler_with_spy()
        h.render_welcome("sk")
        assert _welcome_rows(spy)[0]["html"] == expected

    def test_class_lands_on_block_node_no_span_wrapper(self):
        """BUG #4 falsifier (round 3): the class rides the emitted BLOCK
        node and NO wrapper element nests it — the emitter hardcodes <p>
        and has no inline mode, so any wrapper (span/div) is
        blocks-invalid. Falsifier: reintroduce the span wrapper (or stamp
        a non-block node) → this fails on the wrapper/nesting assert."""
        h, spy = _handler_with_spy()
        h.render_welcome("sk")
        html_arg = _welcome_rows(spy)[0]["html"]
        assert html_arg.startswith('<p class="welcome-row">')
        assert "<span" not in html_arg  # no wrapper (content has no links)
        assert html_arg.count("<p") == 1  # single block, class on it

    def test_no_img_emitted_logo_is_text_only(self):
        """Survey verdict pin: NO logo/no <img> — the http(s)-only src
        policy is untouched and the welcome passes through it anyway."""
        h, spy = _handler_with_spy()
        h.render_welcome("sk")
        html_arg = _welcome_rows(spy)[0]["html"]
        assert "<img" not in html_arg.lower()
        assert "data:" not in html_arg.lower()


# ── Gate 2: reopen semantics (once per surface mount) ─────────────────────


class TestWelcomeReopenSemantics:
    def test_second_call_is_suppressed(self):
        """Gate 2: re-emission for the same surface is suppressed — one
        emission per mount."""
        h, spy = _handler_with_spy()
        h.render_welcome("sk")
        h.render_welcome("sk")
        h.render_welcome("sk")
        assert len(_welcome_rows(spy)) == 1

    def test_close_then_fresh_surface_rewelcomes(self):
        """Production close→reopen sequence: the closed key's welcome flag
        dies with its surface; after the reopen signal the fresh surface
        gets a fresh welcome. Falsifier: drop the close_session discard →
        the old flag survives → zero rows on the fresh surface.

        SP5a-audit nit fix (provenance): production drives the PLURAL
        pop_tombstones_for_box (main_content.create_chat_tab); this test
        uses the singular pop_tombstone — a direct unit shim with NO
        production caller — to exercise the same flag-discard contract
        for one key in isolation. The production entry is pinned by
        TestRound3Lifecycle (test_chat_render_handler)."""
        h, _ = _handler_with_spy()
        h.render_welcome("sk")
        h.close_session("sk")
        h.pop_tombstone("sk")  # unit shim — see provenance note above
        spy2 = SpySurface()
        h._surfaces["sk"] = spy2
        h.render_welcome("sk")
        assert len(_welcome_rows(spy2)) == 1

    def test_reopen_tombstone_pop_rewelcomes(self, monkeypatch):
        """create_chat_tab's reopen signal (pop_tombstones_for_box) clears
        the welcome flag in lockstep with the tombstone fan-pop — a
        reopened project key re-welcomes. Falsifier: remove the discard
        from pop_tombstones_for_box → the post-reopen call emits nothing.

        SP5a-audit BUG #1: "agent:x" is ABSENT from _surfaces here, so
        render_welcome lazily created a REAL WebKit ChatSurface whose
        queued GLib idle (load_html) was never drained/destroyed — any
        later context-draining test fired it → real WebKit load_html →
        SIGTRAP (reproduced: welcome_html then chat_surface in one batch).
        Surface creation is routed through the TextViewFallback factory
        (same pattern as TestCreateChatTabEmitsWelcome._real_tab) — no
        WebKit surface exists to leak."""
        created: list = []

        def factory():
            s = TextViewFallback()
            created.append(s)
            return s

        monkeypatch.setattr(crh_module, "create_chat_surface", factory)
        h, _ = _handler_with_spy()
        h._mounted_box_keys["agent:x"] = "project:alpha"
        h.render_welcome("agent:x")
        h._welcome_shown.add("project:alpha")
        h.pop_tombstones_for_box("project:alpha")
        assert "agent:x" not in h._welcome_shown
        assert "project:alpha" not in h._welcome_shown
        # No WebKit surface escaped: the lazily-created surface is a fallback.
        assert created and isinstance(created[0], TextViewFallback)

    def test_closed_tombstoned_key_drops_welcome(self):
        """A tombstoned (closed) key's late welcome is DROPPED — same
        late-render discipline as _append_to_surface."""
        h, spy = _handler_with_spy()
        h._closed_sessions["sk"] = True
        h.render_welcome("sk")
        assert _welcome_rows(spy) == []
        assert "sk" not in h._welcome_shown


# ── Gate 3: sanitizer guard (fail-closed witnessed at THIS site) ──────────


class TestWelcomeSanitizerGuard:
    def test_script_injected_compose_is_never_raw(self, monkeypatch):
        """Even if the composition input were ever poisoned, the emitted
        row cannot carry script — the belt-and-braces re-sanitize strips
        the tag; the payload survives only as ESCAPED TEXT (fail-closed
        neutralization, never execution)."""
        h, spy = _handler_with_spy()
        monkeypatch.setattr(
            crh_module, "_WELCOME_MARKDOWN", "**x** <script>evil()</script>"
        )
        h.render_welcome("sk")
        row = _welcome_rows(spy)[0]
        assert "<script" not in row["html"].lower()
        assert "<script>" not in row["html"].lower()

    def test_fail_closed_empty_pipeline_emits_nothing(self, monkeypatch):
        """Gate 3, fail-closed leg: if the pipeline returns "" (nh3
        internal failure), NO row is emitted and the flag is not consumed
        (the next mount attempt can still welcome)."""
        import render.html as html_mod

        h, spy = _handler_with_spy()
        monkeypatch.setattr(html_mod, "markdown_to_html", lambda _t: "")
        h.render_welcome("sk")
        assert spy.appended == []
        assert "sk" not in h._welcome_shown

    def test_compose_exception_emits_nothing(self, monkeypatch):
        """The fallback shape: a raising pipeline drops the welcome —
        never raw HTML into the surface."""
        import render.html as html_mod

        h, spy = _handler_with_spy()
        monkeypatch.setattr(
            html_mod, "markdown_to_html", lambda _t: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        h.render_welcome("sk")
        assert spy.appended == []

    def test_evicted_surface_drops_welcome_and_keeps_flag(self):
        """FIX 11 parity: a surface evicted JUST NOW (lazy recreation)
        drops the welcome and does NOT consume the flag — the recreated
        surface still gets one."""
        h, _spy = _handler_with_spy()
        h._MOUNT_MISS_LIMIT = 0  # type: ignore[misc] # any miss evicts immediately
        h.set_chat_container_getter(lambda sk: None)
        h.render_welcome("sk")
        assert h._surfaces.get("sk") is None  # evicted
        assert "sk" not in h._welcome_shown  # flag NOT consumed

    def test_eviction_discards_stale_welcome_flag(self, monkeypatch):
        """BUG #3 falsifier: eviction destroys the surface AND DISCARDS its
        welcome flag (the crh._surface_for eviction branch) — the recreated
        surface re-welcomes. Without the discard the stale flag suppresses
        the fresh mount's welcome forever. Discriminates fix-present vs
        fix-reverted (Debugger probe sp5c1-r2-bug3-probe.py): revert the
        discard → the final assert sees 0 rows."""
        created: list = []

        def factory():
            s = TextViewFallback()
            created.append(s)
            return s

        monkeypatch.setattr(crh_module, "create_chat_surface", factory)
        h = ChatRenderHandler()
        h._MOUNT_MISS_LIMIT = 0  # type: ignore[misc] # any miss evicts
        h.set_chat_container_getter(lambda sk: None)  # dead getter → evict
        h._welcome_shown.add("sk")  # stale flag from a prior (dead) mount
        assert h._surface_for("sk") is None  # created → missed → evicted
        assert h._surfaces.get("sk") is None
        assert "sk" not in h._welcome_shown  # THE DISCARD under witness
        # Fresh-mount leg: re-wire a live getter — the recreated surface
        # must re-welcome (stale flag ⇒ 0 rows ⇒ mutant alive).
        box = Gtk.Box()
        h.set_chat_container_getter(lambda sk: box)
        h.render_welcome("sk")
        assert len(_welcome_rows_surface(created[-1])) == 1


# ── Wiring: once-per-mount driven by create_chat_tab (main_content) ──────


class TestCreateChatTabEmitsWelcome:
    def _real_tab(self, monkeypatch):
        """Real MainContent tab creation with a fallback-surface handler
        (needs a display — runs under xvfb-run like the GUI suite)."""
        created: list = []

        def factory():
            s = TextViewFallback()
            created.append(s)
            return s

        monkeypatch.setattr(crh_module, "create_chat_surface", factory)
        from ui.views.main_content import MainContent

        handler = ChatRenderHandler()
        mc = MainContent()
        win = Gtk.Window()
        win.set_child(mc)
        mc.set_chat_render_handler(handler)
        return mc, win, handler, created

    def test_tab_creation_emits_welcome_once(self, monkeypatch):
        """Production path pin: create_chat_tab emits the welcome for the
        new session — and a SECOND tab for the same key (early-return
        path) does NOT re-emit."""
        mc, win, _handler, created = self._real_tab(monkeypatch)
        try:
            mc.create_chat_tab("agent:wc", "Worker")
            assert len(_welcome_rows_surface(created[0])) == 1
            page = mc.create_chat_tab("agent:wc", "Worker")  # exists → early return
            assert page == 0
            assert len(_welcome_rows_surface(created[0])) == 1  # no duplicate
        finally:
            win.destroy()

    def test_legacy_branch_is_gone(self):
        """Constraint 5 pin: build_welcome_bubble has NO production caller
        left in main_content (the handler-less Pango branch is deleted).
        Falsifier: reinstate the branch → this fails."""
        import inspect

        from ui.views import main_content

        src = inspect.getsource(main_content)
        assert "build_welcome_bubble" not in src


def _welcome_rows_surface(surface: TextViewFallback) -> list:
    return [r for r in surface._rows if 'class="welcome-row"' in r["html"]]
