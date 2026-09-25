# tests/test_render_html.py — SPEC-06 SP2 battery for render/html.py +
# render/syntax_html.py. Pure functions, zero UI.

import re
import time

import pytest

from render.html import markdown_to_html, render_document
from render.syntax_html import highlight_html


class TestInline:
    def test_paragraph_bold(self):
        out = markdown_to_html("hello **world**")
        assert "<p>hello <strong>world</strong></p>" in out

    def test_escaping_contract_script(self):
        """THE contract: raw <script> never survives as a live tag."""
        out = markdown_to_html("<script>alert(1)</script>")
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_inline_code_placeholder_trick(self):
        out = markdown_to_html("`x = 1 < 2`")
        assert "<code>x = 1 &lt; 2</code>" in out
        assert "<script" not in out

    def test_code_span_shields_formatting(self):
        """The placeholder trick: ** inside backticks is not markup."""
        out = markdown_to_html("`**not bold**`")
        assert "<strong>" not in out
        assert "**not bold**" in out

    def test_bold_italic_strike_combos(self):
        assert "<strong><em>x</em></strong>" in markdown_to_html("***x***")
        assert "<strong>b</strong>" in markdown_to_html("**b**")
        assert "<em>i</em>" in markdown_to_html("*i*")
        assert "<del>s</del>" in markdown_to_html("~~s~~")

    def test_markdown_link_emits_anchor(self):
        out = markdown_to_html("[text](https://x.example)")
        assert '<a href="https://x.example">text</a>' in out

    def test_js_url_renders_plain(self):
        out = markdown_to_html("[text](javascript:alert(1))")
        assert "<a" not in out
        assert "text" in out

    def test_uppercase_scheme_href_survives(self):
        """Mirrors SP1 FIX A: case-normalized check, original value emitted."""
        out = markdown_to_html("[text](HTTPS://X.example)")
        assert 'href="HTTPS://X.example"' in out

    def test_relative_url_renders_plain(self):
        out = markdown_to_html("[text](/relative/path)")
        assert "<a" not in out
        assert "text" in out

    def test_markdown_image_renders_alt_only(self):
        """Register ruling: NO img is ever emitted."""
        out = markdown_to_html("![alt text](https://x.example/i.png)")
        assert "<img" not in out
        assert "[alt text]" in out

    def test_code_span_in_link_label_escapes_once(self):
        """FIX A (audit): a code span in a link label is escaped EXACTLY ONCE —
        `` [`a & b`](…) `` → <code>a &amp; b</code>, never &amp;amp;.
        (Falsifier A: pre-fix double-escape fails this.)"""
        out = markdown_to_html("[`a & b`](https://x.example)")
        assert "<code>a &amp; b</code>" in out
        assert "&amp;amp;" not in out

    def test_bare_url_with_query_ampersand(self):
        """FIX C (audit): & stays in the scheme-URL class — full query strings
        link. (Falsifier C: pre-fix & exclusion truncates at ?a=1.)"""
        out = markdown_to_html("see https://a.example/?a=1&b=2. end")
        assert "<a " in out
        href = out.split('href="', 1)[1].split('"', 1)[0]
        # Full URL captured — & arrives entity-encoded (&amp;) because the
        # emitter escapes first and emits the captured text raw into href.
        # HTML-correct: browsers decode the entity; the whole query survives.
        # Trailing '.' stripped by the punct stripper; NOTHING leaks after.
        assert href == "https://a.example/?a=1&amp;b=2"

    def test_bare_url_autolink(self):
        out = markdown_to_html("see https://example.com/page for more")
        assert '<a href="https://example.com/page">https://example.com/page</a>' in out


class TestBlocks:
    def test_heading(self):
        out = markdown_to_html("# Title")
        assert "<h1>Title</h1>" in out

    def test_heading_underscored_inline(self):
        out = markdown_to_html("## has **bold** inside")
        assert "<h2>has <strong>bold</strong> inside</h2>" in out

    def test_fenced_code_block(self):
        out = markdown_to_html("```python\nx = 1 < 2\n```")
        assert '<pre><code class="lang-python">' in out
        # Content escaped (highlight spans may split the text — assert the
        # entity survives and no raw < from the source text does).
        assert "&lt;" in out
        raw_body = out.split('class="lang-python">', 1)[1]
        assert "< 2" not in raw_body

    def test_fenced_code_unsupported_lang_plain_escaped(self):
        out = markdown_to_html("```\n<b>raw & stuff\n```")
        assert "<b>raw" not in out
        assert "&lt;b&gt;raw &amp; stuff" in out

    def test_blockquote(self):
        out = markdown_to_html("> quoted text")
        assert "<blockquote>quoted text</blockquote>" in out

    def test_table_structure(self):
        out = markdown_to_html(
            "| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
        )
        for tag in ("<table>", "<thead>", "<tbody>", "<tr>", "<th>A</th>", "<td>1</td>"):
            assert tag in out

    def test_unordered_list_grouping(self):
        out = markdown_to_html("- one\n- two\n- three")
        assert "<ul><li>one</li><li>two</li><li>three</li></ul>" in out

    def test_ordered_list_grouping(self):
        out = markdown_to_html("1. one\n2. two")
        assert "<ol><li>one</li><li>two</li></ol>" in out

    def test_task_list(self):
        out = markdown_to_html("- [ ] todo\n- [x] done")
        assert 'class="task-checked"' in out
        assert "todo" in out and "done" in out

    def test_terminal_block(self):
        out = markdown_to_html("$ ls -la")
        assert 'class="terminal"' in out
        assert "ls -la" in out

    def test_nested_unclosed_markdown_tolerance(self):
        """Garbage in → no raise, escaped-or-structured sane output."""
        out = markdown_to_html("**unclosed *emph `code <script>x")
        assert "<script>" not in out
        assert "&lt;script&gt;" in out


