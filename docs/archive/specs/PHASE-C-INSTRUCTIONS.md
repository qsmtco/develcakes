# Phase C Instructions: ui/views/file_tree.py — History Tab + Revert Button in Drawer

**Spec:** SPEC-INLINE-FILE-TREE-DIFF-DRAWER.md (§2.1, §5 Phase C)
**Phase:** C of F (history tab + revert button in drawer)
**Target file:** 1 file (ui/views/file_tree.py)

---

## Changes Required

### 1. Add History Tab to Drawer

The drawer currently shows only current diff. Need to add a tabbed interface:
- **Diff tab** — current diff (working tree vs checkpoint/HEAD)
- **History tab** — list of commits that touched this file

### 2. Update Drawer Structure

**Current drawer box** (in `_add_drawer_for_file`):
```python
drawer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
# ... loading label ...
```

**New structure:**
```python
drawer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

# Tab bar
tab_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
tab_bar.add_css_class("file-tree-drawer-tab-bar")

diff_tab = Gtk.Button(label="Diff")
diff_tab.set_active(True)  # toggle button style
history_tab = Gtk.Button(label="History")
history_tab.set_group(diff_tab)

tab_bar.append(diff_tab)
tab_bar.append(history_tab)
drawer_box.append(tab_bar)

# Stack for diff/history content
stack = Gtk.Stack()
stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
stack.set_vexpand(True)

# Diff page
diff_scroll = Gtk.ScrolledWindow()
diff_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
diff_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
diff_scroll.set_child(diff_box)
stack.add_named(diff_scroll, "diff")

# History page
history_scroll = Gtk.ScrolledWindow()
history_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
history_list = Gtk.ListBox()
history_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
history_scroll.set_child(history_list)
stack.add_named(history_scroll, "history")

drawer_box.append(stack)

# Connect tab switching
diff_tab.connect("toggled", lambda btn: stack.set_visible_child_name("diff") if btn.get_active() else None)
history_tab.connect("toggled", lambda btn: (stack.set_visible_child_name("history"), self._load_history(file_path, history_list)) if btn.get_active() else None)

# Store references on drawer box
drawer_box._diff_tab = diff_tab
drawer_box._history_tab = history_tab
drawer_box._stack = stack
drawer_box._diff_box = diff_box
drawer_box._history_list = history_list
```

### 3. Add History Loading

**New method** (similar to `DiffViewer._load_history`):
```python
def _load_history(self, file_path: str, history_list: Gtk.ListBox) -> None:
    """Load commit history for a file into the history list."""
    if getattr(history_list, '_loaded', False):
        return
    history_list._loaded = True

    def _do():
        project_path = self._project_path or ""
        result = file_log(project_path, file_path, count=20)
        entries = []
        if result.success and result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                parts = line.split("\x1f")
                if len(parts) == 3:
                    entries.append({"sha": parts[0], "date": parts[1], "message": parts[2]})
        GLib.idle_add(lambda: self._on_history_loaded(entries, history_list, file_path))

    threading.Thread(target=_do, daemon=True).start()

def _on_history_loaded(self, entries: list[dict], history_list: Gtk.ListBox, file_path: str) -> None:
    if file_path not in self._drawers:
        return

    # Clear previous rows
    while history_list.get_first_child() is not None:
        history_list.remove(history_list.get_first_child())

    if not entries:
        placeholder = Gtk.Label(label="No commit history for this file.")
        placeholder.set_halign(Gtk.Align.CENTER)
        placeholder.set_valign(Gtk.Align.CENTER)
        placeholder.add_css_class("diff-viewer-subtitle")
        history_list.append(placeholder)
        return

    for entry in entries:
        row = Gtk.ListBoxRow()
        row.sha = entry["sha"]
        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row_box.add_css_class("diff-history-row")

        sha_lbl = Gtk.Label(label=entry["sha"][:7])
        sha_lbl.add_css_class("diff-history-row-sha")

        date_lbl = Gtk.Label(label=entry["date"][:10])
        date_lbl.add_css_class("diff-history-row-date")

        msg_lbl = Gtk.Label(label=entry["message"])
        msg_lbl.add_css_class("diff-history-row-msg")
        msg_lbl.set_ellipsize(3)
        msg_lbl.set_hexpand(True)

        row_box.append(sha_lbl)
        row_box.append(date_lbl)
        row_box.append(msg_lbl)
        row.set_child(row_box)
        history_list.append(row)

    # Connect row activation for historical diff
    history_list.connect("row-activated", lambda lb, row: self._load_historical_diff(file_path, row.sha, lb.get_ancestor(Gtk.Stack)))

### 4. Add Historical Diff Loading

**New method:**
```python
def _load_historical_diff(self, file_path: str, sha: str, stack: Gtk.Stack) -> None:
    """Load diff for a historical commit."""
    def _do():
        project_path = self._project_path or ""
        result = diff_file_against(project_path, sha, file_path)
        GLib.idle_add(lambda: self._on_historical_diff_loaded(result, sha, file_path, stack))

    threading.Thread(target=_do, daemon=True).start()

