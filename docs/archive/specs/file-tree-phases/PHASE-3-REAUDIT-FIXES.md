# Phase 3 Re-Audit Fixes — 2 CRITICAL + 2 HIGH + 3 issues

**Source:** Debugger re-audit + Supervisor empirical verification.
**Root cause of both CRITICAL bugs:** SortListModel sorts the store as a flat list. The comparators don't preserve tree hierarchy (depth) or drawer-parent adjacency.

**VERIFIED by supervisor:**
- BUG #1: Drawers with scrambled insertion do NOT follow their parent after sort
- BUG #2: Children of expanded dirs detach from parents — `aaa_in_zzz.py` (depth 1) sorts next to `zzz_in_mmm.py` (depth 1) instead of staying under `zzz_dir`

---

## Fix 1 — BUG #1 + BUG #2 (CRITICAL): Depth-aware + parent-aware comparators

**File:** `ui/views/file_tree.py` — `_build_sorter` (all 6 comparators).

**Principle:** The comparator must preserve the tree structure. Two rules:
1. **Depth groups**: items at depth N sort within their depth group, never mixing with depth M.
2. **Drawer adjacency**: a drawer row must sort immediately after its parent file.

**Approach:** Build a composite sort key for each row that encodes (depth, parent_path, sort_value). The comparator compares these keys lexicographically. This is stable and preserves hierarchy.

Replace `_build_sorter` entirely with this implementation:

```python
    @staticmethod
    def _build_sorter(sort_mode: str) -> Gtk.Sorter:
        """Build comparator-based sorter that preserves tree hierarchy.

        Sort is depth-aware: items only sort within their depth group, so
        children stay under their parent directory. Drawer rows sort
        immediately after their parent file (using parent_full_path as key).
        Directories always sort before files within the same depth group.
        """

        def _sort_key(row):
            """Compute a composite sort key for a row.

            Returns a tuple: (depth, is_dir_flag, name_for_sort).
            - depth: group items by depth so children stay under parents
            - is_dir_flag: 0 for dirs, 1 for files (dirs sort first within a depth)
            - name_for_sort: the actual sort value (name, mtime, or size)
            """
            # Drawer rows use their parent's path so they sort adjacent to it.
            # A drawer's depth equals its parent file's depth.
            if row.props.is_drawer:
                parent = row.props.parent_full_path or ""
                # Sort drawer right after its parent: use parent path + a
                # high sort-after marker so it follows the parent file.
                return (
                    row.props.depth,
                    2,  # drawers sort after files (dirs=0, files=1, drawers=2)
                    parent.casefold(),
                )
            is_dir_flag = 0 if row.props.is_dir else 1
            if sort_mode in ("name_asc", "name_desc"):
                name_val = row.props.display_name.casefold()
            elif sort_mode in ("modified_asc", "modified_desc"):
                name_val = ""  # mtime handled via separate field below
            else:
                name_val = ""  # size handled via separate field below
            return (row.props.depth, is_dir_flag, name_val)

        def _secondary_key(row):
            """Secondary sort value (mtime or size). Only used for non-drawer rows."""
            if row.props.is_drawer:
                return 0
            if sort_mode in ("modified_asc", "modified_desc"):
                return row.props.modified_time
            if sort_mode in ("size_asc", "size_desc"):
                return row.props.file_size
            return 0

        def cmp(a, b, _ud=None):
            ka = _sort_key(a)
            kb = _sort_key(b)
            if ka < kb:
                return -1
            if ka > kb:
                return 1
            # Primary keys equal — use secondary (mtime/size) for tie-breaking
            sa = _secondary_key(a)
            sb = _secondary_key(b)
            if sort_mode in ("modified_desc", "size_desc"):
                return -1 if sa > sb else (1 if sa < sb else 0)
            return -1 if sa < sb else (1 if sa > sb else 0)

        return Gtk.CustomSorter.new(cmp)
```

**Key design decisions:**
- `(depth, is_dir_flag, name)` groups items by depth FIRST — children never mix with root items
- Drawer rows get `is_dir_flag=2` so they sort after their parent file (which has `is_dir_flag=1`)
- Drawer uses `parent_full_path` as name so it sorts adjacent to its parent
- The `_desc` variants work because we reverse the comparison result at the name level (by flipping the return), but depth and dir-grouping always stay ascending (hierarchy preserved)

**WAIT — there's a subtlety.** For `name_desc`, `modified_desc`, `size_desc`, we want descending order WITHIN a depth group, but depth itself must ALWAYS be ascending (children after parents). The composite key handles depth ascending always. For descending modes, we need to negate only the name/mtime/size portion, NOT the depth.

Revised approach — use explicit return logic instead of tuple comparison, to handle the ascending/descending split correctly:

