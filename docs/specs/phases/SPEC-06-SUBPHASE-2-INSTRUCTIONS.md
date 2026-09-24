# SPEC-06 Sub-Phase 2 Instructions — render/html.py + render/syntax_html.py (pure)

**Spec:** docs/specs/SPEC-06-R2A-HTML-CHAT.md §2 (render/html.py + syntax_html.py)
**Parent plan:** docs/specs/phases/SPEC-06-SUBPHASES.md
**MICRO-phase. Scope: exactly 3 files** — `render/html.py` (NEW),
`render/syntax_html.py` (NEW), `tests/test_render_html.py` (NEW). Budget ~12.
ZERO UI. Pure functions only.

## Architecture ruling (supervisor, supersedes spec's "wrap the lexer")

Survey finding: `utils/markdown.py` is NOT a token lexer — it's a regex pipeline
that emits Pango markup directly (with null-byte placeholder protection), and
BLOCK-level structure lives in `utils/block_parser.py` (extract_blocks → dicts).
Porting that architecture to HTML:

**REUSE the structure, EMIT HTML:**
1. `markdown_to_html(text: str) -> str` — accepts RAW markdown (UNLIKE the Pango
   path, which requires pre-escaped input — document this difference prominently).
   Pipeline: block_parser.extract_blocks(text) for structure (code blocks, tables,
   lists, quotes, paragraphs) + an HTML inline emitter for the inline rules
   (bold/italic/strike/code/links), modeled on utils/markdown.py's order-of-
   operations incl. the code-span placeholder trick.
2. **ESCAPING IS THE EMITTER'S JOB**: html.escape() every text node BEFORE any
   tag emission (the spec's "never interpolates raw input" contract). The only
   raw pass-through is NOTHING — verify with a probe: markdown containing
   `<script>` inline renders as escaped text or is consumed as text, never as
   a live tag. (The sanitizer behind us is defense-in-depth, not first line.)
3. **Links**: `[text](url)` → `<a href="...">text</a>`; bare-URL autolink ported
   from _AUTO_LINK_RE. href values: http(s) only — reuse _validate_link_url's
   scheme logic but STrip (render as plain text) rather than the Pango red-warning
   prefix (the sanitizer would strip them anyway; don't emit what dies downstream).
   NOTE: sanitizer's case-insensitive check (SP1 FIX A) — mirror it here.
4. **NO img emission ever** (register item: img is dropped from the pipeline at
   the source; markdown images render as their alt text in brackets).
5. `render_document(text) -> str` convenience: markdown_to_html → sanitize_html
   (import from .sanitize) — the composition every call site will use; one
   function so SP4's guard test pins ONE entry point.

## render/syntax_html.py

Port `utils/syntax_highlight.py`'s highlight(code, lang) → class-based spans:
`<span class="tok-kw">...</span>` etc. (CSS lives in the surface, SP3). NO inline
styles (spec §2). Reuse the token-type mapping from _token_color but map to
class names. Wire into markdown_to_html's fenced-code emission: code blocks get
`<pre><code class="lang-x">` + tokenized spans (fallback: escaped plain).

## Tests — tests/test_render_html.py (~20)

1. Paragraph: `hello **world**` → `<p>hello <strong>world</strong></p>` (escaped first!)
2. Escaping contract: input `<script>alert(1)</script>` → output contains
   `&lt;script&gt;` and NOT `<script>`
3. Inline code: `` `x = 1 < 2` `` → `<code>x = 1 &lt; 2</code>` (placeholder trick holds)
4. Bold/italic/strike combos
5. Fenced code block → pre/code with class, content escaped
6. Nested-unclosed-markdown tolerance (no raise, sane output)
7. Table → table/thead/tbody/tr/th/td structure from block_parser
8. Blockquote → blockquote
9. Lists (ul/li, ordered ol/li)
10. `[text](https://x)` → a href + rel comes from sanitizer (emit WITHOUT rel —
    sanitizer injects; assert via render_document)
11. `[text](javascript:alert(1))` → plain text, no <a>
12. `[text](HTTPS://X)` → href survives (case-insensitive, mirrors SP1 FIX A)
13. Bare URL autolink → <a href>
14. `![alt](https://x/img.png)` → `[alt]` text, NO img tag (register ruling)
15. render_document composition == sanitize_html(markdown_to_html(x)) for corpus
16. Huge input: 1MB code block → no hang (just completes; SP3 handles the cap)
17-20. syntax_html: keyword/string/comment/comment token classes; unknown lang →
    escaped plain

Falsifier: remove the html.escape in the inline emitter → test 2 fails.

## Verify (paste ALL):
```
.venv/bin/python -m pytest tests/test_render_html.py -q
.venv/bin/python -m pytest tests/test_sanitize.py -q               # SP1 unaffected
.venv/bin/python -m ruff check render/ tests/test_render_html.py
.venv/bin/pyright render/html.py render/syntax_html.py 2>&1 | tail -1   # ≤2 = import artifacts
```

## COMPLETENESS
- [ ] 2 modules + tests; escaping-contract probe result reported
- [ ] img never emitted; js-URL stripped at emitter
- [ ] Falsifier run, SAID
- [ ] 4 outputs; deviations flagged
