# FIX: Diff drawer squeezed by Status/Size/Modified columns

## Problem
The diff drawer (drawer row) is confined to the Name column's cell. Status/Size/Modified columns consume 240px globally, squeezing the drawer.

## Root cause
GTK4 ColumnView renders each cell per-column. Column hiding is global (per-column), not per-row. The drawer content lives only in the Name column cell.

## Fix approach
When ANY drawer is open, hide the Status/Size/Modified columns globally. When ALL drawers are closed, show them again. The Name column (`set_expand(True)`) fills the freed space.

## File: `ui/views/file_tree.py`

### Task 1: Store column references on self

In `_show_tree`, after creating the 4 columns, store references:
```python
        self._col_status = col_status
        self._col_size = col_size
        self._col_modified = col_modified
```

Initialize them in `__init__`:
```python
        self._col_status = None
        self._col_size = None
        self._col_modified = None
```

### Task 2: Add helper to show/hide extra columns

```python
    def _update_column_visibility_for_drawers(self) -> None:
        """Hide Status/Size/Modified columns when any drawer is open (drawer width fix)."""
        any_open = len(self._drawer_paths) > 0
        for col in (self._col_status, self._col_size, self._col_modified):
            if col is not None:
                col.set_visible(not any_open)
```

### Task 3: Call the helper in _toggle_drawer

After opening a drawer (after `self._drawer_paths[file_path] = drawer_row`), call:
```python
        self._update_column_visibility_for_drawers()
```

After closing a drawer (after removing from `_drawer_paths`), call:
```python
        self._update_column_visibility_for_drawers()
```

### Task 4: Reset on _show_tree

At the end of `_show_tree`, ensure columns are visible (no drawers open yet):
```python
        self._update_column_visibility_for_drawers()
```

## Verification

```bash
cd /path/to/projects/crabcakes

# 1. Column refs stored
grep "_col_status\|_col_size\|_col_modified" ui/views/file_tree.py

# 2. Helper exists
grep "_update_column_visibility_for_drawers" ui/views/file_tree.py

# 3. Called in _toggle_drawer
grep -n "_update_column_visibility" ui/views/file_tree.py

# 4. Tests pass
python3 -m pytest tests/test_file_icons.py tests/test_projects.py tests/test_git_ops.py tests/test_file_tree_helpers.py tests/test_file_tree_sort_filter.py tests/test_file_tree_handler.py -q
```

## Report COMPLETENESS checklist.
