# Phase 1: Add `replace_word_at_iter` to InputToolbarHandler

**Spec:** `docs/specs/SPEC-SPELL-SUGGESTION-POPOVER.md` §2.2
**File to change:** `ui/handlers/input_toolbar_handler.py`
**Total phases:** 5
**Current phase:** 1 of 5

## What to do

Add a new public method `replace_word_at_iter(self, text_iter, replacement: str) -> None` to the `InputToolbarHandler` class.

This method:
1. Finds word boundaries around `text_iter` using `backward_word_start()` / `forward_word_end()`
2. Deletes the word from the buffer
3. Inserts `replacement` in its place
4. Re-runs spell check if enabled (to update the underline tags)

## Exact code to add

Add this method AFTER the existing `get_suggestions_at_iter` method (currently ends around line 98) and BEFORE the `# Spell check — internal` section header (line 100).

```python
def replace_word_at_iter(self, text_iter, replacement: str) -> None:
    """Replace the word at *text_iter* with *replacement*.

    Finds word boundaries around text_iter, deletes the word, and inserts
    *replacement* in its place. After replacement, re-runs spell check if
    enabled so the underline is removed from the corrected word.

    Args:
        text_iter:  Gtk.TextIter — any iter inside the word to replace.
        replacement: str — the corrected word text.
    """
    word_start = text_iter.copy()
    word_end = text_iter.copy()
    if not word_start.inside_word():
        return
    word_start.backward_word_start()
    word_end.forward_word_end()
    buf = self._mc.user_input.get_buffer()
    buf.delete(word_start, word_end)
    buf.insert(word_start, replacement)
    # Re-run spell check to update tags (removes underline if word is now correct)
    if self._spell_enabled:
        self._run_spell_check()
```

## Patterns this mirrors (verified against source)

- `get_suggestions_at_iter` (line 77-98): same `backward_word_start()` / `forward_word_end()` / `inside_word()` pattern
- `replace_current` (line 212): same `buf.delete(start, end)` + `buf.insert(start, replacement)` pattern
- `self._run_spell_check()` defined at line 102 — standard spell-check refresh entry point
- `self._spell_enabled` flag at line 37

## Verification

After adding the method, run:

```bash
cd /path/to/projects/crabcakes
python3 -m pytest tests/test_input_toolbar_handler.py -q --tb=short
```

Paste the full output.

Also run:
```bash
grep -n "def replace_word_at_iter" ui/handlers/input_toolbar_handler.py
grep -n "^from gi\|^import gi" ui/handlers/input_toolbar_handler.py
```

The second grep confirms NO Gtk imports were added (architecture boundary: handlers must not import Gtk).

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`
- READ ALL FILES before starting — read `ui/handlers/input_toolbar_handler.py` in full first
- Do not modify any other file
- Do not add any imports
- Do not reformat adjacent code

## Deliverable

Report back with:
1. Files changed (with line numbers)
2. Full pytest output
3. Grep outputs above
4. COMPLETENESS checklist:
```
COMPLETENESS:
- [x/not done] Edit 1: Added replace_word_at_iter method — evidence (line N, grep output)
```
