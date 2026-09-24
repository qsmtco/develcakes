# tests/test_sanitize.py — SPEC-06 SP1 XSS battery for render/sanitize.py.
#
# The sanitize layer is FAIL-CLOSED: unsafe HTML must never survive, and any
# internal error must return "" (never raw passthrough). These probes pin the
# contract: strip-everything-unsafe, keep the markdown vocabulary, and fail
# closed on garbage input.

import nh3
import pytest

from render.sanitize import sanitize_html

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
    '<a href="/relative">rel</a>',
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
        assert "rel" in out.lower()  # link text kept, ammonia rel still added


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
