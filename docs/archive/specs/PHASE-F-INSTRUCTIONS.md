# Phase F Instructions: Dead Code Cleanup

**Spec:** SPEC-INLINE-FILE-TREE-DIFF-DRAWER.md (follow-up cleanup)
**Phase:** F of F (dead code removal)
**Target files:** ui/views/file_tree.py, ui/views/diff_viewer.py, ui/views/diff_card.py, ui/window.py

---

## Changes Required

### 1. Remove Dead Imports

**ui/views/diff_card.py:24**
```python
# Before:
from utils.diff_parser import parse_diff, FileDiff

# After:
from utils.diff_parser import parse_diff
```

**ui/views/diff_viewer.py:24**
```python
# Before:
from utils.diff_parser import parse_diff, FileDiff

# After:
from utils.diff_parser import parse_diff
```

**ui/views/file_tree.py:22**
```python
# Before:
from utils.git_ops import diff_file_against_working_tree, diff_working_tree, file_log, diff_file_against, file_log, diff_file_against

# After:
from utils.git_ops import diff_file_against_working_tree, diff_working_tree, file_log, diff_file_against
```

**ui/views/file_tree.py:23**
```python
# Before:
from utils.diff_parser import parse_diff, FileDiff

# After:
from utils.diff_parser import parse_diff
```

**ui/views/file_tree.py:874, 964** — Remove unused `ReviewState` imports inside try blocks:
```python
# Remove these two lines entirely:
# from models.review_state import ReviewState
```

---

### 2. Remove Dead Methods

**ui/views/diff_viewer.py** — Remove dead `_on_revert_complete_main_thread` method (lines ~496-501):
```python
def _on_revert_complete_main_thread(self):
    """Called from the main thread via GLib.idle_add when revert completes."""
    if not self._disposed:
        self._cancel_revert_watchdog()
        self._load_current_diff()
```
**DELETE this entire method.**

Also remove the callback invocation in `_on_revert_confirmed` (line ~454):
```python
# Before:
def _on_revert_confirmed(self, dialog, response_id):
    ...
    def _on_revert_complete():
        GLib.idle_add(lambda: self._on_revert_complete_main_thread())

# After:
def _on_revert_confirmed(self, dialog, response_id):
    ...
    # No callback machinery
```

---

### 3. Remove Dead Callback Parameter

**ui/views/diff_viewer.py:65** — Remove `on_complete` from `on_revert` signature:
```python
# Before:
on_revert: Callable[[str, str, Callable[[], None] | None], None] | None = None,

# After:
on_revert: Callable[[str, str], None] | None = None,
```

**ui/views/diff_viewer.py:438** — Remove `on_complete` argument in `_on_revert_clicked`:
```python
# Before:
self._on_revert(self._file_path, target_sha, _on_revert_complete)

# After:
self._on_revert(self._file_path, target_sha)
```

---

### 4. Update Window.py Wiring

**ui/window.py:891-895** — Remove `on_complete` from callback:
```python
# Before:
def on_revert(file_path: str, target_sha: str):
    self._review_handler.revert_file_to_sha(project_name, file_path, target_sha)

# After (same, but the callback signature is now 2-arg):
def on_revert(file_path: str, target_sha: str):
    self._review_handler.revert_file_to_sha(project_name, file_path, target_sha)
```

---

### 5. Remove Dead Code in FileTree

**ui/views/file_tree.py:1007** — Remove dead `elif self._on_file_selected:` branch:
```python
# Before:
else:
    # File row - toggle drawer
    if full_path in self._drawers:
        self._toggle_drawer(full_path)
    elif self._on_file_selected:
        self._on_file_selected(full_path)

# After:
else:
    # File row - toggle drawer
    if full_path in self._drawers:
        self._toggle_drawer(full_path)
```

Also update docstring line 5 to match:
```python
# Fires on_file_selected(path) callback when a file without a drawer is activated.
```

---

### 6. Remove Dead Parameters

