# Phase 3 of 3 — Update stale tests + add regression tests

**Spec:** `docs/specs/SPEC-PANGO-PLACEHOLDER-ESCAPE-FIX.md` (§2.3, §2.4)
**Master prompt:** `prompts/steelFramedCodeWriter.md` — invoke it. Read it first.
**Scope:** TWO files: `tests/test_escaping.py` + `tests/test_markdown.py`. No production code.

## Context

Phase 1 removed `"a"` from `_PANGO_KNOWN_TAGS` (escape_for_pango now escapes `<a>`).
Phase 2 added `_resolve_code_in_label` (resolves code-span placeholders in link labels).
The supervisor also fixed Step 3a (same placeholder fix for angle-links).

Three categories of test work:
1. **Stale tests in test_escaping.py** — 3 tests assert the OLD behavior (escape_for_pango preserves `<a>`). Must update to assert NEW behavior (escape_for_pango escapes `<a>`).
2. **New regression test in test_markdown.py** — code-span placeholder in link label.
3. **New regression test in test_markdown.py** — code-span in angle-link URL.

## Part A — Update `tests/test_escaping.py` (3 stale tests)

Run `pytest tests/test_escaping.py -q` first to see the 3 failures. Then update each.

### Test 1: `test_anchor_tag_preserved` (line ~74)

CURRENT (asserts OLD behavior — preserves `<a>`):
```python
    def test_anchor_tag_preserved(self):
        assert escape_for_pango('<a href="https://x.com">link</a>') == '<a href="https://x.com">link</a>'
```

UPDATE to assert NEW behavior (escapes `<a>`). Rename to reflect the change:
```python
    def test_anchor_tag_escaped(self):
        """Pango 1.52 does not support <a>; escape_for_pango escapes it."""
        assert escape_for_pango('<a href="https://x.com">link</a>') == '&lt;a href=&quot;https://x.com&quot;&gt;link&lt;/a&gt;'
```

**IMPORTANT:** Verify the EXACT expected output by running `escape_for_pango` yourself before writing the assertion. Do not guess — the escaping may produce `&#x27;` or `&quot;` depending on quote handling. Run:
```
python3 -c "from utils.escaping import escape_for_pango; print(repr(escape_for_pango('<a href=\"https://x.com\">link</a>')))"
```
and assert on the ACTUAL output.

### Test 2: `test_link_tag_with_url` (line ~123)

CURRENT (asserts `'href="http://example.com"' in result` — the href attribute is preserved):
```python
    def test_link_tag_with_url(self):
        result = escape_for_pango('<a href="http://example.com"><u>link</u></a>')
        assert 'href="http://example.com"' in result
```

UPDATE: `<a>` is now escaped, so `href=` no longer appears as a real attribute. Assert the escaped form:
```python
    def test_link_tag_escaped(self):
        """Pango 1.52 does not support <a>; the whole tag is escaped."""
        result = escape_for_pango('<a href="http://example.com"><u>link</u></a>')
        assert '&lt;a' in result  # escaped, not preserved
        assert '<a href' not in result  # not a real tag
```
Again — verify the ACTUAL output first and assert on what's real.

### Test 3: `test_valid_a_tag_pair_preserved` (line ~201)

CURRENT:
```python
    def test_valid_a_tag_pair_preserved(self):
        result = escape_for_pango('<a href="https://x.com">link</a>')
        assert '<a href="https://x.com">link</a>' == result
```

UPDATE (rename + assert escaped):
```python
    def test_a_tag_pair_escaped(self):
        """Even a valid-looking <a> pair is escaped — Pango doesn't support <a>."""
        result = escape_for_pango('<a href="https://x.com">link</a>')
        assert '<a ' not in result
        assert '&lt;a' in result
```

### Other tests that mention `<a>` — CHECK but probably OK

These tests assert `<a>` is NOT preserved (orphan/escaped) — they should still pass:
- `test_orphan_a_tag_escaped` (line ~189)
- `test_grep_output_with_a_tag` (line ~209)
- `test_buggy_autolink_output_robust` (line ~160)

Run pytest after each edit to confirm. If any of these NOW fail (because the escaping behavior changed in a way that affects them), update them too.

## Part B — Add regression tests to `tests/test_markdown.py`

### Test 1: Code-span placeholder in markdown link label

```python
def test_markdown_link_with_code_span_label():
    """Regression (Phase 2): [`code`](url) must produce valid Pango markup.

    Step 3 must resolve code-span placeholders in link labels before
    storing anchor_html, otherwise null bytes reach Pango's C-string parser.
    """
    result = format_markdown(escape_for_pango("[`context.md`](https://example.com)"))
    assert '\x00' not in result, f"Null byte in output: {result!r}"
    assert 'context.md' in result
    assert '<u>' in result
```

### Test 2: Code-span in angle-link URL (BUG #1 regression)

```python
def test_angle_link_with_code_span_in_url():
    """Regression (Step 3a fix): <https://`evil`.com> must not produce null bytes.

    Step 3a must resolve code-span placeholders in angle-link URLs,
    same as Step 3 does for markdown link labels.
    """
    result = format_markdown(escape_for_pango("see <https://`evil`.com> here"))
    assert '\x00' not in result, f"Null byte in output: {result!r}"
```

### Test 3: Multiple code-span labels (thoroughness)

```python
def test_multiple_code_span_labels_in_links():
    """Multiple [`code`](url) links in one message all resolve correctly."""
    result = format_markdown(escape_for_pango("see [`a.py`](url1) and [`b.py`](url2)"))
    assert '\x00' not in result
    assert 'a.py' in result and 'b.py' in result
```

## Rules

- **Two files only:** `tests/test_escaping.py` + `tests/test_markdown.py`.
- **No production code changes.**
- **Verify ACTUAL output before writing assertions.** Run `escape_for_pango` / `format_markdown` on the test input and assert on the real output.
- **Run pytest after each file** to confirm all pass.

## Verify (run these, paste full output)

1. `python3 -m pytest tests/test_escaping.py -q` — all pass (0 failures)
2. `python3 -m pytest tests/test_markdown.py -q -k "not pango_validation"` — all pass
3. Both files compile: `python3 -m py_compile tests/test_escaping.py tests/test_markdown.py && echo COMPILE_OK`

## COMPLETENESS checklist

```
COMPLETENESS:
- [x/not done] test_anchor_tag_preserved → test_anchor_tag_escaped (asserts escaped) — evidence: <pytest>
- [x/not done] test_link_tag_with_url → test_link_tag_escaped (asserts escaped) — evidence: <pytest>
- [x/not done] test_valid_a_tag_pair_preserved → test_a_tag_pair_escaped — evidence: <pytest>
- [x/not done] test_markdown_link_with_code_span_label added — evidence: <pytest>
- [x/not done] test_angle_link_with_code_span_in_url added — evidence: <pytest>
- [x/not done] test_multiple_code_span_labels_in_links added — evidence: <pytest>
- [x/not done] test_escaping.py all pass — evidence: <pytest summary>
- [x/not done] test_markdown.py all pass — evidence: <pytest summary>
```

Please write per the steelFramedCodeWriter prompt.
