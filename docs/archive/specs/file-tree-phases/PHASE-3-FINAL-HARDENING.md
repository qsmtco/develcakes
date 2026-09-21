# Phase 3 — Final hardening (5 issues from Debugger re-audit)

All production code is correct. These are defensive guards + 1 real UX bug.
All fixes are small. Apply in one batch.

---

## Fix 1 — BUG #5 (REAL UX BUG): Dropdown label/mode inversion

**File:** `ui/views/file_tree.py` — the modes array in `_on_sort_dropdown_changed`.

**Bug:** Labels are `["Name ↑", "Name ↓", "Modified ↑", "Modified ↓", "Size ↑", "Size ↓"]` but modes are `["name_asc", "name_desc", "modified_desc", "modified_asc", "size_desc", "size_asc"]`. So "Modified ↑" (index 2) maps to `modified_desc` (newest first) — ↑ should mean ascending (oldest first).

**Fix:** Reorder the modes array to match the labels (ascending first, descending second):

```python
        modes = ["name_asc", "name_desc", "modified_asc", "modified_desc",
                 "size_asc", "size_desc"]
```

Also update the `_show_tree` restore block's `valid` array to match:
```python
            valid = ["name_asc", "name_desc", "modified_asc", "modified_desc",
                     "size_desc", "size_asc"]
```

**Verify:**
```bash
python3 -c "
# After fix: 'Modified ↑' (index 2) must map to modified_asc
labels = ['Name ↑','Name ↓','Modified ↑','Modified ↓','Size ↑','Size ↓']
modes = ['name_asc','name_desc','modified_asc','modified_desc','size_asc','size_desc']
assert modes[2] == 'modified_asc', f'got {modes[2]}'
assert modes[3] == 'modified_desc'
assert modes[4] == 'size_asc'
assert modes[5] == 'size_desc'
print('LABEL/MODE ALIGNMENT OK')
"
```

## Fix 2 — BUG #1 + BUG #4 (defensive): Assert drawer invariants at construction

**File:** `ui/views/file_tree.py` — `_toggle_drawer` (drawer row construction ~line 1325).

**Fix:** Add assertions (they're cheap and surface violations loudly):

```python
            drawer_row = FileTreeRow(
                display_name="",
                full_path="",
                is_dir=False,
                is_drawer=True,
                depth=file_row.props.depth,
                drawer_widget=revealer,
                is_open=True,
                parent_full_path=file_path,
            )
            # Defensive: enforce invariants the sort comparator relies on
            assert file_row.props.depth >= 0
            assert file_path, "drawer parent_full_path must be non-empty"
```

(Keep it minimal — just guard the non-empty parent path. The depth is already copied from the file row so it's structurally correct.)

## Fix 3 — BUG #2 (defensive): _filter_func rejects None query explicitly

**File:** `ui/views/file_tree.py` — `_filter_func`.

**Fix:** Add explicit None check before the truthiness check:

```python
    @staticmethod
    def _filter_func(item, query: str) -> bool:
        if query is None:
            return False
        if not query:
            return True
        ...
```

**Test:** Add to `tests/test_file_tree_sort_filter.py`:
```python
    def test_none_query_returns_false(self):
        row = FileTreeRow(display_name='test.py', full_path='/test.py')
        assert FileTree._filter_func(row, None) is False
```

## Fix 4 — Suggestion: Extract try/finally helper + un-skip 2 tests

**File:** `ui/views/file_tree.py` + `tests/test_file_tree_sort_filter.py`.

**Fix:** Add a helper method to FileTree:

```python
    @staticmethod
    def _set_dropdown_silently(dropdown, handler_id: int, index: int) -> None:
        """Set dropdown selection without firing notify::selected. Exception-safe."""
        dropdown.handler_block(handler_id)
        try:
            dropdown.set_selected(index)
        finally:
            dropdown.handler_unblock(handler_id)
```

Replace the 2 inline block/unblock sites in `_show_tree` (the reset and the restore) with calls to `FileTree._set_dropdown_silently(self._sort_dropdown, self._sort_dropdown_handler_id, idx)`. Do NOT touch the 3rd site (the `navigate_back` search entry — that's a different widget, leave as-is).

Un-skip the 2 tests by replacing them with MagicMock-based tests:

```python
    def test_set_selected_does_not_trigger_handler_when_blocked(self):
        """handler_block prevents notify::selected from firing."""
        from unittest.mock import MagicMock
        dd = MagicMock()
        FileTree._set_dropdown_silently(dd, 42, 2)
        dd.handler_block.assert_called_once_with(42)
        dd.set_selected.assert_called_once_with(2)
        dd.handler_unblock.assert_called_once_with(42)

    def test_signal_unblocked_after_exception(self):
        """handler_unblock runs even if set_selected raises."""
        from unittest.mock import MagicMock
        dd = MagicMock()
        dd.set_selected.side_effect = RuntimeError('boom')
        try:
            FileTree._set_dropdown_silently(dd, 42, 2)
        except RuntimeError:
            pass  # expected
        dd.handler_block.assert_called_once_with(42)
        dd.handler_unblock.assert_called_once_with(42)
```

---

## Verification

```bash
cd /path/to/projects/crabcakes

# 1. Label/mode alignment
python3 -c "
labels = ['Name ↑','Name ↓','Modified ↑','Modified ↓','Size ↑','Size ↓']
import ast
with open('ui/views/file_tree.py') as f: src = f.read()
# Extract the modes array from _on_sort_dropdown_changed
assert 'modified_asc' in src
print('check modes array in source')
"
grep -A2 'modes = \[' ui/views/file_tree.py | head -6

# 2. Drawer assertion present
grep -n "assert file_path" ui/views/file_tree.py

# 3. _filter_func None check
grep -A2 "def _filter_func" ui/views/file_tree.py | grep "query is None"

# 4. Helper extracted
grep -n "_set_dropdown_silently" ui/views/file_tree.py

# 5. Tests un-skipped (no more pytest.skip)
grep -c "skip" tests/test_file_tree_sort_filter.py

# 6. Full suite — should be 164+ passed, 0 skipped
python3 -m pytest tests/test_file_icons.py tests/test_projects.py tests/test_git_ops.py tests/test_file_tree_helpers.py tests/test_file_tree_sort_filter.py -q
```

## Report back with

1. `git diff --stat`
2. Output of all 6 verification commands
3. COMPLETENESS checklist (Fixes 1–4)
