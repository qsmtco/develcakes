# tests/test_sanitize.py — SPEC-06 SP1 XSS battery for render/sanitize.py.
#
# The sanitize layer is FAIL-CLOSED: unsafe HTML must never survive, and any
# internal error must return "" (never raw passthrough). These probes pin the
# contract: strip-everything-unsafe, keep the markdown vocabulary, and fail
# closed on garbage input.

import nh3
import pytest

from render.sanitize import sanitize_agent_html, sanitize_html

# Raw probe inputs — also the idempotence corpus (test 18).
_CORPUS = [
    "<script>alert(1)</script>",
    '<iframe src="https://evil.example"></iframe>',
    '<img src="https://x/i.png" onerror="alert(1)">',
    '<a href="javascript:alert(1)">click</a>',
    '<a href="file:///etc/passwd">file</a>',
    '<a href="data:text/html,<script>alert(1)</script>">data</a>',
    "<scr<script>ipt>alert(1)</script>",
    "<svg><p><style><a href='javascript:alert(1)'>x</a></style></p></svg>",
    '<p style="color:red">styled</p>',
    '<form action="/steal"><input type="text"><button onclick="a()">hi</button></form>',
    '<a href="https://ok.example" onclick="a()" onmouseover="b()">ok</a>',
    '<a href="http://ok.example">plain</a>',
    '<a href="https://ok.example">secure</a>',
    "<h1>H1</h1><ul><li>item</li></ul><pre><code>code()</code></pre>",
    "<table><thead><tr><th>h</th></tr></thead><tbody><tr><td>d</td></tr></tbody></table>",
    '<img src="https://x/i.png" alt="alt text">',
    '<a href="/relative">clickme</a>',
]


class TestStripped:
    """Dangerous constructs are removed."""

    def test_script_tag_stripped_entirely(self):
        out = sanitize_html(_CORPUS[0])
        assert "<script" not in out.lower()
        assert "alert" not in out.lower()

    def test_iframe_stripped(self):
        out = sanitize_html(_CORPUS[1])
        assert "<iframe" not in out.lower()

    def test_img_onerror_stripped_img_kept(self):
        out = sanitize_html(_CORPUS[2])
        assert "<img" in out.lower()
        assert "onerror" not in out.lower()
        assert 'src="https://x/i.png"' in out

    def test_javascript_href_stripped_text_kept(self):
        out = sanitize_html(_CORPUS[3])
        assert "href" not in out.lower()
        assert "click" in out

    def test_file_uri_stripped(self):
        out = sanitize_html(_CORPUS[4])
        assert "href" not in out.lower()
        assert "file:" not in out.lower()

    def test_data_uri_stripped(self):
        out = sanitize_html(_CORPUS[5])
        assert "href" not in out.lower()
        assert "data:" not in out.lower()

    def test_nested_tag_smuggling_neutralized(self):
        out = sanitize_html(_CORPUS[6])
        assert "<script" not in out.lower()

    def test_style_attribute_stripped(self):
        out = sanitize_html(_CORPUS[8])
        # Attribute must die; the word "styled" is content text and passes.
        assert "<p style=" not in out.lower()
        assert "<p>styled</p>" == out

    def test_form_input_button_stripped(self):
        out = sanitize_html(_CORPUS[9])
        for tag in ("<form", "<input", "<button"):
            assert tag not in out.lower()

    def test_event_handlers_on_anchor_stripped(self):
        out = sanitize_html(_CORPUS[10])
        assert "onclick" not in out.lower()
        assert "onmouseover" not in out.lower()
        assert 'href="https://ok.example"' in out

    def test_mxss_mutation_neutralized(self):
        """svg/style mutation fragment: output must not resurrect a javascript URL."""
        out = sanitize_html(_CORPUS[7])
        assert "javascript:" not in out.lower()
        assert "<svg" not in out.lower()
        assert "<style" not in out.lower()

    def test_relative_url_stripped(self):
        """Policy: relative URIs die (no base-URL context in a chat surface)."""
        out = sanitize_html(_CORPUS[16])
        assert "href" not in out.lower()
        # Attribute-exact: ammonia's rel injection must survive on the kept
        # link text (FIX C — substring "rel" is vacuous; pin the attribute).
        assert 'rel="noopener noreferrer nofollow"' in out

    @pytest.mark.parametrize("scheme", ["HTTPS", "HTTP", "Https", "hTTps"])
    def test_uppercase_scheme_link_passes(self, scheme):
        """FIX A (audit): scheme check normalizes case but returns the ORIGINAL
        value — uppercase-scheme links keep their href un-rewritten."""
        raw = f'<a href="{scheme}://ok.example">clickme</a>'
        out = sanitize_html(raw)
        assert f'href="{scheme}://ok.example"' in out, f"href lost/rewritten: {out!r}"

    def test_attacker_rel_stripped(self):
        """FIX B (audit): end-to-end — an author-supplied rel="opener" must
        never survive (reverse tabnabbing). Output rel is only the injected
        value, or absent. NOTE: ammonia alone already neutralizes author rel
        (mutant probe 2026-09-24: gate removed → test STILL passes); the
        filter-level pin below is what actually bites."""
        out = sanitize_html('<a href="https://ok.example" rel="opener">x</a>')
        assert 'rel="opener"' not in out.lower()
        assert 'rel="noopener noreferrer nofollow"' in out

    def test_filter_gate_rejects_noninjected_rel(self):
        """FIX B (audit): the filter's rel gate ITSELF — only the exact
        injected value passes; anything else is stripped. This is the
        mutation-sensitive pin (falsifier-proven)."""
        from render.sanitize import _attribute_filter

        assert (
            _attribute_filter("a", "rel", "noopener noreferrer nofollow")
            == "noopener noreferrer nofollow"
        )
        assert _attribute_filter("a", "rel", "opener") is None
        assert _attribute_filter("a", "rel", "noopener") is None
        assert _attribute_filter("a", "rel", "") is None


