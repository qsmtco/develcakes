# tests/test_html_guard_sites.py
# SPEC-06 SP6 Phase 1: source-catalog guard tests for the HTML render
# pipeline — the v2 analogue of tests/test_pango_guard_sites.py.
#
# The architecture contract (architecture.md, Patterns): "guard tests enforce
# fail-closed sanitization at every converted call site". For the Pango
# pipeline that meant parse_markup guards at set_markup sites; for the HTML
# pipeline it means the escape-first emitter, the fail-closed sanitizer
# allowlist, and the JS-off webview settings are pinned in source.
#
# CATALOG (6 entries — each verified against source before pinning):
#   1. ui/views/chat_surface.py            — JS gated on the LiveGuard attach
#                                            (behavior pin lives in
#                                            tests/test_chat_surface.py:109,
#                                            test_javascript_disabled; here we
#                                            pin the GATING in source).
#   1b. ui/views/feed_surface.py           — the second JS surface, same rule
#                                            (SPEC-66 §11.6; pre-fix it had a
#                                            bare JS-enable and no guard).
#   2. render/sanitize.py                  — fail-closed allowlist posture.
#   3. render/html.py                      — escape-first emission.
#   4. ui/handlers/chat_render_handler.py  — every surface append is
#                                            pipeline-sanitized (render_welcome
#                                            re-sanitize is belt-and-braces).
#   5. ui/views/event_cards.py             — Pango guard on the non-converted
#                                            surface (TestEventCardsCodeLabelGuard
#                                            in test_pango_guard_sites.py owns
#                                            the deep pins; the catalog carries
#                                            a presence witness so the catalog
#                                            stays complete without duplication).
#
# Style follows test_pango_guard_sites.py: read file source, assert guard
# strings; behavior assertions where they are cheap and mock-free (render/
# pipeline functions are pure Python — no GTK/WebKit construction needed).

import inspect
import re

from render.html import markdown_to_html, render_document

# ── Catalog entry 1: chat_surface.py — JavaScript gated on the guard ─────


class TestChatSurfaceJavaScriptOff:
    """WebKit settings: JS posture is a SECURITY decision, not a preference.

    SPEC-66 §2.11 tightened this from "JS follows the live flag" to "JS is
    enabled ONLY by the LiveGuard attaching". Pre-fix, `_ensure_webview` did
    `set_enable_javascript(live)`, so a live-flagged surface ran script from
    the moment the view existed — before the filter compiled, and forever if
    it never did. Now the view is created JS-off and `_on_live_guard_compiled`
    is the ONLY place JS is turned on, after `attach` returns True.

    Behavior pins live in tests/test_chat_surface.py::test_javascript_disabled
    (kill-switch OFF → JS off) and tests/test_live_guard.py (ON → JS on +
    filter attached).
    """

    PATH = "ui/views/chat_surface.py"

    @staticmethod
    def _src():
        from ui.views import chat_surface
        return inspect.getsource(chat_surface)

    def test_webview_is_created_with_js_off(self):
        """The view must never be BORN scriptable: a compile failure then
        degrades to static instead of running unbounded."""
        src = self._src()
        ensure = src[src.index("def _ensure_webview"):]
        ensure_body = ensure[: ensure.index("return self._webview")]
        assert "set_enable_javascript(False)" in ensure_body, (
            "chat_surface: _ensure_webview no longer disables JS at creation — "
            "a filter that never compiles would leave script running"
        )
        assert "set_enable_javascript(live)" not in ensure_body, (
            "chat_surface: JS is again driven by the live FLAG at creation "
            "instead of by the guard attaching (SPEC-66 §2.11)"
        )

    def test_js_enable_only_after_a_successful_attach(self):
        """The single `set_enable_javascript(True)` must be in the attach
        callback AND guarded by a checked `attach(...)` return."""
        src = self._src()
        assert src.count("set_enable_javascript(True)") == 1, (
            "chat_surface: expected exactly one JS-enable site (the attach "
            "callback); a second one is an unguarded path to script"
        )
        callback = src[src.index("def _on_live_guard_compiled"):]
        callback = callback[: callback.index("\n    def ", 1)] if "\n    def " in callback[1:] else callback
        assert "set_enable_javascript(True)" in callback, (
            "chat_surface: JS is no longer enabled from _on_live_guard_compiled"
        )
        assert "if not self._live_guard.attach(self._webview)" in callback, (
            "chat_surface: attach's return value is not checked — a failed "
            "attach would still reach the JS-enable line"
        )