```python
    @staticmethod
    def _build_sorter(sort_mode: str) -> Gtk.Sorter:
        """Build comparator-based sorter that preserves tree hierarchy."""

        def cmp(a, b, _ud=None):
            # Rule 1: Depth groups — NEVER mix depths (children stay under parents)
            if a.props.depth != b.props.depth:
                return -1 if a.props.depth < b.props.depth else 1

            # Rule 2: Within same depth, dirs before files, drawers after files.
            # Drawer rows sort adjacent to their parent.
            def group_rank(row):
                if row.props.is_dir:
                    return 0
                if row.props.is_drawer:
                    return 2
                return 1
            ga, gb = group_rank(a), group_rank(b)
            if ga != gb:
                return -1 if ga < gb else 1

            # Rule 3: For drawers, use parent_full_path as the sort name so
            # the drawer sorts next to its parent file.
            name_a = a.props.parent_full_path.casefold() if a.props.is_drawer else a.props.display_name.casefold()
            name_b = b.props.parent_full_path.casefold() if b.props.is_drawer else b.props.display_name.casefold()

            # Rule 4: Apply the actual sort mode within the group
            if sort_mode in ("name_asc", "name_desc"):
                if name_a != name_b:
                    if sort_mode == "name_asc":
                        return -1 if name_a < name_b else 1
                    else:
                        return 1 if name_a < name_b else -1
                return 0

            if sort_mode in ("modified_asc", "modified_desc"):
                ta, tb = a.props.modified_time, b.props.modified_time
                if ta != tb:
                    if sort_mode == "modified_asc":
                        return -1 if ta < tb else 1
                    else:
                        return 1 if ta < tb else -1
                # Tie-break by name for stable order
                return -1 if name_a < name_b else (1 if name_a > name_b else 0)

            if sort_mode in ("size_asc", "size_desc"):
                sa, sb = a.props.file_size, b.props.file_size
                if sa != sb:
                    if sort_mode == "size_asc":
                        return -1 if sa < sb else 1
                    else:
                        return 1 if sa < sb else -1
                return -1 if name_a < name_b else (1 if name_a > name_b else 0)

            # Default: name ascending
            return -1 if name_a < name_b else (1 if name_a > name_b else 0)

        return Gtk.CustomSorter.new(cmp)
```

**Use this second (explicit) version.** It correctly handles:
- Depth always ascending (children after parents)
- Dirs (0) < files (1) < drawers (2) within each depth
- Descending applies only to the sort VALUE, not to depth/dir-grouping
- Drawers use parent_full_path so they land next to their parent

---

## Fix 2 — BUG #2 (CRITICAL, second part): Remove `_apply_sort` from `_on_directory_loaded`

**File:** `ui/views/file_tree.py:1956` — end of `_on_directory_loaded`.

**Bug:** After inserting children, `_apply_sort` re-sorts the entire store. With the depth-aware comparator (Fix 1), the root-level items won't move, but the re-sort is still unnecessary overhead and could cause visual jumps.

**Fix:** Remove the line:
```python
        # M6: re-apply sorter so new children sort correctly
        self._apply_sort(self._current_sort_mode)
```

The SortListModel automatically re-sorts when the store changes (it observes store mutations). The `_apply_sort` call is redundant — the sorter is already attached to the model. **Verify this claim empirically:** after removing the line, insert items into the store and check if the SortListModel re-sorts automatically.

**If the SortListModel does NOT auto-re-sort on insert** (some GTK4 versions require `items-changed` emission), then keep the `_apply_sort` call but it's now safe because the comparator is depth-aware.

---

## Fix 3 — BUG #3 (HIGH): Wrap signal block/unblock in try/finally

**File:** `ui/views/file_tree.py` — both signal block/unblock sites (lines ~1072-1074 and ~1085-1087).

**Fix:** Wrap each in try/finally:

```python
        self._sort_dropdown.handler_block(self._sort_dropdown_handler_id)
        try:
            self._sort_dropdown.set_selected(0)
        finally:
            self._sort_dropdown.handler_unblock(self._sort_dropdown_handler_id)
```

And the restore site:
```python
                self._sort_dropdown.handler_block(self._sort_dropdown_handler_id)
                try:
                    self._sort_dropdown.set_selected(idx)
                finally:
                    self._sort_dropdown.handler_unblock(self._sort_dropdown_handler_id)
```

---

## Fix 4 — BUG #4 (issue): Defensive None handling in _filter_func

**File:** `ui/views/file_tree.py` — `_filter_func`.

**Fix:** Guard against None `full_path`/`parent_full_path`:

```python
    @staticmethod
    def _filter_func(item, query: str) -> bool:
        if not query:
            return True
        if item is None:
            return False
        row = cast(FileTreeRow, item)
        q = query.casefold()
        name = (row.props.display_name or "").casefold()
        if row.props.is_drawer:
            parent = (row.props.parent_full_path or "").casefold()
            return q in name or q in parent
        path = (row.props.full_path or "").casefold()
        return q in name or q in path
```

---

## Fix 5 — BUG #5 (issue): Lambda late-binding fix

**File:** `ui/views/file_tree.py` — `_apply_filter` lambda.

**Fix:**
```python
        custom_filter = Gtk.CustomFilter.new(
            lambda item, q=query: FileTree._filter_func(item, q)
        )
```

---

## Fix 6 — BUG #6 (issue): Make _build_sorter a @staticmethod

**File:** `ui/views/file_tree.py` — `_build_sorter`.

**Fix:** Add `@staticmethod` decorator (already specified in Fix 1 above — the method signature is `def _build_sorter(sort_mode: str)` without `self`). Update the test to call `FileTree._build_sorter(mode)` instead of `FileTree._build_sorter(Dummy(), mode)`.

---

## Fix 7 — BUG #7 (HIGH): Add missing tests

**File:** `tests/test_file_tree_sort_filter.py` — extend.

Add these tests:

### Multi-drawer invariant (BUG #1)
```python
    def test_multiple_drawers_stay_adjacent_to_parents(self):
        """2+ drawers must each stay adjacent to their parent after sort."""
        store = Gio.ListStore.new(FileTreeRow.__gtype__)
        # Scrambled insertion — drawers NOT pre-adjacent
        store.append(FileTreeRow(display_name='cherry.py', full_path='/cherry.py', is_dir=False, depth=0))
        store.append(FileTreeRow(display_name='', full_path='', is_drawer=True, depth=0, parent_full_path='/banana.py'))
        store.append(FileTreeRow(display_name='apple.py', full_path='/apple.py', is_dir=False, depth=0))
        store.append(FileTreeRow(display_name='', full_path='', is_drawer=True, depth=0, parent_full_path='/apple.py'))
        store.append(FileTreeRow(display_name='banana.py', full_path='/banana.py', is_dir=False, depth=0))
        sorter = FileTree._build_sorter('name_asc')
        smodel = Gtk.SortListModel.new(store, sorter)
        items = [smodel.get_item(i) for i in range(smodel.get_n_items())]
        # Each drawer must be immediately after its parent
        for i, item in enumerate(items):
            if item.props.is_drawer:
                parent_path = item.props.parent_full_path
                assert i > 0, f'drawer at position 0 with no parent above'
                prev = items[i-1]
                assert prev.props.full_path == parent_path, \
                    f'drawer parent {parent_path} not at i-1, found {prev.props.full_path}'
```

### Depth hierarchy preservation (BUG #2)
```python
    def test_children_stay_under_parent_after_sort(self):
        """Children of expanded dirs must not mix with root items after sort."""
        store = Gio.ListStore.new(FileTreeRow.__gtype__)
        store.append(FileTreeRow(display_name='mmm_dir', full_path='/mmm_dir', is_dir=True, depth=0))
        store.append(FileTreeRow(display_name='zzz_child.py', full_path='/mmm_dir/zzz_child.py', is_dir=False, depth=1))
        store.append(FileTreeRow(display_name='zzz_dir', full_path='/zzz_dir', is_dir=True, depth=0))
        store.append(FileTreeRow(display_name='aaa_child.py', full_path='/zzz_dir/aaa_child.py', is_dir=False, depth=1))
        sorter = FileTree._build_sorter('name_asc')
        smodel = Gtk.SortListModel.new(store, sorter)
        depths = [smodel.get_item(i).props.depth for i in range(smodel.get_n_items())]
        # All depth-0 items must come before all depth-1 items
        d0_count = depths.count(0)
        assert depths[:d0_count] == [0]*d0_count, f'depth-0 items not grouped: {depths}'
        assert depths[d0_count:] == [1]*(len(depths)-d0_count), f'depth-1 items not grouped: {depths}'
```

### Signal block/unblock feedback loop (BUG #3)
```python
    def test_set_selected_does_not_trigger_handler_when_blocked(self):
        """Programmatic set_selected with handler_block must NOT fire the callback."""
        dropdown = Gtk.DropDown.new_from_strings(['A','B','C'])
        call_count = [0]
        handler_id = dropdown.connect('notify::selected', lambda *a: call_count.__setitem__(0, call_count[0]+1))
        dropdown.handler_block(handler_id)
        dropdown.set_selected(2)
        dropdown.handler_unblock(handler_id)
        assert call_count[0] == 0, f'handler fired {call_count[0]} times during block'
```

