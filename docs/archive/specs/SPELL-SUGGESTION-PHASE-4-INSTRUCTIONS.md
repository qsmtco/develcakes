# Phase 4: Wire the right-click callback in window.py

**Spec:** `docs/specs/SPEC-SPELL-SUGGESTION-POPOVER.md` §2.4
**File to change:** `ui/window.py`
**Total phases:** 5
**Current phase:** 4 of 5
**Depends on:** Phases 1, 2, 3 must be complete and verified

## What to do

Add a `_on_input_right_click` closure in `MainWindow.__init__` (in the input toolbar wiring section, after the existing `_on_input_buffer_changed` closure at ~line 334) that:

1. Checks if spell check is enabled
2. Converts TextView-local (x, y) coordinates to a TextIter via `get_iter_at_location`
3. Checks if the iter has the `spell-error` tag
4. If yes, fetches suggestions via `handler.get_suggestions_at_iter()`
5. Shows the popover via `toolbar.show_suggestions_menu()` with `parent_widget=text_view`
6. Wires it all via `self._main_content.set_on_input_right_click(...)`

## Exact code to add

Add this AFTER the `self._main_content.set_on_buffer_changed(_on_input_buffer_changed)` line (~line 334) and BEFORE the "Project handler" section (~line 337):

```python
# Right-click spell-check suggestions on the input TextView.
# Wires: MainContent GestureClick → InputToolbarHandler → ChatInputToolbar popover.
def _on_input_right_click(n_press, x, y):
    """Right-click on input TextView — show spell suggestions if word is misspelled."""
    handler = self._input_toolbar_handler
    if not handler._spell_enabled:
        return
    text_view = self._main_content.user_input
    # Convert (x, y) to a TextIter
    result, iter_at_pos = text_view.get_iter_at_location(int(x), int(y))
    # GTK4 TextView.get_iter_at_location returns (bool success, TextIter)
    if not result:
        return
    # Check if the iter has the spell-error tag
    buf = text_view.get_buffer()
    tag_table = buf.get_tag_table()
    spell_tag = tag_table.lookup("spell-error")
    if spell_tag is None:
        return  # spell check never ran (no tag created yet)
    if not iter_at_pos.has_tag(spell_tag):
        return  # word is not misspelled — no popover
    # Fetch suggestions and show popover
    suggestions = handler.get_suggestions_at_iter(iter_at_pos)
    def _apply_suggestion(suggestion):
        # Re-derive the iter at the same offset (the original iter may be stale)
        offset = iter_at_pos.get_offset()
        fresh_iter = buf.get_iter_at_offset(offset)
        handler.replace_word_at_iter(fresh_iter, suggestion)
    self._main_content.toolbar.show_suggestions_menu(
        suggestions, _apply_suggestion, parent_widget=text_view
    )

self._main_content.set_on_input_right_click(_on_input_right_click)
```

## API verification (confirmed by supervisor against GTK4 source)

- `Gtk.TextView.get_iter_at_location(x, y)` — GTK4 returns `(bool, TextIter)` tuple
- `Gtk.TextIter.has_tag(tag)` — returns bool
- `Gtk.TextBuffer.get_tag_table().lookup("spell-error")` — tag created in `InputToolbarHandler._apply_spell_tags` (line 136: `buf.create_tag("spell-error")`)
- `Gtk.TextIter.get_offset()` — returns int
- `Gtk.TextBuffer.get_iter_at_offset(offset)` — returns TextIter
- The closure pattern mirrors the existing `_on_input_buffer_changed` closure at line ~327-334
- `handler._spell_enabled` — the private flag on InputToolbarHandler, used to short-circuit when spell check is off

## Verification

After making changes, run:

```bash
cd /path/to/projects/crabcakes
python3 -c "from ui.window import MainWindow; print('Import OK')"
python3 -m pytest tests/test_window.py -q --tb=short 2>/dev/null || echo "No test_window.py found or failures"
```

Also run:
```bash
grep -n "_on_input_right_click\|set_on_input_right_click" ui/window.py
```

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`
- READ ALL FILES before starting — read `ui/window.py` in full first, especially lines 310-340
- Do not modify any other file
- Do not reformat adjacent code
- This is an INTEGRATION phase — if anything doesn't work, report it immediately rather than guessing

## Deliverable

Report back with:
1. Files changed (with line numbers)
2. Full import/pytest output
3. Grep outputs above
4. COMPLETENESS checklist:
```
COMPLETENESS:
- [x/not done] Edit 1: Added _on_input_right_click closure — evidence (line N)
- [x/not done] Edit 2: Wired via set_on_input_right_click — evidence (line N)
```