# ── Catalog entry 1b: feed_surface.py — the second JS surface ────────────


class TestFeedSurfaceJavaScriptGated:
    """SPEC-66 §11.6: `ui/views/feed_surface.py` was a SECOND JS-enabled
    WebView with NO LiveGuard (pre-fix `:288` did a bare
    `set_enable_javascript(True)` in `__init__`). The feed genuinely needs JS
    (FEED_SCRIPT drives incremental patching and scroll restore), so it is
    not a JS-off surface — it is a guard-gated one, under the same rule as
    the chat surface. `_esc` escaping every card field lives in
    render/feed_html.py (catalog entry 3) and is defense in depth, not the
    boundary; this entry pins the boundary itself.
    """

    PATH = "ui/views/feed_surface.py"

    @staticmethod
    def _src():
        from ui.views import feed_surface
        return inspect.getsource(feed_surface)

    def test_feed_webview_is_created_with_js_off(self):
        src = self._src()
        assert "set_enable_javascript(False)" in src, (
            "feed_surface: the webview is no longer created JS-off"
        )

    def test_feed_has_exactly_one_guarded_js_enable(self):
        src = self._src()
        assert src.count("set_enable_javascript(True)") == 1, (
            "feed_surface: expected exactly one JS-enable site (the guard "
            "callback); more means an unguarded path to script"
        )
        callback = src[src.index("def _on_live_guard_compiled"):]
        assert "set_enable_javascript(True)" in callback, (
            "feed_surface: JS is not enabled from the guard callback"
        )

    def test_feed_compiles_and_attaches_the_guard(self):
        """A guard that is constructed but never compiled/attached is
        decoration — the feed would stay static forever and the JS-enable
        line would be dead code that can never fire."""
        src = self._src()
        assert "LiveGuard()" in src, "feed_surface: no LiveGuard constructed"
        assert ".compile(self._on_live_guard_compiled)" in src, (
            "feed_surface: the guard is never compiled, so the callback (and "
            "therefore JS) can never activate"
        )
        callback = src[src.index("def _on_live_guard_compiled"):]
        assert "if not self._live_guard.attach(self._webview)" in callback, (
            "feed_surface: attach's return value is not checked — fail-closed "
            "is broken"
        )

    def test_no_bare_js_enable_outside_the_callback(self):
        src = self._src()
        init = src[src.index("def __init__"):]
        init_body = init[: init.index("def _on_live_guard_compiled")]
        assert "set_enable_javascript(True)" not in init_body, (
            "feed_surface: JS is enabled during construction, before the "
            "filter exists (the original §11.6 bug)"
        )


# ── Catalog entry 2: sanitize.py — fail-closed allowlist posture ──────────


