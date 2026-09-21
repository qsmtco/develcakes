# Phase A.1 Instructions: ui/views/file_tree.py — Remaining Bug Fixes

**Spec:** SPEC-INLINE-FILE-TREE-DIFF-DRAWER.md (follow-up to Phase A)
**Phase:** A.1 of F (bug fixes from Debugger audit)
**Target file:** 1 file (ui/views/file_tree.py)

---

## Changes Required

### 1. BUG #5 (MEDIUM) — Fix Pango Markup Injection in `_show_tree`

**Location:** Line ~381 (in `_show_tree` method)

**Current code:**
```python
self._title_lbl.set_markup(f"<b>{name}</b>")
```

**Fix:**
```python
from utils.escaping import escape_for_pango

# At top of file, add import:
from utils.escaping import escape_for_pango

# In _show_tree:
safe_name = escape_for_pango(name)
self._title_lbl.set_markup(f"<b>{safe_name}</b>")
```

---

### 2. BUG #6 (MEDIUM) — Add Drawer Cleanup in `_show_project_picker`

**Location:** Start of `_show_project_picker` method (line ~190)

**Add at start of `_show_project_picker`:**
```python
# Clear drawer state before replacing content
for revealer, _, _ in self._drawers.values():
    self._drawer_area.remove(revealer)
self._drawers.clear()
```

This prevents memory leak when switching from tree mode back to project picker (e.g., during search).

---

### 3. Additional Cleanup — Remove Dead Code

**Location:** `_on_row_activated` (line ~493)

**Current code:**
```python
else:
    # File row - toggle drawer if we have one, otherwise fire callback
    if full_path in self._drawers:
        self._toggle_drawer(full_path)
    elif self._on_file_selected:
        self._on_file_selected(full_path)
```

**Fix:** Remove the dead `elif` branch since every file now has a drawer:
```python
else:
    # File row - toggle drawer
    if full_path in self._drawers:
        self._toggle_drawer(full_path)
```

Also update docstring line 5 to match:
```python
# File tree widget — GTK4 TreeView with lazy-loading directory expansion.
# Double-click a file to toggle its inline diff drawer.
# Fires on_file_selected(path) callback when a file without a drawer is activated.
```

---

### 4. Remove Dead State — `_page` Field

**Location:** `__init__` (line ~41) and `set_page` method (line ~163)

**Remove:**
- `self._page = None` in `__init__`
- Entire `set_page` method (lines 163-165)

---

### 5. Remove Dead State — `is_loaded` TreeStore Column

**Location:** TreeStore creation (line ~89)

**Change:**
```python
# Before:
self._store = Gtk.TreeStore.new([str, str, bool, bool])

# After:
self._store = Gtk.TreeStore.new([str, str, bool])  # display_name, full_path, is_dir
```

**Update all `append()` calls** to pass 3 values instead of 4:
- Line ~365: `[prefix + entry_name, full_path, is_dir]` (remove `False`)
- Line ~375: `["…", "", True]` (remove `False`)
- Line ~530: `[prefix + entry_name, full_path, is_dir]` (remove `False`)
- Line ~535: `["…", "", True]` (remove `False`)

---

### 6. Fix `lstrip` Misuse in `_update_drawer_prefix`

**Location:** Line ~474 in `_on_row_activated`

**Current:**
```python
display_name = model.get_value(it, 0).lstrip("📁 ").lstrip("  ").lstrip("▶ ").lstrip("▼ ")
```

**Fix:**
```python
current = model.get_value(it, 0)
for prefix in ("📁 ", "  ", "▶ ", "▼ "):
    if current.startswith(prefix):
        current = current[len(prefix):]
        break
name_part = current
```

---

## Rules (steelFramedCodeWriter.md)

- Read `ui/views/file_tree.py` in full before editing
- Verify every change with evidence (pytest output, grep, wc -l)
- No fabricated APIs — use existing `escape_for_pango`, `scan_directory`, `Gtk.Revealer`
- Hard part first: drawer cleanup + Pango escape + TreeStore column removal
- Wire it or delete it — no stubs

---

## Deliverable Expectations

```
Files changed:
- ui/views/file_tree.py:XX-YY (multiple fixes)

Verification:
grep -n "escape_for_pango\|_drawer_area.remove\|self._drawers.clear\|self._page" ui/views/file_tree.py
→ [paste output]
wc -l ui/views/file_tree.py
→ [paste output]
python3 -c "from ui.views.file_tree import FileTree; print('import ok')"
→ import ok

COMPLETENESS:
- [x] BUG #5: Pango markup escape in _show_tree
- [x] BUG #6: Drawer cleanup in _show_project_picker
- [x] Dead code: _page field and set_page method removed
- [x] Dead state: is_loaded TreeStore column removed
- [x] Dead code: unreachable elif in _on_row_activated removed
- [x] lstrip misuse fixed in _update_drawer_prefix
- [x] Docstring updated to match behavior
```

---

## Word Marker

**please write**