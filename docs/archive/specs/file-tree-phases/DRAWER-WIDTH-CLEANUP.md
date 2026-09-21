# Drawer Width Fix — Audit Cleanup (1 item)

## Fix — _clear_all_state missing helper call

**File:** `ui/views/file_tree.py` — `_clear_all_state` (~line 583).

**Issue:** `_clear_all_state` clears `_drawer_paths` but doesn't call `_update_column_visibility_for_drawers`. Latent fragility — currently harmless because the next `_show_tree` or `_show_project_picker` corrects it, but a future caller could expose it.

**Fix:** Add the helper call after `_drawer_paths.clear()` in `_clear_all_state`. The helper's `None` guard makes it safe even when columns aren't created yet (picker mode).

```python
        self._drawer_paths.clear()
        self._loaded_drawers.clear()
        self._last_toggle_per_file.clear()
        self._update_column_visibility_for_drawers()
```

## Verification

```bash
cd /path/to/projects/crabcakes
grep -A1 "_drawer_paths.clear" ui/views/file_tree.py | head -4
python3 -m pytest tests/test_file_icons.py tests/test_projects.py tests/test_git_ops.py tests/test_file_tree_helpers.py tests/test_file_tree_sort_filter.py tests/test_file_tree_handler.py -q
```

Report COMPLETENESS.
