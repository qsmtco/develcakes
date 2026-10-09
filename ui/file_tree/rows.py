"""Row model and column factories for the file tree."""
from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gdk, Gtk, GObject, Pango

from typing import Optional, cast

from utils.escaping import escape_for_pango
from utils.file_icons import get_icon_for_path, guess_mime
from ui.views.file_tree import format_mtime, format_size, git_status_to_display

# ── Phase 1: FileTreeRow — GObject data model for Gio.ListStore ─────────

class FileTreeRow(GObject.Object):
    """A single row in the file tree list store.

    Properties are GObject properties so ColumnView factory can bind/unbind them.
    """

    __gtype_name__ = 'FileTreeRow'

    display_name = GObject.Property(type=str, default="")
    full_path = GObject.Property(type=str, default="")
    is_dir = GObject.Property(type=bool, default=False)
    is_drawer = GObject.Property(type=bool, default=False)
    depth = GObject.Property(type=int, default=0)
    expanded = GObject.Property(type=bool, default=False)
    has_children = GObject.Property(type=bool, default=False)

    # Drawer state (mirrors old self._drawers[path] dict)
    drawer_widget = GObject.Property(type=GObject.TYPE_PYOBJECT, default=None)
    is_open = GObject.Property(type=bool, default=False)
    diff_text = GObject.Property(type=str, default="")
    history_selected_sha = GObject.Property(type=GObject.TYPE_PYOBJECT, default=None)
    history_loaded = GObject.Property(type=bool, default=False)

    # Phase 1 — file tree metadata
    file_size = GObject.Property(type=int, default=0)
    file_size_display = GObject.Property(type=str, default="—")
    modified_time = GObject.Property(type=int, default=0)
    modified_display = GObject.Property(type=str, default="—")
    git_status = GObject.Property(type=str, default="")
    git_status_display = GObject.Property(type=str, default="")
    mime_type = GObject.Property(type=str, default="")
    icon_name = GObject.Property(type=str, default="text-x-generic-symbolic")
    icon_color_class = GObject.Property(type=str, default="file-icon-default")
    parent_full_path = GObject.Property(type=str, default="")

    def __init__(self, display_name: str = "", full_path: str = "",
                 is_dir: bool = False, is_drawer: bool = False,
                 depth: int = 0, expanded: bool = False,
                 has_children: bool = False,
                 drawer_widget=None, is_open: bool = False,
                 diff_text: str = "", history_selected_sha=None,
                 history_loaded: bool = False,
                 file_size: int = 0, file_size_display: str = "—",
                 modified_time: int = 0, modified_display: str = "—",
                 git_status: str = "", git_status_display: str = "",
                 mime_type: str = "",
                 icon_name: str = "text-x-generic-symbolic",
                 icon_color_class: str = "file-icon-default",
                 parent_full_path: str = ""):
        super().__init__()
        self.props.display_name = display_name
        self.props.full_path = full_path
        self.props.is_dir = is_dir
        self.props.is_drawer = is_drawer
        self.props.depth = depth
        self.props.expanded = expanded
        self.props.has_children = has_children
        self.props.drawer_widget = drawer_widget
        self.props.is_open = is_open
        self.props.diff_text = diff_text
        self.props.history_selected_sha = history_selected_sha
        self.props.history_loaded = history_loaded
        self.props.file_size = file_size
        self.props.file_size_display = file_size_display
        self.props.modified_time = modified_time
        self.props.modified_display = modified_display
        self.props.git_status = git_status
        self.props.git_status_display = git_status_display
        self.props.mime_type = mime_type
        self.props.icon_name = icon_name
        self.props.icon_color_class = icon_color_class
        self.props.parent_full_path = parent_full_path


# ── Phase 1: FileTreeRowWidget — Per-row Gtk.Box ─────────────────────────

