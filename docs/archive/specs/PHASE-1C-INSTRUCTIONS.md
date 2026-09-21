# Phase 1c Instructions: ui/views/diff_viewer.py — New Diff Viewer Widget

**Spec:** SPEC-ONE-CLICK-DIFF.md (§2.3)
**Phase:** 3 of 3 (git_ops → diff_card → diff_viewer → wiring)
**Target files:** 1 new file (ui/views/diff_viewer.py) + 1 test file (tests/test_diff_viewer.py)

---

## Changes Required

### 1. Create `ui/views/diff_viewer.py` — NEW FILE (~350 lines)

**Architecture:** Pure view. No git calls. No state. All actions via callbacks.

```python
import threading
import os

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, GLib, Gdk

from utils.diff_parser import parse_diff, FileDiff
from utils.git_ops import (
    diff_file_against_working_tree,
    diff_working_tree,
    diff_file_against,
    file_log,
)
from ui.views.diff_card import render_diff_hunks, get_lang_from_path


class DiffViewer(Gtk.Box):
    """
    Diff viewer widget for the main content area.

    Shows current diff for a file, with a toggle to view edit history.
    Historical entries show their diff and a revert button.

    All git calls are dispatched on background threads. UI updates via GLib.idle_add.
    Background results are guarded by _current_request_id (race safety) and
    _disposed (destroy safety).

    Widget hierarchy:
        DiffViewer (Gtk.Box, vertical)
        ├── _header (Gtk.Box, horizontal)
        │   ├── back_btn (Gtk.Button)
        │   ├── _title_label (Gtk.Label)
        │   ├── _subtitle_label (Gtk.Label)
        │   └── _tab_box (Gtk.Box)
        │       ├── _diff_toggle (Gtk.CheckButton)  # group leader
        │       └── _history_toggle (Gtk.CheckButton)  # joins group
        ├── _stack (Gtk.Stack)
        │   ├── "diff" → _diff_scroll (Gtk.ScrolledWindow)
        │   │   └── _diff_box (Gtk.Box, vertical)
        │   └── "history" → _history_scroll (Gtk.ScrolledWindow)
        │       └── _history_list (Gtk.ListBox)
        └── _action_bar (Gtk.Box, horizontal)
            └── _revert_btn (Gtk.Button)

    Args:
        file_path: Relative path to the file (relative to project root).
        project_path: Absolute path to the project root.
        checkpoint_sha: Review checkpoint SHA, or None if no active review.
        on_back: Callable called when the Back button is clicked.
        on_revert: Callable[[str, str], None] — (file_path, target_sha).
    """

    def __init__(
        self,
        file_path: str,
        project_path: str,
        checkpoint_sha: str | None = None,
        on_back: GLib.SourceFunc | None = None,
        on_revert: GLib.SourceFunc | None = None,
    ):
        # Validate inputs (H15 fix)
        if not file_path:
            raise ValueError("file_path is required")
        if not project_path:
            raise ValueError("project_path is required")

        # H12 fix: call super().__init__() before any widget operations
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        # State
        self._file_path = file_path
        self._project_path = project_path
        self._checkpoint_sha = checkpoint_sha
        self._on_back = on_back
        self._on_revert = on_revert
        self._selected_sha: str | None = None
        self._history_loaded = False

        # H3 fix: disposal flag
        self._disposed = False
        # H4 fix: request sequence ID
        self._current_request_id = 0

        # CSS: classes defined in ui/styles.py, registered globally by apply_styles()
        self.add_css_class("diff-viewer")

        self._build_ui()
        self._load_current_diff()

    def _build_ui(self):
        """Build the widget hierarchy as shown in class docstring."""
        # Header
        self._header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._header.add_css_class("diff-viewer-header")
        self.append(self._header)

        # Back button
        self._back_btn = Gtk.Button(label="← Back")
        self._back_btn.connect("clicked", self._on_back_clicked)
        self._header.append(self._back_btn)

        # Title (file path)
        self._title_label = Gtk.Label(label=self._file_path)
        self._title_label.set_halign(Gtk.Align.START)
        self._title_label.set_ellipsize(3)  # Pango.EllipsizeMode.END
        self._title_label.set_hexpand(True)
        self._title_label.add_css_class("diff-viewer-title")
        self._header.append(self._title_label)

        # Subtitle (diff context)
        self._subtitle_label = Gtk.Label(label="")
        self._subtitle_label.set_halign(Gtk.Align.START)
        self._subtitle_label.add_css_class("diff-viewer-subtitle")
        self._header.append(self._subtitle_label)

        # Tab toggles (Diff / History) — CheckButton with group
        self._tab_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._diff_toggle = Gtk.CheckButton(label="Diff")
        self._diff_toggle.set_active(True)
        self._history_toggle = Gtk.CheckButton(label="History")
        self._history_toggle.set_group(self._diff_toggle)
        self._history_toggle.connect("toggled", self._on_history_toggled)
        self._tab_box.append(self._diff_toggle)
        self._tab_box.append(self._history_toggle)
        self._header.append(self._tab_box)

        # Stack for diff / history views
        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)
        self.append(self._stack)

        # Diff page
        self._diff_scroll = Gtk.ScrolledWindow()
        self._diff_scroll.set_hexpand(True)
        self._diff_scroll.set_vexpand(True)
        self._diff_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._diff_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._diff_scroll.set_child(self._diff_box)
        self._stack.add_named(self._diff_scroll, "diff")

        # History page
        self._history_scroll = Gtk.ScrolledWindow()
        self._history_scroll.set_hexpand(True)
        self._history_scroll.set_vexpand(True)
        self._history_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._history_list = Gtk.ListBox()
        self._history_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._history_scroll.set_child(self._history_list)
        self._stack.add_named(self._history_scroll, "history")

        # Action bar (revert button)
        self._action_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._action_bar.add_css_class("diff-viewer-action-bar")
        self._revert_btn = Gtk.Button(label="Revert file to this version")
        self._revert_btn.add_css_class("diff-viewer-revert-btn")
        self._revert_btn.set_visible(False)  # only shown on historical entries
        self._revert_btn.connect("clicked", self._on_revert_clicked)
        self._action_bar.append(self._revert_btn)
        self.append(self._action_bar)

    # === Async load methods with race guards ===

    def _load_current_diff(self):
        """Load current diff on background thread."""
        self._show_loading()
        self._current_request_id += 1
        req_id = self._current_request_id

        def _do():
            if self._checkpoint_sha:
                result = diff_file_against_working_tree(
                    self._project_path, self._checkpoint_sha, self._file_path
                )
                subtitle = f"since checkpoint {self._checkpoint_sha[:7]}"
            else:
                result = diff_working_tree(self._project_path, self._file_path)
                subtitle = "since HEAD"

            GLib.idle_add(lambda: self._on_diff_loaded(result, subtitle, req_id))

        threading.Thread(target=_do, daemon=True).start()

    def _on_diff_loaded(self, result, subtitle: str, req_id: int):
        """Handle diff load result. Ignores stale results."""
        if self._disposed:
            return
        if req_id != self._current_request_id:
            return

        self._subtitle_label.set_text(subtitle)

        if not result.success:
            self._show_error(result.error)
            return

        if not result.stdout.strip():
            self._show_placeholder("No changes to this file.")
            return

        parsed = parse_diff(result.stdout)
        if not parsed.files:
            self._show_placeholder("No changes to this file.")
            return

        file_diff = parsed.files[0]

        # H8 fix: binary file handling — caller checks before calling render_diff_hunks
        if file_diff.is_binary:
            self._show_placeholder("Binary file — not shown")
            return

        # Clear previous content
        while self._diff_box.get_first_child() is not None:
            self._diff_box.remove(self._diff_box.get_first_child())

        lang = get_lang_from_path(file_diff.display_path)
        self._diff_box.append(render_diff_hunks(file_diff.hunks, lang))
        self._stack.set_visible_child_name("diff")

        # Current diff view never shows revert
        self._revert_btn.set_visible(False)

    def _load_historical_diff(self, sha: str):
        """Load diff from a historical commit on background thread."""
        self._show_loading()
        self._current_request_id += 1
        req_id = self._current_request_id

        def _do():
            result = diff_file_against(self._project_path, sha, self._file_path)
            subtitle = f"Diff from {sha[:7]} → HEAD"
            GLib.idle_add(lambda: self._on_historical_diff_loaded(result, subtitle, sha, req_id))

        threading.Thread(target=_do, daemon=True).start()

    def _on_historical_diff_loaded(self, result, subtitle: str, sha: str, req_id: int):
        if self._disposed:
            return
        if req_id != self._current_request_id:
            return

        self._subtitle_label.set_text(subtitle)

        if not result.success:
            self._show_error(result.error)
            return

        if not result.stdout.strip():
            self._show_placeholder("No changes since this commit.")
            self._revert_btn.set_visible(True)
            return

        parsed = parse_diff(result.stdout)
        if not parsed.files:
            self._show_placeholder("No changes since this commit.")
        else:
            file_diff = parsed.files[0]
            if file_diff.is_binary:
                self._show_placeholder("Binary file — not shown")
            else:
                while self._diff_box.get_first_child() is not None:
                    self._diff_box.remove(self._diff_box.get_first_child())
                lang = get_lang_from_path(file_diff.display_path)
                self._diff_box.append(render_diff_hunks(file_diff.hunks, lang))

        self._stack.set_visible_child_name("diff")
        self._revert_btn.set_visible(True)

    def _load_history(self):
        """Load file commit history on background thread."""
        if self._history_loaded:
            return
        self._history_loaded = True
        self._current_request_id += 1
        req_id = self._current_request_id

        def _do():
            result = file_log(self._project_path, self._file_path, count=20)
            entries = []
            if result.success and result.stdout.strip():
                for line in result.stdout.strip().splitlines():
                    parts = line.split("\x1f")
                    if len(parts) == 3:
                        entries.append({"sha": parts[0], "date": parts[1], "message": parts[2]})
            GLib.idle_add(lambda: self._on_history_loaded(entries, req_id))

        threading.Thread(target=_do, daemon=True).start()

    def _on_history_loaded(self, entries: list[dict], req_id: int):
        if self._disposed:
            return
        if req_id != self._current_request_id:
            return

        # Clear previous rows
        while self._history_list.get_first_child() is not None:
            self._history_list.remove(self._history_list.get_first_child())

        # H13 fix: handle empty history
        if not entries:
            placeholder = Gtk.Label(label="No commit history for this file.")
            placeholder.set_halign(Gtk.Align.CENTER)
            placeholder.set_valign(Gtk.Align.CENTER)
            placeholder.add_css_class("diff-viewer-subtitle")
            self._history_list.append(placeholder)
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
            self._history_list.append(row)

        # Connect row activation
        self._history_list.connect("row-activated", self._on_history_row_activated)

    # === UI callbacks ===

    def _on_back_clicked(self, button):
        if self._on_back:
            self._on_back()

    def _on_history_toggled(self, button):
        if button.get_active():
            self._stack.set_visible_child_name("history")
            self._load_history()

    def _on_history_row_activated(self, listbox, row):
        self._selected_sha = row.sha
        self._load_historical_diff(row.sha)

    def _on_revert_clicked(self, button):
        if self._selected_sha is None or self._on_revert is None:
            return

        short_sha = self._selected_sha[:7]
        dialog = Gtk.MessageDialog(
            transient_for=self.get_root(),
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"Revert {self._file_path}?",
            secondary_text=(
                f"This will restore the file to its state from commit {short_sha}. "
                f"Any uncommitted changes to this file will be lost."
            ),
        )
        dialog.connect("response", self._on_revert_confirmed)
        dialog.present()

    def _on_revert_confirmed(self, dialog, response_id):
        dialog.destroy()
        if response_id != Gtk.ResponseType.YES:
            return

        target_sha = self._selected_sha
        self._selected_sha = None
        self._revert_btn.set_visible(False)

        # H5 fix: don't reload immediately — give revert thread time to complete
        self._show_placeholder(f"Reverting to {target_sha[:7]}...")

        def _delayed_reload():
            import time
            time.sleep(1.0)
            GLib.idle_add(lambda: self._load_current_diff() if not self._disposed else None)

        threading.Thread(target=_delayed_reload, daemon=True).start()

        # Dispatch the actual revert
        self._on_revert(self._file_path, target_sha)

    # === Helpers ===

    def _show_loading(self):
        if self._disposed:
            return
        while self._diff_box.get_first_child() is not None:
            self._diff_box.remove(self._diff_box.get_first_child())
        spinner = Gtk.Spinner()
        spinner.start()
        spinner.set_margin_top(24)
        spinner.set_margin_bottom(24)
        spinner.set_halign(Gtk.Align.CENTER)
        self._diff_box.append(spinner)
        self._stack.set_visible_child_name("diff")

    def _show_placeholder(self, text: str):
        if self._disposed:
            return
        while self._diff_box.get_first_child() is not None:
            self._diff_box.remove(self._diff_box.get_first_child())
        lbl = Gtk.Label(label=text)
        lbl.add_css_class("diff-viewer-subtitle")
        lbl.set_margin_top(24)
        lbl.set_margin_bottom(24)
        lbl.set_halign(Gtk.Align.CENTER)
        self._diff_box.append(lbl)
        self._stack.set_visible_child_name("diff")

    def _show_error(self, error: str):
        self._show_placeholder(f"Error: {error}")

    # H3/M16/M21/M25 fix: GTK4 dispose vfunc
    def do_dispose(self):
        self._disposed = True
        Gtk.Box.do_dispose(self)
```