class TestSanitizerAllowlistFailClosed:
    """render/sanitize.py policy: anything not explicitly allowed is stripped.

    Fail-closed contract: ANY internal error in sanitize_html returns ""
    (never the raw input). The tags/attributes maps below ARE the policy —
    a new tag/attr lands here only deliberately.
    """

    FORBIDDEN_TAGS = ("script", "iframe", "style", "form")

    def test_forbidden_tags_absent_from_allowed_tags(self):
        from render.sanitize import _ALLOWED_TAGS
        for tag in self.FORBIDDEN_TAGS:
            assert tag not in _ALLOWED_TAGS, (
                f"<{tag}> is in _ALLOWED_TAGS — sanitizer policy weakened"
            )

    def test_src_admitted_only_for_img(self):
        """`src` may appear ONLY on img, and only with the http(s) filter
        gate (_attribute_filter_inner). Any second src admission widens the
        injection surface (every other element must not carry URLs)."""
        from render.sanitize import _ATTRIBUTES
        carriers = [tag for tag, attrs in _ATTRIBUTES.items() if "src" in attrs]
        assert carriers == ["img"], (
            f"src admitted for non-img tags: {carriers}"
        )

    def test_src_url_scheme_gate_is_http_s_only(self):
        """The filter owns src's scheme gate: http/https pass, everything
        else (javascript:, data:, file:, relative) returns None = strip."""
        from render.sanitize import _attribute_filter_inner
        assert _attribute_filter_inner("img", "src", "https://x/y.png") == "https://x/y.png"
        assert _attribute_filter_inner("img", "src", "http://x/y.png") == "http://x/y.png"
        assert _attribute_filter_inner("img", "src", "javascript:alert(1)") is None
        assert _attribute_filter_inner("img", "src", "data:text/html;base64,PHNjcmlwdD4=") is None
        assert _attribute_filter_inner("img", "src", "file:///etc/passwd") is None
        assert _attribute_filter_inner("img", "src", "/relative/x.png") is None

    def test_class_tokens_individually_allowlisted(self):
        """Every class TOKEN must match the allowlist regex; unknown tokens
        are dropped token-wise, and an all-unknown class is stripped."""
        from render.sanitize import _CLASS_TOKEN_RE
        allowed = ["terminal", "task-list", "task-checked", "welcome-row",
                   "tok-kw", "lang-python", "lang-c++"]
        for tok in allowed:
            assert _CLASS_TOKEN_RE.fullmatch(tok), f"allowed token regressed: {tok}"
        for tok in ["eviltoken", "welcome-row-extra", "LANG-PY",
                    "task list", "terminal drop-table"]:
            assert not _CLASS_TOKEN_RE.fullmatch(tok), f"token leaked: {tok}"
        # SP6 Phase 1 tighten (supervisor ruling on the audit finding): the
        # suffix quantifier is now `+` — bare "tok-"/"lang-" (empty suffix)
        # are STRIPPED. All real emitters produce non-empty suffixes.
        assert not _CLASS_TOKEN_RE.fullmatch("tok-"), "empty-suffix token leaked"
        assert not _CLASS_TOKEN_RE.fullmatch("lang-"), "empty-suffix token leaked"
        # Token-wise behavior through the filter: mixed → only the allowed
        # token survives; all-unknown → None (strip).
        from render.sanitize import _attribute_filter_inner
        assert _attribute_filter_inner("p", "class", "hello welcome-row") == "welcome-row"
        assert _attribute_filter_inner("p", "class", "hello world") is None

    def test_p_additive_class_entry_documented(self):
        """SP5c-1 BUG#4 ruling (option a): the welcome row's class rides the
        emitted block <p> — `p` admits `class` additively. The VALUE stays
        gated by the token allowlist (no policy weakening)."""
        from render.sanitize import _ATTRIBUTES
        assert "class" in _ATTRIBUTES.get("p", set()), (
            "p lost its additive class entry — welcome-row hook is dead"
        )
        # ...and the comment block documenting the ruling travels with it.
        from render import sanitize as sanitize_mod
        src = inspect.getsource(sanitize_mod)
        assert "SP5c-1" in src and "option a" in src, (
            "the p/class ruling comment is gone from sanitize.py"
        )

    def test_sanitize_html_returns_empty_on_internal_error(self):
        """Fail-closed contract: sanitize_html returns "" on ANY internal
        error — the raw input is never passed through. Simulated by an
        nh3.clean that raises (the PanicException shape included)."""
        import render.sanitize as sanitize_mod

        def _boom(*a, **k):
            raise RuntimeError("simulated ammonia failure")

        original = sanitize_mod.nh3.clean
        sanitize_mod.nh3.clean = _boom
        try:
            assert sanitize_mod.sanitize_html("<p>never passes through</p>") == ""
        finally:
            sanitize_mod.nh3.clean = original


