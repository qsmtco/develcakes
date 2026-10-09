# ui/views/file_tree.py
# File tree widget — GTK4 ColumnView with Gio.ListStore + FileTreeRow GObject model.
#
# Phase 1: Row widget, data model, factory, ColumnView setup.
# Expand/collapse, drawer toggle, and diff loading are in Phases 2-7.
#
# Public API:
#   tree = FileTree(on_file_selected=None)
#   tree.load_project(name, path)  # load a project root
#   tree.navigate_back()            # return to project picker

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gio', '2.0')
from gi.repository import Gtk, GLib, Gdk, Gio, GObject, Pango

import os
import threading
import time
from typing import Optional, cast

from utils.escaping import escape_for_pango
from utils.projects import scan_directory
from utils.git_ops import (
    diff_file_against_working_tree, diff_working_tree, file_log, diff_file_against, GitResult,
)
from utils.diff_parser import parse_diff
from ui.views.diff_card import render_diff_hunks, get_lang_from_path
from utils.file_icons import get_icon_for_path, guess_mime


# ── Module-level helpers ────────────────────────────────────────────────


def format_size(bytes_: int) -> str:
    """Human-readable file size. Float division for fractional KB/MB."""
    if bytes_ <= 0:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    val = float(bytes_)
    for unit in units:
        if val < 1024:
            if unit == "B":
                return f"{int(val)} B"
            return f"{val:.1f} {unit}".replace(".0 ", " ")
        val /= 1024.0
    return f"{val:.1f} PB"


