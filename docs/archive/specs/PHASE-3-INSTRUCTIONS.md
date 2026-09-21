# Phase 3 Instructions: Revert Flow + Polish

**Spec:** SPEC-ONE-CLICK-DIFF.md (§5 Phase 3)
**Phase:** Final polish for diff_viewer
**Target files:** ui/views/diff_viewer.py, tests/test_diff_viewer.py

---

## Changes Required

### 1. Keyboard Navigation in History List

Add key event handling to `_history_list`:

- **Up/Down arrows** — navigate between history rows (GTK4 ListBox handles this natively if focus is on the list)
- **Enter** — activate selected row (same as double-click → `_on_history_row_activated`)
- **Escape** — close diff viewer (call `on_back` callback)

Implementation approach:
```python
# In _build_ui(), after creating _history_list:
self._history_list.set_focus_on_click(True)
self._history_list.connect("key-press-event", self._on_history_key_press)

# In DiffViewer:
def _on_history_key_press(self, widget, event):
    keyval = event.keyval
    if keyval == Gdk.KEY_Escape:
        if self._on_back:
            self._on_back()
            return True
    return False
```

Also ensure Escape closes viewer from anywhere:
```python
# In _build_ui(), add key controller to DiffViewer itself:
from gi.repository import Gtk, Gdk
key_controller = Gtk.EventControllerKey()
key_controller.connect("key-pressed", self._on_key_pressed)
self.add_controller(key_controller)

def _on_key_pressed(self, controller, keyval, keycode, state):
    if keyval == Gdk.KEY_Escape:
        if self._on_back:
            self._on_back()
            return True
    return False
```

### 2. Copy Diff to Clipboard Button

Add button to action bar (next to revert button):

```python
# In _build_ui(), in action bar section:
self._copy_btn = Gtk.Button(label="Copy diff to clipboard")
self._copy_btn.add_css_class("diff-viewer-copy-btn")
self._copy_btn.connect("clicked", self._on_copy_clicked)
self._action_bar.append(self._copy_btn)
```

Handler:
```python
def _on_copy_clicked(self, button):
    """Copy current diff text to clipboard."""
    # Get the diff text from the current view
    if self._stack.get_visible_child_name() == "diff":
        # Collect all text from _diff_box children
        lines = []
        child = self._diff_box.get_first_child()
        while child:
            if hasattr(child, 'get_text'):  # Gtk.Label
                lines.append(child.get_text())
            elif isinstance(child, Gtk.Box):  # hunk view
                # Recursively get labels
                self._collect_label_text(child, lines)
            child = child.get_next_sibling()
        diff_text = "\n".join(lines)
    else:
        # History view - no diff to copy
        return
    
    display = self.get_display()
    if display:
        clipboard = display.get_clipboard()
        clipboard.set(diff_text)

def _collect_label_text(self, box, lines):
    child = box.get_first_child()
    while child:
        if hasattr(child, 'get_text'):
            lines.append(child.get_text())
        elif isinstance(child, Gtk.Box):
            self._collect_label_text(child, lines)
        child = child.get_next_sibling()
```

---

## Rules (steelFramedCodeWriter.md)

- Read diff_viewer.py in full before editing
- Verify Gdk import at module level
- Add tests for keyboard navigation and clipboard copy
- Run tests under xvfb

---

## Deliverable Expectations

```
Files changed:
- ui/views/diff_viewer.py:XX-YY (keyboard nav + copy button)
- tests/test_diff_viewer.py:AA-BB (new tests)

Verification:
pytest tests/test_diff_viewer.py -v
→ [all tests pass including new keyboard/clipboard tests]
```

---

## Word Marker

**please write**