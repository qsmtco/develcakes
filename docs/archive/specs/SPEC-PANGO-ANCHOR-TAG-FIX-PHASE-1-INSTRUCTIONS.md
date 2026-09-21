# Phase 1 of 2 — Fix `<a href>` emission in `format_markdown`

**Spec:** `docs/specs/SPEC-PANGO-ANCHOR-TAG-FIX.md` (§2.1)
**Master prompt:** `prompts/steelFramedCodeWriter.md` — invoke it. Read it first.
**Scope:** ONE file: `utils/markdown.py`. No other files.

## Why this fix is needed

Pango's markup parser does NOT support the `<a>` tag. Any `<a href="...">` in
markup causes `Gtk.Label.set_markup()` to raise `g-markup-error-quark: Unknown
tag 'a'` and reject the ENTIRE markup string. The user sees an empty or
truncated bubble. This is the root cause of the "text cut off after a backtick"
bug. The fix: replace `<a href="URL"><u>text</u></a>` with just `<u>text</u>`
(underlined, non-clickable). HIGH-6 validation is PRESERVED.

## Task — three edits in `utils/markdown.py`

Read the file first. Find each site by its surrounding context (line numbers
may have drifted). Make ONLY the change shown — do not alter anything else.

### Edit 1 — Step 3: markdown links (the `_link_replace_and_protect` inner function)

Find this exact block (inside `_link_replace_and_protect`, around line 231):

```python
        anchor_html = f'<a href="{safe_url}"><u>{label}</u></a>'
        # HIGH-6: prepend red warning prefix for non-allowlisted schemes
        if not _validate_link_url(url):
            anchor_html = _WARNING_PREFIX + anchor_html
        anchor_spans.append(anchor_html)
```

Replace the `anchor_html = ...` line ONLY (keep the `if not _validate_link_url`
block and `anchor_spans.append` unchanged):

```python
        # Pango does NOT support <a href> in markup (raises Unknown tag 'a').
        # Render link text as underlined (non-clickable). HIGH-6 validation
        # is preserved: non-allowlisted schemes still get the warning prefix.
        # Clickable links require Pango AttrType.LINK (future spec).
        anchor_html = f'<u>{label}</u>'
        if not _validate_link_url(url):
            anchor_html = _WARNING_PREFIX + anchor_html
        anchor_spans.append(anchor_html)
```

### Edit 2 — Step 3a: angle-bracket auto-links (the `_angle_link_replace` inner function)

Find this exact block (inside `_angle_link_replace`, around line 258):

```python
        anchor_html = f'<a href="{safe_href}"><u>{display_url}</u></a>'
        if not _validate_link_url(url):
            anchor_html = _WARNING_PREFIX + anchor_html
        anchor_spans.append(anchor_html)
```

Replace the `anchor_html = ...` line ONLY:

```python
        anchor_html = f'<u>{display_url}</u>'
        if not _validate_link_url(url):
            anchor_html = _WARNING_PREFIX + anchor_html
        anchor_spans.append(anchor_html)
```

### Edit 3 — Step 4: bare-URL auto-linking (the `_auto_link` inner function)

Find this exact block (inside `_auto_link`, around line 300):

```python
        anchor_html = f'<a href="{safe_url}"><u>{url}</u></a>'
        # HIGH-6: prepend red warning prefix for non-allowlisted schemes
        if not _validate_link_url(url):
            anchor_html = _WARNING_PREFIX + anchor_html
        return anchor_html
```

Replace the `anchor_html = ...` line ONLY:

```python
        anchor_html = f'<u>{url}</u>'
        # HIGH-6: prepend red warning prefix for non-allowlisted schemes
        if not _validate_link_url(url):
            anchor_html = _WARNING_PREFIX + anchor_html
        return anchor_html
```

## Rules

- **One file only:** `utils/markdown.py`. Do not touch any test file (that's Phase 2).
- **Do NOT change `_validate_link_url`, `_WARNING_PREFIX`, `_ALLOWED_LINK_SCHEMES`,
  `_AUTO_LINK_RE`, `safe_url`, `safe_href`, `display_url`, `url`, or `label` variables.**
  The only thing changing is the string assigned to `anchor_html` — the `<a href="...">`
  wrapper is removed, leaving just `<u>text</u>`.
- **Do NOT remove the `if not _validate_link_url(url):` check.** HIGH-6 validation is
  PRESERVED. Non-allowlisted schemes must still get `_WARNING_PREFIX`.
- **Leave Step 3b (href-protection) UNCHANGED.** It still protects pre-existing
  `href="URL"` from being re-auto-linked by Step 4.
- **The `safe_url`, `safe_href`, `url` variables may now be "unused"** at their
  respective sites (they were only used inside the `<a href="{safe_url}">` string).
  Do NOT remove them — they're still used by `_validate_link_url(url)` which must
  stay. (`safe_url`/`safe_href` become genuinely unused after this change; that's
  acceptable — leave them, a linter warning is better than over-editing.)

## Verify (run these, paste full output)

1. No `<a href` remains in the production code paths (Step 3b's href-protection
   is in a comment/string literal, not an emitted tag — verify):
   ```
   grep -n '<a href' utils/markdown.py
   ```
   Expected: the only matches should be in COMMENTS or the module docstring at
   the top of the file (which documents the OLD behavior). Report what you find.
   If any `<a href` remains in executable code (not a comment), that's a miss.

2. Compile check:
   ```
   python3 -m py_compile utils/markdown.py && echo COMPILE_OK
   ```

3. The three `anchor_html` assignments now use `<u>` not `<a href`:
   ```
   grep -n 'anchor_html = ' utils/markdown.py
   ```
   Expected: 3 matches, all using `f'<u>...` format (no `<a`).

4. `_validate_link_url` still called in all 3 sites (HIGH-6 preserved):
   ```
   grep -n '_validate_link_url' utils/markdown.py
   ```
   Expected: 1 definition + 3 call sites = 4 matches.

5. Smoke test — does a link-containing message now produce valid markup?
   ```
   python3 -c "
   from utils.escaping import escape_for_pango
   from utils.markdown import format_markdown
   msg = 'see context.md and http://example.com and [click](https://x.com)'
   result = format_markdown(escape_for_pango(msg))
   print(result)
   assert '<a href' not in result, 'still emitting <a>!'
   assert '<u>' in result, 'no underline emitted!'
   print('SMOKE_OK')
   "
   ```

## COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x/not done] Site 1 (Step 3 markdown links): anchor_html uses <u> — evidence: <grep>
- [x/not done] Site 2 (Step 3a angle-links): anchor_html uses <u> — evidence: <grep>
- [x/not done] Site 3 (Step 4 auto-link): anchor_html uses <u> — evidence: <grep>
- [x/not done] _validate_link_url still called in all 3 sites (HIGH-6 preserved) — evidence: <grep>
- [x/not done] No executable <a href in production code — evidence: <grep + report>
- [x/not done] py_compile passes — evidence: COMPILE_OK
- [x/not done] Smoke test: no <a href, has <u> — evidence: SMOKE_OK
```

Report back with files changed, all verification outputs, and the COMPLETENESS block. Please write per the steelFramedCodeWriter prompt.
