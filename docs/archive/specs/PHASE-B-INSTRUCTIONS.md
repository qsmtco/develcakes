# Phase B Instructions: ui/views/file_tree.py — Diff Content Loading

**Spec:** SPEC-INLINE-FILE-TREE-DIFF-DRAWER.md (§2.1, §5 Phase B)
**Phase:** B of F (populate drawer with diff content)
**Target file:** 1 file (ui/views/file_tree.py)

---

## Changes Required

### 1. Add Diff Loading to Drawer

**Replace the placeholder label in `_add_drawer_for_file` with actual diff content loading.**

**Current `_add_drawer_for_file`** (lines ~405-430):
```python
def _add_drawer_for_file(self, file_path: str, display_name: str) -> None:
    revealer = Gtk.Revealer()
    revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
    revealer.set_reveal_child(False)
    revealer.set_transition_duration(150)
    revealer.add_css_class("file-tree-drawer")

    # Skeleton content box — will be populated in Phase B
    drawer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    drawer_box.set_margin_start(20)
    drawer_box.set_margin_end(8)
    drawer_box.set_margin_top(4)
    drawer_box.set_margin_bottom(4)

    # Phase A: Just a placeholder label
    placeholder = Gtk.Label(label=" ")
    placeholder.set_size_request(-1, 1)
    drawer_box.append(placeholder)

    revealer.set_child(drawer_box)
    self._drawer_area.append(revealer)
    self._drawers[file_path] = (revealer, display_name, False)
```

**Replace with:** Full diff loading using `ProjectHandler.get_file_diff()` + `render_diff_hunks()`.

---

### 2. Add Required Imports

**Add to imports at top of file:**
```python
from utils.git_ops import diff_file_against_working_tree, diff_working_tree
from utils.diff_parser import parse_diff, FileDiff
from ui.views.diff_card import render_diff_hunks, get_lang_from_path
```

---

### 3. Add `_load_drawer_diff` Method

**New method** (similar to `DiffViewer._load_current_diff`):
```python
def _load_drawer_diff(self, file_path: str, drawer_revealer: Gtk.Revealer, drawer_box: Gtk.Box, project_path: str, checkpoint_sha: str | None = None) -> None:
    """Load current diff for a file into the drawer box on background thread."""
    
    def _do():
        # Determine checkpoint SHA from active review
        if checkpoint_sha:
            result = diff_file_against_working_tree(project_path, checkpoint_sha, file_path)
            subtitle = f"since checkpoint {checkpoint_sha[:7]}"
        else:
            result = diff_working_tree(project_path, file_path)
            subtitle = "since HEAD"

        GLib.idle_add(lambda: self._on_drawer_diff_loaded(result, subtitle, drawer_revealer, drawer_box, file_path))

    threading.Thread(target=_do, daemon=True).start()

def _on_drawer_diff_loaded(self, result, subtitle: str, drawer_revealer: Gtk.Revealer, drawer_box: Gtk.Box, file_path: str) -> None:
    """Handle diff load result for drawer."""
    # Check if drawer still exists (not cleaned up)
    if file_path not in self._drawers:
        return
    
    # Clear placeholder
    while drawer_box.get_first_child() is not None:
        drawer_box.remove(drawer_box.get_first_child())

    if not result.success:
        error_lbl = Gtk.Label(label=f"Error: {result.error}")
        error_lbl.add_css_class("diff-viewer-subtitle")
        error_lbl.set_margin_top(12)
        error_lbl.set_margin_bottom(12)
        drawer_box.append(error_lbl)
        return

    if not result.stdout.strip():
        no_changes_lbl = Gtk.Label(label="No changes to this file.")
        no_changes_lbl.add_css_class("diff-viewer-subtitle")
        no_changes_lbl.set_margin_top(12)
        no_changes_lbl.set_margin_bottom(12)
        drawer_box.append(no_changes_lbl)
        return

    parsed = parse_diff(result.stdout)
    if not parsed.files:
        no_changes_lbl = Gtk.Label(label="No changes to this file.")
        no_changes_lbl.add_css_class("diff-viewer-subtitle")
        no_changes_lbl.set_margin_top(12)
        no_changes_lbl.set_margin_bottom(12)
        drawer_box.append(no_changes_lbl)
        return

    file_diff = parsed.files[0]

    # Binary file handling
    if file_diff.is_binary:
        bin_lbl = Gtk.Label(label="Binary file — not shown")
        bin_lbl.add_css_class("diff-viewer-subtitle")
        bin_lbl.set_margin_top(12)
        bin_lbl.set_margin_bottom(12)
        drawer_box.append(bin_lbl)
        return

    lang = get_lang_from_path(file_diff.display_path)
    drawer_box.append(render_diff_hunks(file_diff.hunks, lang))
```

---

### 4. Update `_add_drawer_for_file` to Store Drawer Box and Trigger Load

**Modify `_add_drawer_for_file`** to:
1. Create the drawer box with a loading placeholder
2. Store `(revealer, display_name, is_open, drawer_box)` in `_drawers`
3. Call `_load_drawer_diff` when drawer is toggled open

**Update `_drawers` dict value** from `(revealer, display_name, is_open)` to `(revealer, display_name, is_open, drawer_box)`

**Update `_toggle_drawer`** to:
1. If opening (not `is_open`), create drawer box if not exists, call `_load_drawer_diff`
2. Get checkpoint SHA from active review via `ProjectHandler`

---

### 5. Get Checkpoint SHA from Active Review

**Need access to `ProjectHandler`** — add to `FileTree` constructor or setter:
```python
def set_project_handler(self, handler):
    self._project_handler = handler
```

Then in `_load_drawer_diff`:
```python
checkpoint_sha = None
if self._project_handler:
    project_name = self._project_handler.get_active_project_name()
    if project_name:
        review_state = self._project_handler.get_review_state(project_name)
        if review_state and review_state.is_active():
            checkpoint_sha = review_state.checkpoint_sha
```

---

## Rules (steelFramedCodeWriter.md)

- Read `ui/views/file_tree.py` in full before editing
- Verify every change with evidence (pytest output, grep, wc -l)
- No fabricated APIs — use existing `ProjectHandler`, `diff_file_against_working_tree`, `render_diff_hunks`
- Hard part first: background loading + UI update via GLib.idle_add
- Wire it or delete it — no stubs

---

## Deliverable Expectations

```
Files changed:
- ui/views/file_tree.py:XX-YY (diff loading, imports, drawer box management)

Verification:
grep -n "_load_drawer_diff\|_on_drawer_diff_loaded\|render_diff_hunks\|get_file_diff" ui/views/file_tree.py
→ [paste output]
wc -l ui/views/file_tree.py
→ [paste output]
python3 -c "from ui.views.file_tree import FileTree; print('import ok')"
→ import ok

COMPLETENESS:
- [x] Imports added for diff/git operations
- [x] _load_drawer_diff method with background thread
- [x] _on_drawer_diff_loaded with UI update
- [x] _add_drawer_for_file creates drawer box, stores in _drawers
- [x] _toggle_drawer triggers load when opening
- [x] ProjectHandler integration for checkpoint SHA
- [x] Error/empty/binary handling in drawer
```

---

## Word Marker

**please write**