### 2. Write Tests — `tests/test_diff_viewer.py`

Test the following (use `xvfb-run -a`):

- `test_diff_viewer_creation` — widget instantiates, shows header/title
- `test_get_lang_from_path_integration` — uses `get_lang_from_path` for various extensions
- `test_render_diff_hunks_integration` — renders hunks via `render_diff_hunks`
- `test_history_empty` — empty history shows placeholder
- `test_revert_button_visibility` — revert button only visible on historical diff
- `test_back_callback` — clicking Back calls on_back
- `test_revert_callback` — clicking Revert calls on_revert with (file_path, sha)

---

## Rules (steelFramedCodeWriter.md)

- Read all referenced files in full before editing
- Hard part first: implement `DiffViewer` class, then tests
- Verify every claim with evidence (pytest output, grep, wc -l)
- No fabricated APIs — use existing `GitResult`, `diff_file_against_working_tree`, `diff_working_tree`, `diff_file_against`, `file_log`, `parse_diff`, `render_diff_hunks`, `get_lang_from_path`
- Wire it or delete it — no stubs

---

## Deliverable Expectations

Report back with:

```
Files changed:
- ui/views/diff_viewer.py:XX-YY (new file)
- tests/test_diff_viewer.py:AA-BB (new file)

Verification:
pytest tests/test_diff_viewer.py -v
→ [paste full output]
grep -n "class DiffViewer\|def __init__\|def _build_ui\|def _load_current_diff\|def _load_history\|def _load_historical_diff\|def _on_revert_clicked\|def do_dispose" ui/views/diff_viewer.py
→ [paste output]

COMPLETENESS:
- [x/not done] Edit 1: DiffViewer class with all async load methods
- [x/not done] Edit 2: Header with Back/Title/Subtitle/Tabs
- [x/not done] Edit 3: Stack with Diff/History pages
- [x/not done] Edit 4: Action bar with Revert button
- [x/not done] Edit 5: Race guards (_current_request_id, _disposed)
- [x/not done] Edit 6: Binary file handling (H8)
- [x/not done] Edit 7: Empty history placeholder (H13)
- [x/not done] Edit 8: Confirmation dialog for revert (H5)
- [x/not done] Edit 9: Delayed reload after revert (H5)
- [x/not done] Edit 10: do_dispose() vfunc (M25)
- [x/not done] Edit 11: Tests for DiffViewer
- [x/not done] Regression: ui/styles.py CSS classes exist for diff-viewer*
```

---

## Word Marker

**please write**