class FileTreeRowWidget(Gtk.Box):
    """Widget for a single row in the ColumnView.

    Contains: expander button, icon, label, drawer_container (for drawer rows).
    """

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.add_css_class("file-tree-row")

        # Expander button (▶/▼ for dirs, spacer for files/drawers)
        self._expander_btn = Gtk.Button()
        self._expander_btn.add_css_class("file-tree-row-expander")
        self._expander_btn.set_size_request(16, 16)
        self._expander_btn.set_halign(Gtk.Align.CENTER)
        self._expander_btn.set_valign(Gtk.Align.CENTER)
        self.append(self._expander_btn)

        # Icon (folder/file)
        self._icon = Gtk.Image()
        self._icon.add_css_class("file-tree-row-icon")
        self._icon.set_pixel_size(16)
        self.append(self._icon)

        # Label (markup for prefix + name)
        self._label = Gtk.Label()
        self._label.add_css_class("file-tree-row-label")
        self._label.set_halign(Gtk.Align.START)
        self._label.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        self._label.set_hexpand(True)
        self._label.set_use_markup(True)
        self.append(self._label)

        # Drawer container — only populated for drawer rows (is_drawer=True)
        self._drawer_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._drawer_container.set_visible(False)
        self.append(self._drawer_container)

        # Track bound row for cleanup
        self._bound_row: Optional[FileTreeRow] = None
        # Phase 2: expander button signal handler ID
        self._expander_handler_id: Optional[int] = None

    def set_depth(self, depth: int) -> None:
        """Set indentation via CSS margin-left on the whole row."""
        self.set_margin_start(depth * 20)

    def set_expanded(self, expanded: bool) -> None:
        """Update expander button label (▶ / ▼)."""
        if expanded:
            self._expander_btn.set_label("▼")
        else:
            self._expander_btn.set_label("▶")

    def set_label(self, display_name: str) -> None:
        """Set label markup. Display name already includes prefix."""
        markup = escape_for_pango(display_name)
        # Pre-validate markup to avoid Gtk-WARNING and empty label on parse
        # failure. Falls back to set_text so content is never lost.
        # See commit 898062a post-mortem.
        try:
            Pango.parse_markup(markup, -1, "\x00")
            self._label.set_markup(markup)
        except Exception:
            self._label.set_text(display_name)

    def set_icon(self, icon_name: str, is_dir: bool, is_drawer: bool) -> None:
        """Set icon based on icon_name. Drawer rows hide the icon."""
        if is_drawer:
            self._icon.set_visible(False)
        else:
            self._icon.set_visible(True)
            self._icon.set_from_icon_name(icon_name)

    def set_icon_color(self, color_class: str) -> None:
        """Remove previous file-icon-* class, add the new one."""
        for cls in list(self._icon.get_css_classes()):
            if cls.startswith("file-icon-"):
                self._icon.remove_css_class(cls)
        if color_class:
            self._icon.add_css_class(color_class)

    def attach_drawer(self, revealer: Gtk.Revealer) -> None:
        """Attach a drawer revealer to this row's container."""
        while self._drawer_container.get_first_child():
            self._drawer_container.remove(self._drawer_container.get_first_child())
        self._drawer_container.append(revealer)
        self._drawer_container.set_visible(True)

    def detach_drawer(self) -> None:
        """Detach drawer revealer — called from factory unbind."""
        while self._drawer_container.get_first_child():
            self._drawer_container.remove(self._drawer_container.get_first_child())
        self._drawer_container.set_visible(False)

    def cleanup(self) -> None:
        """Detach drawer, clear bound row reference."""
        self.detach_drawer()
        self._bound_row = None

    def bind_row(self, row: FileTreeRow) -> None:
        """Store reference to bound row for signal connections."""
        self._bound_row = row


# ── Phase 1: FileTreeFactory — SignalListItemFactory ─────────────────────

class FileTreeFactory(Gtk.SignalListItemFactory):
    """Factory for ColumnView rows. Creates FileTreeRowWidget and binds FileTreeRow properties."""

    def __init__(self, tree: 'FileTree'):
        super().__init__()
        self._tree = tree
        self.connect('setup', self._on_setup)
        self.connect('bind', self._on_bind)
        self.connect('unbind', self._on_unbind)

    def _on_setup(self, factory: 'FileTreeFactory', list_item: Gtk.ListItem) -> None:
        widget = FileTreeRowWidget()
        list_item.set_child(widget)

        # Right-click gesture for context menu (Copy Path / Copy File)
        # NOTE: Gesture is attached ONCE per widget instance. The row is read
        # LIVE at click time from widget._bound_row to avoid stale-row bugs
        # from ColumnView recycling.
        right_ctrl = Gtk.GestureClick()
        right_ctrl.set_button(Gdk.BUTTON_SECONDARY)
        right_ctrl.connect("pressed", self._tree._on_tree_row_right_click, widget)
        widget.add_controller(right_ctrl)

    def _on_bind(self, factory: 'FileTreeFactory', list_item: Gtk.ListItem) -> None:
        row = cast(FileTreeRow, list_item.get_item())
        widget: FileTreeRowWidget = list_item.get_child()

        widget.bind_row(row)
        widget.set_depth(row.props.depth)
        widget.set_expanded(row.props.expanded)
        widget.set_label(row.props.display_name)
        widget.set_icon(row.props.icon_name, row.props.is_dir, row.props.is_drawer)
        widget.set_icon_color(row.props.icon_color_class)

        # Drawer rows: hide label (no text needed), let drawer_container fill space
        if row.props.is_drawer:
            widget._label.set_visible(False)
            widget._label.set_hexpand(False)  # don't compete for space
            widget._drawer_container.set_hexpand(True)
        else:
            widget._label.set_visible(True)
            widget._label.set_hexpand(True)
            widget._drawer_container.set_hexpand(False)

        if row.props.is_drawer and row.props.drawer_widget:
            widget.attach_drawer(row.props.drawer_widget)

        # Phase 2: Wire expander button for directories
        if row.props.is_dir and not row.props.is_drawer:
            # Disconnect previous handler if re-binding
            if widget._expander_handler_id is not None:
                widget._expander_btn.disconnect(widget._expander_handler_id)
            # BUG #2: Pass `row` object instead of stale `position`.
            # The current position is re-queried at click time via _find_row_index.
            widget._expander_handler_id = widget._expander_btn.connect(
                "clicked", lambda btn: self._on_expander_clicked(row)
            )
            widget._expander_btn.set_visible(True)
        else:
            widget._expander_btn.set_visible(False)

    def _on_unbind(self, factory: 'FileTreeFactory', list_item: Gtk.ListItem) -> None:
        widget: FileTreeRowWidget = list_item.get_child()
        widget.cleanup()

    def _on_expander_clicked(self, row: FileTreeRow) -> None:
        """Handle expander button click for a directory row.

        Re-queries the row's current position at click time to handle
        stale indices from prior store mutations (BUG #2).
        """
        position = self._tree._find_row_index(row)
        if position is not None:
            self._tree._on_expander_clicked(row, position)


# ── Phase 2: Multi-column factories (Status, Size, Modified) ────────────

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
        # Clear previous color class, add current (keep the base badge class)
        for cls in list(label.get_css_classes()):
            if cls.startswith("file-tree-status-") and cls != "file-tree-status-badge":
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