**ui/views/diff_viewer.py:547** — Remove `drawer_revealer` from `_on_drawer_diff_loaded`:
```python
# Before:
def _on_drawer_diff_loaded(self, result, subtitle: str,
                           drawer_revealer: Gtk.Revealer,
                           drawer_box: Gtk.Box, file_path: str) -> None:

# After:
def _on_drawer_diff_loaded(self, result, subtitle: str,
                           drawer_box: Gtk.Box, file_path: str) -> None:
```

**Update call sites:**
- Line 222: `_on_drawer_diff_loaded(result, subtitle, drawer_revealer, drawer_box, file_path)` → remove `drawer_revealer`
- Line 275: same

**ui/views/file_tree.py:580, 655** — Remove `drawer_revealer` from `_load_drawer_diff`:
```python
# Before:
def _load_drawer_diff(self, file_path: str, drawer_revealer: Gtk.Revealer,
                      drawer_box: Gtk.Box, project_path: str,
                      checkpoint_sha: str | None = None) -> None:

# After:
def _load_drawer_diff(self, file_path: str, drawer_box: Gtk.Box, project_path: str,
                      checkpoint_sha: str | None = None) -> None:
```

Update call sites (lines 822, 856).

---

### 7. Remove Dead Callback Branch

**ui/views/file_tree.py:1007** — Remove dead `elif self._on_file_selected:` branch:
```python
# Before:
else:
    # File row - toggle drawer
    if full_path in self._drawers:
        self._toggle_drawer(full_path)
    elif self._on_file_selected:
        self._on_file_selected(full_path)

# After:
else:
    # File row - toggle drawer
    if full_path in self._drawers:
        self._toggle_drawer(full_path)
```

Update docstring line 5:
```python
# Double-click a file to toggle its inline diff drawer.
# Fires on_file_selected(path) callback when a file without a drawer is activated.
```

---

### 8. Remove Duplicate Imports

**ui/views/file_tree.py:22**
```python
# Before:
from utils.git_ops import diff_file_against_working_tree, diff_working_tree, file_log, diff_file_against, file_log, diff_file_against

# After:
from utils.git_ops import diff_file_against_working_tree, diff_working_tree, file_log, diff_file_against
```

---

### 9. Remove Unused ReviewState Imports

**ui/views/file_tree.py:874, 964** — Remove these lines:
```python
# Remove these two lines:
# from models.review_state import ReviewState
```

---

## Rules (steelFramedCodeWriter.md)

- Read all target files in full before editing
- Verify every change with evidence (pytest output, grep, wc -l)
- No fabricated APIs
- Hard part first: verify imports still work after removal
- Wire it or delete it — no stubs

---

## Deliverable Expectations

```
Files changed:
- ui/views/file_tree.py
- ui/views/diff_viewer.py
- ui/views/diff_card.py
- ui/window.py

Verification:
grep -n "FileDiff\|_on_revert_complete\|drawer_revealer\|_on_revert_complete\|_on_file_selected" ui/views/file_tree.py ui/views/diff_viewer.py ui/views/diff_card.py
→ [should return 0 matches for dead items]

python3 -c "from ui.views.file_tree import FileTree; from ui.views.diff_viewer import DiffViewer; from ui.views.diff_card import render_diff_hunks; print('import ok')"
→ import ok

COMPLETENESS:
- [x] FileDiff imports removed from diff_card.py, diff_viewer.py, file_tree.py
- [x] _on_revert_complete_main_thread removed from diff_viewer.py
- [x] on_revert callback simplified (2-arg) in diff_viewer.py and window.py
- [x] drawer_revealer param removed from _load_drawer_diff and _on_drawer_diff_loaded
- [x] Dead elif self._on_file_selected branch removed from file_tree.py
- [x] Duplicate imports removed from file_tree.py
- [x] Unused ReviewState imports removed from file_tree.py
- [x] All tests pass under xvfb
```

---

## Word Marker

**please write**