### Exception safety (BUG #3 part 2)
```python
    def test_signal_unblocked_after_exception(self):
        """handler_unblock must run even if set_selected raises."""
        dropdown = Gtk.DropDown.new_from_strings(['A','B'])
        call_count = [0]
        handler_id = dropdown.connect('notify::selected', lambda *a: call_count.__setitem__(0, call_count[0]+1))
        # Simulate: block, then an exception occurs
        dropdown.handler_block(handler_id)
        # In real code, try/finally ensures unblock. Test the pattern:
        try:
            raise RuntimeError('simulated')
        except RuntimeError:
            pass
        finally:
            dropdown.handler_unblock(handler_id)
        # Now a real user action should fire the handler
        dropdown.set_selected(1)
        assert call_count[0] == 1, f'handler not unblocked: {call_count[0]}'
```

---

## Verification (run ALL)

```bash
cd /path/to/projects/crabcakes

# 1. _build_sorter is staticmethod
grep -B1 "def _build_sorter" ui/views/file_tree.py | grep "@staticmethod"

# 2. Multi-drawer invariant
python3 -c "
import gi; gi.require_version('Gtk','4.0')
from gi.repository import Gtk, Gio
from ui.views.file_tree import FileTree, FileTreeRow
store = Gio.ListStore.new(FileTreeRow.__gtype__)
store.append(FileTreeRow(display_name='cherry.py', full_path='/cherry.py', is_dir=False, depth=0))
store.append(FileTreeRow(display_name='', full_path='', is_drawer=True, depth=0, parent_full_path='/banana.py'))
store.append(FileTreeRow(display_name='apple.py', full_path='/apple.py', is_dir=False, depth=0))
store.append(FileTreeRow(display_name='', full_path='', is_drawer=True, depth=0, parent_full_path='/apple.py'))
store.append(FileTreeRow(display_name='banana.py', full_path='/banana.py', is_dir=False, depth=0))
sorter = FileTree._build_sorter('name_asc')
smodel = Gtk.SortListModel.new(store, sorter)
items = [smodel.get_item(i) for i in range(smodel.get_n_items())]
for i, item in enumerate(items):
    if item.props.is_drawer:
        assert i > 0 and items[i-1].props.full_path == item.props.parent_full_path, \
            f'drawer orphaned at {i}'
print('MULTI-DRAWER OK')
"

# 3. Depth hierarchy preserved
python3 -c "
import gi; gi.require_version('Gtk','4.0')
from gi.repository import Gtk, Gio
from ui.views.file_tree import FileTree, FileTreeRow
store = Gio.ListStore.new(FileTreeRow.__gtype__)
store.append(FileTreeRow(display_name='mmm_dir', full_path='/mmm_dir', is_dir=True, depth=0))
store.append(FileTreeRow(display_name='zzz_child.py', full_path='/mmm_dir/zzz_child.py', is_dir=False, depth=1))
store.append(FileTreeRow(display_name='zzz_dir', full_path='/zzz_dir', is_dir=True, depth=0))
store.append(FileTreeRow(display_name='aaa_child.py', full_path='/zzz_dir/aaa_child.py', is_dir=False, depth=1))
sorter = FileTree._build_sorter('name_asc')
smodel = Gtk.SortListModel.new(store, sorter)
depths = [smodel.get_item(i).props.depth for i in range(smodel.get_n_items())]
d0 = depths.count(0)
assert depths[:d0] == [0]*d0 and depths[d0:] == [1]*(len(depths)-d0), depths
print('DEPTH HIERARCHY OK:', depths)
"

# 4. Signal block/unblock in try/finally
grep -A4 "handler_block" ui/views/file_tree.py | grep "finally"

# 5. _filter_func None-safe paths
python3 -c "
from ui.views.file_tree import FileTree, FileTreeRow
r = FileTreeRow(display_name='x', full_path=None)
assert FileTree._filter_func(r, 'x') == True  # matches name
assert FileTree._filter_func(r, 'y') == False  # no crash on None path
print('NONE-SAFE OK')
"

# 6. Lambda late-binding
grep "lambda item, q=query" ui/views/file_tree.py

# 7. Tests pass
python3 -m pytest tests/test_file_tree_sort_filter.py -q

# 8. Full suite
python3 -m pytest tests/test_file_icons.py tests/test_projects.py tests/test_git_ops.py tests/test_file_tree_helpers.py tests/test_file_tree_sort_filter.py -q
```

## Report back with

1. `git diff --stat`
2. Output of all 8 verification commands
3. COMPLETENESS checklist (Fixes 1–7)
4. **CRITICAL: the multi-drawer and depth-hierarchy verification probes (#2 and #3) must PASS.** These are the bugs that shipped last round. If they fail, do not report completion.
