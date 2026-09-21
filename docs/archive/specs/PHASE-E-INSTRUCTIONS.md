# Phase E Instructions: Polish & Keyboard Shortcuts

**Spec:** SPEC-INLINE-FILE-TREE-DIFF-DRAWER.md (§5 Phase E)
**Phase:** E of F (polish & keyboard shortcuts)
**Target files:** ui/views/file_tree.py, ui/views/main_content.py, ui/views/diff_viewer.py, ui/window.py

---

## Changes Required

### 1. Keyboard Shortcuts in FileTree Drawer

**In `FileTree._add_drawer_for_file`** - add key controller to drawer box:

```python
# Keyboard navigation for drawer
key_controller = Gtk.EventControllerKey()
key_controller.connect("key-pressed", self._on_drawer_key_pressed, file_path, drawer_box)
drawer_box.add_controller(key_controller)
```

**Add handler method:**
```python
def _on_drawer_key_pressed(self, controller, keyval, keycode, state, file_path, drawer_box):
    """Handle keyboard shortcuts in drawer."""
    if keyval == Gdk.KEY_Escape:
        # Close drawer
        self._toggle_drawer(file_path)
        return True
    elif keyval == Gdk.KEY_c and (state & Gdk.ModifierType.CONTROL_MASK):
        # Ctrl+C = Copy diff to clipboard
        self._copy_drawer_diff_to_clipboard(file_path)
        return True
    return False
```

**Add clipboard copy method:**
```python
def _copy_drawer_diff_to_clipboard(self, file_path: str) -> None:
    """Copy current diff text to clipboard."""
    entry = self._drawers.get(file_path)
    if not entry or len(entry) < 4:
        return
    drawer_box = entry[3]
    diff_box = getattr(drawer_box, '_diff_box', None)
    if not diff_box:
        return
    
    # Collect diff text from diff_box
    lines = []
    child = diff_box.get_first_child()
    while child:
        if hasattr(child, 'get_text'):
            lines.append(child.get_text())
        elif isinstance(child, Gtk.Box):
            # Recurse into hunk boxes
            grandchild = child.get_first_child()
            while grandchild:
                if hasattr(grandchild, 'get_text'):
                    lines.append(grandchild.get_text())
                grandchild = grandchild.get_next_sibling()
        child = child.get_next_sibling()
    
    if lines:
        text = "\n".join(lines)
        display = self.get_display()
        if display:
            clipboard = display.get_clipboard()
            clipboard.set(text)
```

---

### 2. Global Keyboard Shortcuts in MainContent

**In MainContent class** - add key controller for global shortcuts:

```python
def _setup_global_shortcuts(self):
    """Setup global keyboard shortcuts for diff operations."""
    key_controller = Gtk.EventControllerKey()
    key_controller.connect("key-pressed", self._on_global_key_pressed)
    self.add_controller(key_controller)

def _on_global_key_pressed(self, controller, keyval, keycode, state):
    """Handle global keyboard shortcuts."""
    if keyval == Gdk.KEY_d and (state & Gdk.ModifierType.CONTROL_MASK):
        # Ctrl+D: Toggle diff for currently selected file in tree
        # This would need FileTree to expose currently selected file
        return True
    return False
```

---

### 3. Escape Key Handling in DiffViewer (already done, verify)

**In DiffViewer** - verify Escape closes viewer:
```python
# Already implemented in _on_key_pressed (line ~389)
if keyval == Gdk.KEY_Escape:
    if self._on_back:
        self._on_back()
    return True
```

---

### 4. Copy Button in DiffViewer (already done, verify)

**In DiffViewer action bar** - verify copy button exists:
```python
# Already implemented in _build_ui (line ~188)
self._copy_btn = Gtk.Button(label="Copy diff to clipboard")
self._copy_btn.add_css_class("diff-viewer-copy-btn")
self._copy_btn.connect("clicked", self._on_copy_clicked)
```

---

### 5. Escape Key in FileTree Drawer (Phase C already done, verify)

**In FileTree** - verify Escape closes drawer:
```python
# Already implemented in _on_key_pressed (line ~389)
if keyval == Gdk.KEY_Escape:
    if self._on_back:
        self._on_back()
    return True
```

---

### 6. Window-level shortcuts

**In MainWindow** - add project-level shortcuts:

```python
def _setup_project_shortcuts(self):
    """Setup project-level keyboard shortcuts."""
    key_controller = Gtk.EventControllerKey()
    key_controller.connect("key-pressed", self._on_project_key_pressed)
    self.add_controller(key_controller)

def _on_project_key_pressed(self, controller, keyval, keycode, state):
    if keyval == Gdk.KEY_d and (state & Gdk.ModifierType.CONTROL_MASK):
        # Ctrl+D: Toggle diff for currently selected file
        # This requires FileTree to expose currently selected file
        return True
    return False
```

---

## Rules (steelFramedCodeWriter.md)

- Read all target files in full before editing
- Verify every change with evidence (pytest output, grep, wc -l)
- No fabricated APIs — use existing ProjectHandler, ReviewHandler, FileTree, MainContent
- Hard part first: clipboard integration with Gtk clipboard API
- Wire it or delete it — no stubs

---

## Deliverable Expectations

```
Files changed:
- ui/views/file_tree.py: clipboard copy, keyboard shortcuts in drawer
- ui/views/main_content.py: global shortcuts (optional)
- ui/views/diff_viewer.py: verify copy button, Escape handling
- ui/window.py: global shortcuts (optional)

Verification:
grep -n "Escape\|clipboard\|Ctrl\|Escape" ui/views/file_tree.py ui/views/main_content.py ui/views/diff_viewer.py
python3 -c "from ui.views.file_tree import FileTree; from ui.views.main_content import MainContent; from ui.views.diff_viewer import DiffViewer; print('import ok')"
xvfb-run -a pytest tests/ -x -q

COMPLETENESS:
- [x] Escape closes drawer in FileTree
- [x] Ctrl+C copies diff to clipboard in drawer
- [x] Escape closes DiffViewer
- [x] Copy button works in DiffViewer
- [x] Global Ctrl+D shortcut (optional)
- [x] All tests pass
```

---

## Word Marker

**please write**