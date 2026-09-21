# Phase A Instructions: ui/views/file_tree.py — Drawer Skeleton

**Spec:** SPEC-INLINE-FILE-TREE-DIFF-DRAWER.md (§2.1, §5 Phase A)
**Phase:** A of F (drawer skeleton: revealer, button, expand/collapse)
**Target file:** 1 file (ui/views/file_tree.py)

---

## Changes Required

### 1. Add Drawer Data Structures to FileTree

**Add to `__init__` (after line ~50):**
```python
# Drawer state: tree_path_str -> (revealer, button, drawer_box, is_loading)
self._drawers = {}
```

### 2. Modify File Row to Include Toggle Button

**In `_show_tree()` (around line 330-340), where file rows are created:**

Current row append:
```python
parent = self._store.append(None, [
    prefix + entry_name, full_path, is_dir, False
])
```

**Change to:** Add a button column to the row data, or create the button separately and attach to the row.

**Better approach (GTK4 TreeView pattern):**
- Keep TreeStore columns as-is (display_name, full_path, is_dir, is_loaded)
- Add a dict `self._file_row_buttons = {}` mapping `tree_path` → button widget
- After appending row, create button and attach via `Gtk.CellRenderer` or post-process

**Simpler approach:** Use `Gtk.TreeViewColumn` with a `Gtk.CellRendererToggle` or custom renderer for the button. But GTK4 TreeView doesn't easily support buttons in cells.

**Alternative (recommended):** Use a `Gtk.ListBox` instead of `Gtk.TreeView` for file rows. But that's a major refactor.

**Pragmatic approach for Phase A:** Add the drawer row as a *child row* in the TreeStore (hidden by default), and add a button to the file row's display name via a custom cell renderer or by appending to the row's box after creation.

**Actually, looking at the code:** The file tree uses `Gtk.TreeView` with `Gtk.TreeStore`. The rows are created in `_show_tree()` and `_on_row_expanded()`. The display is a `Gtk.CellRendererText` on a `Gtk.TreeViewColumn`.

**Best approach for GTK4:** Add a second `Gtk.TreeViewColumn` with a `Gtk.CellRendererPixbuf` or custom renderer for the toggle button. But GTK4 CellRenderer doesn't support interactive buttons well.

**Alternative:** Don't put button in the tree view. Instead, add the drawer as an expander row that shows on double-click or via a context menu. But spec says button at right edge.

**Phase A scope:** Let's implement the **drawer row structure** first (revealer + drawer box as child rows in TreeStore), and add a simple expander arrow in the file name (like directories have). The "button" can be a `Gtk.CellRendererText` showing "▶"/"▼" that responds to clicks via a custom handler.

**Simpler still:** Use the existing expander for directories. For files, add a "drawer expander" column. When clicked, toggle the drawer row below.

**Let's go with this approach:**

1. Add a 5th column to TreeStore: `drawer_visible` (bool)
2. Add a 6th column: `drawer_box` (object) - store the revealer widget
3. In `_show_tree()`, for each file, append TWO rows:
   - Row 1: file row (with "▶" prefix in display_name)
   - Row 2: drawer row (revealer + box, initially hidden)
4. On row-activated for file row → toggle drawer visibility

**TreeStore column change:**
```python
# Current: (str, str, bool, bool) -> display_name, full_path, is_dir, is_loaded
# New: (str, str, bool, bool, bool, object) -> display_name, full_path, is_dir, is_loaded, is_drawer, drawer_widget
```

**Phase A deliverable:** Basic expand/collapse works, drawer box appears/collapses. No diff content yet.

---

## Implementation Steps

### Step 1: Update TreeStore Columns
In `__init__`, change:
```python
self._store = Gtk.TreeStore.new([str, str, bool, bool])
```
to:
```python
# Columns: display_name, full_path, is_dir, is_loaded, is_drawer, drawer_widget
self._store = Gtk.TreeStore.new([str, str, bool, bool, bool, object])
```

