# Phase D Instructions: Full Window Integration

**Spec:** SPEC-INLINE-FILE-TREE-DIFF-DRAWER.md (§2.4, §2.5, §5 Phase D)
**Phase:** D of F (wiring + integration)
**Target files:** ui/views/main_content.py, ui/window.py, ui/views/file_tree.py

---

## Changes Required

### 1. Update FileTree to support external toggle

**Add method to FileTree class** (ui/views/file_tree.py):
```python
def toggle_drawer_for_file(self, file_path: str) -> None:
    """Public method to toggle drawer for a specific file path.
    Used by external widgets (e.g., main_content) to open/close drawer."""
    if file_path in self._drawers:
        self._toggle_drawer(file_path)
```

**Add getter for drawer visibility:**
```python
def is_drawer_open(self, file_path: str) -> bool:
    """Check if drawer for file is currently open."""
    entry = self._drawers.get(file_path)
    return entry[2] if entry else False
```

---

### 2. Wire FileTree to MainContent for external toggle

**In ui/window.py** - after line ~423 where `left_panel._file_tree.set_project_handler` is called, add:

```python
# Wire MainContent to FileTree for external drawer control
self._main_content.set_file_tree(self._left_panel._file_tree)
```

**Add to MainContent class** (ui/views/main_content.py):

```python
def set_file_tree(self, file_tree) -> None:
    """Set FileTree reference for external drawer control."""
    self._file_tree = file_tree

def toggle_file_diff(self, file_path: str) -> None:
    """Toggle diff drawer for a file (called from external widgets)."""
    if self._file_tree:
        self._file_tree.toggle_drawer_for_file(file_path)
```

---

### 3. Add Diff Drawer to MainContent (optional enhancement)

**Option A:** Keep current behavior (DiffViewer in main_content slot)
**Option B:** Also show diff in FileTree drawer (current implementation)

**Current behavior is fine** - DiffViewer in main_content shows full diff, FileTree drawer shows inline diff. Both serve different purposes.

---

### 4. Add "Open in Diff Viewer" action in FileTree drawer

**In FileTree drawer action bar** (below revert button), add button:
```python
open_in_viewer_btn = Gtk.Button(label="Open in Diff Viewer")
open_in_viewer_btn.connect("clicked", lambda btn: self._on_open_in_viewer_clicked(file_path))
action_bar.append(open_in_viewer_btn)
```

**Handler:**
```python
def _on_open_in_viewer_clicked(self, button, file_path: str) -> None:
    """Open file in main_content DiffViewer."""
    if self._on_open_in_viewer:
        self._on_open_in_viewer(file_path)
```

**Add callback setter in FileTree:**
```python
def set_on_open_in_viewer(self, callback):
    self._on_open_in_viewer = callback
```

**Wire in window.py:**
```python
self._left_panel._file_tree.set_on_open_in_viewer(
    lambda path: self._main_content.show_diff_viewer_for_file(path)
)
```

**Add to MainContent:**
```python
def show_diff_viewer_for_file(self, file_path: str) -> None:
    """Show file in main_content DiffViewer (existing functionality)."""
    # This reuses existing DiffViewer slot
    project_path = self._project_handler.get_active_project_path()
    if project_path:
        rel_path = os.path.relpath(file_path, project_path)
        checkpoint_sha = self._get_checkpoint_sha_for_project()
        viewer = DiffViewer(
            file_path=rel_path,
            project_path=project_path,
            checkpoint_sha=checkpoint_sha,
            on_back=lambda: self.hide_diff_viewer(),
            on_revert=lambda fp, sha: self._review_handler.revert_file_to_sha(...),
        )
        self.show_diff_viewer(viewer)
```

---

### 5. Sync drawer state with DiffViewer

**When DiffViewer opens/closes**, sync FileTree drawer:
- FileTree drawer open → DiffViewer opens same file
- DiffViewer closes → FileTree drawer can stay open (or close)

---

### 6. Keyboard shortcuts (Phase E preview)

Add to MainContent or Window:
- `Ctrl+D` → toggle diff drawer for selected file
- `Escape` → close diff drawer/viewer

---

## Rules (steelFramedCodeWriter.md)

- Read all target files in full before editing
- Verify every change with evidence (pytest output, grep, wc -l)
- No fabricated APIs — use existing ProjectHandler, ReviewHandler, FileTree
- Hard part first: wiring callbacks between components
- Wire it or delete it — no stubs

---

## Deliverable Expectations

```
Files changed:
- ui/views/file_tree.py: public toggle/getter methods
- ui/views/main_content.py: file_tree reference, toggle method
- ui/window.py: wire FileTree <-> MainContent

Verification:
grep -n "toggle_drawer_for_file\|is_drawer_open\|set_file_tree\|toggle_file_diff" ui/views/file_tree.py ui/views/main_content.py ui/window.py
python3 -c "from ui.views.file_tree import FileTree; from ui.views.main_content import MainContent; print('import ok')"
xvfb-run -a pytest tests/ -x -q

COMPLETENESS:
- [x] FileTree.toggle_drawer_for_file(file_path) public method
- [x] FileTree.is_drawer_open(file_path) getter
- [x] MainContent.set_file_tree(file_tree) setter
- [x] MainContent.toggle_file_diff(file_path) method
- [x] window.py wires FileTree <-> MainContent
- [x] All tests pass
```

---

## Word Marker

**please write**