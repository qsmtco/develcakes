# Sort Architecture Fix — Audit Fixes (5 bugs)

**Source:** Debugger audit on the SortListModel removal fix.
3 real bugs + 2 cleanup items.

---

## Fix 1 — BUG #1 + #10 (CRITICAL): Drawers split sibling groups

**File:** `ui/views/file_tree.py` — `_sort_store_in_place` + drawer construction.

**Bug:** Drawers have `parent_full_path="/proj/src/b.py"` (the file's path), but the file's siblings have `parent_full_path="/proj/src"` (the directory). When `_sort_store_in_place` groups by `parent_full_path`, the drawer splits the directory's children into two groups. After sort, siblings after the drawer end up in a separate group.

**Fix:** Give drawer rows the SAME `parent_full_path` as their parent file (the containing directory), not the file's own path. Then exclude drawers from group sorting — they must stay adjacent to their file by insertion order.

### 1a. In `_toggle_drawer` (~line 1334), change the drawer construction:

Find:
```python
                parent_full_path=file_path,
```
Change to:
```python
                parent_full_path=file_row.props.parent_full_path,
```

This makes the drawer's `parent_full_path` match its file's `parent_full_path` (the directory), so it stays in the same sibling group.

### 1b. In `_sort_store_in_place`, exclude drawers from the group sort — they stay at insertion position:

In the group-sorting loop, BEFORE sorting each group, split out drawers:
```python
            group = all_items[i:j]
            # Drawers stay at insertion position (adjacent to their file).
            # Only sort non-drawer items within the group.
            non_drawers = [item for item in group if not item.props.is_drawer]
            drawers = [item for item in group if item.props.is_drawer]
            non_drawers.sort(key=functools.cmp_to_key(self._make_group_comparator()))
            # Re-interleave: for each file, check if a drawer follows it
            # (by matching parent file identity). Drawers keep their relative
            # position to their file.
            sorted_group: list[FileTreeRow] = []
            di = 0  # drawer index
            for item in non_drawers:
                sorted_group.append(item)
                # Attach any drawers whose parent file is this item
                while di < len(drawers):
                    # A drawer's file is identified by: the drawer was inserted
                    # right after its file. We match by checking if this item's
                    # full_path corresponds to the drawer's original position.
                    # Since we don't store the drawer's file path on the drawer
                    # anymore (we changed parent_full_path), we need another way.
                    # Store the drawer's file path in the drawer's full_path property
                    # (which is currently "" for drawers).
                    # Actually — drawers already have full_path="" — let's use a
                    # different approach.
                    break
            sorted_items.extend(sorted_group)
```

**SIMPLER APPROACH:** Since `_toggle_drawer` always inserts the drawer immediately after its file, and the file+drawer are in the same sibling group, just sort non-drawer items and re-insert drawers after their corresponding file. To know which file a drawer belongs to, we need the drawer to store its file's `full_path`. Use the drawer's `full_path` property (currently empty `""`):

### 1c. In `_toggle_drawer`, set the drawer's `full_path` to the file's path:

Find the drawer construction (~line 1325-1335):
```python
            drawer_row = FileTreeRow(
                display_name="",
                full_path="",                    # ← currently empty
                ...
                parent_full_path=file_row.props.parent_full_path,  # ← Fix 1a
            )
```
Change `full_path=""` to `full_path=file_path` (the file's path). This lets `_sort_store_in_place` match drawers to their file.

### 1d. Rewrite the group sort in `_sort_store_in_place` to handle drawers:

```python
            group = all_items[i:j]
            # Separate drawers from regular items
            non_drawers = [item for item in group if not item.props.is_drawer]
            drawers = [item for item in group if item.props.is_drawer]
            # Sort non-drawer items
            non_drawers.sort(key=functools.cmp_to_key(self._make_group_comparator()))
            # Re-insert drawers right after their parent file
            sorted_group: list[FileTreeRow] = []
            for item in non_drawers:
                sorted_group.append(item)
                # Attach drawers belonging to this file
                for d in drawers:
                    if d.props.full_path == item.props.full_path:
                        sorted_group.append(d)
            sorted_items.extend(sorted_group)
```

## Fix 2 — BUG #2 (HIGH): Selection drifts across splice

**File:** `ui/views/file_tree.py` — `_sort_store_in_place`.

**Bug:** `splice()` reorders items. `SingleSelection` tracks by position. After splice, position N points to a different item.

**Fix:** Save selected item before splice, restore after:

```python
    def _sort_store_in_place(self) -> None:
        if self._store.get_n_items() == 0:
            return

        # Save selection by object identity (BUG #2 fix)
        selected_row = None
        if self._selection:
            pos = self._selection.get_selected()
            if pos >= 0 and self._filter_model and pos < self._filter_model.get_n_items():
                selected_row = self._filter_model.get_item(pos)

        # ... (existing extract + sort + splice logic) ...

        self._store.splice(0, self._store.get_n_items(), sorted_items)

        # Restore selection by object identity (BUG #2 fix)
        if selected_row is not None and self._filter_model:
            for i in range(self._filter_model.get_n_items()):
                if self._filter_model.get_item(i) is selected_row:
                    self._selection.set_selected(i)
                    break
```

## Fix 3 — BUG #4 (HIGH): Race condition with background expand

**File:** `ui/views/file_tree.py` — `_expand_directory` + `_on_directory_loaded`.

**Bug:** `_expand_directory` captures `row_index` at call time. If `_sort_store_in_place` runs before `_on_directory_loaded`, the parent row moved. Children inserted at wrong position.

**Fix:** Capture the parent row object, find its current index in the callback:

In `_expand_directory`, the callback already receives `row_index`. Change `_on_directory_loaded` to re-find the parent row by object identity:

In `_on_directory_loaded`, AFTER removing the loading row and BEFORE the stale-request check, re-find the parent:
```python
        # BUG #4: Re-find parent row by object identity (sort may have moved it)
        parent_row: FileTreeRow | None = None
        # The parent is the row that was at row_index when _expand_directory was called.
        # We need to find it by scanning for the expanded directory.
        # Use loading_row's depth to find the parent: parent is at depth = loading_row.depth - 1
        # Actually — pass the parent row object through the callback.
```

**Better fix:** Pass the parent row OBJECT through the closure (not just the index). In `_expand_directory`, capture `parent_row_obj = row` and pass it:

```python
    def _expand_directory(self, row_index: int) -> None:
        ...
        row: FileTreeRow = self._store.get_item(row_index)
        ...
        parent_row_obj = row  # capture object identity (BUG #4)

        def _do():
            ...
            GLib.idle_add(lambda: self._on_directory_loaded(
                entries, _loading_row, parent_row_obj, parent_depth, request_id
            ))
```

Then in `_on_directory_loaded`, change the signature to accept the row object and find its current index:
```python
    def _on_directory_loaded(self, entries, loading_row: FileTreeRow, parent_row_obj: FileTreeRow, parent_depth: int, request_id: int) -> None:
        ...
        # Remove loading row (existing code)
        ...

        # BUG #4: Find parent's CURRENT index (sort may have moved it)
        row_index = self._find_row_index(parent_row_obj)
        if row_index is None:
            return  # parent was removed (project switch or collapse)
        parent_row = parent_row_obj
        if not parent_row.props.is_dir or not parent_row.props.expanded:
            return
        ...
```

Update the `_on_directory_loaded` call in `_do()` to pass `parent_row_obj` instead of `row_index`.

## Fix 4 — BUG #8 (LOW): Remove dead `make_key` function

**File:** `ui/views/file_tree.py` — inside `_sort_store_in_place`.

The `make_key` function is defined but never called. Remove it entirely.

## Fix 5 — BUG #9 (LOW): Remove duplicate @staticmethod

**File:** `ui/views/file_tree.py` — `_set_dropdown_silently`.

Remove the duplicate `@staticmethod` decorator (keep only one).

---

## Verification

```bash
cd /path/to/projects/crabcakes

# 1. Drawer parent_full_path matches file's parent (not file's path)
grep -A2 "parent_full_path=file_row" ui/views/file_tree.py

# 2. Drawer full_path set to file_path
grep "full_path=file_path" ui/views/file_tree.py

# 3. Selection save/restore in _sort_store_in_place
grep "selected_row" ui/views/file_tree.py

# 4. Parent row object passed through closure
grep "parent_row_obj" ui/views/file_tree.py

# 5. No dead make_key
grep -c "def make_key" ui/views/file_tree.py  # should be 0

# 6. No duplicate @staticmethod
grep -A1 "@staticmethod" ui/views/file_tree.py | grep -c "@staticmethod"  # each block should have 1

# 7. Tests pass
python3 -m pytest tests/test_file_icons.py tests/test_projects.py tests/test_git_ops.py tests/test_file_tree_helpers.py tests/test_file_tree_sort_filter.py tests/test_file_tree_handler.py -q
```

## Report back with

1. `git diff --stat`
2. Output of all 7 verification commands
3. COMPLETENESS checklist (Fixes 1–5)