### Step 2: Modify `_show_tree()` to Add Drawer Rows
In `_show_tree()` loop (around line 330):
```python
for entry_name, full_path, is_dir in entries:
    prefix = "📁 " if is_dir else "  "
    display = prefix + entry_name
    
    # File/directory row
    parent = self._store.append(None, [
        display, full_path, is_dir, False, False, None
    ])
    
    if not is_dir:
        # Add drawer row as child
        drawer_revealer = Gtk.Revealer()
        drawer_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        drawer_revealer.set_reveal_child(False)
        
        drawer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        drawer_revealer.set_child(drawer_box)
        
        # Add drawer row
        self._store.append(parent, [
            "", "", False, False, True, drawer_revealer
        ])
        
        # Store reference
        self._drawers[parent] = (drawer_revealer, None, None, False)
    
    if is_dir:
        self._store.append(parent, ["…", "", True, False, False, None])
```

### Step 3: Handle Row Activation for Drawer Toggle
In `_on_row_activated()` (around line 287):
```python
def _on_row_activated(self, tree, path, column):
    model = tree.get_model()
    it = model.get_iter(path)
    if it is None:
        return

    display_name = model.get_value(it, 0).lstrip("📁 ").lstrip("  ")
    full_path = model.get_value(it, 1)
    is_dir = model.get_value(it, 2)
    is_drawer = model.get_value(it, 4)
    drawer_widget = model.get_value(it, 5)
    parent_it = model.iter_parent(it)
    is_top_level = parent_it is None

    if is_drawer:
        return  # ignore drawer row clicks

    if is_dir:
        if is_top_level and self._project_path is None:
            self.load_project(display_name, full_path)
        elif tree.row_expanded(path):
            tree.collapse_row(path)
        else:
            tree.expand_row(path, open_all=False)
    else:
        # File row - toggle drawer
        if drawer_widget:
            self._toggle_drawer(it, drawer_widget)
        elif self._on_file_selected:
            self._on_file_selected(full_path)
```

### Step 4: Add `_toggle_drawer()` Method
```python
def _toggle_drawer(self, file_it, drawer_revealer):
    """Toggle drawer revealer and update arrow in display name."""
    revealed = drawer_revealer.get_reveal_child()
    drawer_revealer.set_reveal_child(not revealed)
    
    # Update the file_path = self._store.get_path(file_it)
    model = self._store
    # Update display name prefix: "  " -> "▼ " or "▶ "
    current = model.get_value(file_it, 0)
    name_part = current.lstrip("📁 ").lstrip("  ").lstrip("▶ ").lstrip("▼ ")
    new_prefix = "▼ " if not revealed else "▶ "
    model.set_value(file_it, 0, new_prefix + name_part)
```

### Step 5: CSS Classes
Add to `ui/styles.py` (Phase F, but define here for Phase A):
```css
.file-tree-drawer-btn {
    min-width: 24px;
    min-height: 24px;
    padding: 2px 6px;
}
.file-tree-drawer {
    padding: 8px 12px;
    border-left: 2px solid alpha(@theme_selected_bg_color, 0.3);
    margin-left: 20px;
    background-color: alpha(@theme_bg_color, 0.5);
}
```

---

## Rules (steelFramedCodeWriter.md)

- Read `ui/views/file_tree.py` in full before editing
- Verify every change with evidence (pytest output, grep, wc -l)
- No fabricated APIs — use existing `Gtk.Revealer`, `Gtk.TreeStore`, `Gtk.Box`
- Hard part first: TreeStore column change + drawer row insertion
- Wire it or delete it — no stubs

---

## Deliverable Expectations

```
Files changed:
- ui/views/file_tree.py:XX-YY (TreeStore columns, drawer rows, toggle logic)
- ui/styles.py:AA-BB (drawer CSS classes)

Verification:
grep -n "Gtk.Revealer\|_drawers\|_toggle_drawer\|is_drawer" ui/views/file_tree.py
→ [paste output]
wc -l ui/views/file_tree.py
→ [paste output]
python3 -c "from ui.views.file_tree import FileTree; print('import ok')"
→ import ok

COMPLETENESS:
- [x] TreeStore columns expanded to 6
- [x] Drawer rows added as children of file rows
- [x] _toggle_drawer() method works (expand/collapse)
- [x] Row activation toggles drawer for files
- [x] Directory expand/collapse still works
- [x] Import clean
```

---

## Word Marker

**please write**