class TestPasses:
    """The markdown vocabulary survives."""

    def test_http_link_passes(self):
        out = sanitize_html(_CORPUS[11])
        assert 'href="http://ok.example"' in out

    def test_https_link_passes_with_rel(self):
        out = sanitize_html(_CORPUS[12])
        assert 'href="https://ok.example"' in out
        assert 'rel="noopener noreferrer nofollow"' in out

    @pytest.mark.parametrize(
        ("tag", "fragment"),
        [
            ("h1", "<h1>H</h1>"),
            ("h2", "<h2>H</h2>"),
            ("h3", "<h3>H</h3>"),
            ("h4", "<h4>H</h4>"),
            ("h5", "<h5>H</h5>"),
            ("h6", "<h6>H</h6>"),
            ("p", "<p>text</p>"),
            ("br", "<p>a<br>b</p>"),
            ("hr", "<hr>"),
            ("blockquote", "<blockquote>q</blockquote>"),
            ("ul", "<ul><li>i</li></ul>"),
            ("ol", "<ol><li>i</li></ol>"),
            ("li", "<ul><li>i</li></ul>"),
            ("pre", "<pre>c</pre>"),
            ("code", "<pre><code>c()</code></pre>"),
            ("em", "<em>e</em>"),
            ("strong", "<strong>s</strong>"),
            ("del", "<del>d</del>"),
            ("a", '<a href="https://x">t</a>'),
            ("img", '<img src="https://x/i.png" alt="i">'),
            ("table", "<table><tbody><tr><td>d</td></tr></tbody></table>"),
            ("thead", "<table><thead><tr><th>h</th></tr></thead></table>"),
            ("tbody", "<table><tbody><tr><td>d</td></tr></tbody></table>"),
            ("tr", "<table><tr><td>d</td></tr></table>"),
            ("th", "<table><thead><tr><th>h</th></tr></thead></table>"),
            ("td", "<table><tr><td>d</td></tr></table>"),
        ],
    )
    def test_allowlist_tag_survives(self, tag, fragment):
        out = sanitize_html(fragment)
        assert f"<{tag}" in out.lower(), f"{tag} missing from: {out!r}"


