# tests/test_chat_surface.py — SPEC-06 SP3 battery (xvfb; WebKit 6.0 present
# on this box, TextViewFallback is the environment-independent path).

import json

import pytest

gi = pytest.importorskip("gi")
pytest.importorskip("gi.repository.Gtk")
GLib = pytest.importorskip("gi.repository.GLib")


from ui.views.chat_surface import (
    _BOTTOM_SCRIPT,
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


# ── Tests 1-3: fallback API contract (append / stream) ────────────────────

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

    # UI-PILLBAR P2: test_pill_cycles_text_and_class RETIRED with its
    # subject — the per-surface pill (set_activity_pill/_pill_label) was
    # deleted from both surface classes. Disposition: the shared bar pill
    # (ui/views/activity_pill.ActivityPillLabel) owns that logic now; its
    # swap contract is pinned in tests/test_activity_pill_label.py and the
    # retirement contract in test_bar_pill_is_sole_pill (same file).

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

    def test_javascript_disabled(self, monkeypatch):
        """SPEC-06 posture KEPT under the SPEC-19 kill-switch: with live JS
        DISABLED (`DEVELCAKES_LIVE_JS=0`) the webview boots non-scriptable —
        byte-identical to pre-SP2. (SP2 flips the DEFAULT on; the OFF path is
        the degrade mode, pinned here.)"""
        from ui.views.chat_surface import ChatSurface

        s = ChatSurface()
        s._live_js = False  # the kill-switch OFF path
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


# ── SPEC-12 SP1: grouped agent boxes (spec §2a) ──────────────────────────


class TestGroupedAgentBoxes:
    """SPEC-12 §2a: consecutive same-agent rows collapse under ONE
    .agent-box with a single header; empty-agent rows render bare. All
    assertions are count/substring pins on the composed document."""

    def test_document_groups_consecutive_same_agent(self):
        rows = [
            {"role": "agent", "html": "<p>c1</p>", "agent": "Coder"},
            {"role": "agent", "html": "<p>c2</p>", "agent": "Coder"},
            {"role": "agent", "html": "<p>c3</p>", "agent": "Coder"},
            {"role": "agent", "html": "<p>d1</p>", "agent": "Debugger"},
        ]
        doc = _document(rows)
        assert doc.count('class="agent-box') == 2  # Coder run + Debugger run
        assert doc.count('agent-name">Coder<') == 1  # ONE header, not three
        assert doc.count('agent-name">Debugger<') == 1
        for frag in ("<p>c1</p>", "<p>c2</p>", "<p>c3</p>", "<p>d1</p>"):
            assert frag in doc  # all four bodies survive the grouping

    def test_document_repeated_agent_name_single_header(self):
        rows = [
            {"role": "agent", "html": "<p>a</p>", "agent": "Coder"},
            {"role": "agent", "html": "<p>b</p>", "agent": "Coder"},
        ]
        doc = _document(rows)
        assert doc.count('class="agent-box') == 1
        assert doc.count('agent-name">Coder<') == 1

    def test_document_user_rows_group_and_class(self):
        from ui.views.chat_surface import _BASE_CSS

        rows = [
            {"role": "user", "html": "<p>u1</p>", "agent": "You"},
            {"role": "user", "html": "<p>u2</p>", "agent": "You"},
        ]
        doc = _document(rows)
        assert doc.count('class="agent-box') == 1
        assert 'class="agent-box role-user"' in doc
        # SP1-audit BUG#3 correction: the box element ITSELF carries
        # role-user, so the header span is still a DESCENDANT and the OLD
        # `.role-user .agent-name` rule still matches — the box-level rule
        # is REDUNDANT (kept for explicitness/intent). It stays pinned here.
        assert ".agent-box.role-user .agent-name" in _BASE_CSS

    def test_document_no_agent_rows_render_bare(self):
        # Single empty-agent row: bare message-row, no box, no name span.
        doc = _document([{"role": "system", "html": "<p>w</p>", "agent": ""}])
        assert doc.count('class="agent-box') == 0
        assert 'class="message-row role-system"' in doc
        # No name SPAN in the bare branch — assert the ELEMENT, not the bare
        # literal: 'agent-name' also appears in _BASE_CSS selectors inside
        # the <style> block, so a raw-string absence assert can never pass.
        assert '<span class="agent-name">' not in doc
        assert "<p>w</p>" in doc
        # Run boundary: an empty-agent row BREAKS a Coder run — the two
        # Coder rows around it must NOT merge into one box.
        doc2 = _document([
            {"role": "agent", "html": "<p>a</p>", "agent": "Coder"},
            {"role": "system", "html": "<p>s</p>", "agent": ""},
            {"role": "agent", "html": "<p>b</p>", "agent": "Coder"},
        ])
        assert doc2.count('class="agent-box') == 2
        assert doc2.count('agent-name">Coder<') == 2
        assert 'class="message-row role-system"' in doc2

    def test_document_interleaved_agents_two_boxes(self):
        """Name per spec §5 label; behavior per spec §5 body: Coder,
        Debugger, Coder = THREE runs → three boxes, name order pinned."""
        rows = [
            {"role": "agent", "html": "<p>1</p>", "agent": "Coder"},
            {"role": "agent", "html": "<p>2</p>", "agent": "Debugger"},
            {"role": "agent", "html": "<p>3</p>", "agent": "Coder"},
        ]
        doc = _document(rows)
        assert doc.count('class="agent-box') == 3
        assert doc.count('agent-name">Coder<') == 2
        assert doc.count('agent-name">Debugger<') == 1
        c1 = doc.index('agent-name">Coder<')
        d = doc.index('agent-name">Debugger<')
        c2 = doc.index('agent-name">Coder<', d + 1)
        assert c1 < d < c2  # interleaved order preserved

    def test_document_agent_name_escaped_still(self):
        rows = [{"role": "agent", "html": "<p>x</p>", "agent": "<b>evil</b>"}]
        doc = _document(rows)
        assert "&lt;b&gt;evil&lt;/b&gt;" in doc  # header escaped in the box
        assert "<b>evil</b>" not in doc
        assert doc.count('class="agent-box') == 1  # still boxed

    def test_document_you_named_agent_does_not_merge_with_user(self):
        """SP1-audit BUG#1: an agent whose display name is literally "You"
        (agent-builder does not reserve the name) must NOT merge with the
        user's adjacent rows — grouping keys on (agent, role), and the box
        class comes from the row's ROLE, not the name string."""
        rows = [
            {"role": "user", "html": "<p>q</p>", "agent": "You"},
            {"role": "agent", "html": "<p>r</p>", "agent": "You"},
        ]
        doc = _document(rows)
        # Two boxes: the user's run and the agent's run — never one merged box.
        assert doc.count('class="agent-box') == 2
        assert 'class="agent-box role-user"' in doc  # user run stays user
        assert 'class="agent-box role-agent"' in doc  # agent run stays agent
        assert "<p>q</p>" in doc and "<p>r</p>" in doc  # both bodies survive

    def test_document_agent_run_pins_role_agent(self):
        """SP1-audit BUG#2: role-agent was unpinned — a mutant emitting
        role-user for every box passed the whole suite. Pin BOTH directions:
        an agent run is role-agent and nothing else."""
        rows = [{"role": "agent", "html": "<p>c</p>", "agent": "Coder"}]
        doc = _document(rows)
        assert 'class="agent-box role-agent"' in doc
        assert 'class="agent-box role-user"' not in doc

    def test_document_user_run_pins_role_user_only(self):
        """SP1-audit BUG#2 (user side): a user run is role-user; role-agent
        must be ABSENT (kills the always-role-agent / both-classes mutants)."""
        rows = [
            {"role": "user", "html": "<p>u1</p>", "agent": "You"},
            {"role": "user", "html": "<p>u2</p>", "agent": "You"},
        ]
        doc = _document(rows)
        assert 'class="agent-box role-user"' in doc
        assert 'class="agent-box role-agent"' not in doc

    def test_document_missing_role_defaults_to_system_no_crash(self):
        """SP1 re-audit residual (defensive parity): a row lacking "role" (or
        with role=None) must not raise KeyError nor leak a `role-None` class —
        emission normalizes through `or "system"`, matching the run-key. Not
        production-reachable (append_message always stores a normalized role),
        but pins the defensive consistency."""
        doc = _document([
            {"html": "<p>a</p>", "agent": "Coder"},  # role key absent
            {"role": None, "html": "<p>b</p>", "agent": "Coder"},  # role None
        ])
        # Both rows normalize to "system": they share one (agent, role) run.
        assert doc.count('class="agent-box') == 1
        assert 'role-None' not in doc
        assert 'class="message-row role-system"' in doc
        # A bare row (no agent) with no role must also not crash.
        doc2 = _document([{"html": "<p>w</p>", "agent": ""}])
        assert 'class="message-row role-system"' in doc2


# ── SPEC-13 SP2: agent-payload surface defaults + fallback parity ─────────


class TestAgentPayloadCss:
    """SPEC-13 §2d: the surface gains default CSS for the agent-author
    vocabulary so an UNSTYLED payload (a bare <div>/<button>/<img>) does not
    render as black-on-black or inline-collapsed. The three blocks are
    REQUIRED defaults; absence is a real regression (agent cards without
    their own style would be unreadable).

    The append_message flow is exercised end-to-end: a payload row lands in
    the surface deque, the COALESCED render hook (_drain_renders + a
    _load_html monkeypatch, the established pattern in this file) emits the
    document, and the CSS is asserted in the loaded document.
    """

    def test_default_block_element_display_in_base_css(self):
        from ui.views.chat_surface import _BASE_CSS

        assert (
            "div, section, article, header, footer, aside, nav, figure "
            "{ display: block; }"
        ) in _BASE_CSS, "block-display default missing from _BASE_CSS"

    def test_media_constraint_default_in_base_css(self):
        from ui.views.chat_surface import _BASE_CSS

        assert (
            "img, video { max-width: 100%; height: auto; border-radius: 4px; }"
        ) in _BASE_CSS, "media constraint default missing from _BASE_CSS"

    def test_button_default_in_base_css(self):
        from ui.views.chat_surface import _BASE_CSS

        assert "button {" in _BASE_CSS
        assert "background: #2f334d;" in _BASE_CSS
        assert "color: #c0caf5;" in _BASE_CSS
        assert "border: 1px solid #3b4261;" in _BASE_CSS
        assert "border-radius: 6px;" in _BASE_CSS
        assert "padding: 4px 10px;" in _BASE_CSS

    def test_defaults_present_in_loaded_document_after_append(self, monkeypatch):
        """THE flow pin: append_message → _drain_renders → the loaded
        document carries all three default blocks. Kills a mutant that
        moves the CSS out of the <style> it emits (e.g. appends it after
        the closing tag) — the assertions read the ACTUAL loaded doc."""
        from ui.views.chat_surface import ChatSurface

        if WebKit is None:
            pytest.skip("WebKit unavailable — coalesced load path needs real loader")
        s = ChatSurface()
        loads: list[str] = []
        monkeypatch.setattr(s, "_load_html", lambda doc: loads.append(doc))
        s.append_message("agent", '<div class="card">hi</div>', "Coder")
        s._drain_renders()
        assert len(loads) == 1
        doc = loads[0]
        assert "div, section, article, header, footer, aside, nav, figure { display: block; }" in doc
        assert "img, video { max-width: 100%; height: auto; border-radius: 4px; }" in doc
        assert "button {" in doc and "background: #2f334d;" in doc
        s.destroy()

    def test_defaults_survive_fallback_document_source(self, monkeypatch):
        """Environment-independent witness: _document() (what BOTH surface
        classes re-render through) embeds _BASE_CSS, so the defaults reach
        the fallback's document source too — no WebKit needed."""
        from ui.views.chat_surface import ChatSurface

        rows = [{"role": "agent", "html": '<div class="card">hi</div>', "agent": "Coder"}]
        doc = _document(rows)
        assert "div, section, article, header, footer, aside, nav, figure { display: block; }" in doc
        assert "img, video { max-width: 100%; height: auto; border-radius: 4px; }" in doc
        assert "background: #2f334d;" in doc
        _ = ChatSurface  # keep the import honest (surface class owns _BASE_CSS)


class TestTextViewFallbackPayloadTagStrip:
    """SPEC-13 §2c degradation contract: an agent HTML PAYLOAD (sanitized
    rich HTML — <div>/<span style>/<button>) fed to the WebKit-less
    TextViewFallback must degrade to READABLE TEXT: the existing
    `_TAG_STRIP_RE` path strips tags, html.unescape restores entities, and
    no '<' survives into the buffer. No code change is required — this pins
    the contract so a future tag-strip change cannot silently show source.
    """

    def test_sanitized_payload_strips_to_readable_text(self):
        from render.html import render_message
        from ui.views.chat_surface import TextViewFallback

        # A whole-message ```html fence → the real sanitized payload an
        # agent turn produces (exactly what append_message receives).
        payload = render_message(
            '```html\n<div class="card"><b>Status</b>: '
            '<span style="color:red">green</span></div>\n```'
        )
        assert "<div" in payload  # precondition: rich HTML, not markdown

        s = TextViewFallback()
        s.append_message("agent", payload, "Coder")
        text = s._text()
        assert "<" not in text, f"tags leaked into fallback text: {text!r}"
        assert "<div" not in text and "<span" not in text
        assert "Status" in text and "green" in text  # readable content survives
        assert "[Coder]" in text
        s.destroy()

    def test_payload_button_and_img_strip_to_text(self):
        """The agent-author vocabulary (button/img) also degrades to text —
        no tags, no leaked attribute source."""
        from render.sanitize import sanitize_agent_html
        from ui.views.chat_surface import TextViewFallback

        payload = sanitize_agent_html(
            '<div><button>Run</button>'
            '<img src="https://x/y.png" alt="chart"></div>'
        )
        s = TextViewFallback()
        s.append_message("agent", payload, "Coder")
        text = s._text()
        assert "<" not in text
        assert "Run" in text  # button label content survives as text
        s.destroy()


# ── SPEC-14 SP1: system chrome for agent cards (spec §2a / §2d) ──────────


class TestSanitizeColorGate:
    """SPEC-14 §2a.2: the ONLY shapes that ever reach a style attribute are
    exact six-hex-digit colors, lowercased. Everything else → '' (CSS
    default). This is the security boundary, so the matrix is exhaustive."""

    def test_valid_hex_survives_lowercased(self):
        from ui.views.chat_surface import _sanitize_color

        assert _sanitize_color("#16A34A") == "#16a34a"
        assert _sanitize_color("#16a34a") == "#16a34a"

    @pytest.mark.parametrize(
        "bad",
        [
            "javascript:alert(1)",
            "url(x)",
            "#16a34",       # five digits
            "#16a34aa",     # eight digits
            "red",          # named color
            "expression(alert(1))",
            "#16a34g",      # non-hex character
            "16a34a",       # missing '#'
            "#16a34a ",     # trailing space
            "#16a34a\n",    # trailing newline ($-loophole probe)
        ],
    )
    def test_illegitimate_colors_dropped(self, bad):
        from ui.views.chat_surface import _sanitize_color

        assert _sanitize_color(bad) == ""

    def test_none_and_empty_dropped(self):
        from ui.views.chat_surface import _sanitize_color

        assert _sanitize_color(None) == ""
        assert _sanitize_color("") == ""


class TestChromeDom:
    """SPEC-14 §2a.3: every agent row renders the system chrome — avatar +
    name header + card body — with the color applied only when it passed the
    gate."""

    def test_agent_row_renders_chrome(self):
        doc = _document([{"role": "agent", "html": "<p>hi</p>", "agent": "Coder"}])
        assert 'class="agent-chrome"' in doc
        assert 'class="agent-avatar"' in doc
        assert ">C</span>" in doc  # initial = first letter, upper-cased
        assert 'class="agent-name"' in doc
        assert 'class="agent-card"' in doc
        assert "<p>hi</p>" in doc  # payload lives inside the card

    def test_initial_upper_and_escaped(self):
        doc = _document([{"role": "agent", "html": "<p>x</p>", "agent": "coder"}])
        assert ">C</span>" in doc

    def test_chrome_color_applied_when_gate_passed(self):
        rows = [{"role": "agent", "html": "<p>x</p>", "agent": "Coder", "color": "#16a34a"}]
        doc = _document(rows)
        assert 'style="background-color:#16a34a"' in doc  # avatar
        assert 'style="color:#16a34a"' in doc             # name
        assert 'style="border-bottom-color:#16a34a"' in doc  # card frame

    def test_no_color_no_inline_style(self):
        doc = _document([{"role": "agent", "html": "<p>x</p>", "agent": "Coder", "color": ""}])
        # Chrome must EXIST — otherwise the absence asserts below are vacuous
        # (they held on pre-chrome code; RED-first requires the positive pin).
        assert 'class="agent-chrome"' in doc
        assert "background-color:" not in doc
        assert 'class="agent-avatar" style=' not in doc
        assert 'class="agent-name" style=' not in doc
        assert 'class="agent-card" style=' not in doc

    def test_group_color_first_nonempty_wins(self):
        rows = [
            {"role": "agent", "html": "<p>a</p>", "agent": "Coder", "color": ""},
            {"role": "agent", "html": "<p>b</p>", "agent": "Coder", "color": "#16a34a"},
            {"role": "agent", "html": "<p>c</p>", "agent": "Coder", "color": "#000000"},
        ]
        doc = _document(rows)
        assert doc.count('class="agent-box') == 1
        assert "#16a34a" in doc
        assert "#000000" not in doc  # first non-empty color in the run wins


class TestChromeGroupingAndRoles:
    def test_grouping_holds_with_color(self):
        rows = [
            {"role": "agent", "html": "<p>1</p>", "agent": "Coder", "color": "#16a34a"},
            {"role": "agent", "html": "<p>2</p>", "agent": "Coder", "color": "#16a34a"},
            {"role": "agent", "html": "<p>3</p>", "agent": "Debugger", "color": "#7aa2f7"},
        ]
        doc = _document(rows)
        assert doc.count('class="agent-box') == 2
        assert doc.count('class="agent-chrome"') == 2

    def test_user_row_green_via_css_not_inline(self):
        from ui.views.chat_surface import _BASE_CSS

        rows = [{"role": "user", "html": "<p>q</p>", "agent": "You", "color": "#16a34a"}]
        doc = _document(rows)
        assert 'class="agent-box role-user"' in doc
        assert 'class="agent-chrome"' in doc
        assert "#16a34a" not in doc  # row color never reaches a user DOM
        assert "background-color:" not in doc
        assert ".agent-box.role-user .agent-name { color: #9ece6a; }" in _BASE_CSS

    def test_no_agent_rows_stay_bare(self):
        doc = _document([{"role": "system", "html": "<p>w</p>", "agent": ""}])
        assert 'class="agent-box' not in doc
        assert 'class="agent-chrome"' not in doc
        assert 'class="message-row role-system"' in doc
        # Positive anti-vacuous pin: a chrome-bearing row in the SAME doc
        # proves the chrome exists — so the two absence asserts above redden
        # at RED-time if chrome is not implemented (not merely "bare path
        # untouched"). Guards the "bare rows stay bare" contract from a
        # mutant that chrome-wraps EVERY row.
        doc2 = _document([
            {"role": "system", "html": "<p>w</p>", "agent": ""},
            {"role": "agent", "html": "<p>a</p>", "agent": "Coder"},
        ])
        assert 'class="agent-chrome"' in doc2
        assert 'class="message-row role-system"><div class="msg-body"><p>w</p></div></div>' in doc2

    def test_markdown_payload_inside_chrome_unchanged(self):
        rows = [{"role": "agent", "html": "<h2>H</h2><ul><li>a</li></ul>", "agent": "Coder"}]
        doc = _document(rows)
        assert "<h2>H</h2><ul><li>a</li></ul>" in doc  # payload byte-identical
        assert 'class="agent-card"' in doc

    def test_chrome_css_defaults_present(self):
        from ui.views.chat_surface import _BASE_CSS

        assert ".agent-chrome {" in _BASE_CSS
        assert ".agent-avatar {" in _BASE_CSS
        assert ".agent-card {" in _BASE_CSS
        # SP1-audit suggestion applied: full-bleed payload backgrounds must
        # clip at the card's rounded corners (cosmetic leak, auditor-fixed).
        assert "overflow: hidden" in _BASE_CSS
        assert ".agent-box.role-user .agent-avatar { background: #9ece6a; }" in _BASE_CSS
        assert ".agent-box { border-left: none; margin-bottom: 14px; }" in _BASE_CSS
        # The SPEC-12 border-left rule is gone (replaced by the card frame).
        assert "border-left: 2px solid #3b4261" not in _BASE_CSS


class TestColorFrozenAtAppend:
    def test_surface_freezes_gated_color_on_row(self):
        from ui.views.chat_surface import ChatSurface

        if WebKit is None:
            pytest.skip("WebKit unavailable")
        s = ChatSurface()
        s.append_message("agent", "<p>x</p>", "Coder", agent_color="#16A34A")
        assert s._rows[-1]["color"] == "#16a34a"  # gated + lowercased at append
        s.append_message("agent", "<p>y</p>", "Coder", agent_color="red")
        assert s._rows[-1]["color"] == ""
        s.destroy()

    def test_existing_callers_unaffected_by_new_kwarg(self):
        from ui.views.chat_surface import ChatSurface

        if WebKit is None:
            pytest.skip("WebKit unavailable")
        s = ChatSurface()
        s.append_message("agent", "<p>x</p>", "Coder")  # no agent_color
        assert s._rows[-1]["color"] == ""
        s.destroy()


class TestFallbackAutoscroll:
    """MICRO-SMART-SCROLL BUG#2 fix: TextViewFallback (the no-WebKit path) must
    keep the SAME social-feed scroll contract as ChatSurface — follow new rows
    only when at the bottom, preserve the reading position otherwise. The
    18-site sweep removed the handler-side force-scroll that used to cover
    this path; the surface owns its scroll now.

    The fallback appends in place (no reload) so the follow is idle-deferred
    (GTK updates `upper` a frame later) — tests drain idles with _pump_frame.
    """

    @staticmethod
    def _pump_frames(n: int = 6) -> None:
        ctx = GLib.MainContext.default()
        for _ in range(n):
            guard = 0
            while ctx.pending() and guard < 50:
                ctx.iteration(False)
                guard += 1

    def _fallback(self, monkeypatch):
        """Real TextView surface with real (detached-widget) vadjustment.
        NOTE: page_size stays 0 in a detached widget, so 'bottom' is
        value == upper; we assert against the live upper, not a fabricated
        page_size (the real GTK adjustment rejects made-up heights)."""
        s = TextViewFallback()
        vadj = s.get_vadjustment()
        assert vadj is not None
        # Seed enough content that the adjustment carries a real height.
        for i in range(40):
            s.append_message("agent", f"<p>seed {i} {'x' * 200}</p>", "Coder")
        self._pump_frames()
        return s, vadj

    def test_fallback_autoscrolls_at_bottom(self, monkeypatch):
        """At bottom → append follows the new row (value tracks the new
        bottom after the deferred follow runs)."""
        s, vadj = self._fallback(monkeypatch)
        # Reader is at the bottom.
        vadj.set_value(vadj.get_upper())
        self._pump_frames()
        assert s._was_at_bottom is True

        s.append_message("agent", "<p>NEW ROW " + "y" * 200 + "</p>", "Coder")
        self._pump_frames()
        assert vadj.get_value() == vadj.get_upper(), (
            "fallback must follow to the bottom when the reader was at bottom"
        )
        s.destroy()

    def test_fallback_preserves_reading_position(self, monkeypatch):
        """Scrolled up (not at bottom) → append preserves the reading
        position: the value is unchanged by the append."""
        s, vadj = self._fallback(monkeypatch)
        vadj.set_value(50.0)  # reading, far from bottom
        self._pump_frames()
        assert s._was_at_bottom is False

        s.append_message("agent", "<p>ANOTHER ROW " + "z" * 200 + "</p>", "Coder")
        self._pump_frames()
        assert vadj.get_value() == 50.0, (
            "fallback must preserve the reading position when scrolled up"
        )
        s.destroy()

    def test_fallback_at_bottom_user_grab_mid_gap_does_not_yank(self, monkeypatch):
        """BUG#7: the deferred follow races a user grab. The reader was at the
        bottom → append arms a deferred follow; the user scrolls UP during the
        idle gap (set_value fires value-changed, re-arming _was_at_bottom=
        False). The queued follow must NOT run unconditionally and yank them
        back down — it must RE-READ the tracker right before driving the
        vadjustment and abort when the at-bottom intent is stale."""
        s, vadj = self._fallback(monkeypatch)
        vadj.set_value(vadj.get_upper())  # reader at the bottom
        self._pump_frames()
        assert s._was_at_bottom is True

        s.append_message("agent", "<p>NEW ROW " + "y" * 200 + "</p>", "Coder")
        assert s._follow_source is not None  # follow queued, idle not yet run
        # USER GRAB mid-gap: scroll up BEFORE the idle follow fires.
        vadj.set_value(50.0)
        assert s._was_at_bottom is False  # value-changed re-armed the tracker
        self._pump_frames()  # the queued follow now runs

        assert vadj.get_value() == 50.0, (
            "deferred follow yanked the reader down despite a mid-gap grab"
        )
        s.destroy()

    def test_fallback_destroy_cancels_pending_follow(self, monkeypatch):
        """destroy() cancels a queued follow and disconnects the tracker — a
        late height change must not scroll a dead surface."""
        s, vadj = self._fallback(monkeypatch)
        vadj.set_value(vadj.get_upper())  # at bottom
        self._pump_frames()
        s.append_message("agent", "<p>x</p>", "Coder")
        assert s._follow_source is not None  # a follow is queued
        hid = s._bottom_handler_id
        s.destroy()
        assert s._follow_source is None
        assert not vadj.handler_is_connected(hid)

    def test_fallback_scroll_to_latest_drives_to_bottom(self, monkeypatch):
        """SPEC-17 SP1.3: the fallback's scroll_to_latest keeps today's
        behavior — arm the follow intent and drive the adjustment to the
        bottom, with NO document script (the fallback has no webview)."""
        s, vadj = self._fallback(monkeypatch)
        vadj.set_value(50.0)  # reading up top
        self._pump_frames()
        assert s._was_at_bottom is False
        assert not hasattr(s, "_document_eval")  # no script path at all
        s.scroll_to_latest()
        assert s._was_at_bottom is True  # follow intent armed
        self._pump_frames()              # deferred follow runs
        assert vadj.get_value() == vadj.get_upper()
        s.destroy()


class TestFallbackChromeParity:
    """SPEC-14 §2a.5: TextViewFallback accepts agent_color for signature
    parity; the value is stored on the row but INERT in plain-text mode."""

    def test_fallback_prefix_unchanged_and_color_inert(self):
        s = TextViewFallback()
        s.append_message("agent", "<p>hi</p>", "Coder", agent_color="#16a34a")
        text = s._text()
        assert "[Coder] hi" in text   # [name] prefix unchanged
        assert "#16a34a" not in text  # color has no text medium
        assert s._rows[-1]["color"] == "#16a34a"  # stored for symmetry
        s.destroy()

    def test_fallback_stores_sanitized_color(self):
        s = TextViewFallback()
        s.append_message("agent", "<p>hi</p>", "Coder", agent_color="javascript:x")
        assert s._rows[-1]["color"] == ""
        s.destroy()


# ── MICRO smart-scroll: at-bottom follow / reading-position preserve ─────


def _surface_supports_smart_scroll() -> bool:
    """Runtime gate: on a WebKit-less box the import-time alias binds
    ChatSurface = TextViewFallback (chat_surface.py module bottom), which has
    no _load_html / smart-scroll. Gate on the CLASS CAPABILITY, not an env-var
    name (survives future env renames)."""
    from ui.views.chat_surface import ChatSurface

    return hasattr(ChatSurface, "_load_html")


@pytest.mark.skipif(WebKit is None, reason="WebKit introspection unavailable")
class TestGtkRestoreMachinery:
    """MICRO smart-scroll: the legacy GTK-adjustment capture/restore.

    SPEC-17 SP1 retained this machinery (it is NOT the fix; the DOCUMENT
    script is — see the TestSmartScroll below). Its contracts (cross-frame
    settle, collapse guard, stable-frame count) are NOT invalidated by
    SPEC-17, so these tests stay verbatim and keep pinning the retained
    GTK path. They no longer constitute the "smart scroll" proof: a green
    run here says nothing about the document position (the original bug).

    The WebKit full-document reload collapses content height mid-load → the
    vadjustment clamps → the view snaps to top on EVERY append. This
    machinery tracks at-bottom on the surface's OWN vadjustment and restores
    after the new height lands.

    Driving the real WebKit load is non-deterministic; per the established
    pattern (TestAgentPayloadCss.test_defaults_present_in_loaded_document_
    after_append) `_load_html` is monkeypatched. The test then simulates the
    async height landing by `set_upper(...)` — which fires the `changed`
    signal exactly as a real content-height change does (probe-verified).

    Test fidelity (load-bearing): these tests drive a REAL Gtk.Adjustment
    from the surface ScrolledWindow — real signal semantics (set_upper fires
    `changed` and NOT `value-changed`; set_value clamps to [lower, max]).
    Do not replace with a fake.

    Consumption is idle-deferred (BUG#1): a height change schedules a settle
    check; `_pump()` drains GLib idles so the deferred restore runs.
    """

    def _surface(self, monkeypatch, page_size=100.0):
        if not _surface_supports_smart_scroll():
            pytest.skip("ChatSurface is the TextViewFallback alias (no smart-scroll)")
        from ui.views.chat_surface import ChatSurface

        s = ChatSurface()
        s._live_js = False  # SPEC-17/legacy GTK machinery = the NON-live path
        vadj = s.get_vadjustment()
        assert vadj is not None  # ScrolledWindow always has an adjustment
        vadj.set_page_size(page_size)
        loads: list[str] = []

        def _fake_load(doc):
            loads.append(doc)
            # BUG#1 (audit fix): the real load is async — `_issue_load` arms
            # `_load_in_flight` and it clears on FINISHED/load-failed. These
            # GTK-machinery tests bypass real WebKit (they monkeypatch
            # `_load_html` and never drive the document signals), so model the
            # load as SYNCHRONOUSLY COMPLETE here; otherwise the gate would
            # never release and later renders would defer. (The document-path
            # signal lifecycle is covered by TestSmartScroll.)
            s._load_in_flight = False
            # Reproduce the REAL failure mode: a full-document load collapses
            # WebKit content height mid-load → upper drops and the view clamps
            # to the top. Without this, the monkeypatch would not exercise the
            # bug the fix targets (steelFramed: reproduce the failure).
            vadj.set_upper(0.0)
            vadj.set_value(0.0)

        monkeypatch.setattr(s, "_load_html", _fake_load)
        s._test_loads = loads  # test-visible record of loaded docs
        return s, vadj

    @staticmethod
    def _pump():
        """Drain pending GLib idle callbacks (the deferred settle restore)."""
        ctx = GLib.MainContext.default()
        guard = 0
        while ctx.pending() and guard < 200:
            ctx.iteration(False)
            guard += 1

    @staticmethod
    def _frame():
        """Run EXACTLY ONE idle frame (for cross-frame settle tests)."""
        ctx = GLib.MainContext.default()
        if ctx.pending():
            ctx.iteration(False)

    def test_cross_frame_intermediate_does_not_consume(self, monkeypatch):
        """Finding 1 (priority fix): real WebKit layout lands ACROSS frames —
        an intermediate scrollable height can be stable for one frame. With
        N=1 the capture consumes at the intermediate (was_at_bottom=False →
        set_value(min(300, upper-page=50)) ≈ top); the final height then has
        nothing to restore. N=2 consecutive stable frames fixes it.
        """
        s, vadj = self._surface(monkeypatch)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        vadj.set_upper(1000.0)
        self._pump()
        vadj.set_value(300.0)  # reading → capture intent = 300

        s.append_message("agent", "<p>m2</p>", "Coder")
        s._drain_renders()  # capture = (False, 300); _fake_load collapses to 0
        vadj.set_upper(150.0)   # intermediate: scrollable (max=50), NOT final
        self._frame()           # ONE idle frame — must NOT consume here
        vadj.set_upper(1200.0)  # FINAL height lands in a later frame
        self._pump()
        assert vadj.get_value() == 300.0, (
            "capture consumed at the intermediate frame (cross-frame bug)"
        )
        s.destroy()

    def test_two_consecutive_stable_frames_consumes(self, monkeypatch):
        """Finding 1 (N=2) pinned BOTH ways: after ONE stable frame the capture
        is NOT yet consumed; after the SECOND it is. (N=1 fails the interim
        assert; N>2 fails the post-second assert.)"""
        s, vadj = self._surface(monkeypatch)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()  # capture = (True, 0)
        vadj.set_upper(1000.0)
        self._frame()  # stable frame #1 — must NOT consume yet
        assert vadj.get_value() == 0.0, "consumed after only one stable frame"
        self._frame()  # stable frame #2 — now consume
        assert vadj.get_value() == 900.0, "did not consume after two stable frames"
        s.destroy()

    def test_collapse_does_not_rearm_at_bottom(self, monkeypatch):
        """Finding 2 (priority fix): the load_html collapse clamps value→0 and
        fires value-changed. Without a guard the tracker derives
        was_at_bottom=True even when the user was reading up top — poisoning
        the next render. The guard: upper <= page_size is a layout artifact,
        not user intent."""
        s, vadj = self._surface(monkeypatch)
        vadj.set_upper(1000.0)
        vadj.set_value(300.0)  # reading: (1000-100-300)=600 > 80 → False
        assert s._was_at_bottom is False
        # Collapse: upper shrinks below page_size; value clamps to 0.
        vadj.set_upper(0.0)
        vadj.set_value(0.0)  # value-changed fires with upper=0 (not scrollable)
        assert s._was_at_bottom is False, (
            "collapse re-armed was_at_bottom — tracker poisoned by layout"
        )
        s.destroy()

    def test_first_render_lands_at_bottom(self, monkeypatch):
        """Edge 4: a fresh surface with no prior content lands at bottom once
        the first document's height arrives."""
        s, vadj = self._surface(monkeypatch)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        assert s._test_loads  # the render happened
        vadj.set_upper(500.0)  # content height lands
        self._pump()  # deferred settle restore runs
        assert vadj.get_value() == 400.0  # upper - page_size = bottom
        s.destroy()

    def test_at_bottom_follows_new_message(self, monkeypatch):
        """At bottom + append → the view follows the new message."""
        s, vadj = self._surface(monkeypatch)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        vadj.set_upper(1000.0)
        self._pump()
        assert vadj.get_value() == 900.0  # settled at bottom

        s.append_message("agent", "<p>m2</p>", "Coder")
        s._drain_renders()
        vadj.set_upper(1100.0)  # new, taller content
        self._pump()
        assert vadj.get_value() == 1000.0  # followed to the new bottom
        s.destroy()

    def test_reading_position_preserved(self, monkeypatch):
        """Scrolled-up + append → the reading position is preserved."""
        s, vadj = self._surface(monkeypatch)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        vadj.set_upper(1000.0)
        self._pump()

        vadj.set_value(300.0)  # user scrolls UP (away from bottom) → re-arms
        s.append_message("agent", "<p>m2</p>", "Coder")
        s._drain_renders()
        vadj.set_upper(1200.0)  # taller content lands
        self._pump()
        assert vadj.get_value() == 300.0  # position preserved, not snapped
        s.destroy()

    def test_intermediate_height_does_not_consume_capture(self, monkeypatch):
        """BUG#1 (fix round): real WebKit fires MULTIPLE `changed` events as
        layout settles — the FIRST scrollable height is NOT final. A premature
        consume would restore to the wrong offset (audit Probe D: captured
        300 → got 100). Model the burst: set_upper(0) → 200 → 1200 with NO
        idle between the height events (the main loop is not idle mid-burst),
        then one drain; the capture must survive to the FINAL 1200.
        """
        s, vadj = self._surface(monkeypatch)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        vadj.set_upper(1000.0)
        self._pump()
        vadj.set_value(300.0)  # reading → capture intent = 300

        s.append_message("agent", "<p>m2</p>", "Coder")
        s._drain_renders()  # capture = (False, 300); _fake_load collapses to 0
        # The burst: intermediate scrollable height, then the real one — all
        # before the loop idles (no _pump between them).
        vadj.set_upper(200.0)   # intermediate: scrollable, but NOT final
        vadj.set_upper(1200.0)  # FINAL height lands in the same burst
        self._pump()            # now the settle check runs
        assert vadj.get_value() == 300.0  # captured value, not the interim 100
        s.destroy()

    def test_resize_does_not_scroll(self, monkeypatch):
        """Edge 2: a pane resize (page_size change) with no render pending
        must NOT move the view — the restore is gated on a pending capture.
        Positive witness: the SAME value DOES move on a render+height."""
        s, vadj = self._surface(monkeypatch)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        vadj.set_upper(1000.0)
        self._pump()
        vadj.set_value(300.0)  # reading
        # Positive witness: a real render + height lands restores the view.
        s.append_message("agent", "<p>m2</p>", "Coder")
        s._drain_renders()
        vadj.set_upper(1200.0)
        self._pump()
        assert vadj.get_value() == 300.0  # restore moved it back from the
        # collapse-to-0 that _fake_load induced — proving the mechanism runs.
        vadj.set_page_size(200.0)  # RESIZE — `changed` fires, but no capture
        self._pump()  # a resize with no capture must not move the view
        assert vadj.get_value() == 300.0  # no scroll from resize alone
        s.destroy()

    def test_rapid_coalesced_renders_last_capture_wins(self, monkeypatch):
        """Edge 1: appends coalesce into ONE render; the capture at drain
        time reflects the LATEST scroll state (last capture wins)."""
        s, vadj = self._surface(monkeypatch)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        vadj.set_upper(1000.0)
        self._pump()

        # Two appends before the coalesced render drains.
        s.append_message("agent", "<p>m2</p>", "Coder")
        s.append_message("agent", "<p>m3</p>", "Coder")
        vadj.set_value(200.0)  # user is reading when the render drains
        s._drain_renders()
        vadj.set_upper(1400.0)
        self._pump()
        assert vadj.get_value() == 200.0  # preserve (latest capture), no snap
        s.destroy()

    def test_deque_shrink_clamps_gracefully(self, monkeypatch):
        """Edge 3: content SHRINKS (rows dropped from the windowed deque)
        while reading — position clamps gracefully, no crash, no snap."""
        s, vadj = self._surface(monkeypatch)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        vadj.set_upper(2000.0)
        self._pump()
        vadj.set_value(1500.0)  # reading deep in old content

        s.append_message("agent", "<p>m2</p>", "Coder")
        s._drain_renders()
        vadj.set_upper(1800.0)  # content shrank; max = 1700
        self._pump()
        assert vadj.get_value() == 1500.0  # preserved within the new range

        # Extreme shrink: the captured value now exceeds the new max.
        vadj.set_value(1700.0)
        s.append_message("agent", "<p>m3</p>", "Coder")
        s._drain_renders()
        vadj.set_upper(600.0)  # max = 500 — captured 1700 is out of range
        self._pump()
        assert vadj.get_value() == 500.0  # clamped to the new bottom, no crash
        s.destroy()

    def test_destroy_mid_load_no_restore(self, monkeypatch):
        """Edge 7: destroy() during an in-flight load disconnects — a later
        height change must not fire a restore on the dead surface.
        Positive witness: BEFORE destroy the same height change DOES restore
        (proving the handler is the thing destroy disconnects)."""
        s, vadj = self._surface(monkeypatch)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        vadj.set_upper(1000.0)
        self._pump()
        vadj.set_value(400.0)  # reading
        # Witness: a live surface restores on the height landing.
        s.append_message("agent", "<p>m2</p>", "Coder")
        s._drain_renders()
        vadj.set_upper(1200.0)
        self._pump()
        assert vadj.get_value() == 400.0  # live → restore ran

        s.append_message("agent", "<p>m3</p>", "Coder")
        s._drain_renders()
        # Capture the handler ids so the DISCONNECT itself is pinned: the
        # _destroyed guards are defense-in-depth and would mask a missing
        # disconnect in a pure behavior assert (audit ISSUE#10).
        hid_bottom = s._bottom_handler_id
        hid_restore = s._restore_handler_id
        s.destroy()  # before m3's height lands
        assert not vadj.handler_is_connected(hid_bottom)
        assert not vadj.handler_is_connected(hid_restore)
        vadj.set_upper(1400.0)  # must not restore on the dead surface
        self._pump()
        # A capture (400) WAS pending at destroy; if the handlers were not
        # disconnected, the settle restore would drive the value to 400.
        assert vadj.get_value() == 0.0  # _fake_load's collapse value remains

    def test_user_grab_mid_load_restore_applies(self, monkeypatch):
        """Edge 6: the user grabs the scrollbar between capture and restore —
        the restore still applies the pre-load intent; the NEXT scroll
        re-arms tracking normally."""
        s, vadj = self._surface(monkeypatch)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        vadj.set_upper(1000.0)
        self._pump()
        vadj.set_value(500.0)  # reading → capture intent = 500

        s.append_message("agent", "<p>m2</p>", "Coder")
        s._drain_renders()
        vadj.set_value(550.0)  # user grabs again mid-load (before height lands)
        vadj.set_upper(1200.0)  # height lands → restore the pre-load intent
        self._pump()
        assert vadj.get_value() == 500.0
        s.destroy()

    def test_at_bottom_user_grab_during_settle_does_not_yank(self, monkeypatch):
        """BUG#7 (ChatSurface mirror): the capture said at-bottom, but the user
        grabs the scrollbar during the CROSS-FRAME settle window (before the
        restore consumes). The at-bottom restore must RE-READ the live tracker
        right before set_value and abort when a user-grab moved it off-bottom —
        the queued restore must not yank the reader back down. (The reading-
        position preserve path is unaffected: it already respects intent.)"""
        s, vadj = self._surface(monkeypatch)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        vadj.set_upper(1000.0)
        self._pump()
        assert vadj.get_value() == 900.0  # settled at bottom, tracker True

        s.append_message("agent", "<p>m2</p>", "Coder")
        s._drain_renders()  # capture = (True, 900); _fake_load collapses to 0
        vadj.set_upper(1200.0)  # height lands → schedules the settle (N=2)
        vadj.set_value(200.0)   # USER GRAB mid-settle → re-arms _was_at_bottom
        assert s._was_at_bottom is False
        self._pump()  # the settle now consumes the capture

        assert vadj.get_value() == 200.0, (
            "at-bottom restore yanked the reader down despite a mid-settle grab"
        )
        s.destroy()

    def test_do_render_captures_before_load(self, monkeypatch):
        """Structural pin: _do_render installs a pending restore BEFORE
        calling _load_html (the capture must exist when the height lands)."""
        s, _vadj = self._surface(monkeypatch)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        # A capture is pending until a `changed` consumes it.
        assert s._pending_restore is not None
        assert len(s._pending_restore) == 2  # (was_at_bottom, captured_value)
        s.destroy()


# ── SPEC-17 SP1: document scroll via evaluate_javascript ─────────────────


@pytest.mark.skipif(WebKit is None, reason="WebKit introspection unavailable")
class TestSmartScroll:
    """SPEC-17 SP1: scroll the DOCUMENT, not the outer adjustment.

    The old tests above (TestGtkRestoreMachinery) drove the ScrolledWindow's
    vadjustment — the WRONG object (WebKit does not implement Gtk.Scrollable;
    the page scrolls inside the web view). This class retargets the proof to
    the DOCUMENT script: after each full-document reload, the FINISHED handler
    issues an `evaluate_javascript` script that sets `el.scrollTop`.

    Harness: monkeypatch the ONE WebKit C-API seam (`_document_eval`) — the
    test records every issued script and feeds back the pre-load READ payload
    (the JSON string a real document would return). The real load is bypassed
    via a `_load_html` monkeypatch (established file pattern); FINISHED is
    dispatched by calling `_on_load_changed` directly (probe-verified to
    match the signal's own dispatch). Assertions watch the SCRIPT STRING —
    the spec is explicit that `vadj.get_value()` is NOT the proof.
    """

    def _surface(self, monkeypatch, *, fresh=False, read='{"y": 0, "atBottom": true}'):
        """Build a surface with the eval seam recorded.

        fresh=True leaves `_webview` None (the no-document path); otherwise a
        sentinel stands in for the web view so `_do_render` takes the READ
        path (the seam is monkeypatched, so no real WebKit call happens).
        """
        if not _surface_supports_smart_scroll():
            pytest.skip("ChatSurface is the TextViewFallback alias (no document scroll)")
        from ui.views.chat_surface import ChatSurface

        s = ChatSurface()
        s._live_js = False  # SPEC-17 model: the NON-live document path (SP2 default-ON)
        reads: list[str] = []
        applies: list[str] = []
        loads: list[str] = []
        read_box = {"text": read}

        def _fake_eval(script, callback):
            if "JSON.stringify" in script:  # the SP1.1 read script
                reads.append(script)
                callback(read_box["text"])
            else:  # an SP1.2 apply script
                applies.append(script)
                callback(None)

        monkeypatch.setattr(s, "_document_eval", _fake_eval)
        monkeypatch.setattr(s, "_load_html", lambda doc: loads.append(doc))
        if not fresh:
            s._webview = object()  # sentinel: a document is "loaded"
        s._test_reads = reads
        s._test_applies = applies
        s._test_loads = loads
        s._test_read = read_box
        return s

    def _finish(self, s):
        """Dispatch FINISHED the way WebKit does (probe-verified)."""
        s._on_load_changed(s._webview, WebKit.LoadEvent.FINISHED)

    def test_at_bottom_append_issues_bottom_script(self, monkeypatch):
        """At bottom + append → FINISHED issues the scrollHeight script.
        Fails if that script is NOT issued (the spec's required assertion)."""
        s = self._surface(monkeypatch)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        self._finish(s)  # seed the loaded document
        s._test_applies.clear()
        # Reader at bottom (y near max).
        s._test_read["text"] = '{"y": 900, "atBottom": true}'
        s.append_message("agent", "<p>m2</p>", "Coder")
        s._drain_renders()  # READ runs → load
        self._finish(s)         # FINISHED → bottom script
        assert s._test_applies == [_BOTTOM_SCRIPT], (
            "FINISHED did not issue the bottom (scrollHeight) script"
        )
        assert "el.scrollTop = el.scrollHeight" in s._test_applies[-1]
        s.destroy()

    def test_reading_preserves_captured_y(self, monkeypatch):
        """Reading (atBottom false, y captured) → FINISHED sets scrollTop to
        that y, NOT to scrollHeight."""
        s = self._surface(monkeypatch)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        self._finish(s)
        s._test_applies.clear()
        s._test_read["text"] = '{"y": 300, "atBottom": false}'
        s.append_message("agent", "<p>m2</p>", "Coder")
        s._drain_renders()
        self._finish(s)
        assert len(s._test_applies) == 1
        script = s._test_applies[-1]
        assert "el.scrollTop = 300.0" in script  # Python-formatted NUMBER
        assert "scrollHeight" not in script       # NOT the bottom script
        assert script != _BOTTOM_SCRIPT
        s.destroy()

    def test_load_reset_to_top_does_not_cancel_follow(self, monkeypatch):
        """The load forces the document to the top (y=0). FINISHED must use
        the PRE-load intent — it must NOT re-read the (now top) position and
        conclude the reader left the bottom. No extra read at FINISHED."""
        s = self._surface(monkeypatch)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        self._finish(s)
        s._test_applies.clear()
        s._test_read["text"] = '{"y": 900, "atBottom": true}'
        s.append_message("agent", "<p>m2</p>", "Coder")
        s._drain_renders()
        reads_before = len(s._test_reads)
        # The load has reset the document to y=0 by now — if FINISHED re-read,
        # it would see atBottom=false and issue a scrollTop=0 script instead.
        self._finish(s)
        assert len(s._test_reads) == reads_before, (
            "FINISHED re-read the document position (the bug)"
        )
        assert s._test_applies == [_BOTTOM_SCRIPT]
        s.destroy()

    def test_first_message_read_fails_lands_at_bottom(self, monkeypatch):
        """First message on a fresh surface (no web view → no read) → intent
        is at-bottom and FINISHED scrolls to scrollHeight."""
        s = self._surface(monkeypatch, fresh=True)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        assert s._pending_scroll == (True, 0.0)  # armed before the load
        assert s._test_reads == []               # no document to read
        self._finish(s)
        assert s._test_applies == [_BOTTOM_SCRIPT]
        s.destroy()

    def test_malformed_read_payload_falls_back_to_at_bottom(self, monkeypatch):
        """A malformed/timed-out read → (True, 0.0) → FINISHED scrolls to the
        bottom (fail-safe, never strands the reader at the top)."""
        s = self._surface(monkeypatch, read="not-json{")
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        assert s._pending_scroll == (True, 0.0)  # parse failure → fail-safe
        self._finish(s)
        assert s._test_applies == [_BOTTOM_SCRIPT]
        s.destroy()

    def test_scroll_to_latest_issues_bottom_and_arms_follow(self, monkeypatch):
        """scroll_to_latest: issues the bottom script NOW and arms the intent
        (the next append follows)."""
        s = self._surface(monkeypatch, read='{"y": 300, "atBottom": false}')
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        self._finish(s)
        s._test_applies.clear()
        s.scroll_to_latest()
        assert s._test_applies == [_BOTTOM_SCRIPT]  # ran the bottom script now
        assert s._pending_scroll == (True, 0.0)     # armed the follow intent
        assert s._was_at_bottom is True
        # After the "go to latest" script the document IS at the bottom, so
        # the next read reports it → the next append follows.
        s._test_read["text"] = '{"y": 1100, "atBottom": true}'
        s.append_message("agent", "<p>m2</p>", "Coder")
        s._drain_renders()
        s._test_applies.clear()
        self._finish(s)
        assert s._test_applies == [_BOTTOM_SCRIPT]
        s.destroy()

    def test_scroll_to_latest_without_document_only_arms_intent(self, monkeypatch):
        """No document yet → scroll_to_latest stores the intent only; the
        next FINISHED lands at the bottom."""
        s = self._surface(monkeypatch, fresh=True)
        s.scroll_to_latest()
        assert s._test_applies == []            # nothing to script yet
        assert s._pending_scroll == (True, 0.0)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        self._finish(s)
        assert s._test_applies == [_BOTTOM_SCRIPT]
        s.destroy()

    def test_dirty_row_mid_load_schedules_rerender(self, monkeypatch):
        """A row that arrived while the load was in flight must not be
        dropped — FINISHED schedules one more render."""
        s = self._surface(monkeypatch)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        self._finish(s)
        s._test_applies.clear()
        s.append_message("agent", "<p>m2</p>", "Coder")
        s._drain_renders()          # READ + load (in flight)
        s._dirty = True             # a row arrived mid-load
        s._render_pending = False
        self._finish(s)                 # FINISHED must re-arm the render
        assert s._render_pending is True
        s.destroy()

    def test_read_in_flight_coalesces_to_one_load(self, monkeypatch):
        """SP1.1 coalescing: while a pre-load READ is in flight, a second
        rendered append must NOT issue a second read+load — the in-flight
        callback loads the CURRENT rows (one load, one slot)."""
        s = self._surface(monkeypatch)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        self._finish(s)
        s._test_reads.clear()
        s._test_loads.clear()

        # Issue the first read but DO NOT complete it (simulate in-flight):
        # monkeypatch _document_eval to record and NOT call back yet.
        pending = []
        monkeypatch.setattr(s, "_document_eval", lambda script, cb: pending.append((script, cb)))
        s.append_message("agent", "<p>m2</p>", "Coder")
        s._drain_renders()          # issues read #1, _read_in_flight=True
        assert s._read_in_flight is True
        s.append_message("agent", "<p>m3</p>", "Coder")
        s._drain_renders()          # must be swallowed by the in-flight guard
        assert len(pending) == 1, "issued a second read while one was in flight"
        # Completing the in-flight read loads the CURRENT rows (m2 + m3).
        _script, cb = pending[0]
        cb('{"y": 0, "atBottom": true}')
        assert s._read_in_flight is False
        assert len(s._test_loads) == 1
        assert "m3" in s._test_loads[0]  # the row that arrived mid-read survives
        s.destroy()

    def test_second_append_during_load_defers_to_finished_kick(self, monkeypatch):
        """BUG#1 (audit fix): while a LOAD is in flight (read completed, load
        issued, FINISHED still pending), a second append must NOT issue a
        second read+load. Doing so overwrites the single `_pending_scroll`
        slot, so the first load's FINISHED consumes the SECOND's intent and
        the second load's FINISHED finds `None` — the reader strands at the
        top of the newest document. The append must instead stay dirty so the
        FINISHED kick re-renders it."""
        s = self._surface(monkeypatch)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        self._finish(s)  # seed the loaded document
        s._test_reads.clear()
        s._test_loads.clear()
        s._test_read["text"] = '{"y": 900, "atBottom": true}'
        s.append_message("agent", "<p>m2</p>", "Coder")
        s._drain_renders()  # read 1 → load 1 issued (in flight)
        assert len(s._test_loads) == 1
        assert s._load_in_flight is True
        reads_after_load1 = len(s._test_reads)

        # A second append arrives while load 1 is in flight.
        s._test_read["text"] = '{"y": 300, "atBottom": false}'
        s.append_message("agent", "<p>m3</p>", "Coder")
        s._drain_renders()

        # PRE-FIX these fail: a second read+load IS issued, overwriting intent.
        assert len(s._test_reads) == reads_after_load1, "issued a second read mid-load"
        assert len(s._test_loads) == 1, "issued a second load mid-load"
        assert s._dirty is True, "the mid-load append must stay dirty for the kick"
        s.destroy()

    def test_finished_applies_original_intent_then_kicks_fresh_render(self, monkeypatch):
        """BUG#1 continued: load 1's FINISHED applies READ-1's intent (not the
        overwritten read-2 intent), then the dirty-kick issues a FRESH read+load
        for the mid-load row. FINALLY both loads get an apply."""
        s = self._surface(monkeypatch)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        self._finish(s)
        s._test_reads.clear()
        s._test_loads.clear()
        s._test_applies.clear()

        s._test_read["text"] = '{"y": 900, "atBottom": true}'   # read-1 intent
        s.append_message("agent", "<p>m2</p>", "Coder")
        s._drain_renders()                                       # read 1 → load 1
        s._test_read["text"] = '{"y": 300, "atBottom": false}'  # read-2 intent (fresh)
        s.append_message("agent", "<p>m3</p>", "Coder")
        s._drain_renders()                                       # deferred (in flight)

        # FINISHED load 1 → apply READ-1's intent (at-bottom), then kick.
        self._finish(s)
        assert s._test_applies == [_BOTTOM_SCRIPT], (
            "load 1 must apply READ-1's intent, not the overwritten read-2 intent"
        )
        assert s._render_pending is True, "the dirty-kick must schedule a re-render"

        s._drain_renders()  # the kick issues a FRESH read 2 + load 2
        assert len(s._test_loads) == 2
        assert len(s._test_reads) == 2
        assert s._load_in_flight is True

        # FINISHED load 2 → apply READ-2's intent (reading offset 300).
        self._finish(s)
        assert len(s._test_applies) == 2, "BOTH loads must get an apply"
        assert "el.scrollTop = 300.0" in s._test_applies[-1]
        s.destroy()

    def test_load_failed_clears_in_flight_flag(self, monkeypatch):
        """BUG#1 wedge guard: a load that fails never reaches FINISHED. Without
        clearing the in-flight flag on the failure signal, every later render
        defers forever. Adapted to the REAL API — WebKit 6.0 has no
        `WebKit.LoadEvent.FAILED`; the failure signal is `load-failed`
        (probe-verified)."""
        s = self._surface(monkeypatch)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        self._finish(s)
        s.append_message("agent", "<p>m2</p>", "Coder")
        s._drain_renders()
        assert s._load_in_flight is True
        reads_before = len(s._test_reads)

        # The real failure signal clears the flag.
        s._on_load_failed(s._webview, WebKit.LoadEvent.STARTED, "about:blank", None)
        assert s._load_in_flight is False

        # A subsequent append is NOT wedged — its render issues a fresh load.
        s._test_read["text"] = '{"y": 0, "atBottom": true}'
        s.append_message("agent", "<p>m3</p>", "Coder")
        s._drain_renders()
        assert len(s._test_reads) == reads_before + 1, "render wedged after a failed load"
        assert len(s._test_loads) == 3  # m1, m2, m3 — all three loaded
        s.destroy()

    def test_load_failed_signal_is_connected(self):
        """BUG#1 wiring (Rule 5): `_ensure_webview` must actually connect the
        `load-failed` signal, or the flag never clears on a real failure."""
        from ui.views.chat_surface import ChatSurface

        if WebKit is None:
            pytest.skip("WebKit unavailable")
        s = ChatSurface()
        s._ensure_webview()
        assert s._load_failed_handler_id
        assert s._webview.handler_is_connected(s._load_failed_handler_id)
        s.destroy()

    def test_finished_after_destroy_issues_nothing(self, monkeypatch):
        """A late FINISHED on a destroyed surface must issue no script."""
        s = self._surface(monkeypatch)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        s._test_applies.clear()
        s.destroy()
        s._on_load_changed(None, WebKit.LoadEvent.FINISHED)
        assert s._test_applies == []

    # ── FIX ROUND 2 (Debugger re-audit): wedge-guard hardening ───────────

    def test_web_process_terminated_releases_gate_and_renders(self, monkeypatch):
        """FIX ROUND 2 (BUG#1b, HIGH): a web-process crash (or OOM kill) fires
        `web-process-terminated` and reaches NEITHER `load-failed` NOR FINISHED
        — without a handler the gate stuck and every later render deferred
        (auditor: 2/5 robust trials wedged). Drive the REAL signal
        (`WebView.emit`, arity `(view, reason)` — probe-verified) on a real web
        view whose handler `_ensure_webview` connected; assert the gate clears
        AND a waiting dirty row re-renders (the WebView recovers on the fresh
        load)."""
        from ui.views.chat_surface import ChatSurface

        if WebKit is None:
            pytest.skip("WebKit unavailable")
        s = ChatSurface()
        loads, reads, applies = [], [], []

        def _fake_eval(script, callback):
            if "JSON.stringify" in script:
                reads.append(script)
                callback('{"y": 0, "atBottom": true}')
            else:
                applies.append(script)
                callback(None)

        monkeypatch.setattr(s, "_document_eval", _fake_eval)
        monkeypatch.setattr(s, "_load_html", lambda doc: loads.append(doc))
        s._ensure_webview()  # REAL web view — connects web-process-terminated
        assert s._web_process_handler_id
        assert s._webview.handler_is_connected(s._web_process_handler_id)

        s._load_in_flight = True  # a load is in flight (as after _issue_load)
        s._dirty = True           # a row is waiting on the deferred render
        s._render_pending = False

        s._webview.emit(
            "web-process-terminated",
            WebKit.WebProcessTerminationReason.CRASHED,
        )
        assert s._load_in_flight is False, "gate not released on a web-process crash"
        assert s._render_pending is True, "dirty row not re-rendered after the crash"

        s._drain_renders()  # the kicked render issues a fresh load (recovery)
        assert loads, "no fresh load issued after the crash kick"
        s.destroy()

    def test_raising_load_releases_gate(self, monkeypatch):
        """FIX ROUND 2 (BUG#1a): `_load_html` can RAISE (probe: `load_html(None,
        ...)` raises). The flag is armed BEFORE the call, so an escaping
        exception left the gate set forever (deterministic wedge) and escaped
        the idle callback. Assert: no exception escapes, the gate clears, and a
        later append renders (not wedged)."""
        s = self._surface(monkeypatch)
        calls = {"n": 0}

        def _load(doc):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("load_html failed")
            s._test_loads.append(doc)

        monkeypatch.setattr(s, "_load_html", _load)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()  # _issue_load → _load raises → must NOT escape
        assert s._load_in_flight is False, "gate stuck after a raising load"

        # A dirty row re-renders on the next drain (the surface is not wedged).
        s.append_message("agent", "<p>m2</p>", "Coder")
        s._drain_renders()
        assert len(s._test_loads) == 1, "surface wedged after a raising load"
        s.destroy()

        # Direct pin of the kick-if-dirty branch: with a row already waiting, a
        # raising load must re-arm the render instead of stranding it.
        s2 = self._surface(monkeypatch)

        def _boom(doc):
            raise RuntimeError("boom")

        monkeypatch.setattr(s2, "_load_html", _boom)
        s2._dirty = True
        s2._render_pending = False
        s2._issue_load("<doc>")  # must NOT raise
        assert s2._load_in_flight is False
        assert s2._render_pending is True, "raising load stranded a dirty row"
        s2.destroy()

    def test_load_failed_then_finished_still_applies_intent(self, monkeypatch):
        """FIX ROUND 2 (BUG#1c): a failed NAVIGATION's `load-failed` is
        immediately followed by FINISHED (probe CASE B). Clearing the intent in
        `_on_load_failed` made that FINISHED apply NOTHING (probe D: intent
        (True, 900) → applies=0). The gate must clear, the intent must SURVIVE,
        and the trailing FINISHED must issue the apply script."""
        s = self._surface(monkeypatch)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        self._finish(s)
        s._test_applies.clear()

        s._test_read["text"] = '{"y": 900, "atBottom": true}'
        s.append_message("agent", "<p>m2</p>", "Coder")
        s._drain_renders()  # read → intent (True, 900.0) → load in flight
        assert s._pending_scroll == (True, 900.0)

        # Failed navigation: load-failed fires, then FINISHED (probe CASE B).
        s._on_load_failed(s._webview, WebKit.LoadEvent.STARTED, "about:blank", None)
        assert s._load_in_flight is False
        assert s._pending_scroll == (True, 900.0), "load-failed clobbered the intent"

        self._finish(s)  # the trailing FINISHED must still apply the intent
        assert s._test_applies == [_BOTTOM_SCRIPT], (
            "FINISHED after load-failed applied nothing (the intent was cleared)"
        )
        s.destroy()

    def test_finished_with_no_pending_still_kicks_dirty(self, monkeypatch):
        """FIX ROUND 2 (BUG#2, LOW): `_on_load_changed`'s `if pending is None:
        return` ran BEFORE the dirty-kick — with pending=None, dirty=True the
        row rendered only on a FUTURE append (delayed, not lost). The kick must
        run regardless of the intent's presence."""
        s = self._surface(monkeypatch)
        s._load_in_flight = True
        s._pending_scroll = None
        s._dirty = True
        s._render_pending = False
        self._finish(s)
        assert s._load_in_flight is False
        assert s._render_pending is True, (
            "FINISHED with no pending intent skipped the dirty-kick"
        )
        s.destroy()

    def test_scroll_to_latest_wins_over_in_flight_read(self, monkeypatch):
        """FIX ROUND 2 (Issue #3): `scroll_to_latest` while a pre-load READ is
        in flight — the read's STALE position must not clobber the user's
        "go to latest" intent (probe B: (False, 300.0)). The next FINISHED must
        apply `_BOTTOM_SCRIPT` (the user is not left mid-document)."""
        s = self._surface(monkeypatch)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        self._finish(s)
        s._test_applies.clear()

        # Start a read and DON'T complete it (in-flight); continue recording
        # applies so the FINISHED assertion below reads the real script.
        pending = []

        def _record_eval(script, callback):
            if "JSON.stringify" in script:
                pending.append((script, callback))
            else:
                s._test_applies.append(script)
                callback(None)

        monkeypatch.setattr(s, "_document_eval", _record_eval)
        s.append_message("agent", "<p>m2</p>", "Coder")
        s._drain_renders()  # read #1 issued, _read_in_flight=True
        assert s._read_in_flight is True
        assert len(pending) == 1

        # User presses "go to latest" while the read is in flight.
        s.scroll_to_latest()
        assert s._pending_scroll == (True, 0.0)

        # The stale read completes with a NON-bottom position.
        _script, cb = pending[0]
        cb('{"y": 300, "atBottom": false}')

        # FINISHED must apply the BOTTOM script — not scrollTop=300.
        self._finish(s)
        assert s._test_applies and s._test_applies[-1] == _BOTTOM_SCRIPT, (
            f"stale read clobbered the go-to-latest intent: {s._test_applies!r}"
        )
        s.destroy()

    def test_scroll_override_survives_web_process_crash(self, monkeypatch):
        """ROUND 3 (closure audit BUG#1, LOW): button intent armed while a read
        is in flight, then the web process crashes — `_on_web_process_terminated`
        clears the intent but left `_scroll_override` armed. The completing read
        took the override branch and KEPT a None intent → FINISHED applied
        NOTHING (the button's scroll silently dropped; probe-confirmed). The
        override branch must self-heal to the bottom intent (the button wins,
        SP1.3 ruling)."""
        s = self._surface(monkeypatch)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()
        self._finish(s)
        s._test_applies.clear()

        pending = []

        def _record_eval(script, callback):
            if "JSON.stringify" in script:
                pending.append((script, callback))
            else:
                s._test_applies.append(script)
                callback(None)

        monkeypatch.setattr(s, "_document_eval", _record_eval)
        s.append_message("agent", "<p>m2</p>", "Coder")
        s._drain_renders()
        assert len(pending) == 1

        # "Go to latest" pressed while the read is in flight.
        s.scroll_to_latest()
        assert s._scroll_override is True

        # Web-process crash mid-read: intent cleared, read still completes.
        s._on_web_process_terminated(s._webview, None)
        assert s._pending_scroll is None
        _script, cb = pending[0]
        cb('{"y": 300, "atBottom": false}')

        # Self-heal: the override branch re-arms the bottom intent.
        assert s._pending_scroll == (True, 0.0), (
            "crash dropped the button's intent (override kept a None intent)"
        )
        self._finish(s)
        assert s._test_applies and s._test_applies[-1] == _BOTTOM_SCRIPT
        s.destroy()

    def test_raising_load_on_render_path_requeues_the_row(self, monkeypatch):
        """ROUND 3 (closure audit BUG#2, LOW): a raising `_load_html` on the
        PRODUCTION `_do_render` path stranded the row — both call sites clear
        `_dirty` BEFORE `_issue_load`, so the except-branch's `if self._dirty`
        never fired (the old test set `_dirty` directly on the surface, a
        state production callers never produce). The except branch must
        re-queue UNCONDITIONALLY so the row renders on the next drain."""
        s = self._surface(monkeypatch)
        loads = s._test_loads
        raised = []

        def _raising_load(doc):
            raised.append(doc)
            raise RuntimeError("boom")

        monkeypatch.setattr(s, "_load_html", _raising_load)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()  # read completes → _issue_load → raise
        assert raised and s._load_in_flight is False
        assert s._render_pending is True, (
            "raising load left the row stranded — no re-queue on the render path"
        )
        # Recover with a sane load: the queued render must include the row.
        monkeypatch.setattr(s, "_load_html", lambda doc: loads.append(doc))
        s._drain_renders()
        assert len(loads) == 1 and "<p>m1</p>" in loads[0]
        s.destroy()

    def test_persistent_raising_load_is_bounded(self, monkeypatch):
        """ROUND 4 (spin latch): an UNCONDITIONALLY re-queued raising load is
        a self-feeding idle loop — each iteration clears `_dirty`, calls
        `_issue_load`, raises, re-queues (round-3 probe: ~4,500 attempts/sec,
        unbounded). The one-shot latch caps self-triggered retries: a
        persistent failure attempts the load at most TWICE, then waits. A
        new append after a successful load clears the latch (recovery)."""
        s = self._surface(monkeypatch)
        loads = s._test_loads
        state = {"raise": True, "attempts": 0}

        def _flaky_load(doc):
            state["attempts"] += 1
            if state["raise"]:
                raise RuntimeError("boom")
            loads.append(doc)

        monkeypatch.setattr(s, "_load_html", _flaky_load)
        s.append_message("agent", "<p>m1</p>", "Coder")
        s._drain_renders()  # attempt 1 raises → latch set → re-queued
        assert state["attempts"] == 1
        assert s._load_retry_pending is True

        # The re-queued render: attempt 2 raises again — latch blocks the
        # re-queue this time. NO further attempts, bounded.
        s._drain_renders()
        assert state["attempts"] == 2
        assert s._load_retry_pending is True
        assert s._render_pending is False, "latch failed — spin continues"

        # PERSISTENT failure must stay bounded across many drains.
        for _ in range(10):
            s._drain_renders()
        assert state["attempts"] == 2, "unbounded re-queue spin (latch broken)"

        # Recovery: the raise clears → the NEXT append retries and succeeds,
        # clearing the latch for future failures.
        state["raise"] = False
        s.append_message("agent", "<p>m2</p>", "Coder")
        s._drain_renders()
        assert state["attempts"] == 3 and len(loads) == 1
        assert s._load_retry_pending is False
        s.destroy()

    def test_document_eval_runs_in_named_isolated_world(self, monkeypatch):
        """The C-API deviation: with JS OFF, evaluate_javascript must target a
        NAMED world (a None world is the disabled MAIN world, which raises
        error 699). Pins `_DOC_WORLD` usage inside the real `_document_eval`."""
        from ui.views import chat_surface as cs

        calls = []
        s = cs.ChatSurface()
        fake_view = type("V", (), {
            "evaluate_javascript": lambda self, *a: calls.append(a),
        })()
        monkeypatch.setattr(s, "_ensure_webview", lambda: fake_view)
        # A multibyte script makes byte-length != char-length — a mutant that
        # passes len(script) (chars) fails the byte-length assertion below.
        script_in = "/* — · — */"
        s._document_eval(script_in, lambda _t: None)
        assert calls, "evaluate_javascript was not called"
        script, length, world = calls[0][0], calls[0][1], calls[0][2]
        assert script == script_in
        assert length == len(script_in.encode("utf-8"))
        assert length != len(script_in)  # non-tautological: proves bytes
        assert world == cs._DOC_WORLD and world is not None
        s.destroy()

    def test_read_script_uses_bottom_threshold_constant(self, monkeypatch):
        """SP1.1: the 80 MUST come from `_BOTTOM_THRESHOLD` (interpolated as
        a number), never a second literal. Change the constant → the script
        changes (kills a hardcoded-80 mutant)."""
        from ui.views import chat_surface as cs

        s = cs.ChatSurface()
        assert f"<= {cs.ChatSurface._BOTTOM_THRESHOLD:g}" in s._read_scroll_script()
        monkeypatch.setattr(cs.ChatSurface, "_BOTTOM_THRESHOLD", 123.0, raising=False)
        assert "<= 123" in s._read_scroll_script()
        assert "<= 80" not in s._read_scroll_script()
        s.destroy()


# ── SPEC-19 × SPEC-17: injection follow transaction + rebuild intent ──────


@pytest.mark.skipif(WebKit is None, reason="WebKit introspection unavailable")
class TestInjectionFollowTransaction:
    """The injection script is ONE atomic scroll transaction (measure →
    append → pin + bounded settle tail), and compaction reloads CAPTURE the
    reader intent instead of assuming bottom (the 2026-10-08 fix for the
    live-tier follow regression)."""

    def test_inject_script_interpolates_threshold_as_number(self):
        from ui.views.chat_surface import _inject_script

        script = _inject_script("<b>x</b>", 80.0)
        assert "var T = 80;" in script  # interpolated as a NUMBER
        assert "<= T" in script
        assert "el.scrollTop = el.scrollHeight" in script  # the pin
        assert "requestAnimationFrame" in script  # the settle tail
        assert "var T = 123;" in _inject_script("<b>x</b>", 123.0)  # mutant kill

    def test_inject_script_payload_is_json_literal(self):
        from ui.views.chat_surface import _inject_script

        payload = '</script><img onerror="x">'
        script = _inject_script(payload, 80.0)
        assert json.dumps(payload) in script  # JSON literal, never raw
        assert 'onerror="x"' not in script  # quotes escaped inside the literal

    def test_inject_script_tail_bounds(self):
        from ui.views import chat_surface as cs

        script = cs._inject_script("<p>x</p>", 80.0)
        assert f"frames > {cs._FOLLOW_TAIL_CAP}" in script
        assert f"stable > {cs._FOLLOW_SETTLE_FRAMES}" in script

    def _seam_surface(self, monkeypatch, *, read, webview=True):
        from ui.views.chat_surface import ChatSurface

        s = ChatSurface()
        s._live_js = True
        reads: list[str] = []
        applies: list[str] = []
        loads: list[str] = []

        def _fake_eval(script, callback):
            if "JSON.stringify" in script:  # the SP1.1 read script
                reads.append(script)
                callback(read)
            else:  # an apply script
                applies.append(script)
                callback(None)

        monkeypatch.setattr(s, "_document_eval", _fake_eval)
        mains: list[str] = []

        def _fake_eval_main(script, callback):
            mains.append(script)
            callback(None)

        monkeypatch.setattr(s, "_document_eval_main", _fake_eval_main)
        monkeypatch.setattr(s, "_schedule_bridge_poll", lambda: None)
        monkeypatch.setattr(s, "_load_html", lambda doc: loads.append(doc))
        if webview:
            s._webview = object()  # sentinel: a document is "loaded"
        s._test_reads = reads
        s._test_applies = applies
        s._test_mains = mains
        s._test_loads = loads
        return s

    def test_rebuild_captures_reading_position(self, monkeypatch):
        """A compaction must READ the document position first and restore it
        at FINISHED — never yank a mid-history reader to the bottom."""
        if not _surface_supports_smart_scroll():
            pytest.skip("ChatSurface is the TextViewFallback alias")
        s = self._seam_surface(monkeypatch, read='{"y": 150, "atBottom": false}')
        s._dirty = True
        s._needs_compact = True
        s._do_render()
        assert s._test_reads, "compaction did not READ the position first"
        assert s._pending_scroll == (False, 150.0)
        assert s._test_loads, "read callback did not issue the rebuild load"
        s._on_load_changed(s._webview, WebKit.LoadEvent.FINISHED)
        assert s._test_applies and s._test_applies[-1] != _BOTTOM_SCRIPT
        assert "el.scrollTop = 150.0" in s._test_applies[-1]
        s.destroy()

    def test_rebuild_at_bottom_lands_bottom(self, monkeypatch):
        if not _surface_supports_smart_scroll():
            pytest.skip("ChatSurface is the TextViewFallback alias")
        s = self._seam_surface(monkeypatch, read='{"y": 900, "atBottom": true}')
        s._dirty = True
        s._needs_compact = True
        s._do_render()
        s._on_load_changed(s._webview, WebKit.LoadEvent.FINISHED)
        assert s._test_applies[-1] == _BOTTOM_SCRIPT
        s.destroy()

    def test_rebuild_bottom_with_live_sections_arms_settle_tail(self, monkeypatch):
        """Resurrection regrows live-section heights AFTER the FINISHED apply
        (probe R7: 960px drift) — the bottom apply must carry the settle tail."""
        if not _surface_supports_smart_scroll():
            pytest.skip("ChatSurface is the TextViewFallback alias")
        from ui.views.chat_surface import _bottom_and_settle_script

        s = self._seam_surface(monkeypatch, read='{"y": 900, "atBottom": true}')
        row = {"role": "agent", "html": "<b>x</b>", "agent": "", "color": ""}
        s._rows.append(row)  # PRESENT row — `_reap_evicted_live` must NOT
        s._live_sections[7] = {"row": row, "payload": "<b>x</b>"}  # flatten it
        s._dirty = True
        s._needs_compact = True
        s._do_render()
        s._on_load_changed(s._webview, WebKit.LoadEvent.FINISHED)
        assert s._test_applies[-1] == _bottom_and_settle_script(s._BOTTOM_THRESHOLD)
        assert s._test_applies[-1] != _BOTTOM_SCRIPT
        assert "requestAnimationFrame" in s._test_applies[-1]
        s.destroy()

    def test_rebuild_without_webview_lands_bottom(self, monkeypatch):
        if not _surface_supports_smart_scroll():
            pytest.skip("ChatSurface is the TextViewFallback alias")
        s = self._seam_surface(monkeypatch, read="unused", webview=False)
        s._dirty = True
        s._needs_compact = True
        s._do_render()
        assert s._pending_scroll == (True, 0.0)
        assert s._test_loads, "no load issued"
        assert not s._test_reads, "no document → no read"
        s.destroy()


# ── SPEC-19 SP2: the ```live tier (fence detection, T2 degrade, cap) ──────


class TestLiveFenceDetection:
    """§2.1: the pure whole-message ```live detector — mirrors the ```html
    fence exactly; prose around the fence makes it None; ```html is NOT live."""

    def test_whole_message_live_fence_returns_payload(self):
        from render.html import live_fence
        assert live_fence("```live\n<b>hi</b>\n```") == "<b>hi</b>"

    def test_crlf_and_uppercase_tolerated(self):
        from render.html import live_fence
        assert live_fence("```LIVE\r\n<div>x</div>\r\n```") == "<div>x</div>"

    def test_prose_before_or_after_is_not_live(self):
        from render.html import live_fence
        assert live_fence("here:\n```live\nx\n```") is None
        assert live_fence("```live\nx\n```\ntrailing") is None

    def test_html_fence_is_not_live(self):
        from render.html import live_fence
        assert live_fence("```html\n<b>x</b>\n```") is None

    def test_empty_payload_is_empty_string_not_none(self):
        from render.html import live_fence
        assert live_fence("```live\n```") == ""

    def test_non_str_and_empty(self):
        from render.html import live_fence
        assert live_fence("") is None
        assert live_fence(None) is None


class TestLiveSectionMarkup:
    """The island markup: payload is inert JSON; `<` escaped so a `</script>`
    in the payload can never close the island early; flattened markup has no
    script/on*."""

    def test_island_escapes_lt(self):
        from ui.views.chat_surface import _live_section_island_html
        html = _live_section_island_html(3, "<script>alert(1)</script>")
        assert 'data-live-id="3"' in html
        assert "\\u003cscript" in html  # escaped, cannot terminate the island
        assert "</script><script>" not in html
        assert html.count("</script>") == 1  # only the island's own close

    def test_flatten_mirror_strips_scripts_and_onstar(self):
        from ui.views.chat_surface import _live_flattened_section_html
        out = _live_flattened_section_html(
            1, '<b>keep</b><script>bad()</script><img src="x" onerror="boom()">')
        assert "<script" not in out and "bad()" not in out
        assert "onerror" not in out
        assert "<b>keep</b>" in out
        assert 'src="x"' in out  # non-on* attrs survive


class TestLiveKillSwitch:
    """§2.5/§4: the kill-switch default is ON; =0 disables (T2 static)."""

    def test_default_on(self, monkeypatch):
        monkeypatch.delenv("DEVELCAKES_LIVE_JS", raising=False)
        monkeypatch.delenv("CRABCAKES_LIVE_JS", raising=False)
        from ui.views.chat_surface import _live_js_enabled
        assert _live_js_enabled() is True

    def test_explicit_zero_disables(self, monkeypatch):
        monkeypatch.setenv("DEVELCAKES_LIVE_JS", "0")
        from ui.views.chat_surface import _live_js_enabled
        assert _live_js_enabled() is False

    def test_old_name_zero_disables(self, monkeypatch):
        monkeypatch.delenv("DEVELCAKES_LIVE_JS", raising=False)
        monkeypatch.setenv("CRABCAKES_LIVE_JS", "0")
        from ui.views.chat_surface import _live_js_enabled
        assert _live_js_enabled() is False

    def test_explicit_one_still_enables(self, monkeypatch):
        monkeypatch.setenv("DEVELCAKES_LIVE_JS", "1")
        from ui.views.chat_surface import _live_js_enabled
        assert _live_js_enabled() is True


class TestLiveCapAndFlattenPython:
    """§4 cap-10 + flatten mirror at the PYTHON level (no WebKit): registry
    bounded, oldest flattened (row html rewritten to a script-free section)."""

    def test_cap_twelve_leaves_ten_and_flattens_oldest_two(self):
        from ui.views.chat_surface import ChatSurface
        if WebKit is None:
            pytest.skip("ChatSurface needs WebKit for the live registry")
        s = ChatSurface()
        s._live_js = True
        try:
            ids = [s.append_live(f"<b>{i}</b><script>x{i}()</script>") for i in range(12)]
            assert len(s._live_sections) == 10
            # The two OLDEST are gone from the registry and flattened in the deque.
            assert ids[0] not in s._live_sections
            assert ids[1] not in s._live_sections
            assert ids[11] in s._live_sections
            flat_rows = [r for r in s._rows if r.get("live_id") == ids[0]]
            assert flat_rows and "<script" not in flat_rows[0]["html"]
        finally:
            s.destroy()

    def test_flatten_rewrites_row_for_rebuild(self):
        from ui.views.chat_surface import ChatSurface
        if WebKit is None:
            pytest.skip("ChatSurface needs WebKit for the live registry")
        s = ChatSurface()
        s._live_js = True
        try:
            lid = s.append_live("<b>hi</b><script>leak()</script>")
            s._flatten_live_section(lid, dom=False)
            assert lid not in s._live_sections
            row = [r for r in s._rows if r.get("live_id") == lid][0]
            assert "<script" not in row["html"] and "leak()" not in row["html"]
            assert "<b>hi</b>" in row["html"]
        finally:
            s.destroy()


class TestLiveDocumentBuild:
    """§2.2/§2.3: _blocks_html emits a live section VERBATIM as a top-level
    child (not wrapped in agent-box chrome); the emitted document carries the
    inert island."""

    def test_live_row_emitted_verbatim(self):
        from ui.views.chat_surface import _document, _live_section_island_html
        rows = [{
            "role": "agent",
            "html": _live_section_island_html(7, "<b>x</b>"),
            "agent": "Coder",
            "live_id": 7,
        }]
        doc = _document(rows)
        assert 'class="live-section"' in doc
        assert 'data-live-id="7"' in doc
        assert 'class="agent-box' not in doc  # not chrome-wrapped


class TestLiveHandlerTierDecision:
    """§2.2/§2.3: the handler routes a ```live fence to the live append ONLY
    when the surface reports the tier enabled; otherwise it degrades to the
    T2-static sanitized fragment (never error, never raw script)."""

    def test_compose_live_fence_yields_payload_and_static(self):
        from ui.handlers.chat_render_handler import _compose_text
        payload, static = _compose_text("```live\n<b>x</b><script>y()</script>\n```")
        assert payload == "<b>x</b><script>y()</script>"
        assert "<script" not in static and "<b>x</b>" in static

    def test_compose_non_live_has_no_payload(self):
        from ui.handlers.chat_render_handler import _compose_text
        payload, static = _compose_text("plain **md**")
        assert payload is None
        assert "<strong>md</strong>" in static

    def test_append_composed_uses_live_path_when_enabled(self):
        from ui.handlers.chat_render_handler import _append_composed

        calls = {}

        class _LiveSurface:
            def is_live_enabled(self):
                return True

            def append_live(self, payload, agent_name=None, agent_color=None):
                calls["live"] = payload

            def append_message(self, *a, **k):
                calls["static"] = a

        _append_composed(_LiveSurface(), "Agent", "<b>L</b>", "<b>L</b>", "Coder", None)
        assert calls.get("live") == "<b>L</b>"
        assert "static" not in calls

    def test_append_composed_degrades_when_disabled(self):
        from ui.handlers.chat_render_handler import _append_composed

        calls = {}

        class _OffSurface:
            def is_live_enabled(self):
                return False

            def append_live(self, payload, agent_name=None, agent_color=None):
                calls["live"] = payload

            def append_message(self, role, fragh, agent_name=None, agent_color=None):
                calls["static"] = (role, fragh)

        _append_composed(_OffSurface(), "Agent", "<b>L</b>", "<b>STATIC</b>", "Coder", None)
        assert "live" not in calls
        assert calls["static"] == ("agent", "<b>STATIC</b>")

    def test_append_composed_static_when_no_payload(self):
        from ui.handlers.chat_render_handler import _append_composed

        calls = {}

        class _Surface:
            def is_live_enabled(self):
                return True

            def append_live(self, payload, **k):
                calls["live"] = payload

            def append_message(self, role, fragh, agent_name=None, agent_color=None):
                calls["static"] = fragh

        _append_composed(_Surface(), "Agent", None, "<p>md</p>", "Coder", None)
        assert "live" not in calls
        assert calls["static"] == "<p>md</p>"


class TestSurfaceClear:
    """SPEC-19 SP4 follow-up (the /clear UI plane): the surface clears
    itself in place — rows/live sections/stream state reset, empty
    document reloaded, no tombstone (the tab stays renderable).

    Uses the TextViewFallback where the state contract overlaps (rows/
    buffers) and skips WebKit-only internals headless."""

    def test_clear_resets_all_content_state(self, surface):
        if not hasattr(surface, "_live_sections"):
            pytest.skip("headless fallback — WebKit-only internals")
        surface._rows.append({"role": "user", "html": "<p>old</p>", "agent": "", "color": ""})
        surface._stream_buffers["sk"] = ["partial"]
        surface._stream_placeholders["sk"] = {"role": "agent"}
        surface._live_sections[7] = {"row": {"html": "x"}, "payload": "x"}
        surface._next_live_id = 8
        surface._injected_seq = 99
        surface._dom_row_count = 120
        surface.clear()
        assert not surface._rows
        assert not surface._stream_buffers
        assert not surface._stream_placeholders
        assert not surface._live_sections
        assert surface._next_live_id == 1
        assert surface._injected_seq == 0
        assert surface._dom_row_count == 0

    def test_clear_is_idempotent_on_destroyed_surface(self, surface):
        surface._destroyed = True
        surface._rows.append({"role": "user", "html": "x", "agent": "", "color": ""})
        surface.clear()
        assert len(surface._rows) == 1  # untouched — destroy guard holds

    def test_clear_schedules_empty_document_load(self, surface, monkeypatch):
        if not hasattr(surface, "_issue_load"):
            pytest.skip("headless fallback — WebKit-only internals")
        loads = []
        monkeypatch.setattr(surface, "_issue_load", lambda doc: loads.append(doc))
        surface._rows.append({"role": "user", "html": "<p>old</p>", "agent": "", "color": ""})
        surface.clear()
        assert len(loads) == 1
        assert "old" not in loads[0]
