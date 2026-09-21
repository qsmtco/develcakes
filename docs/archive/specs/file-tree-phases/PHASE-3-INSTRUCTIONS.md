# Phase 3 — Sort/Filter Model Chain + Sort Dropdown + Search Debounce

**Spec of record:** `docs/specs/SPEC-FILE-TREE-ENHANCEMENTS.md` (§3.4.5, §3.4.9, §3.4.10, §3.4.11)
**Prerequisite:** Phase 2 complete and audited (P2-1, P2-2, P2-3 fixed).
**Target:** Add the SortListModel + FilterListModel chain (in-place mutation),
the sort dropdown widget, 6 comparators, and the debounced tree search.

**Do NOT create** `file_tree_handler.py` or modify `left_panel.py` — that is Phase 4.

---

## Architecture

```
Gio.ListStore (store)           ← rows inserted/removed here (existing)
    └── Gtk.SortListModel        ← created ONCE, set_sorter() in-place (BUG #11)
        └── Gtk.FilterListModel  ← created ONCE, set_filter() in-place (BUG #11)
            └── Gtk.SingleSelection (selection) ← repointed ONCE in _init_sort_filter
                └── Gtk.ColumnView (view)
```

The selection model is currently created in `__init__` pointing at `self._store`.
Phase 3's `_init_sort_filter` must repoint it to `self._filter_model`.

---

## Tasks

### Task 1 — Add sort/filter model state to `__init__`

After the existing `self._on_get_git_status = None` line, add:

```python
# Phase 3: Sort/filter model chain (lives in view — uses Gtk types)
self._sort_model: Gtk.SortListModel | None = None
self._filter_model: Gtk.FilterListModel | None = None
self._sort_dropdown = None  # created in _build_header
self._current_sort_mode = "name_asc"  # tracked for re-apply on subtree expand
self._sort_changed_count = 0  # BUG #34: generation counter for sort signals
self._search_timeout_id = None  # BUG #9: tree search debounce timeout

# Phase 3: Callbacks to handler (Phase 4 wires these)
self._on_sort_changed = None
self._on_get_sort_mode = None
```

### Task 2 — Add sort dropdown widget to header

After the `_search_entry` setup and before `self._header.append(self._search_entry)`,
add the sort dropdown construction. Then append it to the header AFTER the search
entry but BEFORE the copy status label:

```python
# Phase 3: Sort dropdown — visible only in tree mode
self._sort_dropdown = Gtk.DropDown.new_from_strings([
    "Name ↑", "Name ↓", "Modified ↑", "Modified ↓", "Size ↑", "Size ↓"
])
self._sort_dropdown.set_selected(0)
self._sort_dropdown.set_valign(Gtk.Align.CENTER)
self._sort_dropdown.add_css_class("file-tree-sort-dropdown")
self._sort_dropdown.set_visible(False)  # hidden until _show_tree
self._sort_dropdown.connect("notify::selected", self._on_sort_dropdown_changed)
```

Append order in header: `_back_btn`, `_folder_icon`, `_title_lbl`, `_search_entry`,
`_sort_dropdown`, `_tree_copy_status_label`. (Add `_sort_dropdown` append after
the `_search_entry` append.)

### Task 3 — Add `_init_sort_filter()` method

This creates the model chain ONCE and repoints the selection. Call it at the END
of `_show_tree` (after the row-population loop) and do NOT call it in
`_show_project_picker` (picker uses cards, not the model chain).

```python
def _init_sort_filter(self) -> None:
    """Create SortListModel + FilterListModel chain once, repoint selection."""
    self._sort_model = Gtk.SortListModel.new(self._store, None)
    self._filter_model = Gtk.FilterListModel.new(self._sort_model, None)
    self._selection.set_model(self._filter_model)
```

**IMPORTANT:** `Gtk.SortListModel.new()` and `Gtk.FilterListModel.new()` take the
underlying model as first arg and a sorter/filter as second (None = no sorting/
filtering initially). Verify the exact GTK4 Python signatures — they may require
`Gio.ListStore` or `GListModel` interface. If the constructor differs, adapt.

### Task 4 — Add `_apply_sort()` method (in-place mutation — BUG #11)

```python
def _apply_sort(self, sort_mode: str) -> None:
    """In-place sorter change. Tracks _current_sort_mode for re-apply (M6)."""
    self._current_sort_mode = sort_mode
    if self._sort_model is None:
        return
    sorter = self._build_sorter(sort_mode)
    self._sort_model.set_sorter(sorter)
```

### Task 5 — Add `_build_sorter()` with 6 comparators (§3.4.9, BUG #31/#32)