# ── Catalog entry 3: html.py — escape-first emission ──────────────────────


class TestEmitterEscapeFirst:
    """render/html.py inverts the Pango contract: it takes RAW markdown and
    escaping is THE EMITTER'S JOB — every text node passes html.escape()
    before tag emission; no raw input reaches the output."""

    def test_escape_precedes_tag_emission_in_inline(self):
        """Order-of-operations pin: html.escape(text) must run in _inline
        BEFORE the first tag-emitting substitution (the bold pass)."""
        from render import html as html_mod
        src = inspect.getsource(html_mod._inline)
        escape_pos = src.index("text = html.escape(text)")
        first_emit_pos = src.index("re.sub(")
        assert escape_pos < first_emit_pos, (
            "_inline emits tags BEFORE escaping — raw HTML passthrough"
        )

    def test_module_docstring_states_escape_first_contract(self):
        """html.py states its contract in a header COMMENT (module __doc__
        is empty — verified); pin the comment, not the attribute."""
        from render import html as html_mod
        src = inspect.getsource(html_mod)
        header = src[: src.index("import")]
        assert "ESCAPING IS THE" in header and "EMITTER" in header.upper(), (
            "html.py header no longer states the escape-first contract"
        )

    def test_raw_text_nodes_are_escaped(self):
        """Behavior: raw HTML-looking agent text can never survive as markup."""
        out = markdown_to_html("hello <script>alert(1)</script> & <b>bold</b>")
        assert "<script>" not in out and "<b>bold" not in out
        assert "&lt;script&gt;" in out and "&lt;b&gt;" in out and "&amp;" in out

    def test_non_http_link_schemes_render_as_plain_text(self):
        """js:/data:/relative labels are wrapped in <span>, never <a href> —
        the emitter doesn't emit what the sanitizer would strip."""
        out = markdown_to_html("[t](javascript:alert(1))")
        assert "<a href=" not in out
        assert "javascript:" not in out
        assert "<span>t</span>" in out

    def test_render_document_composes_through_sanitizer(self):
        """The composition entry point chains markdown_to_html → sanitize_html;
        its output contains no element outside the sanitizer allowlist."""
        doc = render_document("# Hi\n\n<script>alert(1)</script>\n\n```python\nx = 1\n```")
        assert "<script" not in doc.lower()
        for tag in re.findall(r"<([a-z0-9]+)", doc):
            from render.sanitize import _ALLOWED_TAGS
            assert tag in _ALLOWED_TAGS, f"render_document emitted non-allowlisted <{tag}>"


# ── Catalog entry 4: chat_render_handler.py — pipeline in every append ────