class TestFailClosed:
    """Any internal error → "" — never raw passthrough."""

    def test_none_returns_empty(self):
        assert sanitize_html(None) == ""  # type: ignore[arg-type]

    def test_non_string_garbage_returns_empty(self):
        assert sanitize_html(123) == ""  # type: ignore[arg-type]
        assert sanitize_html(["<b>x</b>"]) == ""  # type: ignore[arg-type]

    def test_nh3_internal_error_returns_empty(self, monkeypatch):
        """The except path itself: nh3.clean raising must yield "", not raise."""
        def boom(html):
            raise RuntimeError("simulated sanitizer internal failure")
        monkeypatch.setattr(nh3, "clean", boom)
        assert sanitize_html("<p>hi</p>") == ""


class TestIdempotence:
    def test_resanitize_is_identity_over_corpus(self):
        """Mutation-XSS resistance: sanitize(sanitize(x)) == sanitize(x)."""
        for raw in _CORPUS:
            once = sanitize_html(raw)
            twice = sanitize_html(once)
            assert twice == once, f"not idempotent for {raw!r}: {once!r} vs {twice!r}"


class TestFilterSelfFailClosed:
    """FIX B (SP3 audit r2): pyo3 does not propagate filter exceptions — it
    logs them and RETAINS the attribute (probe-verified). The filter must
    therefore catch its own internals and strip (return None)."""

    def test_raising_filter_yields_stripped_attribute(self, monkeypatch):
        """End-to-end: a filter whose internals raise must yield a STRIPPED
        attribute (with the bare filter, nh3 would retain it)."""
        from render import sanitize as san

        def boom(element, attribute, value):
            raise RuntimeError("filter internal failure")

        monkeypatch.setattr(san, "_attribute_filter_inner", boom)
        out = san.sanitize_html('<a href="https://ok.example">click</a>')
        assert "href" not in out.lower()

    def test_direct_filter_call_strips_on_internal_raise(self):
        """Unit pin: the wrapper converts ANY raise to None (strip)."""
        from unittest.mock import patch

        from render import sanitize as san

        with patch.object(san, "_attribute_filter_inner", side_effect=ValueError("x")):
            assert san._attribute_filter("a", "href", "https://ok.example") is None

    def test_filter_still_admits_valid_values(self):
        """Wrapping regression: the happy path is untouched by the wrapper."""
        from render import sanitize as san

        out = san.sanitize_html('<a href="https://ok.example">click</a>')
        assert 'href="https://ok.example"' in out


# ── SPEC-13 SP1: agent-author policy ─────────────────────────────────────
#
# The agent is the AUTHOR (Phosphor model): a whole-message ```html fence is
# a rich payload — containers, inline text, inert interactive elements,
# static SVG, and INLINE STYLE (through a CSS property-name allowlist).
# Security invariants are UNCHANGED from sanitize_html: no script/iframe/
# object/embed/form, no event handlers, href/src http(s)-only, link_rel
# forced, fail-closed. The CSS allowlist is deny-by-omission: every
# url()-bearing property is simply absent, so background/background-image/
# list-style-image/filter/… die by not being named.

