"""Drawer open/diff/history/revert/clipboard behavior for FileTree."""
from __future__ import annotations

import time
from typing import Optional, cast

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk

from ui.file_tree.rows import FileTreeRow
from ui.file_tree._facade import _mod
from ui.views.diff_card import get_lang_from_path, render_diff_hunks
from utils.diff_parser import parse_diff
from utils.git_ops import (
    GitResult,
    diff_file_against,
    diff_file_against_working_tree,
    diff_working_tree,
    file_log,
)


class FileTreeDrawerMixin:
    # ── Phase 4: Drawer Content — Diff Tab ────────────────────────────

    # BUG #1: Set of known binary file extensions for quick check
    _BINARY_EXTENSIONS: frozenset = frozenset({
        '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.svg', '.webp',
        '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
        '.so', '.dll', '.dylib', '.o', '.a', '.lib',
        '.pyc', '.pyo', '.pyd',
        '.zip', '.tar', '.gz', '.bz2', '.xz', '.zst', '.7z', '.rar',
        '.exe', '.bin', '.dat', '.db', '.sqlite', '.sqlite3',
        '.mp3', '.mp4', '.avi', '.mov', '.mkv', '.wav', '.flac', '.ogg',
        '.ttf', '.otf', '.woff', '.woff2', '.eot',
    })

    def toggle_drawer_for_file(self, file_path: str) -> None:
        """Public method to toggle a file's diff drawer open/closed from outside.

        Called by MainContent when the user requests a file diff from a tab.
        """
        self._toggle_drawer(file_path)

    def is_drawer_open(self, file_path: str) -> bool:
        """Return True if the drawer for the given file path is currently open."""
        return file_path in self._drawer_paths

    def _update_column_visibility_for_drawers(self) -> None:
        """Hide Status/Size/Modified columns when any drawer is open (drawer width fix)."""
        any_open = len(self._drawer_paths) > 0
        for col in (self._col_status, self._col_size, self._col_modified):
            if col is not None:
                col.set_visible(not any_open)

    def _add_drawer_for_file(self, file_path: str, display_name: str) -> Gtk.Revealer:
        """Create a drawer revealer for a file row.

        The drawer is inserted as a separate row in the ListStore (is_drawer=True)
        immediately below the file row. On toggle, the revealer slides open.

        Returns the Gtk.Revealer so _toggle_drawer can insert it into the store.
        """
        drawer_box = self._build_drawer_content(file_path, display_name)

        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_reveal_child(False)
        revealer.set_transition_duration(150)
        revealer.add_css_class("file-tree-drawer")
        revealer.set_child(drawer_box)

        revealer.connect("notify::child-revealed", self._on_revealer_child_revealed, file_path)

        return revealer

    def _build_drawer_content(self, file_path: str, display_name: str) -> Gtk.Box:
        """Build the drawer content widget (tabs, stack, action bar).

        Returns the drawer_box Gtk.Box. The revealer is created in _add_drawer_for_file.
        """
        drawer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        drawer_box.set_margin_start(10)
        drawer_box.set_margin_end(8)
        drawer_box.set_margin_top(4)
        drawer_box.set_margin_bottom(4)
        drawer_box.set_hexpand(True)

        # Top bar: Tabs (left) + Action buttons (right)
        top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        top_bar.add_css_class("file-tree-drawer-tab-bar")
        top_bar.set_margin_bottom(4)
        top_bar.set_hexpand(True)

        diff_tab = Gtk.ToggleButton(label="Diff")
        diff_tab.set_active(True)
        diff_tab.add_css_class("file-tree-drawer-tab-btn")
        history_tab = Gtk.ToggleButton(label="History")
        history_tab.set_group(diff_tab)
        history_tab.add_css_class("file-tree-drawer-tab-btn")

        # Spacer to push action buttons to the right
        top_spacer = Gtk.Label()
        top_spacer.set_hexpand(True)

        revert_btn = Gtk.Button(label="Revert file to this version")
        revert_btn.add_css_class("diff-viewer-revert-btn")
        revert_btn.add_css_class("file-tree-drawer-tab-btn")
        revert_btn.set_visible(False)

        copy_btn = Gtk.Button(label="Copy diff")
        copy_btn.add_css_class("diff-viewer-copy-btn")
        copy_btn.add_css_class("file-tree-drawer-tab-btn")

        top_bar.append(diff_tab)
        top_bar.append(history_tab)
        top_bar.append(top_spacer)
        top_bar.append(revert_btn)
        top_bar.append(copy_btn)
        drawer_box.append(top_bar)

        # Stack
        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        stack.set_vexpand(True)
        stack.set_hexpand(True)

        # Diff page
        diff_scroll = Gtk.ScrolledWindow()
        diff_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        diff_scroll.set_propagate_natural_height(True)
        diff_scroll.set_min_content_height(72)
        diff_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        diff_box.set_margin_top(2)
        diff_box.set_margin_bottom(2)
        diff_scroll.set_child(diff_box)
        stack.add_named(diff_scroll, "diff")

        loading_spinner = Gtk.Spinner()
        loading_spinner.set_margin_top(8)
        loading_spinner.set_margin_bottom(8)
        loading_spinner.set_halign(Gtk.Align.CENTER)
        loading_spinner.set_size_request(24, 24)
        loading_spinner.start()
        diff_box.append(loading_spinner)

        # History page
        history_scroll = Gtk.ScrolledWindow()
        history_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        history_scroll.set_propagate_natural_height(True)
        history_scroll.set_min_content_height(72)
        history_list = Gtk.ListBox()
        history_list.set_margin_top(2)
        history_list.set_margin_bottom(2)
        history_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        history_scroll.set_child(history_list)
        stack.add_named(history_scroll, "history")

        drawer_box.append(stack)

        # Wire tab switching
        diff_tab.connect("toggled", lambda btn:
            stack.set_visible_child_name("diff") if btn.get_active() else None)
        history_tab.connect("toggled", lambda btn:
            (stack.set_visible_child_name("history"),
             self._load_history(file_path, history_list)) if btn.get_active() else None)

        # Wire revert button
        revert_btn.connect("clicked", lambda btn:
            self._on_drawer_revert_clicked(file_path, drawer_box))

        # Wire copy button
        copy_btn.connect("clicked", lambda btn:
            self._on_copy_diff_to_clipboard(file_path, drawer_box))

        # Wire history row activation
        history_list.connect("row-activated", lambda lb, row:
            self._load_historical_diff(file_path, getattr(row, 'sha', 'HEAD'), stack)
            if isinstance(row, Gtk.ListBoxRow) and row.get_activatable()
            else None)

        # Keyboard navigation in history list
        history_list.connect("keynav-failed", lambda lb, direction: True)
        history_list_controller = Gtk.EventControllerKey()
        history_list_controller.connect("key-pressed", lambda ctrl, keyval, keycode, state:
            self._on_history_key_pressed(keyval, history_list))
        history_list.add_controller(history_list_controller)

        # Store references on drawer_box for later access
        drawer_box._diff_tab = diff_tab
        drawer_box._history_tab = history_tab
        drawer_box._stack = stack
        drawer_box._diff_box = diff_box
        drawer_box._history_list = history_list
        drawer_box._revert_btn = revert_btn
        drawer_box._copy_btn = copy_btn
        drawer_box._history_selected_sha = None
        drawer_box._diff_text = ""

        # Unified key controller: Escape closes drawer, Ctrl+C copies diff
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", lambda ctrl, keyval, keycode, state:
            self._on_drawer_key_pressed(keyval, keycode, state, file_path, drawer_box))
        drawer_box.add_controller(key_controller)

        return drawer_box

    def _toggle_drawer(self, file_path: str) -> None:
        """Toggle a file's drawer open/closed.

        On first open, creates the drawer revealer and inserts it as a row below the file.
        On close, animates the revealer closed and removes the row.
        """
        # Debounce
        now = time.monotonic()
        if now - self._last_toggle_per_file.get(file_path, 0) < 0.3:
            return
        self._last_toggle_per_file[file_path] = now

        if file_path in self._drawer_paths:
            # Drawer entry exists — get the row object directly (BUG #1-R)
            drawer_row: FileTreeRow = self._drawer_paths[file_path]
            # Verify the row is still alive in the store (not removed by collapse)
            alive = False
            n = self._store.get_n_items()
            for i in range(n):
                if self._store.get_item(i) is drawer_row:
                    alive = True
                    break

            if not alive:
                # Stale entry from ancestor collapse — clean up and re-open
                del self._drawer_paths[file_path]
            else:
                # Drawer row is alive — close it
                revealer = drawer_row.props.drawer_widget
                if revealer is not None:
                    revealer.set_reveal_child(False)
                    # Row removal happens in _on_revealer_child_revealed
                else:
                    # BUG #2: Revealer is None — orphan state. Remove row directly.
                    self._store.remove(i)  # i is the index from the scan above
                    del self._drawer_paths[file_path]
                # DRAWER-WIDTH-FIX: Update column visibility (all drawers closed now)
                self._update_column_visibility_for_drawers()
                return  # Close path done

        if file_path not in self._drawer_paths:
            # Drawer doesn't exist (or was cleaned up above) — create and insert
            file_index = self._find_file_index(file_path)
            if file_index is None:
                return

            file_row = cast(FileTreeRow, self._store.get_item(file_index))
            revealer = self._add_drawer_for_file(file_path, file_row.props.display_name)

            # Create drawer row
            drawer_row = FileTreeRow(
                display_name="",
                full_path=file_path,
                is_dir=False,
                is_drawer=True,
                depth=file_row.props.depth,
                drawer_widget=revealer,
                is_open=True,
                parent_full_path=file_row.props.parent_full_path,
            )
            # Defensive: validate invariants the sort comparator relies on
            if not file_row or file_row.props.depth < 0:
                raise ValueError("Invalid file row for drawer toggle")
            if not file_path:
                raise ValueError("file_path must be non-empty for drawer toggle")
            self._store.insert(file_index + 1, drawer_row)
            # BUG #1-R: Store the row object, not the index
            self._drawer_paths[file_path] = drawer_row

            # DRAWER-WIDTH-FIX: Update column visibility (drawer now open)
            self._update_column_visibility_for_drawers()

            # Animate open
            revealer.set_reveal_child(True)

            # Trigger lazy load of diff content
            if file_path not in self._loaded_drawers:
                self._loaded_drawers.add(file_path)
                self._trigger_diff_load(file_path, revealer.get_child())

    def _find_file_index(self, file_path: str) -> Optional[int]:
        """Find the index of a file row in the store by full_path. O(n) walk."""
        n = self._store.get_n_items()
        for i in range(n):
            row = cast(FileTreeRow, self._store.get_item(i))
            if not row.props.is_dir and not row.props.is_drawer and row.props.full_path == file_path:
                return i
        return None

    def _on_revealer_child_revealed(self, revealer: Gtk.Revealer, pspec, file_path: str) -> None:
        """When revealer animation completes and reveal_child is False, remove the drawer row."""
        # BUG #2-R: Guard against None revealer
        if revealer is None:
            return

        if revealer.get_reveal_child():
            return

        # BUG #1-R: Walk the store to find the row whose drawer_widget is this revealer.
        # Using object identity instead of a potentially-stale index.
        n = self._store.get_n_items()
        drawer_index = None
        for i in range(n):
            row = cast(FileTreeRow, self._store.get_item(i))
            if row.props.drawer_widget is revealer:
                drawer_index = i
                break

        if drawer_index is None:
            # Revealer not found in store — clean up any stale _drawer_paths entry
            if file_path in self._drawer_paths:
                del self._drawer_paths[file_path]
            return

        # Remove the drawer row
        self._store.remove(drawer_index)

        # Clean up _drawer_paths
        if file_path in self._drawer_paths:
            del self._drawer_paths[file_path]

        # BUG #2: Allow lazy reload on next open
        self._loaded_drawers.discard(file_path)

        # DRAWER-WIDTH-FIX: Update column visibility (all drawers closed now)
        self._update_column_visibility_for_drawers()

    def _trigger_diff_load(self, file_path: str, drawer_box: Gtk.Box) -> None:
        """Trigger lazy load of diff content for a file's drawer.

        Resolves checkpoint SHA from active review if ProjectHandler is available.
        """
        if not isinstance(drawer_box, Gtk.Box):
            return
        project_path = self._project_path or ""
        checkpoint_sha = None
        if self._project_handler and self._project_name:
            try:
                from models.review_state import ReviewState
                review_state = self._project_handler.get_review_state(self._project_name)
                if review_state and review_state.is_active():
                    checkpoint_sha = review_state.checkpoint_sha
            except Exception:
                pass  # Non-fatal — fall back to HEAD
        self._load_drawer_diff(file_path, drawer_box, project_path, checkpoint_sha)

    @staticmethod
    def _is_binary_path(file_path: str) -> bool:
        """Return True if the file path has a known binary extension."""
        idx = file_path.rfind('.')
        if idx == -1:
            return False
        ext = file_path[idx:].lower()
        return ext in FileTree._BINARY_EXTENSIONS

    def _load_drawer_diff(self, file_path: str, drawer_box: Gtk.Box, project_path: str,
                          checkpoint_sha: str | None = None) -> None:
        """Load current diff for a file into the drawer box on background thread."""
        def _do():
            try:
                if checkpoint_sha:
                    result = diff_file_against_working_tree(
                        project_path, checkpoint_sha, file_path
                    )
                    subtitle = f"since checkpoint {checkpoint_sha[:7]}"
                else:
                    result = diff_working_tree(project_path, file_path)
                    subtitle = "since HEAD"
            except Exception as e:
                result = GitResult(success=False, stdout="", error=str(e))
                subtitle = ""
            _mod().GLib.idle_add(lambda: self._on_drawer_diff_loaded(
                result, subtitle, drawer_box, file_path
            ))
        _mod().threading.Thread(target=_do, daemon=True).start()

    def _on_drawer_diff_loaded(self, result, subtitle: str,
                               drawer_box: Gtk.Box, file_path: str) -> None:
        """Handle diff load result for drawer — update the Diff page on main thread."""
        # Check if drawer still exists (not cleaned up)
        if file_path not in self._drawer_paths:
            return

        # BUG #3: Verify the current drawer's child box is still this drawer_box
        # (not a stale reference from a reopened/replaced drawer)
        drawer_row = cast(FileTreeRow, self._drawer_paths.get(file_path))
        if drawer_row is not None:
            current_revealer = drawer_row.props.drawer_widget
            if current_revealer is not None:
                current_child = current_revealer.get_child()
                if current_child is not drawer_box:
                    return

        # Populate the diff_box inside the tabbed stack
        diff_box = getattr(drawer_box, '_diff_box', drawer_box)

        # Clear loading placeholder / previous content
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
            no_changes_lbl = Gtk.Label(label="No changes to this file.")
            no_changes_lbl.add_css_class("diff-viewer-subtitle")
            no_changes_lbl.set_margin_top(12)
            no_changes_lbl.set_margin_bottom(12)
            diff_box.append(no_changes_lbl)
            return

        # BUG #1: Check for binary extension before attempting to parse diff
        if self._is_binary_path(file_path):
            bin_lbl = Gtk.Label(label="Binary file — not shown")
            bin_lbl.add_css_class("diff-viewer-subtitle")
            bin_lbl.set_margin_top(12)
            bin_lbl.set_margin_bottom(12)
            diff_box.append(bin_lbl)
            return

        parsed = parse_diff(result.stdout)
        if not parsed.files:
            no_changes_lbl = Gtk.Label(label="No changes to this file.")
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

        # Store diff text for clipboard
        drawer_box._diff_text = result.stdout

    def _load_history(self, file_path: str, history_list: Gtk.ListBox) -> None:
        """Load commit history for a file into the history list (background thread).

        Only loads once per drawer — subsequent clicks are no-ops unless
        the drawer is closed and re-opened (which creates a new drawer row).
        """
        if file_path not in self._drawer_paths:
            return  # Drawer was closed
        drawer_row: FileTreeRow = self._drawer_paths[file_path]
        if drawer_row.props.history_loaded:
            return  # Already loaded for this drawer

        drawer_row.props.history_loaded = True

        def _do():
            try:
                project_path = self._project_path or ""
                result = file_log(project_path, file_path, count=20)
            except Exception as e:
                result = GitResult(success=False, stdout="", error=str(e))
            entries: list[dict] = []
            if result.success and result.stdout.strip():
                for line in result.stdout.strip().splitlines():
                    parts = line.split("\x1f")
                    if len(parts) == 3:
                        entries.append({
                            "sha": parts[0],
                            "date": parts[1],
                            "message": parts[2],
                        })
            _mod().GLib.idle_add(lambda: self._on_history_loaded(entries, history_list, file_path))

        _mod().threading.Thread(target=_do, daemon=True).start()

    def _on_history_loaded(self, entries: list[dict], history_list: Gtk.ListBox,
                           file_path: str) -> None:
        """Populate the history ListBox with commit entries (main thread)."""
        if file_path not in self._drawer_paths:
            return  # Drawer was closed

        # Clear previous rows
        while history_list.get_first_child() is not None:
            history_list.remove(history_list.get_first_child())

        if not entries:
            placeholder_row = Gtk.ListBoxRow()
            placeholder_row.set_activatable(False)
            placeholder_row.set_selectable(False)
            placeholder = Gtk.Label(label="No commit history for this file.")
            placeholder.set_halign(Gtk.Align.CENTER)
            placeholder.set_valign(Gtk.Align.CENTER)
            placeholder.add_css_class("diff-viewer-subtitle")
            placeholder_row.set_child(placeholder)
            history_list.append(placeholder_row)
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

    def _load_historical_diff(self, file_path: str, sha: str, stack: Gtk.Stack) -> None:
        """Load diff for a historical commit on a background thread."""
        if file_path not in self._drawer_paths:
            return
        drawer_row: FileTreeRow = self._drawer_paths[file_path]
        revealer = drawer_row.props.drawer_widget
        if revealer is None:
            return
        drawer_box = revealer.get_child()
        if drawer_box is None:
            return

        def _do():
            try:
                project_path = self._project_path or ""
                result = diff_file_against(project_path, sha, file_path)
            except Exception as e:
                result = GitResult(success=False, stdout="", error=str(e))
            _mod().GLib.idle_add(lambda: self._on_historical_diff_loaded(
                result, sha, file_path, stack, drawer_box))

        _mod().threading.Thread(target=_do, daemon=True).start()

    def _on_historical_diff_loaded(self, result, sha: str, file_path: str,
                                   stack: Gtk.Stack, drawer_box: Gtk.Box) -> None:
        """Render diff from a historical commit in the diff_box and show revert button."""
        if file_path not in self._drawer_paths:
            return
        drawer_row: FileTreeRow = self._drawer_paths[file_path]
        revealer = drawer_row.props.drawer_widget
        if revealer is None:
            return
        current_drawer_box = revealer.get_child()
        if current_drawer_box is not drawer_box:
            return  # Stale drawer_box

        # Switch to diff view
        stack.set_visible_child_name("diff")

        diff_box = getattr(drawer_box, '_diff_box', None)
        if diff_box is None:
            return

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

        # Store selected sha on drawer for revert
        drawer_row.props.history_selected_sha = sha

        # Store diff text for clipboard
        drawer_box._diff_text = result.stdout

        # Show revert button
        revert_btn = getattr(drawer_box, '_revert_btn', None)
        if revert_btn is not None:
            revert_btn.set_visible(True)

    def _on_drawer_revert_clicked(self, file_path: str, drawer_box: Gtk.Box) -> None:
        """Show confirmation dialog before reverting a file to a historical commit."""
        if file_path not in self._drawer_paths:
            return
        drawer_row: FileTreeRow = self._drawer_paths[file_path]
        target_sha = drawer_row.props.history_selected_sha
        if not target_sha or not self._project_handler or not self._project_name:
            return

        root = self.get_root()
        dialog = Gtk.MessageDialog(
            transient_for=root if isinstance(root, Gtk.Window) else None,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"Revert {file_path}?",
            secondary_text=f"This will restore the file to its state from commit "
                           f"{target_sha[:7]}. Any uncommitted changes will be lost."
        )
        dialog.connect("response", lambda d, r:
            self._on_drawer_revert_confirmed(d, r, file_path, target_sha, drawer_box))
        dialog.present()

    def _on_drawer_revert_confirmed(self, dialog, response_id: int, file_path: str,
                                    target_sha: str, drawer_box: Gtk.Box) -> None:
        """Handle revert confirmation — call ProjectHandler and reload diff."""
        # BUG #2: Guard against None dialog
        if dialog is not None:
            dialog.destroy()
        if response_id != Gtk.ResponseType.YES:
            return

        # Validate drawer still exists
        if file_path not in self._drawer_paths:
            return
        drawer_row: FileTreeRow = self._drawer_paths[file_path]
        revealer = drawer_row.props.drawer_widget
        if revealer is None:
            return
        current_drawer_box = revealer.get_child()
        if current_drawer_box is not drawer_box:
            return  # Stale drawer_box

        # BUG #1: Wrap revert in try/except — show error in diff_box on failure
        if self._project_name:
            try:
                self._project_handler.revert_file_to_sha(self._project_name, file_path, target_sha)
            except Exception as e:
                diff_box = getattr(drawer_box, '_diff_box', None)
                if diff_box is not None:
                    while diff_box.get_first_child() is not None:
                        diff_box.remove(diff_box.get_first_child())
                    error_lbl = Gtk.Label(label=f"Revert failed: {e}")
                    error_lbl.add_css_class("diff-viewer-subtitle")
                    error_lbl.set_margin_top(12)
                    error_lbl.set_margin_bottom(12)
                    diff_box.append(error_lbl)
                return

        # Switch back to Diff tab
        diff_tab = getattr(drawer_box, '_diff_tab', None)
        if diff_tab is not None:
            diff_tab.set_active(True)

        # BUG #4: Reset state to prevent accidental double-revert
        drawer_row.props.history_selected_sha = None
        revert_btn = getattr(drawer_box, '_revert_btn', None)
        if revert_btn is not None:
            revert_btn.set_visible(False)

        # Reset history tab so it can be re-fetched after revert
        drawer_row.props.history_loaded = False

        # Reload current diff content
        self._load_current_diff(file_path)

    def _load_current_diff(self, file_path: str) -> None:
        """Reload the current working-tree diff for a file (e.g. after revert)."""
        if file_path not in self._drawer_paths:
            return
        drawer_row = cast(FileTreeRow, self._drawer_paths.get(file_path))
        if drawer_row is None:
            return
        revealer = drawer_row.props.drawer_widget
        if revealer is None:
            return
        drawer_box = revealer.get_child()
        if drawer_box is None or not isinstance(drawer_box, Gtk.Box):
            return

        # Clear existing diff content
        diff_box = getattr(drawer_box, '_diff_box', None)
        if diff_box is not None:
            while diff_box.get_first_child() is not None:
                diff_box.remove(diff_box.get_first_child())

        # Resolve checkpoint SHA
        project_path = self._project_path or ""
        checkpoint_sha = None
        if self._project_handler and self._project_name:
            try:
                from models.review_state import ReviewState
                review_state = self._project_handler.get_review_state(self._project_name)
                if review_state and review_state.is_active():
                    checkpoint_sha = review_state.checkpoint_sha
            except Exception:
                pass

        self._load_drawer_diff(file_path, drawer_box, project_path, checkpoint_sha)

    def _on_history_key_pressed(self, keyval: int, history_list: Gtk.ListBox) -> bool:
        """Handle Enter key in history list to activate selected row."""
        if keyval == Gdk.KEY_Return or keyval == Gdk.KEY_KP_Enter:
            selected = history_list.get_selected_row()
            if selected is not None and isinstance(selected, Gtk.ListBoxRow) and selected.get_activatable():
                history_list.emit("row-activated", selected)
                return True
        return False

    def _on_drawer_key_pressed(self, keyval: int, keycode: int, state: Gdk.ModifierType,
                               file_path: str, drawer_box: Gtk.Box) -> bool:
        """Handle keyboard shortcuts in the drawer: Escape closes, Ctrl+C copies."""
        # Escape: close the drawer
        if keyval == Gdk.KEY_Escape:
            if file_path not in self._drawer_paths:
                return False
            drawer_row: FileTreeRow = self._drawer_paths[file_path]
            revealer = drawer_row.props.drawer_widget
            if revealer is None or not revealer.get_reveal_child():
                return False
            self._toggle_drawer(file_path)
            self._column_view.grab_focus()
            return True

        # Ctrl+C: copy diff text to clipboard
        if (keyval == Gdk.KEY_c or keyval == Gdk.KEY_C) and (state & Gdk.ModifierType.CONTROL_MASK):
            # BUG #1: Only return True if copy succeeded
            return self._copy_drawer_diff_to_clipboard(drawer_box)

        return False

    def _on_copy_diff_to_clipboard(self, file_path: str, drawer_box: Gtk.Box) -> bool:
        """Button click handler — delegates to _copy_drawer_diff_to_clipboard.
        Returns True if the copy succeeded, False otherwise.
        """
        return self._copy_drawer_diff_to_clipboard(drawer_box)

    def _copy_drawer_diff_to_clipboard(self, drawer_box: Gtk.Box) -> bool:
        """Copy the diff text from a drawer to the system clipboard.
        Returns True if the copy succeeded, False otherwise.
        """
        diff_text = getattr(drawer_box, '_diff_text', None)
        if not diff_text:
            return False
        # BUG #2: None check for display
        display = Gdk.Display.get_default()
        if display is None:
            return False
        clipboard = display.get_clipboard()
        clipboard.set(diff_text)
        return True