def format_mtime(mtime_ns: int) -> str:
    """Relative time from nanosecond timestamp. Integer division (BUG #14)."""
    if mtime_ns < 1_000_000_000:  # sub-second-since-epoch is invalid (BUG #5)
        return "—"
    from datetime import datetime
    dt = datetime.fromtimestamp(mtime_ns // 1_000_000_000)
    now = datetime.now()
    diff = now - dt
    if diff.days < 0:  # future timestamp — show absolute date
        return dt.strftime("%b %d, %Y")
    if diff.days == 0:
        if diff.seconds < 60:
            return "just now"
        if diff.seconds < 3600:
            return f"{diff.seconds // 60}m ago"
        return f"{diff.seconds // 3600}h ago"
    if diff.days == 1:
        return "yesterday"
    if diff.days < 7:
        return f"{diff.days}d ago"
    if diff.days < 30:
        return f"{diff.days // 7}w ago"
    return dt.strftime("%b %d")


def git_status_to_display(status_code: str) -> str:
    """2-char porcelain → single-char badge. Index col has precedence."""
    if not status_code or len(status_code) < 2:
        return ""
    char = status_code[0] if status_code[0] != ' ' else status_code[1]
    return {'M': 'M', 'A': 'A', 'D': 'D', 'R': 'R', 'C': 'C', '?': '?', '!': '!'}.get(char, "")


from ui.file_tree.drawer import FileTreeDrawerMixin
from ui.file_tree.rows import (
    FileTreeFactory,
    FileTreeModifiedFactory,
    FileTreeRow,
    FileTreeRowWidget,
    FileTreeSizeFactory,
    FileTreeStatusFactory,
)

# ── FileTree — Main widget class ─────────────────────────────────────────

class FileTree(FileTreeDrawerMixin, Gtk.Box):
    """
    File tree browser widget.
    Displays project list, or directory tree when a project is selected.
    """

    def __init__(self, on_file_selected=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._on_file_selected = on_file_selected
        # Project list handler — wired by window via set_project_list_handler()
        self._project_list_handler = None
        # On project opened callback — wired by window via set_on_project_opened()
        self._on_project_opened = None
        # On create project callback — wired by window via set_on_create_project()
        self._on_create_project = None
        # Callback when navigate_back is called — window wires this to close project tabs
        self._on_navigate_back = None

        # Project state
        self._project_name = None
        self._project_path = None
        self._project_history = []  # stack of paths for back navigation
        # ProjectHandler reference — set externally for checkpoint SHA resolution
        self._project_handler = None

        # Phase 1: Drawer state tracking (replaces old self._drawers dict)
        self._drawer_paths: dict[str, FileTreeRow] = {}  # file_path -> drawer row object
        self._loaded_drawers: set[str] = set()
        self._last_toggle_per_file: dict[str, float] = {}
        # Per-parent async load tokens: parent full_path -> latest request id.
        # Only the newest load for a given directory may insert children.
        self._dir_load_requests: dict[str, int] = {}

        # Phase 2: Git status stub callback — wired by handler in Phase 4
        self._on_get_git_status = None
        # Phase 2: Git status map for child rows — set in _show_tree, used in _on_directory_loaded
        self._git_status_map: dict[str, str] = {}

        # Phase 4: Column references for drawer width fix (DRAWER-WIDTH-FIX)
        self._col_status = None
        self._col_size = None
        self._col_modified = None

        # Phase 3: Sort/filter state (FilterListModel only — sort is local)
        self._filter_model: Gtk.FilterListModel | None = None
        self._sort_dropdown = None  # created in _build_header
        self._current_sort_mode = "name_asc"  # tracked for re-apply on subtree expand
        self._search_timeout_id = None  # BUG #9: tree search debounce timeout

        # Phase 3: Callbacks to handler (Phase 4 wires these)
        self._on_sort_changed = None
        self._on_get_sort_mode = None

        # ── Header ────────────────────────────────────────────────────────
        self._header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self._header.set_halign(Gtk.Align.FILL)
        self._header.set_margin_start(4)
        self._header.set_margin_end(4)
        self._header.set_margin_top(4)
        self._header.set_margin_bottom(4)

        self._back_btn = Gtk.Button()
        self._back_btn.set_tooltip_text("Back to projects")
        self._back_btn.set_size_request(28, 28)
        self._back_btn.add_css_class("flat")
        back_img = Gtk.Image.new_from_icon_name("go-previous-symbolic")
        back_img.set_pixel_size(20)
        self._back_btn.set_child(back_img)
        self._back_btn.connect("clicked", self._on_back_clicked)
        self._back_btn.set_visible(False)

        self._folder_icon = Gtk.Image.new_from_icon_name("folder-symbolic")
        self._folder_icon.set_pixel_size(18)
        self._folder_icon.set_margin_end(6)

        self._title_lbl = Gtk.Label()
        self._title_lbl.set_halign(Gtk.Align.START)
        self._title_lbl.add_css_class("project-selector-title")

        # Search entry — visible only in picker mode
        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Search projects...")
        self._search_entry.set_hexpand(True)
        self._search_entry.set_valign(Gtk.Align.CENTER)
        self._search_changed_handler_id = self._search_entry.connect("search-changed", self._on_search_changed)
        self._search_entry.set_visible(False)

        # Phase 3: Sort dropdown — visible only in tree mode
        self._sort_dropdown = Gtk.DropDown.new_from_strings([
            "Name ↑", "Name ↓", "Modified ↑", "Modified ↓", "Size ↑", "Size ↓"
        ])
        self._sort_dropdown.set_selected(0)
        self._sort_dropdown.set_valign(Gtk.Align.CENTER)
        self._sort_dropdown.add_css_class("file-tree-sort-dropdown")
        self._sort_dropdown.set_visible(False)  # hidden until _show_tree
        self._sort_dropdown_handler_id = self._sort_dropdown.connect(
            "notify::selected", self._on_sort_dropdown_changed)

        self._header.append(self._back_btn)
        self._header.append(self._folder_icon)
        self._header.append(self._title_lbl)
        self._header.append(self._search_entry)
        self._header.append(self._sort_dropdown)

        # Status label for copy confirmation (transient, ~2.5s)
        self._tree_copy_status_label = Gtk.Label()
        self._tree_copy_status_label.add_css_class("dim-label")
        self._tree_copy_status_label.set_halign(Gtk.Align.END)
        self._tree_copy_status_label.set_valign(Gtk.Align.CENTER)
        self._tree_copy_status_label.set_margin_end(8)
        self._tree_copy_status_label.set_visible(False)  # start hidden
        self._tree_copy_status_timeout_id = None
        self._header.append(self._tree_copy_status_label)

        # ── Phase 1: ColumnView + ListStore ───────────────────────────────
        self._store = Gio.ListStore.new(FileTreeRow.__gtype__)
        self._selection = Gtk.SingleSelection.new(self._store)
        self._column_view = Gtk.ColumnView.new(self._selection)
        self._column_view.set_show_row_separators(False)
        self._column_view.set_show_column_separators(False)
        self._column_view.add_css_class("file-tree-column-view")

        factory = FileTreeFactory(self)
        column = Gtk.ColumnViewColumn.new("Name", factory)
        column.set_expand(True)
        self._column_view.append_column(column)

        # Key controller for keyboard nav (Esc, Ctrl+C, Enter)
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self._column_view.add_controller(key_controller)

        # Row activation (double-click)
        self._column_view.connect("activate", self._on_row_activated)

        # ScrolledWindow
        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_vexpand(True)
        self._scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroll.set_child(self._column_view)

        # Content widget — switches between ColumnView (tree mode) and card box (picker mode)
        self._content = self._scroll
        self.append(self._header)
        self.append(self._content)

        # Load project picker on init
        self._show_project_picker()

    # ── Public API ────────────────────────────────────────────────────────

    def _clear_all_state(self) -> None:
        """Clear all FileTree state — store, drawers, async requests.

        Called on project switch (navigate_back) and when switching to project picker.
        """
        # Clear the store
        while self._store.get_n_items() > 0:
            self._store.remove(0)

        # Clear drawer state
        self._drawer_paths.clear()
        self._loaded_drawers.clear()
        self._last_toggle_per_file.clear()
        self._update_column_visibility_for_drawers()

        # Clear git status map
        self._git_status_map = {}

        # BUG #9: cancel outstanding search timeout
        if self._search_timeout_id is not None:
            try:
                GLib.source_remove(self._search_timeout_id)
            except Exception:
                pass
            self._search_timeout_id = None

        # Clear filter model reference (will be recreated in _init_sort_filter)
        self._filter_model = None

        # Invalidate any in-flight async requests (per-parent tokens)
        self._dir_load_requests.clear()

    def load_project(self, name, path):
        """Load a project root and show its directory tree."""
        self._project_name = name
        self._project_path = path
        self._project_history.clear()
        if self._on_project_opened:
            self._on_project_opened(name, path)
        self._show_tree(name, path)

    def navigate_back(self, fire_callback: bool = True):
        """
        Return to the project picker. Fires on_navigate_back if set.

        Args:
            fire_callback: If True (default), fires _on_navigate_back callback.
                          Pass False when caller manages the callback to avoid double-fire.
        """
        project_name = self._project_name  # capture before clearing
        self._project_name = None
        self._project_path = None
        self._project_history.clear()
        # Clear all FileTree state
        self._clear_all_state()
        # Clear search when returning to picker
        if self._project_list_handler:
            self._project_list_handler.clear_search()
        # Block signal to prevent _on_search_changed from firing while FileTree
        # is still inside the nested notebook (would build cards in wrong parent).
        self._search_entry.handler_block(self._search_changed_handler_id)
        try:
            self._search_entry.set_text("")
        finally:
            self._search_entry.handler_unblock(self._search_changed_handler_id)
        if fire_callback and self._on_navigate_back:
            self._on_navigate_back(project_name)
        self._show_project_picker()

    def set_on_navigate_back(self, cb):
        """Set callback for when navigate_back is called. cb(project_name)."""
        self._on_navigate_back = cb

    def set_on_project_opened(self, cb):
        """Set callback for when a project is opened (name, path)."""
        self._on_project_opened = cb

    def set_on_create_project(self, cb):
        """Set callback for creating a new project. cb(name) -> path | None."""
        self._on_create_project = cb

    def set_project_list_handler(self, handler):
        """Set the ProjectListHandler — provides project data and colors for cards."""
        self._project_list_handler = handler
        # Refresh the picker if it's currently showing
        self._show_project_picker()

    def set_project_handler(self, handler) -> None:
        """Set ProjectHandler reference for checkpoint SHA resolution in diff loading."""
        self._project_handler = handler

    def set_on_get_git_status(self, cb):
        """Set callback to fetch git status dict {rel_path: code} from handler.
        Returns dict[str, str]. Called by _show_tree when populating root rows."""
        self._on_get_git_status = cb

    # ── Phase 3: Sort/Filter Model Chain ──────────────────────────────

    def _init_sort_filter(self) -> None:
        """Create FilterListModel for search filtering (no SortListModel).

        The sort is applied locally at insertion time (see _sort_range) rather
        than via a global SortListModel. A flat SortListModel cannot preserve
        tree hierarchy — it mixes children from different parents. Local sort
        keeps children grouped under their parent.
        """
        self._filter_model = Gtk.FilterListModel.new(self._store, None)
        self._selection.set_model(self._filter_model)

    def _apply_sort(self, sort_mode: str) -> None:
        """Re-sort the entire store in-place, preserving tree hierarchy.

        Walks the store and sorts each group of siblings (items sharing the
        same parent_full_path) locally. This keeps children under their parent
        directory, which a global SortListModel cannot do.
        """
        self._current_sort_mode = sort_mode
        self._sort_store_in_place()

    def _sort_store_in_place(self) -> None:
        """Sort the store in-place using full-path hierarchical ordering.

        Each row is sorted by its position in the tree: the parent directory's
        full_path determines which sibling group it belongs to, and within each
        group, items sort by the current sort mode. A parent directory appears
        immediately before its children because the parent's full_path is a
        prefix of its children's parent_full_path grouping key.
        """
        if self._store.get_n_items() == 0:
            return

        # Extract all items
        all_items: list[FileTreeRow] = []
        for i in range(self._store.get_n_items()):
            all_items.append(cast(FileTreeRow, self._store.get_item(i)))

        # Build a hierarchical sort key for each item.
        # The key is: (ancestor_chain, group_rank, sort_value)
        # ancestor_chain = the list of parent directories from root to this item's parent.
        # This ensures items are ordered by their position in the tree.
        import functools

        # Sort contiguous sibling groups (items sharing the same parent_full_path)
        sorted_items: list[FileTreeRow] = []
        i = 0
        while i < len(all_items):
            group_parent = all_items[i].props.parent_full_path or ""
            j = i
            while j < len(all_items) and (all_items[j].props.parent_full_path or "") == group_parent:
                j += 1
            group = all_items[i:j]
            # Separate drawers from regular items — drawers stay at insertion
            # position (adjacent to their file).
            non_drawers = [item for item in group if not item.props.is_drawer]
            drawers = [item for item in group if item.props.is_drawer]
            # Sort non-drawer items
            non_drawers.sort(key=functools.cmp_to_key(self._make_group_comparator()))
            # Re-insert drawers right after their parent file (matched by full_path)
            sorted_group: list[FileTreeRow] = []
            for item in non_drawers:
                sorted_group.append(item)
                for d in drawers:
                    if d.props.full_path == item.props.full_path:
                        sorted_group.append(d)
            sorted_items.extend(sorted_group)
            i = j

        # Save selection by object identity before splice (BUG #2)
        selected_row = None
        if self._selection:
            pos = self._selection.get_selected()
            if pos >= 0 and self._filter_model and pos < self._filter_model.get_n_items():
                selected_row = self._filter_model.get_item(pos)

        # Rebuild the store
        self._store.splice(0, self._store.get_n_items(), sorted_items)

        # Restore selection by object identity (BUG #2)
        if selected_row is not None and self._filter_model:
            for k in range(self._filter_model.get_n_items()):
                if self._filter_model.get_item(k) is selected_row:
                    self._selection.set_selected(k)
                    break

    def _make_group_comparator(self):
        """Return a comparator function for sibling groups based on current sort mode."""
        import os as _os
        mode = self._current_sort_mode

        def group_rank(row):
            if row.props.is_dir:
                return 0
            if row.props.is_drawer:
                return 2
            return 1

        def sort_name(row):
            return (row.props.display_name or "").casefold()

        def cmp(a, b):
            # Rule 1: dirs before files before drawers
            ga, gb = group_rank(a), group_rank(b)
            if ga != gb:
                return -1 if ga < gb else 1

            # Rule 2: file-before-drawer tiebreaker (when names match)
            na, nb = sort_name(a), sort_name(b)
            if na == nb:
                if not a.props.is_drawer and b.props.is_drawer:
                    return -1
                if a.props.is_drawer and not b.props.is_drawer:
                    return 1

            # Rule 3: apply sort mode
            if mode in ("name_asc", "name_desc"):
                if na != nb:
                    if mode == "name_asc":
                        return -1 if na < nb else 1
                    else:
                        return 1 if na < nb else -1
                return 0

            if mode in ("modified_asc", "modified_desc"):
                ta, tb = a.props.modified_time, b.props.modified_time
                if ta != tb:
                    if mode == "modified_asc":
                        return -1 if ta < tb else 1
                    else:
                        return 1 if ta < tb else -1
                return -1 if na < nb else (1 if na > nb else 0)

            if mode in ("size_asc", "size_desc"):
                sa, sb = a.props.file_size, b.props.file_size
                if sa != sb:
                    if mode == "size_asc":
                        return -1 if sa < sb else 1
                    else:
                        return 1 if sa < sb else -1
                return -1 if na < nb else (1 if na > nb else 0)

            return -1 if na < nb else (1 if na > nb else 0)

        return cmp

    @staticmethod
    def _set_dropdown_silently(dropdown, handler_id: int, index: int) -> None:
        """Set dropdown selection without firing notify::selected. Exception-safe."""
        dropdown.handler_block(handler_id)
        try:
            dropdown.set_selected(index)
        finally:
            dropdown.handler_unblock(handler_id)

    def _apply_filter(self, query: str) -> None:
        """In-place filter change. casefold() for Unicode-safe match (BUG #12)."""
        if self._filter_model is None:
            return
        if not query:
            self._filter_model.set_filter(None)
            return
        custom_filter = Gtk.CustomFilter.new(
            lambda item, q=query: FileTree._filter_func(item, q)
        )
        self._filter_model.set_filter(custom_filter)

    @staticmethod
    def _filter_func(item, query: str) -> bool:
        """Substring match on name + path. casefold() (BUG #12).
        Drawer rows pass through via parent_full_path (BUG #18, #26).
        Defensive None handling for query, full_path, parent_full_path.
        """
        if query is None:
            return False
        if not query:
            return True
        if item is None:  # BUG #24: race on concurrent mutation
            return False
        row = cast(FileTreeRow, item)
        q = query.casefold()
        name = (row.props.display_name or "").casefold()
        if row.props.is_drawer:
            parent = (row.props.parent_full_path or "").casefold()
            return q in name or q in parent
        path = (row.props.full_path or "").casefold()
        return q in name or q in path

    # ── Phase 3: Sort dropdown handler ─────────────────────────────────

    def _on_sort_dropdown_changed(self, dropdown, pspec):
        """Handle sort selection — update sort model + notify handler."""
        selected = dropdown.get_selected()
        modes = ["name_asc", "name_desc", "modified_asc", "modified_desc",
                 "size_asc", "size_desc"]
        mode = modes[selected] if 0 <= selected < len(modes) else "name_asc"
        self._apply_sort(mode)
        if self._on_sort_changed:
            self._on_sort_changed(mode)

    # ── Phase 3: Setters for sort-related callbacks ────────────────────

    def set_on_sort_changed(self, cb):
        """Set callback for sort mode changes. cb(mode_str)."""
        self._on_sort_changed = cb

    def set_on_get_sort_mode(self, cb):
        """Set callback to fetch saved sort mode. cb() -> str."""
        self._on_get_sort_mode = cb



    # ── Private ───────────────────────────────────────────────────────────

    def _show_project_picker(self):
        """Show project cards (replaces ColumnView tree rows)."""
        # Clear all FileTree state
        self._clear_all_state()
        self._back_btn.set_visible(False)
        self._folder_icon.set_visible(False)
        self._title_lbl.set_markup(
            '<span foreground="#6b6b7a" font_desc="Sans 11">Projects</span>'
        )
        # Phase 2: search visible in both modes
        self._search_entry.set_visible(True)
        self._search_entry.set_placeholder_text("Search projects...")
        # Phase 3: hide sort dropdown in picker mode
        self._sort_dropdown.set_visible(False)
        # Rebuild title to not expand so search entry gets space
        self._title_lbl.set_hexpand(False)

        # Phase 2: Reset ColumnView to single Name column for picker mode
        for col in list(self._column_view.get_columns()):
            self._column_view.remove_column(col)
        factory = FileTreeFactory(self)
        col_name = Gtk.ColumnViewColumn.new("Name", factory)
        col_name.set_expand(True)
        self._column_view.append_column(col_name)

        # Build card grid
        card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card_box.set_margin_start(8)
        card_box.set_margin_end(8)
        card_box.set_margin_top(4)
        card_box.set_spacing(6)

        if self._project_list_handler is None:
            # No handler wired — show nothing (degraded but non-crashing)
            pass
        else:
            # New project card (always first)
            new_card = self._make_new_project_card()
            card_box.append(new_card)

            # Use filtered results (respects current search query)
            projects = self._project_list_handler._filtered_projects()
            if not projects:
                query = self._project_list_handler._search_query
                if query:
                    empty_lbl = Gtk.Label(label=f"No projects matching \"{query}\"")
                else:
                    empty_lbl = Gtk.Label(label="No projects found")
                empty_lbl.add_css_class("dim-label")
                card_box.append(empty_lbl)
            else:
                for name, path, color in projects:
                    card = self._make_project_card(name, path, color)
                    card_box.append(card)

        # Replace ColumnView content with card box
        self.remove(self._content)
        self._content = card_box
        self.append(self._content)

    def _make_project_card(self, name: str, path: str, color_hex: str) -> Gtk.Widget:
        """
        Build a project card widget: [folder_icon] [name] [path]
        Colored folder icon with first letter of project name.
        """
        from utils.icons import render_folder_icon

        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        card.set_halign(Gtk.Align.FILL)
        card.set_spacing(10)
        card.set_margin_top(4)
        card.set_margin_bottom(4)
        card.add_css_class("project-card")

        # Folder icon (44x44)
        letter = name[0].upper() if name else "?"
        texture = render_folder_icon(color_hex, letter, size=44)

        icon_pic = Gtk.Picture()
        icon_pic.set_size_request(44, 44)
        if texture is not None:
            icon_pic.set_paintable(texture)
        else:
            fallback = Gtk.Label(label=letter)
            fallback.set_halign(Gtk.Align.CENTER)
            fallback.set_valign(Gtk.Align.CENTER)
            icon_pic.set_child(fallback)

        # Text column
        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        text_box.set_valign(Gtk.Align.CENTER)

        name_lbl = Gtk.Label(label=name)
        name_lbl.set_halign(Gtk.Align.START)
        name_lbl.add_css_class("project-card-name")

        path_lbl = Gtk.Label(label=path)
        path_lbl.set_halign(Gtk.Align.START)
        path_lbl.add_css_class("project-card-path")

        text_box.append(name_lbl)
        text_box.append(path_lbl)

        card.append(icon_pic)
        card.append(text_box)

        # Single-click opens project
        ev = Gtk.GestureClick()
        ev.connect("pressed", lambda *a: self.load_project(name, path))
        card.add_controller(ev)

        return card

    def _make_new_project_card(self) -> Gtk.Widget:
        """Build the '+' new project card with dashed border."""
        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        card.set_halign(Gtk.Align.FILL)
        card.set_spacing(10)
        card.set_margin_top(4)
        card.set_margin_bottom(4)
        card.add_css_class("new-project-card")

        plus_lbl = Gtk.Label(label="+")
        plus_lbl.set_halign(Gtk.Align.CENTER)
        plus_lbl.set_valign(Gtk.Align.CENTER)
        plus_lbl.set_size_request(44, 44)
        plus_lbl.add_css_class("new-project-plus")

        text_lbl = Gtk.Label(label="New Project")
        text_lbl.set_halign(Gtk.Align.START)
        text_lbl.set_valign(Gtk.Align.CENTER)
        text_lbl.add_css_class("dim-label")

        card.append(plus_lbl)
        card.append(text_lbl)

        ev = Gtk.GestureClick()
        ev.connect("pressed", lambda *a: self._show_create_popover(card))
        card.add_controller(ev)

        return card

    def _show_create_popover(self, anchor: Gtk.Widget):
        """Show a popover form to create a new project."""
        if not self._on_create_project:
            return

        popover = Gtk.Popover()
        popover.set_parent(anchor)
        popover.set_position(Gtk.PositionType.BOTTOM)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        vbox.set_margin_start(12)
        vbox.set_margin_end(12)
        vbox.set_margin_top(8)
        vbox.set_margin_bottom(8)

        name_entry = Gtk.Entry()
        name_entry.set_placeholder_text("Project name")
        name_entry.set_hexpand(True)

        create_btn = Gtk.Button(label="Create")
        create_btn.add_css_class("suggested-action")

        def on_create(_btn):
            name = name_entry.get_text().strip()
            if not name:
                return
            result = self._on_create_project(name)
            if result is not None:
                popover.popdown()
                self.load_project(name, result)

        create_btn.connect("clicked", on_create)

        name_entry.connect("activate", lambda _e: on_create(create_btn))

        vbox.append(name_entry)
        vbox.append(create_btn)
        popover.set_child(vbox)
        popover.popup()

        GLib.idle_add(lambda: name_entry.grab_focus() and False)

    def _show_tree(self, name, path):
        """Show the directory tree for a project. Populates ListStore with root entries."""
        # Swap card box back to scroll/ColumnView
        if self._content != self._scroll:
            self.remove(self._content)
            self._content = self._scroll
            self.append(self._content)
        # Clear all FileTree state
        self._clear_all_state()
        self._back_btn.set_visible(True)
        self._folder_icon.set_visible(True)
        safe_name = escape_for_pango(name)
        title_markup = f"<b>{safe_name}</b>"
        # Pre-validate markup to avoid Gtk-WARNING and empty label on parse
        # failure. Falls back to set_text so content is never lost.
        # See commit 898062a post-mortem.
        try:
            Pango.parse_markup(title_markup, -1, "\x00")
            self._title_lbl.set_markup(title_markup)
        except Exception:
            self._title_lbl.set_text(name)
        self._title_lbl.set_use_markup(True)
        self._title_lbl.set_hexpand(True)
        # Phase 2: search visible in both modes
        self._search_entry.set_visible(True)
        self._search_entry.set_placeholder_text("Search files...")
        # Phase 3: sort dropdown visible only in tree mode
        self._sort_dropdown.set_visible(True)

        # Phase 2: Remove existing columns, add 4-column layout
        for col in list(self._column_view.get_columns()):
            self._column_view.remove_column(col)

        factory_name = FileTreeFactory(self)
        col_name = Gtk.ColumnViewColumn.new("Name", factory_name)
        col_name.set_expand(True)
        self._column_view.append_column(col_name)

        self._col_status = Gtk.ColumnViewColumn.new("Status", FileTreeStatusFactory())
        self._col_status.set_fixed_width(60)
        self._column_view.append_column(self._col_status)

        self._col_size = Gtk.ColumnViewColumn.new("Size", FileTreeSizeFactory())
        self._col_size.set_fixed_width(50)
        self._column_view.append_column(self._col_size)

        self._col_modified = Gtk.ColumnViewColumn.new("Modified", FileTreeModifiedFactory())
        self._col_modified.set_fixed_width(75)
        self._column_view.append_column(self._col_modified)

        # Phase 2: Query git status via stub callback
        status_map: dict[str, str] = {}
        if self._on_get_git_status:
            status_map = self._on_get_git_status() or {}
        self._git_status_map = status_map

        # Populate root entries
        try:
            entries = scan_directory(path)
        except Exception as e:
            entries = [(f"[error: {type(e).__name__}: {e}]", "", False, 0, 0)]
        for entry_name, full_path, is_dir, size_bytes, mtime_ns in entries:
            icon = get_icon_for_path(full_path, is_dir)
            # Look up git status from status_map
            rel_path = os.path.relpath(full_path, path) if path else full_path
            raw_status = status_map.get(rel_path, "")
            row = FileTreeRow(
                display_name=entry_name,
                full_path=full_path,
                is_dir=is_dir,
                depth=0,
                has_children=is_dir,
                expanded=False,
                file_size=0 if is_dir else size_bytes,
                file_size_display="—" if is_dir else format_size(size_bytes),
                modified_time=mtime_ns // 1_000_000_000 if mtime_ns else 0,
                modified_display=format_mtime(mtime_ns) if mtime_ns else "—",
                git_status=raw_status,
                git_status_display=git_status_to_display(raw_status),
                mime_type=guess_mime(full_path),
                icon_name=icon.icon_name,
                icon_color_class=icon.color_class,
            )
            self._store.append(row)

        # Phase 3: Initialize sort/filter model chain and restore saved sort mode (M6)
        # Reset dropdown to default before restoring (BUG #7 fix)
        FileTree._set_dropdown_silently(self._sort_dropdown, self._sort_dropdown_handler_id, 0)
        self._init_sort_filter()
        # Always apply default sort first (P3-1 fix)
        self._apply_sort("name_asc")
        # Restore saved mode if handler provides one (block signal to avoid feedback loop — BUG #3)
        if self._on_get_sort_mode:
            saved = self._on_get_sort_mode()
            valid = ["name_asc", "name_desc", "modified_asc", "modified_desc",
                     "size_asc", "size_desc"]
            if saved in valid:
                idx = valid.index(saved)
                FileTree._set_dropdown_silently(self._sort_dropdown, self._sort_dropdown_handler_id, idx)
                self._apply_sort(saved)

        # DRAWER-WIDTH-FIX: Ensure columns visible on fresh tree load (no drawers open yet)
        self._update_column_visibility_for_drawers()


    # ── Phase 3: Drawer Row Insertion ──────────────────────────────────










    # ── Phase 5+ Stubs (History, Revert, Keyboard) ────────────────────












    # ── Phase 2: Directory Expand/Collapse ──────────────────────────────

    def _find_row_index(self, row: FileTreeRow) -> Optional[int]:
        """Find the current index of a FileTreeRow in the store.

        Returns None if the row is no longer in the store (e.g. deleted
        by collapse or project switch). Linear scan — safe for typical tree
        sizes. (BUG #2)
        """
        n = self._store.get_n_items()
        for i in range(n):
            if self._store.get_item(i) is row:
                return i
        return None

    def _on_expander_clicked(self, row: FileTreeRow, position: int) -> None:
        """Handle expander button click for a directory row."""
        if position < 0 or position >= self._store.get_n_items():
            return
        # Verify the row at this position is still the one we expect
        current = self._store.get_item(position)
        if current is not row:
            return
        if row.props.expanded:
            self._collapse_directory(position)
        else:
            self._expand_directory(position)

    def _expand_directory(self, row_index: int) -> None:
        """Expand a directory row: load children on background thread, insert into store."""
        if row_index < 0 or row_index >= self._store.get_n_items():
            return
        row: FileTreeRow = self._store.get_item(row_index)
        if not row.props.is_dir or row.props.expanded:
            return

        # Per-parent request token (replaces global BUG #7 counter): only the
        # newest load for THIS directory may insert children. Other
        # directories' in-flight loads are unaffected.
        parent_path = row.props.full_path
        self._dir_load_requests[parent_path] = self._dir_load_requests.get(parent_path, 0) + 1
        request_id = self._dir_load_requests[parent_path]

        # Mark as expanded immediately for UI feedback
        row.props.expanded = True
        parent_depth = row.props.depth

        # BUG #4: Capture parent row OBJECT, not index (sort may move parent)
        parent_row_obj = row

        # BUG #8: Insert loading spinner row
        loading_row = FileTreeRow(
            display_name="Loading...",
            full_path="",
            is_dir=False,
            depth=parent_depth + 1,
        )
        self._store.insert(row_index + 1, loading_row)

        def _do():
            try:
                entries = scan_directory(parent_path)
            except Exception as e:
                entries = [(f"[error: {type(e).__name__}: {e}]", "", False, 0, 0)]
            # BUG #1: Capture loading_row object identity, not position.
            # Store mutations (sibling expand/collapse) can shift positions.
            _loading_row = loading_row
            GLib.idle_add(lambda: self._on_directory_loaded(
                entries, _loading_row, parent_row_obj, parent_depth, request_id
            ))

        threading.Thread(target=_do, daemon=True).start()

    def _on_directory_loaded(self, entries, loading_row: FileTreeRow, parent_row_obj: FileTreeRow, parent_depth: int, request_id: int) -> None:
        """Handle directory scan result on main thread. Guard against stale requests.

        Unconditionally removes the loading spinner row (by object identity)
        before any early return to prevent orphan "Loading..." rows (BUG #1).

        Uses parent_row_obj (object identity) to find the parent's current
        position, which may have shifted due to sort between the expand request
        and the background load completing (BUG #4).
        """
        # Unconditionally remove loading spinner row by object identity
        # Walk the store to find it — survives intervening store mutations
        n = self._store.get_n_items()
        for i in range(n):
            if self._store.get_item(i) is loading_row:
                self._store.remove(i)
                break

        # Per-parent staleness guard: discard unless this is the newest load
        # for THIS directory (superseded by re-expand, or invalidated by
        # _clear_all_state).
        if self._dir_load_requests.get(parent_row_obj.props.full_path) != request_id:
            return

        # BUG #4: Re-find parent row by object identity (sort may have moved it)
        row_index = self._find_row_index(parent_row_obj)
        if row_index is None:
            return  # parent was removed (project switch or collapse)
        if row_index < 0 or row_index >= self._store.get_n_items():
            return
        parent_row = parent_row_obj
        if not parent_row.props.is_dir or not parent_row.props.expanded:
            # Parent was collapsed; loading row already removed above
            return

        # Insert real children
        insert_pos = row_index + 1
        for entry_name, full_path, is_dir, size_bytes, mtime_ns in entries:
            icon = get_icon_for_path(full_path, is_dir)
            rel_path = os.path.relpath(full_path, self._project_path) if self._project_path else full_path
            raw_status = self._git_status_map.get(rel_path, "")
            child = FileTreeRow(
                display_name=entry_name,
                full_path=full_path,
                is_dir=is_dir,
                depth=parent_depth + 1,
                has_children=is_dir,
                expanded=False,
                parent_full_path=parent_row.props.full_path,
                file_size=0 if is_dir else size_bytes,
                file_size_display="—" if is_dir else format_size(size_bytes),
                modified_time=mtime_ns // 1_000_000_000 if mtime_ns else 0,
                modified_display=format_mtime(mtime_ns) if mtime_ns else "—",
                git_status=raw_status,
                git_status_display=git_status_to_display(raw_status),
                mime_type=guess_mime(full_path),
                icon_name=icon.icon_name,
                icon_color_class=icon.color_class,
            )
            self._store.insert(insert_pos, child)
            insert_pos += 1

        # Sort the newly inserted children locally (no global SortListModel)
        self._sort_store_in_place()

    def _collapse_directory(self, row_index: int) -> None:
        """Collapse a directory row: remove all descendants with greater depth."""
        if row_index < 0 or row_index >= self._store.get_n_items():
            return
        row: FileTreeRow = self._store.get_item(row_index)
        if not row.props.is_dir or not row.props.expanded:
            return

        parent_depth = row.props.depth
        row.props.expanded = False

        # Remove all descendants with depth > parent_depth
        i = row_index + 1
        while i < self._store.get_n_items():
            descendant = self._store.get_item(i)
            if descendant.props.depth > parent_depth:
                self._store.remove(i)
                # Don't increment i — next item shifted down
            else:
                break

        # DRAWER-WIDTH-FIX: Clean up stale drawer entries that were removed by collapse,
        # then update column visibility (columns should come back if no drawers remain)
        stale_paths = []
        for path, drawer_row in self._drawer_paths.items():
            alive = False
            for k in range(self._store.get_n_items()):
                if self._store.get_item(k) is drawer_row:
                    alive = True
                    break
            if not alive:
                stale_paths.append(path)
        for path in stale_paths:
            del self._drawer_paths[path]
            self._loaded_drawers.discard(path)
        self._update_column_visibility_for_drawers()

    # ── Row Activation (ColumnView ::activate signal) ─────────────────────

    def _on_row_activated(self, column_view: Gtk.ColumnView, position: int) -> None:
        """
        Handle row activation (double-click or Enter) on the ColumnView.

        Directories toggle expand/collapse, files toggle inline drawer.
        """
        if position < 0 or position >= self._store.get_n_items():
            return
        row: FileTreeRow = self._store.get_item(position)
        if row.props.is_dir:
            # Directory — expand/collapse
            if row.props.expanded:
                self._collapse_directory(position)
            else:
                self._expand_directory(position)
        elif not row.props.is_drawer:
            # File — toggle drawer
            self._toggle_drawer(row.props.full_path)
        # Drawer rows are not activatable

    # ── Keyboard Navigation ───────────────────────────────────────────────

    def _on_key_pressed(self, controller, keyval: int, keycode: int, state: Gdk.ModifierType) -> bool:
        """Handle keyboard shortcuts at the ColumnView level.

        Esc closes the active drawer (any open drawer, not just selected).
        Ctrl+C copies the current diff from the selected drawer row.
        """
        selected_pos = self._selection.get_selected()
        if selected_pos == Gtk.INVALID_LIST_POSITION:
            return False
        if selected_pos < 0 or selected_pos >= self._store.get_n_items():
            return False

        # BUG #3: Escape closes any active drawer — not just the selected row.
        # Iterate the store to find any open drawer and close it.
        if keyval == Gdk.KEY_Escape:
            n = self._store.get_n_items()
            for i in range(n):
                row = cast(FileTreeRow, self._store.get_item(i))
                if row.props.is_drawer and row.props.drawer_widget is not None:
                    revealer = row.props.drawer_widget
                    if revealer.get_reveal_child():
                        file_path = self._find_file_path_for_drawer(i)
                        if file_path:
                            self._toggle_drawer(file_path)
                            self._column_view.grab_focus()
                            return True
            return False

        # Ctrl+C: copy the current diff from the selected drawer row
        if (keyval == Gdk.KEY_c or keyval == Gdk.KEY_C) and (state & Gdk.ModifierType.CONTROL_MASK):
            row: FileTreeRow = self._store.get_item(selected_pos)
            if row.props.is_drawer and row.props.drawer_widget is not None:
                drawer_box = row.props.drawer_widget.get_child()
                if drawer_box is not None:
                    # BUG #1: Only return True if copy succeeded
                    return self._copy_drawer_diff_to_clipboard(drawer_box)
            return False

        return False

    def _find_file_path_for_drawer(self, drawer_pos: int) -> Optional[str]:
        """Find the file_path for a drawer row by walking backwards to find the file row."""
        for i in range(drawer_pos - 1, -1, -1):
            row: FileTreeRow = self._store.get_item(i)
            if not row.props.is_dir and not row.props.is_drawer:
                return row.props.full_path
        return None

    # ── Search ────────────────────────────────────────────────────────────

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
            return GLib.SOURCE_REMOVE

        self._search_timeout_id = GLib.timeout_add(150, _apply)

    def _on_back_clicked(self, button):
        """Navigate back to the project picker."""
        self.navigate_back()

    # ── Right-Click Context Menu (Copy Path / Copy File) ──────────────────

    def _on_tree_row_right_click(self, ctrl, n_press, x, y, widget) -> None:
        """
        Right-click on a file tree row — show the context popover menu.

        Args:
            ctrl:    Gtk.GestureClick (sender).
            n_press: int — number of presses (only respond to single click).
            x, y:    float — local click coordinates (unused).
            widget:  FileTreeRowWidget — the right-clicked widget.
        """
        if n_press != 1:
            return

        # Read the bound row LIVE at click time — never capture in closure.
        # This avoids stale-row bugs from ColumnView recycling.
        row = widget._bound_row
        if row is None:
            return  # unbind/rebind window

        # Skip drawer rows (inline containers, not navigable files/dirs)
        if row.props.is_drawer:
            return

        # Skip loading rows (empty path)
        path = row.props.full_path
        if not path:
            return

        popover = Gtk.Popover()
        popover.set_parent(widget)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        vbox.set_margin_top(6)
        vbox.set_margin_bottom(6)
        vbox.set_margin_start(6)
        vbox.set_margin_end(6)

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)

        # Row 1: Copy Path (always shown — for files and directories)
        copy_path_row = Gtk.ListBoxRow()
        copy_path_row.set_activatable(True)
        copy_path_row.set_selectable(False)
        copy_path_row._action = "copy_path"
        copy_path_label = Gtk.Label(label="Copy Path", xalign=0)
        copy_path_label.set_margin_top(4)
        copy_path_label.set_margin_bottom(4)
        copy_path_label.set_margin_start(8)
        copy_path_label.set_margin_end(8)
        copy_path_row.set_child(copy_path_label)
        list_box.append(copy_path_row)

        # Row 2: Copy File (only shown for files, not directories)
        if not row.props.is_dir:
            copy_file_row = Gtk.ListBoxRow()
            copy_file_row.set_activatable(True)
            copy_file_row.set_selectable(False)
            copy_file_row._action = "copy_file"
            copy_file_label = Gtk.Label(label="Copy File", xalign=0)
            copy_file_label.set_margin_top(4)
            copy_file_label.set_margin_bottom(4)
            copy_file_label.set_margin_start(8)
            copy_file_label.set_margin_end(8)
            copy_file_row.set_child(copy_file_label)
            list_box.append(copy_file_row)

        list_box.connect("row-activated", self._on_tree_menu_row_activated, popover, row)
        vbox.append(list_box)
        popover.set_child(vbox)
        popover.connect("closed", lambda *_: popover.unparent())
        popover.popup()

    def _on_tree_menu_row_activated(self, _lb, menu_row, popover, source_row) -> None:
        """
        One of "Copy Path" / "Copy File" was clicked. Dispatch and dismiss the popover.

        Dispatch uses the menu_row._action attribute (set at row build time), NOT
        the label text — robust to i18n.
        """
        popover.popdown()
        action = getattr(menu_row, "_action", None)
        if action == "copy_path":
            self._on_copy_tree_path(source_row)
        elif action == "copy_file":
            self._on_copy_tree_file(source_row)
        # Unknown action → no-op (defensive).

    def _on_copy_tree_path(self, row) -> None:
        """Copy the absolute path of the right-clicked row to the clipboard."""
        path = row.props.full_path if hasattr(row, 'props') else None
        if not path:
            return
        self._copy_text_to_clipboard(path)
        self._show_tree_copy_status("Copied path")

    def _on_copy_tree_file(self, row) -> None:
        """Copy the file content of the right-clicked row to the clipboard.

        Handles binary files gracefully: on UnicodeDecodeError, copies a
        notice message instead of crashing.
        """
        path = row.props.full_path if hasattr(row, 'props') else None
        if not path:
            return
        try:
            from pathlib import Path
            content = Path(path).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = "<binary file — not copied>"
            # Stay consistent with the drawer's "Binary file — not shown" wording.
        except Exception:
            return  # I/O error — silently skip (no crash)
        self._copy_text_to_clipboard(content)
        self._show_tree_copy_status("Copied file")

    def _copy_text_to_clipboard(self, text: str) -> None:
        """Copy text to the system clipboard using GTK4 clipboard API."""
        display = Gdk.Display.get_default()
        if display is None:
            return
        clipboard = display.get_clipboard()
        clipboard.set(text)

    def _show_tree_copy_status(self, message: str) -> None:
        """Show a transient confirmation in the file tree header for ~2.5s."""
        if self._tree_copy_status_label is None:
            return
        self._tree_copy_status_label.set_text(message)
        self._tree_copy_status_label.set_visible(True)
        # Cancel any pending clear, then schedule a new one.
        if self._tree_copy_status_timeout_id is not None:
            try:
                GLib.source_remove(self._tree_copy_status_timeout_id)
            except Exception:
                pass
            self._tree_copy_status_timeout_id = None

        def _clear():
            if self._tree_copy_status_label is not None:
                self._tree_copy_status_label.set_text("")
                self._tree_copy_status_label.set_visible(False)
            self._tree_copy_status_timeout_id = None
            return GLib.SOURCE_REMOVE

        self._tree_copy_status_timeout_id = GLib.timeout_add(2500, _clear)