class TestAgentAuthorSanitize:
    """SPEC-13 §2a/§2g: the agent-author sanitize policy."""

    # ── Dangerous constructs still die (same invariants as sanitize_html) ─

    def test_script_dies(self):
        out = sanitize_agent_html("<script>alert(1)</script>")
        assert "<script" not in out.lower()
        assert "alert" not in out.lower()

    def test_iframe_dies(self):
        out = sanitize_agent_html('<iframe src="https://evil.example"></iframe>')
        assert "<iframe" not in out.lower()

    def test_object_embed_die(self):
        assert "<object" not in sanitize_agent_html(
            '<object data="https://x"></object>'
        ).lower()
        assert "<embed" not in sanitize_agent_html('<embed src="https://x">').lower()

    def test_event_handler_stripped_img_kept(self):
        out = sanitize_agent_html('<img src="https://x/i.png" onerror="alert(1)">')
        assert "<img" in out.lower()
        assert "onerror" not in out.lower()
        assert 'src="https://x/i.png"' in out

    def test_onclick_stripped_button_kept(self):
        out = sanitize_agent_html('<button onclick="a()">click</button>')
        assert "<button" in out.lower()
        assert "onclick" not in out.lower()

    @pytest.mark.parametrize(
        "href",
        [
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "file:///etc/passwd",
            "/relative/path",
        ],
    )
    def test_bad_schemes_href_stripped_text_kept(self, href):
        out = sanitize_agent_html(f'<a href="{href}">click</a>')
        assert "href" not in out.lower(), f"href survived for {href!r}: {out!r}"
        # The link TEXT survives (attribute stripped, element kept).
        assert "click" in out

    def test_form_dies(self):
        out = sanitize_agent_html(
            '<form action="/steal"><input type="text"></form>'
        )
        assert "<form" not in out.lower()
        assert "<input" not in out.lower()

    # ── Author vocabulary survives ────────────────────────────────────────

    @pytest.mark.parametrize(
        ("tag", "fragment"),
        [
            ("div", "<div>x</div>"),
            ("span", "<span>x</span>"),
            ("section", "<section>x</section>"),
            ("article", "<article>x</article>"),
            ("header", "<header>x</header>"),
            ("footer", "<footer>x</footer>"),
            ("main", "<main>x</main>"),
            ("aside", "<aside>x</aside>"),
            ("nav", "<nav>x</nav>"),
            ("figure", "<figure><figcaption>c</figcaption></figure>"),
            ("details", "<details><summary>t</summary>b</details>"),
            ("hgroup", "<hgroup><h1>a</h1></hgroup>"),
            ("b", "<b>x</b>"),
            ("i", "<i>x</i>"),
            ("u", "<u>x</u>"),
            ("s", "<s>x</s>"),
            ("small", "<small>x</small>"),
            ("sub", "<sub>x</sub>"),
            ("sup", "<sup>x</sup>"),
            ("mark", "<mark>x</mark>"),
            ("abbr", "<abbr>x</abbr>"),
            ("cite", "<cite>x</cite>"),
            ("q", "<q>x</q>"),
            ("time", "<time>x</time>"),
            ("label", "<label>x</label>"),
            ("dl", "<dl><dt>t</dt><dd>d</dd></dl>"),
            ("button", "<button>x</button>"),
            ("svg", "<svg><circle r='1'></circle><path d='M0 0'></path></svg>"),
            ("video", '<video src="https://x/v.mp4"></video>'),
            ("audio", '<audio src="https://x/a.mp3"></audio>'),
            ("source", '<video><source src="https://x/v.mp4"></video>'),
        ],
    )
    def test_author_tag_survives(self, tag, fragment):
        out = sanitize_agent_html(fragment)
        assert f"<{tag}" in out.lower(), f"{tag} missing from: {out!r}"

    def test_class_id_data_survive(self):
        out = sanitize_agent_html(
            '<section class="card" id="c1" data-k="v">x</section>'
        )
        assert 'class="card"' in out
        assert 'id="c1"' in out
        assert 'data-k="v"' in out

    def test_class_id_values_not_restricted_to_alnum(self):
        """The author namespace is ANY value — hyphens/spaces/underscores are
        legal CSS class/id syntax and MUST survive. (This pin exists because a
        foreign `value.isalnum()` gate on class/id slipped past the round-1
        suite, which only used alnum values — see FIX ROUND 1 report.)"""
        out = sanitize_agent_html(
            '<div class="card wide" id="panel-1">x</div>'
        )
        assert 'class="card wide"' in out, f"non-alnum class stripped: {out!r}"
        assert 'id="panel-1"' in out, f"hyphenated id stripped: {out!r}"

    def test_width_height_attrs_survive(self):
        out = sanitize_agent_html(
            '<img src="https://x/i.png" width="10" height="20" alt="a">'
        )
        assert 'width="10"' in out
        assert 'height="20"' in out

    def test_colspan_rowspan_attrs_survive(self):
        """BUG#3: the attributes-map DELTA (width/height/colspan/rowspan/name
        re-admitted deliberately). colspan/rowspan must render ON the td."""
        out = sanitize_agent_html(
            '<table><tr><td colspan="2" rowspan="3">x</td></tr></table>'
        )
        assert 'colspan="2"' in out, f"colspan stripped: {out!r}"
        assert 'rowspan="3"' in out, f"rowspan stripped: {out!r}"

    def test_name_attr_survives(self):
        """BUG#3: `name` is an ANCHOR TARGET, not a URL — inert, admitted."""
        out = sanitize_agent_html('<a name="anchor1">x</a>')
        assert 'name="anchor1"' in out, f"name stripped: {out!r}"

    def test_alt_title_attrs_survive(self):
        out = sanitize_agent_html(
            '<img src="https://x/i.png" title="t" alt="a">'
        )
        assert 'title="t"' in out, f"title stripped: {out!r}"
        assert 'alt="a"' in out, f"alt stripped: {out!r}"

    def test_style_attr_color_survives(self):
        out = sanitize_agent_html('<div style="color:red">hi</div>')
        assert 'style="color:red"' in out

    def test_style_attr_multiple_allowed_props_survive(self):
        out = sanitize_agent_html(
            '<div style="display:flex;opacity:0.5;border-radius:4px">hi</div>'
        )
        for prop in ("display:flex", "opacity:0.5", "border-radius:4px"):
            assert prop in out, f"{prop} stripped from: {out!r}"

    # ── CSS allowlist: SPEC-19 SP3 admitted background/url()-bearing props ─
    # (Prior to SP3 these were denied by property-name omission. SP3 admits
    # background/background-image/mask/border-image; the url() VALUE passes
    # nh3 (property-name filter only) but the LOAD is blocked by E1 — see
    # tests/test_live_guard.py. list-style-image stays OUT.)

    def test_css_background_url_value_passes_but_load_is_e1_blocked(self):
        """SPEC-19 SP3 §2: background now admitted; url() VALUE passes nh3.
        The security boundary is E1 (content filter), proven in
        test_live_guard.py — not this layer."""
        out = sanitize_agent_html('<div style="background:url(http://x)">hi</div>')
        assert "<div" in out.lower()
        assert "background" in out.lower()
        # nh3 keeps the url() value (property-name filter only) — pinned so a
        # future nh3 that strips values flips this test deliberately.
        assert "url(" in out.lower()

    def test_css_background_image_url_value_passes(self):
        out = sanitize_agent_html(
            '<div style="background-image:url(http://x)">hi</div>'
        )
        assert "background-image" in out.lower()

    def test_css_list_style_image_url_still_stripped(self):
        """list-style-image stays OUT of the allowlist (no SP3 use case) —
        deny-by-omission still kills it."""
        out = sanitize_agent_html(
            '<div style="list-style-image:url(http://x)">hi</div>'
        )
        assert "url(" not in out.lower()

    def test_css_allowed_and_denied_mixed(self):
        """A single style attribute keeping allowed props while dropping a
        still-denied one (list-style-image) — the property-level gate."""
        out = sanitize_agent_html(
            '<div style="color:red;list-style-image:url(http://x);display:flex">hi</div>'
        )
        assert "color:red" in out
        assert "display:flex" in out
        assert "list-style-image" not in out.lower()

    # ── link_rel forced; style ELEMENT not admitted (probe verdict) ────────

    def test_link_rel_forced_on_http_anchor(self):
        out = sanitize_agent_html('<a href="https://ok.example">ok</a>')
        assert 'href="https://ok.example"' in out
        assert 'rel="noopener noreferrer nofollow"' in out

    def test_author_rel_cannot_override(self):
        out = sanitize_agent_html('<a href="https://ok.example" rel="opener">x</a>')
        assert 'rel="opener"' not in out.lower()
        assert 'rel="noopener noreferrer nofollow"' in out

    def test_style_element_not_admitted(self):
        """SP1 REQUIRED PROBE VERDICT (nh3 0.3.7 / ammonia 4.1.4):

        `style` is in ammonia's default `clean_content_tags`; passing it in
        `tags=` PANICS unconditionally — "`style` appears in
        `clean_content_tags` and in `tags` at the same time"
        (pyo3_runtime.PanicException, verified 2026-10-06). With `style`
        LEFT OUT of tags, the element AND its CSS content are stripped.

        Therefore <style> is NOT admitted; per-message <style> blocks are a
        REGISTER item, and inline `style=` + surface classes carry SP1. This
        test pins that decision: a payload carrying a <style> element yields
        no <style> tag and no CSS text.
        """
        out = sanitize_agent_html(
            "<style>body{background:url(http://x)}</style><p>hi</p>"
        )
        assert "<style" not in out.lower()
        assert "background" not in out.lower()
        assert "url(" not in out.lower()
        assert "<p>hi</p>" in out

    def test_style_tag_absent_from_author_tags(self):
        """Source-level pin of the probe verdict: 'style' must never enter
        _AGENT_AUTHOR_TAGS (it would panic nh3.clean)."""
        from render.sanitize import _AGENT_AUTHOR_TAGS
        assert "style" not in _AGENT_AUTHOR_TAGS

    # ── agent filter gates (mirror of the markdown path's pins) ───────────

    def test_agent_attribute_filter_self_fail_closed(self, monkeypatch):
        """BUG#7: the agent filter is self-fail-closed like _attribute_filter
        (pyo3 retains attributes when a filter raises). A raising internals
        must convert to None (strip), never leak the value."""
        from render import sanitize as san

        def boom(element, attribute, value):
            raise RuntimeError("agent filter internal failure")

        monkeypatch.setattr(san, "_agent_attribute_filter_inner", boom)
        assert san._agent_attribute_filter("div", "style", "color:red") is None

    def test_agent_attribute_filter_href_stripped(self):
        """BUG#8: the agent path's OWN href branch (before/through delegation)
        strips javascript: — call the inner gate DIRECTLY with a js: value."""
        from render.sanitize import _agent_attribute_filter_inner

        assert _agent_attribute_filter_inner("a", "href", "javascript:alert(1)") is None
        assert (
            _agent_attribute_filter_inner("a", "href", "https://ok.example")
            == "https://ok.example"
        )

    def test_agent_attribute_filter_src_stripped(self):
        """BUG#8: same for src on the agent path, directly."""
        from render.sanitize import _agent_attribute_filter_inner

        assert _agent_attribute_filter_inner("img", "src", "javascript:alert(1)") is None
        assert _agent_attribute_filter_inner("img", "src", "data:text/html,x") is None
        assert (
            _agent_attribute_filter_inner("img", "src", "https://x/i.png")
            == "https://x/i.png"
        )

    # ── fail-closed contract (identical to sanitize_html) ─────────────────

    def test_none_returns_empty(self):
        assert sanitize_agent_html(None) == ""  # type: ignore[arg-type]

    def test_non_string_garbage_returns_empty(self):
        assert sanitize_agent_html(123) == ""  # type: ignore[arg-type]
        assert sanitize_agent_html(["<div>x</div>"]) == ""  # type: ignore[arg-type]

    def test_nh3_internal_error_returns_empty(self, monkeypatch):
        """A Rust-side panic (or any raise) inside nh3.clean must yield "",
        never raw passthrough — the SAME fail-closed contract."""
        def boom(*a, **k):
            raise RuntimeError("simulated sanitizer internal failure")

        monkeypatch.setattr(nh3, "clean", boom)
        assert sanitize_agent_html('<div style="color:red">x</div>') == ""

    # ── idempotence (mutation-XSS resistance) ─────────────────────────────

    def test_resanitize_is_identity(self):
        corpus = [
            '<div style="color:red">hi</div>',
            '<section class="card" data-k="v">x</section>',
            '<details><summary>t</summary>b</details>',
            '<a href="https://ok.example">ok</a>',
            '<img src="https://x/i.png" onerror="a()">',
            '<div style="background:url(http://x)">x</div>',
        ]
        for raw in corpus:
            once = sanitize_agent_html(raw)
            assert sanitize_agent_html(once) == once, f"not idempotent: {raw!r}"

    # ── markdown path UNCHANGED (no policy weakening) ─────────────────────

    def test_sanitize_html_unchanged_by_author_policy(self):
        """The markdown-path sanitizer keeps its strict policy: a style
        attribute dies and a div element is stripped there."""
        assert sanitize_html('<p style="color:red">hi</p>') == "<p>hi</p>"
        assert "<div" not in sanitize_html("<div>x</div>")


