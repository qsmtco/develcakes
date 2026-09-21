# Phase 3b of 4 — Wire `is_in_container` into `feed_tab.py`

**Spec:** `docs/specs/SPEC-GTK-CONTAINER-MEMBERSHIP-FIX.md` (§3.3)
**Master prompt:** `prompts/steelFramedCodeWriter.md` — invoke it. Read it first.
**Scope:** ONE file: `ui/views/feed_tab.py`. No other files.

## Task — one import + five site replacements in `ui/views/feed_tab.py`

### Edit 1: Add import

After the existing GTK import block near the top (the file starts with
`import gi` / `gi.require_version('Gtk', '4.0')` / `from gi.repository import Gtk`),
add at the end of the import section:

```python
from utils.gtk_containers import is_in_container
```

Read the file first to find the exact end of the import block.

### Edit 2: Replace site 2 — `show_empty_state` (line ~193)

Find (inside `show_empty_state`, in a `for` loop over card ids):

```python
            if widget in self._card_container:
```

Replace with:

```python
            if is_in_container(widget, self._card_container):
```

### Edit 3: Replace site 3 — `append_card` (line ~210)

Find (inside `append_card`):

```python
        if self._empty_widget is not None and self._empty_widget in self._card_container:
```

Replace with:

```python
        if self._empty_widget is not None and is_in_container(self._empty_widget, self._card_container):
```

### Edit 4: Replace site 4 — `remove_card` (line ~236)

Find (inside `remove_card`):

```python
        if self._card_container and widget in self._card_container:
```

Replace with:

```python
        if self._card_container is not None and is_in_container(widget, self._card_container):
```

Note: the truthiness guard `if self._card_container` becomes `if self._card_container is not None`
for explicit type safety (the helper also handles None internally, but the explicit
guard is kept per spec §3.3.4 for clarity / early exit).

### Edit 5: Replace site 5 — `prepend_card` (line ~250)

Find (inside `prepend_card`):

```python
        if self._empty_widget is not None and self._empty_widget in self._card_container:
```

Replace with:

```python
        if self._empty_widget is not None and is_in_container(self._empty_widget, self._card_container):
```

**IMPORTANT — sites 3 and 5 have IDENTICAL source text.** When you edit, you must
target them one at a time. Use the surrounding context (the function they live in:
`append_card` for site 3, `prepend_card` for site 5) to disambiguate. Read the file,
confirm which occurrence is in which function, and edit each separately. Do NOT do
a global replace — that could hit the wrong one or both at once unpredictably.

### Edit 6: Replace site 6 — `replace_card` (line ~270)

Find (inside `replace_card`):

```python
        if old_widget not in self._card_container:
```

Replace with:

```python
        if not is_in_container(old_widget, self._card_container):
```

## Rules

- **One file only:** `ui/views/feed_tab.py`. Do not touch chat_render_handler.py (that's Phase 3a).
- **Read the file first.** Confirm exact line numbers and surrounding context for all
  5 sites before editing. Line numbers may have drifted slightly from the spec.
- **Sites 3 and 5 are textually identical** — disambiguate by enclosing function.
- **Do not change any logic other than the membership check.** The `remove()` calls,
  the `_empty_widget = None` assignments, the `return` statements — all stay as-is.

## Verify (run these, paste full output)

1. Compile check:
   ```
   python3 -m py_compile ui/views/feed_tab.py && echo COMPILE_OK
   ```
   Expected: `COMPILE_OK`

2. ALL old patterns gone (zero matches):
   ```
   grep -nE "in self\._card_container" ui/views/feed_tab.py
   ```
   Expected: no output (exit 1). This single grep catches all 5 old patterns
   (sites 2, 3, 4, 5, 6 all contain the substring `in self._card_container`).

3. Import present:
   ```
   grep -n "from utils.gtk_containers import is_in_container" ui/views/feed_tab.py
   ```
   Expected: 1 match.

4. New helper used 5 times:
   ```
   grep -c "is_in_container(" ui/views/feed_tab.py
   ```
   Expected: 5 (or 6 if you count the import line — the `-c` counts lines; the
   import line also contains `is_in_container(`. So expect 6 lines total:
   1 import + 5 call sites. Report the exact count.)

5. Cross-check the 5 call sites are in the right functions:
   ```
   grep -nE "is_in_container\(" ui/views/feed_tab.py
   ```
   Expected: 6 lines — 1 import + 1 in show_empty_state + 1 in append_card +
   1 in remove_card + 1 in prepend_card + 1 in replace_card.

## COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [x/not done] import added — evidence: <grep output>
- [x/not done] Site 2 (show_empty_state) replaced — evidence: <grep -nE output showing it in show_empty_state>
- [x/not done] Site 3 (append_card) replaced — evidence: <grep showing it in append_card>
- [x/not done] Site 4 (remove_card) replaced, guard changed to 'is not None' — evidence: <grep>
- [x/not done] Site 5 (prepend_card) replaced — evidence: <grep showing it in prepend_card>
- [x/not done] Site 6 (replace_card) replaced (not in → not is_in_container) — evidence: <grep>
- [x/not done] All old 'in self._card_container' patterns gone — evidence: <grep exit 1, no output>
- [x/not done] py_compile passes — evidence: <COMPILE_OK>
```

Report back with files changed, all verification outputs, and the COMPLETENESS block. Please write per the steelFramedCodeWriter prompt.