def _on_historical_diff_loaded(self, result, sha: str, file_path: str, stack: Gtk.Stack) -> None:
    if file_path not in self._drawers:
        return

    # Switch to diff view
    stack.set_visible_child_name("diff")

    # Get the diff_box from the drawer
    drawer_entry = self._drawers.get(file_path)
    if not drawer_entry or len(drawer_entry) < 4:
        return
    drawer_box = drawer_entry[3]
    diff_box = drawer_box._diff_box if hasattr(drawer_box, '_diff_box') else None
    if not diff_box:
        return

    # Clear and populate
    while diff_box.get_first_child() is not None:
        diff_box.remove(diff_box.get_first_child())

    if not result.success:
        error_lbl = Gtk.Label(label=f"Error: {result.error}")
        error_lbl.add_css_class("diff-viewer-subtitle")
        error_lbl.set_margin_top(12)
        error_lbl.set_margin_bottom(12)
        diff_box.append(error_lbl)
        return

    if not result.stdout.strip():
        no_changes_lbl = Gtk.Label(label="No changes since this commit.")
        no_changes_lbl.add_css_class("diff-viewer-subtitle")
        no_changes_lbl.set_margin_top(12)
        no_changes_lbl.set_margin_bottom(12)
        diff_box.append(no_changes_lbl)
        return

    parsed = parse_diff(result.stdout)
    if not parsed.files:
        no_changes_lbl = Gtk.Label(label="No changes since this commit.")
        no_changes_lbl.add_css_class("diff-viewer-subtitle")
        no_changes_lbl.set_margin_top(12)
        no_changes_lbl.set_margin_bottom(12)
        diff_box.append(no_changes_lbl)
        return

    file_diff = parsed.files[0]
    if file_diff.is_binary:
        bin_lbl = Gtk.Label(label="Binary file — not shown")
        bin_lbl.add_css_class("diff-viewer-subtitle")
        bin_lbl.set_margin_top(12)
        bin_lbl.set_margin_bottom(12)
        diff_box.append(bin_lbl)
        return

    lang = get_lang_from_path(file_diff.display_path)
    diff_box.append(render_diff_hunks(file_diff.hunks, lang))

    # Show revert button in action bar (will add in next step)
    # For now, just log that historical diff loaded
    print(f"Loaded historical diff for {file_path} from {sha[:7]}")
```

### 5. Add Revert Button (Action Bar)

**In drawer box, below the stack:**
```python
# Action bar with revert button (only visible on historical diff)
action_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
action_bar.add_css_class("diff-viewer-action-bar")
action_bar.set_margin_top(8)
action_bar.set_margin_bottom(8)
action_bar.set_margin_start(20)
action_bar.set_margin_end(8)

revert_btn = Gtk.Button(label="Revert file to this version")
revert_btn.add_css_class("diff-viewer-revert-btn")
revert_btn.set_visible(False)
revert_btn.connect("clicked", lambda btn: self._on_drawer_revert_clicked(file_path, row.sha))

action_bar.append(revert_btn)
drawer_box.append(action_bar)

# Store revert button reference
drawer_box._revert_btn = revert_btn
```

### 6. Add Revert Handler

```python
def _on_drawer_revert_clicked(self, button, file_path: str, target_sha: str) -> None:
    """Handle revert button click in drawer."""
    if not self._project_handler or not self._project_name:
        return

    # Confirm dialog
    dialog = Gtk.MessageDialog(
        transient_for=self.get_root(),
        modal=True,
        message_type=Gtk.MessageType.QUESTION,
        buttons=Gtk.ButtonsType.YES_NO,
        text=f"Revert {file_path}?",
        secondary_text=f"This will restore the file to its state from commit {target_sha[:7]}. Any uncommitted changes will be lost."
    )
    dialog.connect("response", lambda d, r: self._on_drawer_revert_confirmed(d, r, file_path, target_sha))
    dialog.present()

def _on_drawer_revert_confirmed(self, dialog, response_id, file_path: str, target_sha: str) -> None:
    dialog.destroy()
    if response_id != Gtk.ResponseType.YES:
        return

    self._project_handler.revert_file_to_sha(self._project_name, file_path, target_sha)
    # Reload current diff after revert
    self._load_current_diff(file_path)
```

---

## Rules (steelFramedCodeWriter.md)

- Read `ui/views/file_tree.py` in full before editing
- Verify every change with evidence (pytest output, grep, wc -l)
- No fabricated APIs — use existing `file_log`, `diff_file_against`, `render_diff_hunks`
- Hard part first: tabbed interface + history loading
- Wire it or delete it — no stubs

---

## Deliverable Expectations

```
Files changed:
- ui/views/file_tree.py:XX-YY (history tab, revert button, history loading, revert handling)

Verification:
grep -n "_load_history\|_on_history_loaded\|_load_historical_diff\|_on_historical_diff_loaded\|_on_drawer_revert" ui/views/file_tree.py
→ [paste output]
wc -l ui/views/file_tree.py
→ [paste output]
python3 -c "from ui.views.file_tree import FileTree; print('import ok')"
→ import ok

COMPLETENESS:
- [x] Drawer has tabbed interface (Diff/History tabs)
- [x] History tab loads commit list on first click
- [x] Clicking history row loads that commit's diff
- [x] Revert button appears on historical diff
- [x] Revert button shows confirmation dialog
- [x] Confirming revert calls ProjectHandler.revert_file_to_sha
- [x] After revert, current diff reloads
```

---

## Word Marker

**please write**