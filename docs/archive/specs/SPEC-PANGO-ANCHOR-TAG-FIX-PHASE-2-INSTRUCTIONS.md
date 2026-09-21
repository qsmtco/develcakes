# Phase 2 of 2 — Update test_markdown.py + fix stale docstrings in markdown.py

**Spec:** `docs/specs/SPEC-PANGO-ANCHOR-TAG-FIX.md` (§2.2)
**Master prompt:** `prompts/steelFramedCodeWriter.md` — invoke it. Read it first.
**Scope:** TWO files: `tests/test_markdown.py` (assertion updates) + `utils/markdown.py` (stale comment/docstring cleanup per Debugger BUG #5/#8).

## Context

Phase 1 changed `format_markdown` to emit `<u>text</u>` instead of
`<a href="URL"><u>text</u></a>` (Pango rejects `<a>` tags). The tests in
`test_markdown.py` still assert the OLD `<a href>` output and now fail (27
assertions across ~15 tests). This phase updates them to assert the NEW `<u>`
output, and cleans up stale docstrings/comments in `markdown.py`.

## Part A — Update `tests/test_markdown.py`

### The transformation rule (apply consistently)

Every assertion that checked for `<a href="...">` output must now check for
`<u>...</u>` output instead. The mapping:

| OLD assertion pattern | NEW assertion |
|-----------------------|---------------|
| `assert '<a href="URL">' in result` | `assert '<u>TEXT</u>' in result` (where TEXT is the visible link text) |
| `assert '<a href="URL">' not in result` | `assert '<u>TEXT</u>' not in result` (or remove if N/A) |
| `assert result.count('<a ') == N` | `assert result.count('<u>') == N` (count of underlined spans) |
| `assert '<a ' not in result` | `assert '<a ' not in result` (STILL VALID — format_markdown must never emit `<a>`) |

**For link-text extraction:** the visible text is what was between `<u>` and
`</u>` in the OLD output. For markdown links `[click](url)` → visible text is
`click`. For auto-links (bare URLs) → visible text is the URL itself. For
angle-links `<url>` → visible text is the url.

### Specific tests to update (by line number — verify before editing)

Run `pytest tests/test_markdown.py -q` first to see which fail. Then update
each failing test. The known failing assertion lines (from grep) are:
91, 101, 107, 109, 243, 248, 253, 263, 268, 273, 278, 283, 288, 308, 317,
318, 367, 372, 416-418, 423, 428, 440-442, 445-447, 452, 459.

**Key test-specific notes:**

1. **`test_anchor_allowed` (line 227)** — the HIGH-6 allowlist tests. These
   test that allowlisted schemes (http, https, mailto) render WITHOUT the
   warning prefix, and non-allowlisted schemes (file, smb, javascript, data,
   ssh, myapp) render WITH the warning prefix. After the fix:
   - Allowlisted: `assert '<u>TEXT</u>' in result` and
     `assert _WARNING_PREFIX not in result` (no warning)
   - Non-allowlisted: `assert _WARNING_PREFIX in result` (warning present)
     and `assert '<u>' in result` (still underlined)
   - The `_WARNING_PREFIX` import: check how the test currently references it.
     It may be imported or inlined as the literal span string.

2. **`test_pre_existing_anchor_not_double_linked` (line 416)** — this tested
   that a pre-existing `<a href="URL">` in input doesn't get nested `<a>` tags.
   After the fix, format_markdown no longer emits `<a>` at all, so:
   - OLD: `assert result.count('<a ') == 1` (the pre-existing one preserved)
   - NEW: `assert '<a ' in result` (Step 3b still preserves the pre-existing
     `<a href>` via placeholder — this is a KNOWN limitation; Pango will reject
     it, but that's the pre-existing `<a>`-passthrough issue, not format_markdown's
     job to fix. Keep the test documenting this behavior.)
   - Actually — READ the current output of `format_markdown('<a href="URL">link</a>')`
     and assert on what it ACTUALLY produces. Do not guess.

3. **Bare-hostname tests (lines 440-452)** — `httpbin.org/help` and
   `example.com` get auto-linked. After the fix:
   - `assert '<u>httpbin.org/help</u>' in result` (the bare host is underlined)
   - `assert '<u>example.com</u>' in result`

4. **Line 109** — `assert '<a href="https://example.com.">' not in result` —
   this tested that trailing punctuation is stripped from the URL. After the
   fix, there's no `<a href>`, but the trailing-dot stripping still applies to
   the visible `<u>` text:
   - NEW: `assert '<u>https://example.com.</u>' not in result` (trailing dot
     stripped from visible text too)

### Add a regression test

Add ONE new test at the end of the file that verifies the Pango-validity
contract — format_markdown output must NOT contain `<a` (executable) and
MUST be parseable as valid Pango markup:

```python
def test_format_markdown_no_anchor_tags_emitted():
    """Regression: format_markdown must never emit <a> tags (Pango rejects them).

    Pango.parse_markup raises 'Unknown tag a' on any <a> element, causing
    Gtk.Label.set_markup to reject the entire message. This test guards
    against reintroducing <a href> in any link-rendering path.
    """
    from utils.escaping import escape_for_pango
    # Inputs that trigger all 3 link paths (markdown link, angle-link, auto-link)
    inputs = [
        "[click](https://example.com)",
        "see <https://example.com> here",
        "bare http://example.com url",
        "filename.md looking like a host",
        "mixed [a](http://x.com) and bare http://y.com",
    ]
    for inp in inputs:
        result = format_markdown(escape_for_pango(inp))
        assert '<a ' not in result, f"format_markdown emitted <a> for {inp!r}: {result!r}"
        assert '<a>' not in result, f"format_markdown emitted <a> for {inp!r}: {result!r}"
```

## Part B — Fix stale docstrings/comments in `utils/markdown.py`

Per Debugger BUG #5 and #8, update these stale references (they describe the
OLD `<a href>` output):