class TestRenderHandlerAppendsArePipelineSanitized:
    """Every surface append goes through the render pipeline (escape-first
    emitter + fail-closed sanitizer). Raw HTML must never reach the surface:
    even the composition-failure fallback appends html.escape'd text."""

    PATH = "ui/handlers/chat_render_handler.py"

    @staticmethod
    def _read(path):
        with open(path) as f:
            return f.read()

    def test_every_append_message_uses_pipeline_fragment(self):
        """Every `append_message(...)` CALL passes a pipeline fragment or the
        documented escaped fallback — never raw agent text.

        AST-based (not string-scan): comment and docstring mentions of
        append_message must not count as call sites. Verified inventory
        (SPEC-19 SP2: the transcript compose moved into the shared helpers
        `_compose_text`/`_append_composed`): 3 call sites in CRH —
        _append_composed (pipeline), render_welcome (pipeline), and the
        _append_on_main failure fallback (html.escape(text)). The live path
        (`surface.append_live`) is NOT an append_message site; it takes the
        RAW agent payload by design (T3, enforced by E1/E2, not sanitized)."""
        import ast as ast_mod
        src = self._read(self.PATH)
        tree = ast_mod.parse(src)
        sites = []
        for node in ast_mod.walk(tree):
            if (isinstance(node, ast_mod.Call)
                    and isinstance(node.func, ast_mod.Attribute)
                    and node.func.attr == "append_message"):
                seg = ast_mod.get_source_segment(src, node) or ""
                sites.append((node, seg))
        assert len(sites) == 3, (
            f"expected exactly 3 append_message call sites, found {len(sites)} — "
            "catalog stale: verify each new site is pipeline-fed, then "
            "extend this pin"
        )
        for node, seg in sites:
            assert len(node.args) >= 2, f"append_message call lacks fragment arg: {seg!r}"
            frag = node.args[1]
            if isinstance(frag, ast_mod.Name) and frag.id == "html_fragment":
                continue  # the pipeline-composed variable
            # The ONLY other allowed shape: the escaped-raw fallback.
            assert re.search(
                r"html\.escape\(text\)\s*\+\s*['\"]<!-- fallback: escaped raw -->['\"]",
                seg,
            ), f"append_message fed from an unvetted source: {seg!r}"

    def test_append_live_takes_raw_payload_only_through_the_t3_helper(self):
        """SPEC-19 SP2 T3: the ONLY `append_live` call site is `_append_composed`
        — it forwards a payload produced by `_compose_text` from a detected
        whole-message ```live fence. Raw agent TEXT is never appended live
        without the fence detector (the tier decision is centralized)."""
        import ast as ast_mod
        src = self._read(self.PATH)
        tree = ast_mod.parse(src)
        sites = []
        for node in ast_mod.walk(tree):
            if (isinstance(node, ast_mod.Call)
                    and isinstance(node.func, ast_mod.Attribute)
                    and node.func.attr == "append_live"):
                sites.append(ast_mod.get_source_segment(src, node) or "")
        assert len(sites) == 1, f"expected exactly 1 append_live call site, found {len(sites)}"
        # The tier gate: append_live is guarded by is_live_enabled().
        assert "is_live_enabled" in src, (
            "append_live is not gated on surface.is_live_enabled() — a live "
            "payload could reach a JS-off surface"
        )

    def test_composition_failure_fallback_is_escaped(self):
        """Both fallback paths escape: render_document failure appends
        html.escape(text), never raw."""
        src = self._read(self.PATH)
        assert re.search(r"html\.escape\(text\)\s*\+\s*['\"]<!-- fallback: escaped raw -->", src), (
            "composition-failure fallback no longer escapes text"
        )

    def test_render_message_is_the_compose_entry(self):
        """SPEC-13 §2c/§2g (+SPEC-19 SP2): the transcript compose path calls
        render_message (the chat entry with the whole-message ```html fence
        branch); render_document is called ONLY by the welcome path
        (render_welcome), which stays markdown-by-design.

        SPEC-19 SP2 refactor: the two transcript sites now share ONE compose
        helper, `_compose_text` (which adds the ```live branch and delegates
        the non-live text to render_message). `_append_to_surface` and
        `_compose_off_thread` call `_compose_text`; both must keep routing
        through it (NOT back to a bare render_document).

        AST pin, not a string scan.
        """
        import ast as ast_mod
        src = self._read(self.PATH)
        tree = ast_mod.parse(src)
        parents: dict = {}
        for node in ast_mod.walk(tree):
            for child in ast_mod.iter_child_nodes(node):
                parents[child] = node

        def _enclosing_func(node):
            cur = parents.get(node)
            while cur is not None:
                if isinstance(cur, (ast_mod.FunctionDef, ast_mod.AsyncFunctionDef)):
                    return cur.name
                cur = parents.get(cur)
            return None

        rd_calls: list = []
        rm_calls: list = []
        ct_calls: list = []
        for node in ast_mod.walk(tree):
            if (isinstance(node, ast_mod.Call)
                    and isinstance(node.func, ast_mod.Name)):
                if node.func.id == "render_document":
                    rd_calls.append(_enclosing_func(node) or "<module>")
                elif node.func.id == "render_message":
                    rm_calls.append(_enclosing_func(node) or "<module>")
                elif node.func.id == "_compose_text":
                    ct_calls.append(_enclosing_func(node) or "<module>")

        assert rd_calls == ["render_welcome"], (
            "render_document must be called ONLY by render_welcome (the "
            f"markdown-by-design welcome path); found {rd_calls!r}"
        )
        # render_message is now reached via the shared _compose_text helper
        # (itself called by BOTH transcript sites).
        assert rm_calls == ["_compose_text"], (
            f"render_message must be reached via _compose_text; found {rm_calls!r}"
        )
        assert set(ct_calls) >= {"_append_to_surface", "_compose_off_thread"}, (
            "the transcript compose must route through _compose_text at BOTH "
            f"sites; found {ct_calls!r}"
        )

    def test_welcome_re_sanitize_present(self):
        """The welcome row is composed AND passed through sanitize_html again
        (belt-and-braces — the site independently witnesses fail-closed)."""
        src = self._read(self.PATH)
        assert "html_fragment = sanitize_html(inner.replace(" in src, (
            "render_welcome's re-sanitize is gone"
        )
        # ...and the welcome is dropped, never emitted raw, on compose failure.
        welcome = src[src.index("def render_welcome"):]
        welcome_body = welcome[: welcome.index("# ── Async (thread-safe)")]
        assert "welcome compose failed" in welcome_body
        assert "return" in welcome_body.split("except Exception:")[1].split("_logger.exception")[1][:200]

    def test_render_sync_appends_sanitized_fragment(self, monkeypatch):
        """Behavior: render_sync with a stub surface proves the fragment the
        real surface receives is the render_document output (starts with a
        sanitized tag), not raw text — the guard's end-to-end witness."""
        from ui.handlers import chat_render_handler as crh
        received = {}

        class _StubSurface:
            def get_parent(self):
                return None

            def append_message(self, role, html_fragment, agent_name=None, agent_color=None):
                received["role"] = role
                received["html"] = html_fragment

        handler = crh.ChatRenderHandler(GLib_module=None)
        monkey_surface = _StubSurface()
        # Surface FIRST in the guard order: the stub is handed out directly.
        handler._surfaces["sk-guard"] = monkey_surface
        # SP6 Phase 1 audit fold-in (Debugger): monkeypatch.setattr, not bare
        # module assignment — no leak of the stub factory past this test.
        monkeypatch.setattr(crh, "create_chat_surface", lambda *a, **k: monkey_surface)
        handler.render_sync("Agent", "<script>alert(1)</script>", "sk-guard")
        assert received["role"] == "agent"
        assert "<script>" not in received["html"]
        assert received["html"].startswith("<p>")  # sanitized pipeline output

    # (Catalog entry 5's presence witness — event_cards' code-label Pango
    # guard — was dropped in SPEC-58 §2i: ui/views/event_cards.py is deleted
    # and there is no Pango surface left to inspect. The surviving HTML
    # surface's guards are pinned by the classes above.)


# ── Red-first harness (mutation demo documented in the phase report) ──────
#
# RED-FIRST evidence procedure (executed during Phase 1, report §red-first):
#   mutation:  render/html.py — delete the `text = html.escape(text)` line
#              in _inline (the escape-first pivot)
#   expected:  TestEmitterEscapeFirst::test_raw_text_nodes_are_escaped FAILS
#              (raw <script> survives into the output) AND
#              test_escape_precedes_tag_emission_in_inline FAILS (the pivot
#              line is gone).
#   restore:   byte-identical via git (verified with git diff — no residual).
# The catalog therefore demonstrably fails when its subject is broken — every
# test above can fail (steelFramedCodeWriter Rule 4).