```python
def _build_sorter(self, sort_mode: str) -> Gtk.Sorter:
    """Build comparator-based sorter. Directories always sort before files."""
    def cmp_name_asc(a, b):
        if a.props.is_dir != b.props.is_dir:
            return -1 if a.props.is_dir else 1
        a_n = a.props.display_name.casefold()
        b_n = b.props.display_name.casefold()
        return -1 if a_n < b_n else (1 if a_n > b_n else 0)

    def cmp_name_desc(a, b):
        if a.props.is_dir != b.props.is_dir:
            return -1 if a.props.is_dir else 1
        a_n = a.props.display_name.casefold()
        b_n = b.props.display_name.casefold()
        return 1 if a_n < b_n else (-1 if a_n > b_n else 0)

    def cmp_modified_desc(a, b):
        if a.props.is_dir != b.props.is_dir:
            return -1 if a.props.is_dir else 1
        return (b.props.modified_time - a.props.modified_time)

    def cmp_modified_asc(a, b):
        if a.props.is_dir != b.props.is_dir:
            return -1 if a.props.is_dir else 1
        return (a.props.modified_time - b.props.modified_time)

    def cmp_size_desc(a, b):
        if a.props.is_dir != b.props.is_dir:
            return -1 if a.props.is_dir else 1
        return (b.props.file_size - a.props.file_size)

    def cmp_size_asc(a, b):
        if a.props.is_dir != b.props.is_dir:
            return -1 if a.props.is_dir else 1
        return (a.props.file_size - b.props.file_size)

    comparators = {
        "name_asc": cmp_name_asc,
        "name_desc": cmp_name_desc,
        "modified_desc": cmp_modified_desc,
        "modified_asc": cmp_modified_asc,
        "size_desc": cmp_size_desc,
        "size_asc": cmp_size_asc,
    }
    fn = comparators.get(sort_mode, cmp_name_asc)
    return Gtk.CustomSorter.new(fn)
```

**NOTE:** `Gtk.CustomSorter.new(fn)` — the `fn` receives `(a, b)` in GTK4 Python
bindings. Verify this with a quick import test. If the signature differs (e.g.
requires 3 args with user_data), adapt accordingly.

### Task 6 — Add `_apply_filter()` method (in-place — BUG #11, BUG #19)

```python
def _apply_filter(self, query: str) -> None:
    """In-place filter change. casefold() for Unicode-safe match (BUG #12)."""
    if self._filter_model is None:
        return
    if not query:
        self._filter_model.set_filter(None)
        return
    custom_filter = Gtk.CustomFilter.new(
        lambda model, position, user_data: FileTree._filter_func(model, position, query)
    )
    self._filter_model.set_filter(custom_filter)
```