1. **Line 15** (module docstring): `[text](url)-> <a href="url"><u>text</u></a>`
   → change to `[text](url)-> <u>text</u>` (underlined, non-clickable)

2. **Lines 91-97** (function docstring, the "Order of operations" list):
   - Step 3: `Convert markdown links [text](url) -> <a href="url"><u>text</u></a>`
     → `Convert markdown links [text](url) -> <u>text</u> (underlined)`
   - Step 4: `Auto-link bare URLs (now safe — <a> tags are placeholders)`
     → `Auto-link bare URLs -> <u>url</u> (underlined)`
   - Step 6: `Restore <a> anchor tags` → `Restore <u> link text`

3. **Line 223** (Step 3 section header comment):
   `# ── Step 3: Markdown links -> <a> tags, then immediately protect those <a> tags`
   → `# ── Step 3: Markdown links -> <u> underlined text, then immediately protect`

4. **Line 230** (inline comment):
   `# Produce <a> tag, then immediately protect it with a placeholder`
   → `# Produce <u> underlined text, then immediately protect it with a placeholder`

5. **Line 251** (Step 3a comment):
   `# build an <a> tag, and protect it with the same \x00ANCHOR{N}\x00`
   → `# build <u> underlined text, and protect it with the same \x00ANCHOR{N}\x00`

6. **Line 330** (Step 6 header):
   `# ── Step 6: Restore <a> anchor tags`
   → `# ── Step 6: Restore <u> link text`

**Do NOT change line 279** (Step 3b href-protection comment) — that logic is
intentionally kept and still operates on pre-existing `href="URL"` from caller
input. Its comment is still accurate.

**Do NOT remove `safe_url` / `safe_href` variables** (Debugger BUG #2) — leave
them for now. A linter warning is acceptable; removing them risks over-editing.
They'll be cleaned up in a future hardening pass.

## Rules

- **Two files only:** `tests/test_markdown.py` + `utils/markdown.py`.
- **Read each test before editing.** Run pytest first to see failures.
- **Do NOT change any production logic** in markdown.py — only comments/docstrings (Part B).
- **Each test's NEW assertion must match the ACTUAL output.** When unsure,
  run `format_markdown(escape_for_pango(input))` and assert on the real output.
  Do NOT guess what the output should be.

## Verify (run these, paste full output)

1. Run the full test file:
   ```
   python3 -m pytest tests/test_markdown.py -v
   ```
   Expected: ALL tests pass (including the new regression test). Paste the
   full output.

2. No `<a href` assertions remain in test_markdown.py (except any that assert
   `<a` is NOT present, or the pre-existing-anchor test):
   ```
   grep -n '<a href' tests/test_markdown.py
   ```
   Report what remains and why each is acceptable.

3. No stale `<a>` references in markdown.py docstrings/comments (except line 279):
   ```
   grep -n '<a ' utils/markdown.py
   ```

4. Compile both files:
   ```
   python3 -m py_compile tests/test_markdown.py utils/markdown.py && echo COMPILE_OK
   ```

## COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x/not done] All failing test_markdown.py assertions updated to <u> — evidence: <pytest summary>
- [x/not done] New regression test test_format_markdown_no_anchor_tags_emitted added — evidence: <pytest line>
- [x/not done] markdown.py docstrings (lines 15, 91-97) updated — evidence: <grep>
- [x/not done] markdown.py inline comments (223, 230, 251, 330) updated — evidence: <grep>
- [x/not done] All tests pass — evidence: <pytest summary N passed>
- [x/not done] Both files compile — evidence: COMPILE_OK
```

Report back with files changed, all verification outputs, and the COMPLETENESS block. Please write per the steelFramedCodeWriter prompt.
