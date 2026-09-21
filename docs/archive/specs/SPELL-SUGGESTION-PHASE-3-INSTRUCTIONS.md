# Phase 3: Add right-click GestureClick on TextView in MainContent

**Spec:** `docs/specs/SPEC-SPELL-SUGGESTION-POPOVER.md` §2.1
**File to change:** `ui/views/main_content.py`
**Total phases:** 5
**Current phase:** 3 of 5

## What to do

Add a `Gtk.GestureClick` (right-click / `BUTTON_SECONDARY`) controller to `self._user_input` in `MainContent.__init__`, a new callback slot, a setter, and an internal signal handler.

MainContent is a pure view — it does NOT know about spell check or handlers. The callback lets `window.py` wire the handler logic without breaking the architecture boundary. This mirrors the existing `set_on_buffer_changed` pattern (line 221-229).

## Exact changes

### Change 1: New callback slot (in `__init__`, near line 142)

After the existing `self._on_buffer_changed: callable | None = None` line (line 142), add:

```python
self._on_input_right_click: callable | None = None
```

### Change 2: GestureClick controller (in `__init__`, after line 136)

After `self._user_input.add_css_class("input-bubble")` (line 136) and BEFORE `input_scroll.set_child(self._user_input)` (line 137), add:

```python
# Right-click controller for spell-check suggestions.
# Pattern from left_panel.py:756-758 (prompt row right-click).
right_click = Gtk.GestureClick()
right_click.set_button(Gdk.BUTTON_SECONDARY)
right_click.connect("pressed", self._on_input_right_click_internal)
self._user_input.add_controller(right_click)
```

### Change 3: New setter method

Add near the existing `set_on_buffer_changed` setter (after line 223). Add this method:

```python
def set_on_input_right_click(self, cb: callable) -> None:
    """Register callback for right-click on input TextView.

    cb(n_press, x, y) — called on right-click. The callback is responsible
    for checking if the word at (x, y) is misspelled and showing a popover.
    """
    self._on_input_right_click = cb
```

### Change 4: Internal signal handler

Add as a new method in the class (near the existing `_on_input_buffer_changed` method, around line 225):

```python
def _on_input_right_click_internal(self, gesture, n_press, x, y) -> None:
    """Internal: forward right-click to the registered callback."""
    if n_press != 1:
        return
    if self._on_input_right_click is not None:
        self._on_input_right_click(n_press, x, y)
```

## API verification (already confirmed by supervisor)

- `Gdk` is imported at line 3: `from gi.repository import Gtk, Gdk, GLib`
- `Gtk.GestureClick` is already used at lines 353, 359 in the same file
- `Gdk.BUTTON_SECONDARY` is the GTK4 constant for right-click
- `set_on_buffer_changed` pattern is at lines 221-229 — this is the exact same callback-indirection pattern

## Verification

After making changes, run:

```bash
cd /path/to/projects/crabcakes
python3 -m pytest tests/test_main_content.py -q --tb=short 2>/dev/null || echo "No test_main_content.py found"
python3 -c "from ui.views.main_content import MainContent; print('Import OK')"
```

Also run:
```bash
grep -n "set_on_input_right_click\|_on_input_right_click\|BUTTON_SECONDARY" ui/views/main_content.py
```

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`
- READ ALL FILES before starting — read `ui/views/main_content.py` in full first
- Do not modify any other file
- Do not add any imports (Gdk and Gtk already imported)
- Do not reformat adjacent code
- MainContent is a VIEW — no handler logic, no spell-check imports, no business logic

## Deliverable

Report back with:
1. Files changed (with line numbers)
2. Full pytest/import output
3. Grep outputs above
4. COMPLETENESS checklist:
```
COMPLETENESS:
- [x/not done] Edit 1: Added callback slot — evidence (line N)
- [x/not done] Edit 2: Added GestureClick controller — evidence (line N)
- [x/not done] Edit 3: Added set_on_input_right_click setter — evidence (line N)
- [x/not done] Edit 4: Added _on_input_right_click_internal handler — evidence (line N)
```
