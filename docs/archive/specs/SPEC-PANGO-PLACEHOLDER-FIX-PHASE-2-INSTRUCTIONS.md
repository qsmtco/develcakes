# Phase 2 of 3 — Resolve code-span placeholders in Step 3 link labels

**Spec:** `docs/specs/SPEC-PANGO-PLACEHOLDER-ESCAPE-FIX.md` (§2.2)
**Master prompt:** `prompts/steelFramedCodeWriter.md` — invoke it. Read it first.
**Scope:** ONE file: `utils/markdown.py`. No other files.

## Why (the bug)

When a markdown link's label is a backticked code span — `[`code.md`](url)` —
Step 1 replaces `` `code.md` `` with `\x00CODE0\x00`. Step 3's regex then
captures the placeholder as the label and stores `f'<u>\x00CODE0\x00</u>'` in
`anchor_spans`. The placeholder is consumed from `protected`, so Step 5 (code
restoration) never finds it. The null byte reaches Pango, which uses C-string
semantics and truncates at `\x00`, leaving an unclosed `<u>` tag.

## Task — TWO edits in `utils/markdown.py`

Read the file first. Find Step 3's `_link_replace_and_protect` inner function
(around line 226). It currently looks like:

```python
    def _link_replace_and_protect(m):
        label = m.group(1)
        url = m.group(2)
        safe_url = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=-_.~")
        # Pango does NOT support <a href> in markup (raises Unknown tag 'a').
        # Render link text as underlined (non-clickable). HIGH-6 validation
        # is preserved: non-allowlisted schemes still get the warning prefix.
        # Clickable links require Pango AttrType.LINK (future spec).
        anchor_html = f'<u>{label}</u>'
        if not _validate_link_url(url):
            anchor_html = _WARNING_PREFIX + anchor_html
        anchor_spans.append(anchor_html)
        return f'\x00ANCHOR{len(anchor_spans) - 1}\x00'
```

### Edit 1: Add `_resolve_code_in_label` helper

BEFORE `_link_replace_and_protect` (but after `_collect_code_spans` has run,
so `code_spans` is populated), add this helper as a nested function inside
`format_markdown`:

```python
    def _resolve_code_in_label(m):
        """Resolve a \\x00CODE{N}\\x00 placeholder to <tt>code</tt> text.

        Used in Step 3 to resolve code-span placeholders that appear in
        markdown link labels (e.g. [`code`](url)). Without this, the null
        bytes survive into anchor_html and Pango's C-string parser
        truncates the markup at \\x00.
        """
        idx = int(m.group(1))
        if idx < len(code_spans):
            content = code_spans[idx]
            if '&' in content:
                return f'<tt>{content}</tt>'
            return f'<tt>{html.escape(content)}</tt>'
        return m.group(0)
```

Place it right before `_link_replace_and_protect` so it's defined before use.
It must be inside `format_markdown` so it has closure access to `code_spans`.

### Edit 2: Resolve placeholders in the label in `_link_replace_and_protect`

Add ONE line after `label = m.group(1)` that resolves placeholders:

```python
    def _link_replace_and_protect(m):
        label = m.group(1)
        url = m.group(2)
        safe_url = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=-_.~")
        # Resolve code-span placeholders in the label BEFORE storing.
        # Step 1 may have replaced `code` with \x00CODE{N}\x00. If we store
        # the placeholder in anchor_html, Step 5 (code restoration) won't
        # find it (it was consumed from `protected`), and the null bytes
        # reach Pango, which uses C-string semantics and truncates at \x00.
        label = _CODE_PLACEHOLDER_RE.sub(_resolve_code_in_label, label)
        # Pango does NOT support <a href> in markup (raises Unknown tag 'a').
        # Render link text as underlined (non-clickable). HIGH-6 validation
        # is preserved: non-allowlisted schemes still get the warning prefix.
        # Clickable links require Pango AttrType.LINK (future spec).
        anchor_html = f'<u>{label}</u>'
        if not _validate_link_url(url):
            anchor_html = _WARNING_PREFIX + anchor_html
        anchor_spans.append(anchor_html)
        return f'\x00ANCHOR{len(anchor_spans) - 1}\x00'
```

**Verify `_CODE_PLACEHOLDER_RE` is accessible.** It's a module-level regex
(grep for it). The helper uses it to find `\x00CODE{N}\x00` patterns.

## Rules

- **One file only:** `utils/markdown.py`. Do not touch test files (Phase 3).
- **Do NOT change Step 3a** (`_angle_link_replace`) — angle-bracket links use
  the URL as display text, which is not a code span. Only Step 3 needs this fix.
- **Do NOT change Step 4** (auto-linking) — auto-links don't have labels.
- **`_resolve_code_in_label` must be nested inside `format_markdown`** so it
  has closure access to `code_spans`.
- **Verify `_CODE_PLACEHOLDER_RE` exists at module level.** It should — Step 5
  uses it for code restoration. Do not redefine it.

## Verify (run these, paste full output)

1. Compile:
   ```
   python3 -m py_compile utils/markdown.py && echo COMPILE_OK
   ```

2. The bug is fixed — no null bytes in output:
   ```
   python3 -c "
   from utils.escaping import escape_for_pango
   from utils.markdown import format_markdown
   result = format_markdown(escape_for_pango('[\`context.md\`](https://example.com)'))
   print(repr(result))
   assert '\x00' not in result, 'NULL BYTE in output!'
   assert 'context.md' in result, 'code text lost!'
   print('FIXED')
   "
   ```

3. Multiple code-span labels work:
   ```
   python3 -c "
   from utils.escaping import escape_for_pango
   from utils.markdown import format_markdown
   result = format_markdown(escape_for_pango('see [\`a.py\`](url1) and [\`b.py\`](url2)'))
   print(repr(result))
   assert '\x00' not in result
   assert 'a.py' in result and 'b.py' in result
   print('MULTI_OK')
   "
   ```

4. Non-code-span labels still work (no regression):
   ```
   python3 -c "
   from utils.escaping import escape_for_pango
   from utils.markdown import format_markdown
   result = format_markdown(escape_for_pango('[click here](https://example.com)'))
   print(repr(result))
   assert '<u>click here</u>' in result
   print('PLAIN_OK')
   "
   ```

5. Pango accepts the output:
   ```
   python3 -c "
   import gi; gi.require_version('Pango','1.0')
   from gi.repository import Pango
   from utils.escaping import escape_for_pango
   from utils.markdown import format_markdown
   result = format_markdown(escape_for_pango('[\`code.md\`](url)'))
   ok,_,_,_ = Pango.parse_markup(result,-1,'\x00')
   print('PANGO_OK' if ok else 'PANGO_FAIL')
   "
   ```

## COMPLETENESS checklist

```
COMPLETENESS:
- [x/not done] _resolve_code_in_label helper added inside format_markdown — evidence: <grep>
- [x/not done] label = _CODE_PLACEHOLDER_RE.sub(...) added in _link_replace_and_protect — evidence: <grep>
- [x/not done] No null bytes in [`code`](url) output — evidence: <output>
- [x/not done] Plain [text](url) labels still work — evidence: <output>
- [x/not done] Pango accepts [`code`](url) output — evidence: PANGO_OK
- [x/not done] py_compile passes — evidence: COMPILE_OK
```

Please write per the steelFramedCodeWriter prompt.