**CRITICAL (BUG #19):** `Gtk.CustomFilter.new()` callback signature in GTK4
Python is `(model, position, user_data)` → call `model.get_item(position)` to
get the row. NOT `(item, user_data)`. Verify with a GTK import test.

### Task 7 — Add `_filter_func()` static method (BUG #12, #18, #24, #26)

```python
@staticmethod
def _filter_func(model, position: int, query: str) -> bool:
    """Substring match on name + path. casefold() (BUG #12).
    Drawer rows pass through via parent_full_path (BUG #18, #26).
    Guards against None from get_item (BUG #24).
    """
    if not query:
        return True
    item = model.get_item(position)
    if item is None:  # BUG #24: race on concurrent mutation
        return True
    row = cast(FileTreeRow, item)
    q = query.casefold()
    if row.props.is_drawer:
        # BUG #26: drawer rows filter with their parent file
        parent = row.props.parent_full_path or row.props.full_path
        return q in row.props.display_name.casefold() or q in parent.casefold()
    return (q in row.props.display_name.casefold() or
            q in row.props.full_path.casefold())
```

### Task 8 — Add sort dropdown handler (§3.4.10, BUG #34)

```python
def _on_sort_dropdown_changed(self, dropdown, pspec):
    """Handle sort selection — update sort model + notify handler (BUG #34)."""
    self._sort_changed_count += 1
    request_id = self._sort_changed_count
    selected = dropdown.get_selected()
    modes = ["name_asc", "name_desc", "modified_desc", "modified_asc",
             "size_desc", "size_asc"]
    mode = modes[selected] if 0 <= selected < len(modes) else "name_asc"
    self._apply_sort(mode)
    if request_id != self._sort_changed_count:
        return  # stale — project switched during sort
    if self._on_sort_changed:
        self._on_sort_changed(mode)
```

### Task 9 — Add debounced tree search (§3.4.11, BUG #9)

Replace the Phase 2 stub guard in `_on_search_changed` with the real tree routing:

```python
def _on_search_changed(self, entry):
    """Route search to picker or tree handler."""
    if self._project_path is not None:
        # Tree mode — debounced filter
        self._on_search_changed_tree_cb(entry.get_text())
    else:
        # Picker mode — existing behavior
        query = entry.get_text()
        if self._project_list_handler:
            self._project_list_handler.search(query)
            self._show_project_picker()

def _on_search_changed_tree_cb(self, query: str) -> None:
    """Debounced tree search. 150ms via GLib.timeout_add (BUG #9)."""
    if self._search_timeout_id is not None:
        GLib.source_remove(self._search_timeout_id)
    def _apply():
        self._apply_filter(query)
        # BUG #35: update match count in placeholder
        if self._filter_model and self._store:
            count = self._filter_model.get_n_items()
            total = self._store.get_n_items()
            if query and total > 0:
                if count == 0:
                    self._search_entry.set_placeholder_text("No matches")
                else:
                    self._search_entry.set_placeholder_text(f"{count} of {total} files")
        self._search_timeout_id = None
    self._search_timeout_id = GLib.timeout_add(150, _apply)
```

### Task 10 — Cancel search timeout in `_clear_all_state` (BUG #9)

In `_clear_all_state`, BEFORE the existing `self._current_request_id += 1`, add:

```python
        # BUG #9: cancel outstanding search timeout
        if self._search_timeout_id is not None:
            try:
                GLib.source_remove(self._search_timeout_id)
            except Exception:
                pass
            self._search_timeout_id = None
```

Also clear the sort/filter models:
```python
        self._sort_model = None
        self._filter_model = None
```

### Task 11 — Wire sort dropdown visibility + restore in `_show_tree`

In `_show_tree`, after the 4-column layout setup:
- `self._sort_dropdown.set_visible(True)`

At the END of `_show_tree` (after the row-population loop):
- Call `self._init_sort_filter()` (M6 — must be after store is populated)
- Restore saved sort mode:
```python
        if self._on_get_sort_mode:
            saved = self._on_get_sort_mode()
            valid = ["name_asc", "name_desc", "modified_desc", "modified_asc",
                     "size_desc", "size_asc"]
            if saved in valid:
                idx = valid.index(saved)
                self._sort_dropdown.set_selected(idx)
                self._apply_sort(saved)
```

In `_show_project_picker`:
- `self._sort_dropdown.set_visible(False)`

### Task 12 — Re-apply sort in `_on_directory_loaded` (M6)

At the END of `_on_directory_loaded` (after the child-insertion loop), add:
```python
        # M6: re-apply sorter so new children sort correctly
        self._apply_sort(self._current_sort_mode)
```

### Task 13 — Add setter methods

Near the other `set_on_*` methods:
```python
def set_on_sort_changed(self, cb):
    """Set callback for sort mode changes. cb(mode_str)."""
    self._on_sort_changed = cb

def set_on_get_sort_mode(self, cb):
    """Set callback to fetch saved sort mode. cb() -> str."""
    self._on_get_sort_mode = cb
```

---

## What NOT to do in Phase 3

- Do NOT create `file_tree_handler.py`
- Do NOT modify `left_panel.py`
- Do NOT update ARCHITECTURE.md
- Do NOT change `scan_directory` or `GitResult`
- Do NOT modify the Status/Size/Modified factories

## Verification

```bash
cd /path/to/projects/crabcakes

# 1. Module imports cleanly with all new GTK types
python3 -c "
import gi; gi.require_version('Gtk','4.0')
from gi.repository import Gtk
from ui.views.file_tree import FileTree
# Verify methods exist
assert hasattr(FileTree, '_init_sort_filter')
assert hasattr(FileTree, '_apply_sort')
assert hasattr(FileTree, '_apply_filter')
assert hasattr(FileTree, '_build_sorter')
assert hasattr(FileTree, '_on_sort_dropdown_changed')
assert hasattr(FileTree, '_on_search_changed_tree_cb')
assert hasattr(FileTree, '_filter_func')
print('all methods present')
"

# 2. CustomSorter + CustomFilter signatures (GTK4 Python binding check)
python3 -c "
import gi; gi.require_version('Gtk','4.0')
from gi.repository import Gtk
# Verify CustomSorter.new exists and accepts a callable
import inspect
print('CustomSorter.new' in dir(Gtk))
print('CustomFilter.new' in dir(Gtk))
print('SortListModel' in dir(Gtk))
print('FilterListModel' in dir(Gtk))
"

# 3. Full test suite green
python3 -m pytest tests/test_file_icons.py tests/test_projects.py tests/test_git_ops.py tests/test_file_tree_helpers.py -q

# 4. Sort dropdown wired
grep -n "_sort_dropdown" ui/views/file_tree.py | head -10

# 5. Search timeout cancelled in _clear_all_state
grep -n "_search_timeout_id" ui/views/file_tree.py

# 6. M6: _init_sort_filter called at end of _show_tree, _apply_sort at end of _on_directory_loaded
grep -n "_init_sort_filter\|_apply_sort" ui/views/file_tree.py
```

## Report back with

1. `git diff --stat`
2. Output of all 6 verification commands
3. COMPLETENESS checklist (Tasks 1–13)
4. **GTK4 binding notes:** report the exact signature of `Gtk.CustomSorter.new()` and `Gtk.CustomFilter.new()` as observed in this environment — if they differ from `(fn)` and `(fn, user_data)`, note it.
