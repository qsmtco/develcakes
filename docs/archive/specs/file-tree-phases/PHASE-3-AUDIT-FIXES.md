# Phase 3 — Audit Fixes: 2 CRITICAL + 2 HIGH + 3 issues

**Source:** Debugger audit + Supervisor empirical probes.
**Empirical GTK4 binding reality (VERIFIED):**
- `Gtk.CustomSorter.new(fn)` → calls `fn(a, b, user_data)` — **3 args**, `user_data` is None
- `Gtk.CustomFilter.new(fn)` → calls `fn(item)` — **1 arg**, the item directly

The spec (BUG #19) was WRONG about the filter signature. Both signatures are now empirically confirmed.

---

## Fix 1 — BUG #1 (CRITICAL): Sort comparators need 3-arg signature

**File:** `ui/views/file_tree.py` — `_build_sorter` (6 comparator functions).

**Bug:** All 6 comparators are `def cmp_name_asc(a, b):` — but GTK4 calls `fn(a, b, user_data)`. TypeError → no sorting ever happens.

**Fix:** Add `user_data=None` (or `_ud=None`) as 3rd param to ALL 6 comparators:

```python
        def cmp_name_asc(a, b, _ud=None):
            ...
        def cmp_name_desc(a, b, _ud=None):
            ...
        def cmp_modified_desc(a, b, _ud=None):
            ...
        def cmp_modified_asc(a, b, _ud=None):
            ...
        def cmp_size_desc(a, b, _ud=None):
            ...
        def cmp_size_asc(a, b, _ud=None):
            ...
```

Change nothing else in the comparators. Just add the 3rd param.

## Fix 2 — BUG #2 (CRITICAL): Filter callback needs 1-arg signature

**File:** `ui/views/file_tree.py` — `_apply_filter` lambda + `_filter_func` signature.

**Bug:** The lambda is `(model, position, user_data)` but GTK4 calls `fn(item)`. TypeError → tree goes empty on any search.

**Fix:** Change BOTH the lambda and `_filter_func`:

### 2a. `_apply_filter` lambda (line ~733):

```python
        custom_filter = Gtk.CustomFilter.new(
            lambda item: FileTree._filter_func(item, query)
        )
```

### 2b. `_filter_func` signature + body (line ~745):

```python
    @staticmethod
    def _filter_func(item, query: str) -> bool:
        """Substring match on name + path. casefold() (BUG #12).
        Drawer rows pass through via parent_full_path (BUG #18, #26).
        Guards against None (BUG #24).
        """
        if not query:
            return True
        if item is None:  # BUG #24
            return False
        row = cast(FileTreeRow, item)
        q = query.casefold()
        if row.props.is_drawer:
            parent = row.props.parent_full_path or row.props.full_path
            return q in row.props.display_name.casefold() or q in parent.casefold()
        return (q in row.props.display_name.casefold() or
                q in row.props.full_path.casefold())
```

Key changes: `(model, position, query)` → `(item, query)`. Remove `model.get_item(position)` call. Change None guard from `return True` to `return False` (BUG #5 fix — see Fix 6).

## Fix 3 — P3-1 + BUG #3 (HIGH): Apply default sort + block signal during restore

**File:** `ui/views/file_tree.py` — `_show_tree` end (after `_init_sort_filter()`).

**Bug:** Default sort never applied when `_on_get_sort_mode` is None (P3-1). Programmatic `set_selected` triggers a signal feedback loop (BUG #3).

**Fix:** Store the dropdown signal handler id. Apply default sort unconditionally. Block the signal during restore.

### 3a. Store signal handler id in `__init__`:

In the sort dropdown setup, change:
```python
self._sort_dropdown.connect("notify::selected", self._on_sort_dropdown_changed)
```
to:
```python
self._sort_dropdown_handler_id = self._sort_dropdown.connect(
    "notify::selected", self._on_sort_dropdown_changed)
```

### 3b. In `_show_tree`, replace the restore block:

```python
        self._init_sort_filter()
        # Always apply default sort first (P3-1 fix)
        self._apply_sort("name_asc")
        # Restore saved mode if handler provides one (block signal to avoid feedback loop — BUG #3)
        if self._on_get_sort_mode:
            saved = self._on_get_sort_mode()
            valid = ["name_asc", "name_desc", "modified_desc", "modified_asc",
                     "size_desc", "size_asc"]
            if saved in valid:
                idx = valid.index(saved)
                self._sort_dropdown.handler_block(self._sort_dropdown_handler_id)
                self._sort_dropdown.set_selected(idx)
                self._sort_dropdown.handler_unblock(self._sort_dropdown_handler_id)
                self._apply_sort(saved)
```

## Fix 4 — BUG #4 (HIGH): Drawer rows break sort — sort to position 0

**File:** `ui/views/file_tree.py` — `_build_sorter` (6 comparators).

**Bug:** Drawer rows have `display_name=""` and sort to position 0 (or end for desc), breaking the drawer-below-file invariant.

**Fix:** In ALL 6 comparators, add an `is_drawer` check that keeps drawer rows after their parent. The simplest approach: drawer rows should sort as if they are NOT sortable — place them after files but maintain relative order. Since the drawer always has an empty display_name, the safest fix is to treat drawers like directories (sort them together, after files) is WRONG (they'd appear before files).

The correct fix: drawer rows should be **pinned to sort immediately after their parent**. Since CustomSorter doesn't support pinning, the simplest robust approach is: **exclude drawer rows from sort comparison entirely** by making them sort equal to everything (return 0 for any comparison involving a drawer). This means drawers stay in their store-insertion order relative to each other, which is correct because they're always inserted immediately after their parent.

Add this check at the TOP of each comparator, before the is_dir check:

```python
        # Drawer rows: do not re-order (stay at insertion position, which is
        # directly below their parent). Return 0 = "equal" so the stable sort
        # keeps their relative position. (BUG #4 fix)
        if a.props.is_drawer or b.props.is_drawer:
            return 0
```

This makes drawers invisible to the sorter. They stay where they were inserted.

## Fix 5 — BUG #6 (issue): Remove dead stale-check in _on_sort_dropdown_changed

**File:** `ui/views/file_tree.py` — `_on_sort_dropdown_changed`.

**Bug:** The `_sort_changed_count` generation counter check is dead code — it can never fire in synchronous GTK4 execution.

**Fix:** Remove the counter logic entirely. Simplify the handler:

```python
    def _on_sort_dropdown_changed(self, dropdown, pspec):
        """Handle sort selection — update sort model + notify handler."""
        selected = dropdown.get_selected()
        modes = ["name_asc", "name_desc", "modified_desc", "modified_asc",
                 "size_desc", "size_asc"]
        mode = modes[selected] if 0 <= selected < len(modes) else "name_asc"
        self._apply_sort(mode)
        if self._on_sort_changed:
            self._on_sort_changed(mode)
```

Remove `self._sort_changed_count` from `__init__` too (it's no longer used).

## Fix 6 — BUG #5 (issue): _filter_func None guard returns False (already in Fix 2b)

Already handled in Fix 2b: the None guard returns `False`, not `True`.

## Fix 7 — BUG #7 (issue): Reset dropdown on project switch

**File:** `ui/views/file_tree.py` — `_show_tree`, before `_init_sort_filter()`.

**Fix:** Reset the dropdown to index 0 (default) at the start of `_show_tree`'s sort setup, with signal blocked:

```python
        # Reset dropdown to default before restoring (BUG #7 fix)
        self._sort_dropdown.handler_block(self._sort_dropdown_handler_id)
        self._sort_dropdown.set_selected(0)
        self._sort_dropdown.handler_unblock(self._sort_dropdown_handler_id)
```

(This happens before the `_init_sort_filter` + `_apply_sort("name_asc")` + conditional restore in Fix 3b.)

---

## CRITICAL: Add integration tests for sort and filter

The reason BUG #1 and BUG #2 shipped is that there are ZERO tests for the new sort/filter code. Add `tests/test_file_tree_sort_filter.py` (NEW):

```python
import pytest
import gi; gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gio, GObject
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ui.views.file_tree import FileTree, FileTreeRow, format_size, format_mtime

class TestComparators:
    """Test that the 6 sort comparators actually sort correctly via GTK4."""

    def _make_rows(self):
        rows = []
        for name in ['cherry', 'apple', 'banana']:
            r = FileTreeRow(display_name=name, full_path=f'/{name}', is_dir=False)
            rows.append(r)
        return rows

    def test_name_asc_sorts(self):
        rows = self._make_rows()
        store = Gio.ListStore.new(FileTreeRow.__gtype__)
        for r in rows: store.append(r)
        sorter = FileTree._build_sorter.__func__(None, "name_asc")  # call unbound
        # Actually need an instance-free call. Use a helper:
        # _build_sorter is an instance method but doesn't use self.
        # Call it via a dummy approach:
        class Dummy: pass
        result = FileTree._build_sorter(Dummy(), "name_asc")
        smodel = Gtk.SortListModel.new(store, result)
        names = [smodel.get_item(i).props.display_name for i in range(smodel.get_n_items())]
        assert names == ['apple', 'banana', 'cherry'], f'got {names}'

    def test_name_desc_sorts(self):
        # ... similar, expect ['cherry', 'banana', 'apple']
        ...

    def test_dirs_sort_before_files(self):
        # add a dir and a file, verify dir comes first
        ...

    def test_drawer_rows_not_reordered(self):
        # insert a drawer row between files, verify it stays in place after sort
        ...

class TestFilterFunc:
    def test_substring_match(self):
        row = FileTreeRow(display_name='hello.py', full_path='/src/hello.py')
        assert FileTree._filter_func(row, 'hello') is True
        assert FileTree._filter_func(row, 'HELLO') is True  # casefold
        assert FileTree._filter_func(row, 'xyz') is False

    def test_none_returns_false(self):
        assert FileTree._filter_func(None, 'query') is False

    def test_empty_query_returns_true(self):
        row = FileTreeRow(display_name='test.py', full_path='/test.py')
        assert FileTree._filter_func(row, '') is True
```

Adapt as needed — the key is to create REAL SortListModel/FilterListModel instances and verify they actually sort/filter. These tests use GTK4 but NOT a display server (ListStore + SortListModel work headless).

## Verification (run ALL)

```bash
cd /path/to/projects/crabcakes

# 1. Comparators have 3 params
python3 -c "
import ast, sys
with open('ui/views/file_tree.py') as f: tree = ast.parse(f.read())
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name.startswith('cmp_'):
        args = [a.arg for a in node.args.args]
        assert len(args) == 3, f'{node.name} has {args}'
print('all comparators have 3 params')
"

# 2. _filter_func takes (item, query) not (model, position, query)
grep -n "def _filter_func" ui/views/file_tree.py

# 3. Sort actually works via GTK4
python3 -c "
import gi; gi.require_version('Gtk','4.0')
from gi.repository import Gtk, Gio, GObject
from ui.views.file_tree import FileTree, FileTreeRow
store = Gio.ListStore.new(FileTreeRow.__gtype__)
for n in ['cherry','apple','banana']:
    store.append(FileTreeRow(display_name=n, full_path='/'+n, is_dir=False))
class D: pass
sorter = FileTree._build_sorter(D(), 'name_asc')
smodel = Gtk.SortListModel.new(store, sorter)
names = [smodel.get_item(i).props.display_name for i in range(smodel.get_n_items())]
assert names == ['apple','banana','cherry'], names
print('SORT WORKS:', names)
"

# 4. Filter actually works via GTK4
python3 -c "
import gi; gi.require_version('Gtk','4.0')
from gi.repository import Gtk, Gio
from ui.views.file_tree import FileTree, FileTreeRow
store = Gio.ListStore.new(FileTreeRow.__gtype__)
for n in ['apple.py','banana.py','cherry.py']:
    store.append(FileTreeRow(display_name=n, full_path='/'+n, is_dir=False))
cf = Gtk.CustomFilter.new(lambda item: FileTree._filter_func(item, 'ban'))
fmodel = Gtk.FilterListModel.new(store, cf)
n = fmodel.get_n_items()
assert n == 1, f'expected 1, got {n}'
assert fmodel.get_item(0).props.display_name == 'banana.py'
print('FILTER WORKS: 1 match')
"

# 5. Signal handler id stored
grep -n "_sort_dropdown_handler_id" ui/views/file_tree.py

# 6. Dead counter removed
grep -c "_sort_changed_count" ui/views/file_tree.py  # should be 0

# 7. New sort/filter tests pass
python3 -m pytest tests/test_file_tree_sort_filter.py -q

# 8. Full suite
python3 -m pytest tests/test_file_icons.py tests/test_projects.py tests/test_git_ops.py tests/test_file_tree_helpers.py tests/test_file_tree_sort_filter.py -q
```

## Report back with

1. `git diff --stat`
2. Output of all 8 verification commands
3. COMPLETENESS checklist (Fixes 1–7)
4. **The new test file must create REAL SortListModel + FilterListModel instances — not mock them.** If these tests segfault in the sandbox, report it but keep the tests (they'll pass in the real environment).