class TestRenderDocument:
    @pytest.mark.parametrize(
        ("doc", "expected"),
        [
            # Inline
            ("hello **world**", "<p>hello <strong>world</strong></p>"),
            # Code block: SP3 class= ruling — lang-/tok- classes SURVIVE
            # (pin consciously updated when the policy changed; see the
            # class-survival pins in test_chat_surface.py).
            ("```python\nx = 1\n```", '<pre><code class="lang-python">'),
            # Link: emitter emits NO rel; sanitizer injects it
            (
                "[text](https://x.example)",
                '<a href="https://x.example" rel="noopener noreferrer nofollow">text</a>',
            ),
            # Table structure survives the sanitizer
            ("| A | B |\n|---|---|\n| 1 | 2 |", "<table><thead>"),
        ],
    )
    def test_render_document_expected_outputs(self, doc, expected):
        """FIX B (audit): expected-output assertions replace the tautology —
        the composition is pinned against real rendered strings, so a broken
        stage (emitter OR sanitizer) shifts them and fails."""
        assert expected in render_document(doc)

    def test_render_document_safety_invariants(self):
        """FIX B (audit): safety invariants over an adversarial corpus. TAG-ANCHORED
        asserts (probe-verified): ammonia decodes text-node entities to raw
        chars, so escaped-tag text can contain raw quotes — and even the literal
        bytes `onerror=` — as visible CONTENT (safe by design). Substring asserts
        on the whole document false-positive on that content; these target only
        live tags/attributes. (Falsifier B target: identity-compose fires this.)"""
        corpus = [
            "<script>alert(1)</script>",
            '<iframe src="https://evil.example"></iframe>',
            '<img src="https://x/i.png" onerror="alert(1)">',
            "[l](javascript:alert(1))",
            '[x](https://x.example/" onmouseover="alert(1))',
        ]
        for doc in corpus:
            out = render_document(doc)
            # Live dangerous tags never survive.
            assert "<script" not in out.lower(), f"live script for {doc!r}: {out!r}"
            assert "<iframe" not in out.lower(), f"live iframe for {doc!r}"
            assert "<img" not in out.lower(), f"live img for {doc!r}: {out!r}"
            # No live javascript: href anywhere.
            assert 'href="javascript' not in out.lower(), f"live js href: {out!r}"
            # No event-handler attribute INSIDE a live anchor tag. Attribute
            # VALUES are blanked first — URLs may legitimately contain the
            # bytes `onmouseover=` (entity-encoded quotes can't break out of
            # the href, ammonia strips real handler attrs; this checks the
            # tag STRUCTURE, not its string content).
            for anchor in re.findall(r"<a\s[^>]*>", out, flags=re.IGNORECASE):
                stripped = re.sub(r'="[^"]*"', '=""', anchor)
                assert "onmouseover" not in stripped.lower(), (
                    f"live onmouseover attribute for {doc!r}: {anchor!r}"
                )
            # Every emitted href is an allowed scheme.
            for href in re.findall(r'href="([^"]*)"', out):
                assert href.lower().startswith(("http://", "https://")), (
                    f"non-http(s) href {href!r} for {doc!r}"
                )

    def test_render_document_link_gets_rel_from_sanitizer(self):
        """Emitter emits NO rel; sanitizer injects it (brief test 10)."""
        out = render_document("[text](https://x.example)")
        assert 'rel="noopener noreferrer nofollow"' in out

    def test_render_document_escapes_script(self):
        out = render_document("<script>alert(1)</script>")
        assert "<script>" not in out
        assert "&lt;script&gt;" in out


class TestSyntaxHtml:
    def test_keyword_token_class(self):
        out = highlight_html("def foo(): pass", "python")
        assert '<span class="tok-kw">def</span>' in out

    def test_string_token_class(self):
        out = highlight_html('x = "hello"', "python")
        assert '<span class="tok-str">' in out

    def test_comment_token_class(self):
        out = highlight_html("# hi", "python")
        assert '<span class="tok-com">' in out

    def test_unknown_lang_plain_escaped(self):
        out = highlight_html("<b>hi</b>", "notalanguage")
        assert "<span" not in out
        assert "&lt;b&gt;hi&lt;/b&gt;" in out

    def test_no_inline_styles(self):
        """Spec §2: no style= anywhere; classes only."""
        out = highlight_html("def x(): return 'a # b'", "python")
        assert "style=" not in out


class TestHugeInput:
    def test_1mb_code_block_completes(self):
        code = "x = 1\n" * 100_000  # ~600 KB inside the fence
        start = time.monotonic()
        out = markdown_to_html(f"```\n{code}```")
        elapsed = time.monotonic() - start
        assert "<pre><code>" in out
        assert elapsed < 5.0, f"1MB block took {elapsed:.2f}s"


class TestFalsifierTarget:
    def test_no_raw_lt_from_inline_text(self):
        """Direct pin on the escaping contract (falsifier target)."""
        out = markdown_to_html("a < b && c > d")
        assert "&lt;" in out and "&amp;&amp;" in out
        assert "<b" not in out.replace("<b>", "")  # no live tag from raw <