# ── SPEC-19 SP3: T2 CSS property allowlist extension (inline-capable) ─────
#
# SPEC-19 §5 SP3: add inline-capable chart/layout properties to the T2
# agent-author CSS allowlist. animation-*/@keyframes stay OUT (T3-only — they
# need <style>, which nh3 can never pass; SPEC-19 §5 SP3 amendment).
#
# §2 PROBE (nh3 0.3.7, run before this change): `filter_style_properties`
# filters property NAMES only — url() VALUES pass through VERBATIM:
#   IN : <div style="background: url(http://127.0.0.1:1/x.png)">
#   OUT: <div style="background:url(http://127.0.0.1:1/x.png)">
# So adding background/mask/border-image re-opens url() reachability. E1
# (SPEC-19's compiled content filter) BLOCKS the CSS-driven load — proven:
# control (no filter) background:url() → +1 server hit; filtered → +0. The
# T2 card renders in the SAME transcript webview as live sections, so E1
# covers it. url() is therefore INERT on T2; the load-blocking test lives at
# the enforcement layer (tests/test_live_guard.py).


class TestT2CssAllowlistExtension:
    def test_background_conic_gradient_kept(self):
        out = sanitize_agent_html(
            '<div style="background: conic-gradient(red 0 30%, blue 30% 100%)">x</div>'
        )
        assert "conic-gradient" in out, repr(out)
        assert "background" in out

    def test_background_linear_gradient_kept(self):
        out = sanitize_agent_html(
            '<div style="background-image: linear-gradient(to right, red, blue)">x</div>'
        )
        assert "linear-gradient" in out, repr(out)
        assert "background-image" in out

    def test_background_longhand_family_kept(self):
        out = sanitize_agent_html(
            '<div style="background-size: cover; background-position: center; '
            'background-repeat: no-repeat; background-clip: content-box">x</div>'
        )
        for prop in ("background-size", "background-position",
                     "background-repeat", "background-clip"):
            assert prop in out, f"{prop} stripped: {out!r}"

    def test_filter_transform_transition_kept(self):
        out = sanitize_agent_html(
            '<div style="filter: blur(2px); transform: translateX(10px) '
            'scale(1.1); transition: opacity 0.3s ease-in; '
            'transition-duration: 0.3s">x</div>'
        )
        assert "filter:blur(2px)" in out.replace(" ", ""), repr(out)
        assert "transform" in out
        assert "transition" in out

    def test_grid_properties_kept(self):
        out = sanitize_agent_html(
            '<div style="display: grid; grid-template-columns: 1fr 2fr; '
            'grid-auto-flow: row; place-items: center">x</div>'
        )
        assert "grid-template-columns" in out, repr(out)
        assert "grid-auto-flow" in out
        assert "place-items" in out

    def test_position_clip_mask_columns_kept(self):
        out = sanitize_agent_html(
            '<div style="position: absolute; top: 4px; left: 8px; z-index: 3; '
            'clip-path: circle(50%); mask-image: none; '
            'columns: 2; aspect-ratio: 16/9">x</div>'
        )
        for prop in ("position", "top", "left", "z-index", "clip-path",
                     "mask-image", "columns", "aspect-ratio"):
            assert prop in out, f"{prop} stripped: {out!r}"

    def test_animation_not_admitted(self):
        """animation-* is T3-only (needs <style>); it must be STRIPPED on T2."""
        out = sanitize_agent_html(
            '<div style="animation: spin 2s linear infinite; '
            'animation-name: spin; animation-duration: 2s">x</div>'
        )
        assert "animation" not in out.lower(), repr(out)

    def test_url_value_passes_at_nh3_layer(self):
        """§2 probe pin: nh3 passes url() VALUES (property-name filter only).
        The load itself is blocked by E1 at the enforcement layer, NOT here —
        this test pins the nh3 behavior the probe found, so a future nh3 that
        strips values would flip this test (and the E1 layer would still
        hold)."""
        out = sanitize_agent_html(
            '<div style="background: url(http://127.0.0.1:9/x.png)">x</div>'
        )
        assert "url(" in out.lower(), repr(out)

    def test_markdown_path_unchanged_by_extension(self):
        """T1: the markdown-path sanitizer still strips style entirely —
        the CSS allowlist extension is author-policy only."""
        assert sanitize_html('<div style="background: linear-gradient(red,blue)">x</div>') == "x"
        assert sanitize_html("<div>x</div>") == "x"

    def test_extension_does_not_admit_style_element(self):
        """The <style> element stays stripped (nh3 panics if admitted)."""
        out = sanitize_agent_html(
            "<style>div{background:linear-gradient(red,blue)}</style><p>hi</p>"
        )
        assert "<style" not in out.lower()
        assert "<p>hi</p>" in out
