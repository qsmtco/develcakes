# Phase 1 of 3 — Remove "a" from _PANGO_KNOWN_TAGS in escaping.py

**Spec:** `docs/specs/SPEC-PANGO-PLACEHOLDER-ESCAPE-FIX.md` (§2.1)
**Master prompt:** `prompts/steelFramedCodeWriter.md` — invoke it. Read it first.
**Scope:** ONE file: `utils/escaping.py`. No other files.

## Why

Pango 1.52 does NOT support the `<a>` tag in markup. `escape_for_pango`
currently preserves `<a href="...">` because `"a"` is in `_PANGO_KNOWN_TAGS`.
The preserved `<a>` reaches `format_markdown` → `set_markup`, where Pango
rejects it. Removing `"a"` from the known set makes `escape_for_pango` escape
it to `&lt;a&gt;`, preventing it from ever reaching Pango.

## Task — ONE edit in `utils/escaping.py`

Find `_PANGO_KNOWN_TAGS` (around line 31). It currently looks like:

```python
_PANGO_KNOWN_TAGS: frozenset[str] = frozenset({
    # Text style tags
    "b", "i", "u", "s", "tt", "big", "small",
    # Span tag (generic container with attributes)
    "span",
    # Anchor tag
    "a",
    # Sub/superscript
    "sub", "sup",
    # Overline
    "o",
})
```

Remove the `"a"` line and update the comment:

```python
_PANGO_KNOWN_TAGS: frozenset[str] = frozenset({
    # Text style tags
    "b", "i", "u", "s", "tt", "big", "small",
    # Span tag (generic container with attributes)
    "span",
    # Sub/superscript
    "sub", "sup",
    # Overline
    "o",
    # NOTE: "a" (anchor) is NOT included — Pango 1.52 does not support <a>
    # in markup. escape_for_pango escapes <a> to &lt;a&gt; so it never
    # reaches format_markdown or Gtk.Label.set_markup.
})
```

## Rules

- **One file only:** `utils/escaping.py`. Do not touch test files (Phase 3).
- **Only remove `"a"` and update the comment.** Do not change any other tag or logic.

## Verify (run these, paste full output)

1. Compile:
   ```
   python3 -m py_compile utils/escaping.py && echo COMPILE_OK
   ```

2. `"a"` is gone:
   ```
   python3 -c "from utils.escaping import _PANGO_KNOWN_TAGS; print('a' in _PANGO_KNOWN_TAGS)"
   ```
   Expected: `False`

3. `escape_for_pango` now escapes `<a>`:
   ```
   python3 -c "from utils.escaping import escape_for_pango; print(repr(escape_for_pango('<a href=\"x\">y</a>')))"
   ```
   Expected: contains `&lt;a` (escaped), NOT `<a href` (preserved).

4. Still preserves valid tags:
   ```
   python3 -c "from utils.escaping import escape_for_pango; print(repr(escape_for_pango('<b>bold</b>')))"
   ```
   Expected: `<b>bold</b>` (unchanged).

## COMPLETENESS checklist

```
COMPLETENESS:
- [x/not done] "a" removed from _PANGO_KNOWN_TAGS — evidence: <output>
- [x/not done] escape_for_pango escapes <a> — evidence: <output>
- [x/not done] escape_for_pango still preserves <b> etc — evidence: <output>
- [x/not done] py_compile passes — evidence: COMPILE_OK
```

Please write per the steelFramedCodeWriter prompt.
