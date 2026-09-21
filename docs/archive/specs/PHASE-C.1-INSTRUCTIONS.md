# Phase C.1 Instructions: Fix Critical Bugs from Phase C Audit

**Spec:** SPEC-INLINE-FILE-TREE-DIFF-DRAWER.md (follow-up to Phase C)
**Phase:** C.1 of F (critical bug fixes from Debugger audit)
**Target file:** 1 file (ui/views/file_tree.py)

---

## Changes Required

### 1. BUG #1 (CRITICAL) — History Tab Empty Due to Separator Mismatch

**Location:** Line ~623 in `_on_history_loaded`

**Current (WRONG):**
```python
parts = line.split("\x00")
```

**Fix:** The producer (`git_ops.file_log`) emits NUL (\x00) separated output, but the file_log function now re-formats output to use \x1f as separator. The view code correctly uses \x00 split. Wait - let me check...

Actually looking at the code:
- `git_ops.file_log` emits lines with `\x1f` separator (the function re-formats from NUL to \x1f)
- But the view code at line 623 uses `split("\x00")` - WRONG!

**Fix:** Change to `split("\x1f")`:
```python
parts = line.split("\x1f")
```

---

### 2. BUG #2 (CRITICAL) — Revert Calls Wrong Handler

**Location:** Line ~780 in `_on_drawer_revert_confirmed`

**Current (WRONG):**
```python
self._project_handler.revert_file_to_sha(self._project_name, file_path, target_sha)
```

**Fix:** The `revert_file_to_sha` method is on `ReviewHandler`, not `ProjectHandler`. Need to either:
- Add a delegating method on `ProjectHandler` that forwards to `ReviewHandler`, OR
- Wire `ReviewHandler` to `FileTree` and call it directly

Since the architecture uses `ProjectHandler` as the main entry point, add a delegating method on `ProjectHandler`:

**In `ui/handlers/project_handler.py` - add method:**
```python
def revert_file_to_sha(self, project_name: str, file_path: str, target_sha: str) -> None:
    """Revert a single file to its state at an arbitrary commit SHA.
    Delegates to ReviewHandler which implements the actual revert logic."""
    if self._review_handler:
        self._review_handler.revert_file_to_sha(project_name, file_path, target_sha)
    else:
        # No review handler available - could raise or log
        pass
```

**Then in `file_tree.py`:**
```python
self._project_handler.revert_file_to_sha(self._project_name, file_path, target_sha)
```

---

### 3. BUG #3 (HIGH) — `_loaded_drawers` Not Cleared

**Locations:** 
- `navigate_back()` method (around line ~146)
- `_show_tree()` method (around line ~363)

**Fix:** Add `self._loaded_drawers.clear()` after `self._drawers.clear()` in both methods:
```python
# In navigate_back():
self._drawers.clear()
self._loaded_drawers.clear()  # ADD THIS

# In _show_tree():
self._drawers.clear()
self._loaded_drawers.clear()  # ADD THIS
```

---

### 4. BUG #4 (HIGH) — Over-Aggressive Debounce

**Location:** Line ~830 in `_toggle_drawer`

**Current (WRONG):**
```python
now = time.monotonic()
if now - getattr(self, '_last_toggle_time', 0) < 0.3:
    return
self._last_toggle_time = now
```

**Fix:** Key debounce per file_path, not per instance:
```python
now = time.monotonic()
last = getattr(self, '_last_toggle_per_file', {}).get(file_path, 0)
if now - last < 0.3:
    return
self._last_toggle_per_file = getattr(self, '_last_toggle_per_file', {})
self._last_toggle_per_file[file_path] = now
```

---

### 5. BUG #5 (HIGH) — Empty History Placeholder Is Clickable

**Location:** `_on_history_loaded` around line ~642

**Current:** Empty state creates a `Gtk.Label` directly in ListBox - clickable!

**Fix:** Wrap placeholder in non-activatable ListBoxRow:
```python
if not entries:
    row = Gtk.ListBoxRow()
    row.set_activatable(False)
    row.set_selectable(False)
    placeholder = Gtk.Label(label="No commit history for this file.")
    placeholder.set_halign(Gtk.Align.CENTER)
    placeholder.set_valign(Gtk.Align.CENTER)
    placeholder.add_css_class("diff-viewer-subtitle")
    row.set_child(placeholder)
    history_list.append(row)
    return
```

And in row-activated handler, add guard:
```python
def _on_history_row_activated(self, listbox, row):
    if not isinstance(row, Gtk.ListBoxRow) or not row.get_activatable():
        return
    # ... rest of handler
```

---

### 6. Cleanup — Remove Dead Imports

**Line ~23:** Remove duplicate imports:
```python
# Current (WRONG):
from utils.git_ops import diff_file_against_working_tree, diff_working_tree, file_log, diff_file_against, file_log, diff_file_against

# FIX:
from utils.git_ops import diff_file_against_working_tree, diff_working_tree, file_log, diff_file_against
```

**Line ~24:** Remove unused `FileDiff` import:
```python
# Current:
from utils.diff_parser import parse_diff, FileDiff
# FIX:
from utils.diff_parser import parse_diff
```

**Lines ~815, ~850:** Remove unused `ReviewState` imports inside try blocks (they're imported but never used).

---

## Rules (steelFramedCodeWriter.md)

- Read `ui/views/file_tree.py` in full before editing
- Verify every change with evidence (pytest output, grep, wc -l)
- No fabricated APIs — use existing `ProjectHandler`, `ReviewHandler`, `file_log`, etc.
- Hard part first: fix critical bugs first
- Wire it or delete it — no stubs

---

## Deliverable Expectations

```
Files changed:
- ui/views/file_tree.py: multiple fixes
- ui/handlers/project_handler.py: add revert_file_to_sha delegation

Verification:
grep -n "split.*x1f\|split.*x00" ui/views/file_tree.py
grep -n "_loaded_drawers.clear" ui/views/file_tree.py
grep -n "revert_file_to_sha" ui/handlers/project_handler.py
python3 -c "from ui.views.file_tree import FileTree; print('import ok')"
xvfb-run -a pytest tests/test_* -q --tb=line 2>&1 | tail -10

COMPLETENESS:
- [x] BUG #1: History separator fixed (split \x1f)
- [x] BUG #2: Revert delegates to ReviewHandler via ProjectHandler
- [x] BUG #3: _loaded_drawers cleared on navigate_back and _show_tree
- [x] BUG #4: Per-file debounce (not instance-wide)
- [x] BUG #5: Empty history placeholder non-clickable
- [x] Dead imports removed
- [x] All tests pass under xvfb
```

---

## Word Marker

**please write**