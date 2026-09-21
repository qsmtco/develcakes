# Phase 2 — Multi-Column View: 3 New Factories + 4-Column Layout + Git Status Stub

**Spec of record:** `docs/specs/SPEC-FILE-TREE-ENHANCEMENTS.md`
**Prerequisite:** Phase 1 complete and audited (BUG A-1, A-2 fixed).
**Target:** Add the Status/Size/Modified column factories and switch `_show_tree`
to a 4-column layout. The picker stays single-column. Wire git status via a
**stub** callback (the real handler comes in Phase 4).

**Do NOT add** sort/filter models, the sort dropdown widget, search debounce,
or `file_tree_handler.py` — those are Phase 3/4.

---

## Files to edit

1. `ui/views/file_tree.py` (MODIFY — add 3 factory classes, update `_show_tree`, update `_show_project_picker`, add stub callback attrs)
2. `tests/test_file_tree_columnview.py` (EXTEND — add tests for the 3 new factories if GTK permits; otherwise test the pure helpers they call)

## Tasks

### Task 1 — Add 3 new factory classes (§3.4.3)

Add these AFTER the existing `FileTreeFactory` class definition (before the
`FileTree` class). Each is a `Gtk.SignalListItemFactory` subclass with
`_on_setup` / `_on_bind` / `_on_unbind`.

```python
class FileTreeStatusFactory(Gtk.SignalListItemFactory):
    """Factory for the Status column — shows git status badge."""
    def __init__(self):
        super().__init__()
        self.connect('setup', self._on_setup)
        self.connect('bind', self._on_bind)
        self.connect('unbind', self._on_unbind)

    def _on_setup(self, factory, list_item):
        label = Gtk.Label()
        label.set_xalign(0.5)
        label.add_css_class("file-tree-status-badge")
        list_item.set_child(label)

    def _on_bind(self, factory, list_item):
        row = cast(FileTreeRow, list_item.get_item())
        label: Gtk.Label = list_item.get_child()
        display = row.props.git_status_display
        label.set_text(display)
        # Clear previous status class, add current
        for cls in list(label.get_css_classes()):
            if cls.startswith("file-tree-status-"):
                label.remove_css_class(cls)
        class_map = {
            "M": "file-tree-status-modified",
            "A": "file-tree-status-staged",
            "?": "file-tree-status-untracked",
            "D": "file-tree-status-deleted",
            "R": "file-tree-status-renamed",
            "!": "file-tree-status-ignored",
        }
        if display in class_map:
            label.add_css_class(class_map[display])

    def _on_unbind(self, factory, list_item):
        pass


class FileTreeSizeFactory(Gtk.SignalListItemFactory):
    """Factory for the Size column — right-aligned human-readable size."""
    def __init__(self):
        super().__init__()
        self.connect('setup', self._on_setup)
        self.connect('bind', self._on_bind)
        self.connect('unbind', self._on_unbind)

    def _on_setup(self, factory, list_item):
        label = Gtk.Label()
        label.set_xalign(1.0)
        label.add_css_class("file-tree-size-column")
        list_item.set_child(label)

    def _on_bind(self, factory, list_item):
        row = cast(FileTreeRow, list_item.get_item())
        label: Gtk.Label = list_item.get_child()
        label.set_text(row.props.file_size_display)

    def _on_unbind(self, factory, list_item):
        pass


class FileTreeModifiedFactory(Gtk.SignalListItemFactory):
    """Factory for the Modified column — right-aligned relative time."""
    def __init__(self):
        super().__init__()
        self.connect('setup', self._on_setup)
        self.connect('bind', self._on_bind)
        self.connect('unbind', self._on_unbind)

    def _on_setup(self, factory, list_item):
        label = Gtk.Label()
        label.set_xalign(1.0)
        label.add_css_class("file-tree-modified-column")
        list_item.set_child(label)

    def _on_bind(self, factory, list_item):
        row = cast(FileTreeRow, list_item.get_item())
        label: Gtk.Label = list_item.get_child()
        label.set_text(row.props.modified_display)

    def _on_unbind(self, factory, list_item):
        pass
```

### Task 2 — Update `_show_tree` to 4-column layout (§3.4.6)

In `_show_tree`, AFTER `self._clear_all_state()` and the header setup, but
BEFORE populating rows, **remove existing columns and add 4 new ones**:

```python
# Remove existing columns, add 4-column layout
for col in list(self._column_view.get_columns()):
    self._column_view.remove_column(col)

factory_name = FileTreeFactory(self)
col_name = Gtk.ColumnViewColumn.new("Name", factory_name)
col_name.set_expand(True)
self._column_view.append_column(col_name)

col_status = Gtk.ColumnViewColumn.new("Status", FileTreeStatusFactory())
col_status.set_fixed_width(60)
self._column_view.append_column(col_status)

col_size = Gtk.ColumnViewColumn.new("Size", FileTreeSizeFactory())
col_size.set_fixed_width(80)
self._column_view.append_column(col_size)

col_modified = Gtk.ColumnViewColumn.new("Modified", FileTreeModifiedFactory())
col_modified.set_fixed_width(100)
self._column_view.append_column(col_modified)
```

Also update the search visibility per BUG #3:
- `self._search_entry.set_visible(True)` (was False — search visible in BOTH modes)
- `self._search_entry.set_placeholder_text("Search files... (Esc to clear)")`

Also wire git status via the stub callback (added in Task 4):
```python
status_map: dict[str, str] = {}
if self._on_get_git_status:
    status_map = self._on_get_git_status() or {}
```
Then in the row-construction loop, compute the rel path and look up status:
```python
rel_path = os.path.relpath(full_path, path) if path else full_path
raw_status = status_map.get(rel_path, "")
```
Pass `git_status=raw_status` and `git_status_display=git_status_to_display(raw_status)`
to the `FileTreeRow(...)`. (The Phase 1 code hardcoded `git_status=""` — replace
those two lines with the real lookup.)

### Task 3 — Update `_show_project_picker` to single column (§3.4.7)

At the START of `_show_project_picker`, before building cards, remove all
columns and add a single Name column (picker uses cards, not the tree columns,
but the ColumnView must not carry stale tree columns into picker mode):

```python
# Single Name column for picker mode
for col in list(self._column_view.get_columns()):
    self._column_view.remove_column(col)
factory = FileTreeFactory(self)
col_name = Gtk.ColumnViewColumn.new("Name", factory)
col_name.set_expand(True)
self._column_view.append_column(col_name)
```

Also:
- `self._search_entry.set_visible(True)` (keep search visible in picker)
- `self._search_entry.set_placeholder_text("Search projects...")`
- (Sort dropdown is Phase 3 — do not add it here; just ensure it's hidden if
  it ever appears. For now there is no dropdown widget, so nothing to hide.)

### Task 4 — Add stub callback attributes to FileTree (§3.4.14)

In `FileTree.__init__`, after the existing callback attrs, add:

```python
# Callbacks to handler (Phase 4 wires these; Phase 2 uses git status stub)
self._on_get_git_status = None
```

Add setter methods near the other `set_on_*` methods:

```python
def set_on_get_git_status(self, cb):
    """Set callback to fetch git status dict {rel_path: code} from handler.
    Returns dict[str, str]. Called by _show_tree when populating root rows."""
    self._on_get_git_status = cb
```

Do NOT add `_on_sort_changed`, `_on_search_changed_tree`, `_on_expand_requested`,
or `_on_get_sort_mode` yet — those are Phase 3/4.

## What NOT to do in Phase 2

- Do NOT add the sort dropdown widget
- Do NOT add SortListModel / FilterListModel / CustomSorter / CustomFilter
- Do NOT create `file_tree_handler.py` or modify `left_panel.py`
- Do NOT change `_expand_directory` / `_on_directory_loaded` (Phase 1 already
  wired 5-tuple metadata into child rows — the new columns will auto-display)
- Do NOT update ARCHITECTURE.md yet

## Verification

```bash
cd /path/to/projects/crabcakes

# 1. Module imports cleanly (GTK available)
python3 -c "
import gi; gi.require_version('Gtk','4.0')
from gi.repository import Gtk
from ui.views.file_tree import (
    FileTreeStatusFactory, FileTreeSizeFactory, FileTreeModifiedFactory,
    FileTreeFactory, FileTree
)
print('all 4 factories + FileTree import OK')
"

# 2. Unit tests still green
python3 -m pytest tests/test_file_icons.py tests/test_projects.py tests/test_git_ops.py -q

# 3. git status lookup wired (grep the rel_path computation)
grep -n "status_map.get" ui/views/file_tree.py   # must show 1 match in _show_tree

# 4. 4-column setup present
grep -n 'Gtk.ColumnViewColumn.new("Status"' ui/views/file_tree.py   # 1 match
grep -n 'Gtk.ColumnViewColumn.new("Size"' ui/views/file_tree.py     # 1 match
grep -n 'Gtk.ColumnViewColumn.new("Modified"' ui/views/file_tree.py # 1 match

# 5. Picker is single-column (no Status/Size/Modified columns in picker path)
# Verify _show_project_picker has its own column setup, separate from _show_tree
```

## Report back with

1. `git diff --stat`
2. Output of all verification commands
3. COMPLETENESS checklist (Tasks 1–4)
