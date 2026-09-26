# ui/views/left_panel.py
# Left sidebar panel — contains the Prompts/Agents/Projects notebook (PAP)

from __future__ import annotations

import logging

import gi
# Require GTK 4.0 — must be called before importing Gtk
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gio, GLib, Gdk

from typing import TYPE_CHECKING

from utils.projects import load_members
from utils.icons import render_agent_icon
from ui.views.file_tree import FileTree
from ui.views.session_menu import show_session_menu

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ui.views.feed_tab import FeedTab


class LeftPanel(Gtk.Box):
    """
    Left sidebar panel widget.
    Contains a Gtk.Notebook with three tabs: Prompts, Agents, Projects.
    """

    def __init__(self, on_prompt_selected=None, on_project_selected=None,
                 file_tree_handler=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        # Prompt callback
        self._on_prompt_selected = on_prompt_selected

        # Project callback
        self._on_project_selected = on_project_selected

        # Agent state — set via set_agents() after gateway connects
        self._agent_names = {}
        self._on_agent_selected = None
        self._agents_list_box = None
        self._agent_list_handler = None  # set via set_agent_list_handler()

        # Project state — set via set_on_project_opened() when a project tab opens
        self._active_project_name = None
        self._on_project_opened = None
        self._toggle_agent_callback = None  # set via set_toggle_agent_callback()

        # Agent builder callbacks — wired by window.py
        self._on_create_agent = None  # set via set_on_create_agent()
        self._on_edit_agent = None    # set via set_on_edit_agent()
        self._on_delete_agent = None  # set via set_on_delete_agent()

        # Prompts tab state — built once in _build_prompts_tab()
        self._prompts_handler = None     # set via set_prompts_handler()
        self._prompts_list_box = None   # rebuilt on search/refresh
        self._prompts_scroll = None
        self._search_entry = None
        self._prompts_tab_header = None  # [title, search] box
        self._prompt_copy_status_label = None      # built in _build_prompts_tab
        self._prompt_copy_status_timeout_id = None  # GLib source id for the auto-clear

        # Create the notebook (tabbed interface)
        PAP_notebook = Gtk.Notebook()

        # Tab 1: Prompts
        prompts_tab = self._build_prompts_tab()
        PAP_notebook.append_page(
            prompts_tab,
            Gtk.Label(label="Prompts")
        )

        # Tab 2: Agents — placeholder until set_agents() is called
        agents_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        agents_container.set_vexpand(True)
        self._agents_placeholder = Gtk.Label()
        self._agents_placeholder.set_markup(
            '<span foreground="#6b6b7a" font_desc="Sans 11">'
            'Click Connect to discover agents</span>')
        self._agents_placeholder.set_halign(Gtk.Align.CENTER)
        self._agents_placeholder.set_valign(Gtk.Align.CENTER)
        agents_container.append(self._agents_placeholder)
        PAP_notebook.append_page(
            agents_container,
            Gtk.Label(label="Agents")
        )

        # Tab 3: Projects — Gtk.Stack switching between closed (FileTree) and open (nested Notebook) view
        self._projects_stack = Gtk.Stack()
        self._projects_stack.set_vexpand(True)
        self._projects_stack.set_transition_type(Gtk.StackTransitionType.NONE)

        # "picker" page — Box container for FileTree (stable parent, always in Stack)
        self._picker_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._picker_box.set_vexpand(True)
        self._projects_stack.add_titled(self._picker_box, "picker", "Projects")

        # "open" page — Box placeholder, replaced by nested Notebook on first project open
        self._projects_open_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._projects_open_page.set_vexpand(True)
        self._projects_stack.add_titled(self._projects_open_page, "open", "Project")

        # FeedTab reference — set via set_feed_tab() before any project opens
        self._feed_tab = None  # type: FeedTab | None
        self._projects_nested_notebook = None  # type: Gtk.Notebook | None
        self._is_project_view_open = False     # guard: prevents double-open // double-close

        # FileTree — created here, parented into _picker_box (stable), reparents to nested Notebook on open
        self._file_tree = FileTree(on_file_selected=self._on_project_selected)
        self._picker_box.append(self._file_tree)

        # FileTreeHandler — manages sort prefs + git status cache (no GTK).
        # Injected by the composition root (ui/window.py) — the view layer must
        # not import ui.handlers (arch guard test_views_do_not_import_handlers).
        # Optional: a standalone-constructed panel (tests) runs without the
        # handler; project open/close then skip handler state updates.
        self._file_tree_handler = file_tree_handler
        if self._file_tree_handler is not None:
            # Wire view callbacks to handler
            self._file_tree.set_on_sort_changed(self._file_tree_handler.set_sort_mode)
            self._file_tree.set_on_get_sort_mode(self._file_tree_handler.get_sort_mode)
            self._file_tree.set_on_get_git_status(self._file_tree_handler.refresh_git_status)
        else:
            logger.debug(
                "LeftPanel constructed without file_tree_handler — "
                "file-tree sort/git-status callbacks not wired"
            )

        PAP_notebook.append_page(
            self._projects_stack,
            Gtk.Label(label="Projects")
        )

        # Store references needed by window
        self._PAP_notebook = PAP_notebook  # outer notebook (Prompts/Agents/Projects)

        self.append(PAP_notebook)

        # CSS for agent rows is in ui/styles.py (applied globally at startup)

    # ── Agents tab ──────────────────────────────────────────────────────────

    def set_main_content(self, main_content):
        """Set main_content reference for accessing active session key in menus."""
        self._main_content = main_content

    def set_agent_list_handler(self, handler):
        """Set the AgentListHandler for avatar card rendering."""
        self._agent_list_handler = handler
        # If agent names are already populated, refresh now that handler has agent_mgr
        if self._agent_names:
            self._refresh_agents_list()

    def set_special_agents(self, handler):
        """
        Set the AgentRuntimeHandler for special agent cards.


        Special agents (e.g. "Coder") are shown in the agents list
        without requiring a gateway connection.
        Called by window.py._build() after AgentRuntimeHandler is created.
        """
        self._agent_runtime_handler = handler
        self._refresh_agents_list()

    def set_agents(self, agent_names, on_agent_selected):
        """
        Populate the agents tab and register the selection callback.
        Called by window after gateway connects and agents are discovered.
        """
        self._agent_names = agent_names
        self._on_agent_selected = on_agent_selected
        self._refresh_agents_list()

    def set_on_project_opened(self, cb):
        """Set callback for when a project tab is opened."""
        self._on_project_opened = cb

    def set_toggle_agent_callback(self, cb):
        """Set the toggle_agent callback (from ProjectHandler)."""
        self._toggle_agent_callback = cb

    def set_on_create_agent(self, cb):
        """Set callback for when user clicks the Create Agent card."""
        self._on_create_agent = cb

    def set_on_edit_agent(self, cb):
        """Set callback for editing a local agent (right-click → Edit)."""
        self._on_edit_agent = cb

    def set_on_delete_agent(self, cb):
        """Set callback for deleting a local agent (right-click → Delete)."""
        self._on_delete_agent = cb

    def set_prompts_handler(self, handler):
        """Set the PromptsHandler and refresh the prompts tab."""
        self._prompts_handler = handler
        self.refresh_prompts()

    def refresh_agents_with_project(self, project_name):
        """Called by window when a project tab opens — refreshes +/− buttons."""
        self._active_project_name = project_name
        self._refresh_agents_list()

    # ── Projects tab — nested Notebook sub-tabs (FileTree / Feed) ─────────────────────

    def set_feed_tab(self, feed_tab: "FeedTab") -> None:
        """Store FeedTab reference. Called by window before any project opens."""
        self._feed_tab = feed_tab

    def get_feed_tab(self) -> "FeedTab | None":
        """Return the FeedTab reference."""
        return self._feed_tab

    def open_project_view(self, feed_tab: "FeedTab") -> None:
        """
        Open project view: create nested Notebook in Projects tab, reparent FileTree into it.

        - FileTree moves from Stack "picker" page → nested Notebook "File Tree" tab
        - FeedTab added as "Feed" tab
        - Stack switches to "open" page (nested Notebook is now its content)

        Args:
            feed_tab: FeedTab instance for the "Feed" sub-tab.
        """
        # Idempotency guard — if already open, do nothing
        if self._is_project_view_open:
            return
        self._is_project_view_open = True

        # Wire FileTreeHandler to project path when project opens
        if self._file_tree_handler is not None and self._file_tree._project_path:
            self._file_tree_handler.set_project_path(self._file_tree._project_path)
            self._file_tree_handler.invalidate_git_status()

        # Reparent FileTree out of Stack "picker" page
        self._file_tree.unparent()

        # Build nested Notebook: "File Tree" tab + "Feed" tab
        nested_nb = Gtk.Notebook()
        self._projects_nested_notebook = nested_nb

        # Tab A: File Tree — FileTree as direct child
        nested_nb.append_page(self._file_tree, Gtk.Label(label="File Tree"))

        # Tab B: Feed — FeedTab wrapped in a Box (cannot add Notebook as child directly)
        feed_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        feed_box.set_vexpand(True)
        feed_box.append(feed_tab)
        nested_nb.append_page(feed_box, Gtk.Label(label="Feed"))

        # Replace Stack "open" page content with nested Notebook, then show "open" page
        self._projects_open_page.append(nested_nb)
        self._projects_stack.set_visible_child_name("open")

        # Note: Feed tab scroll-on-show is handled by the FeedTab's own
        # ScrolledWindow 'map' signal handler (see FeedTab.__init__).
        # That fires whenever the Feed tab becomes visible, regardless of
        # which mechanism made it visible (notebook tab switch, project
        # open, etc.).

    def close_project_view(self) -> None:
        # Idempotency guard — if already closed, do nothing
        if not self._is_project_view_open:
            return

        # Reset FileTreeHandler when project closes
        if self._file_tree_handler is not None:
            self._file_tree_handler.set_project_path("")

        # 1. Reset FileTree to project picker view BEFORE detaching.
        #    navigate_back clears file-listing state and calls _show_project_picker()
        #    which rebuilds the project card grid. fire_callback=False prevents
        #    the double-close loop (caller already handles project closure).
        self._file_tree.navigate_back(fire_callback=False)

        # 2. Remove pages from nested notebook using Notebook API.
        #    MUST use remove_page(), NOT widget.unparent() — GTK4 Notebook uses
        #    an internal Stack for page management. Direct unparent() bypasses
        #    the Notebook's bookkeeping, corrupting its internal Stack and
        #    causing gtk_stack_remove assertion failures.
        nested_nb = self._projects_open_page.get_first_child()
        if nested_nb is not None:
            # Unparent FeedTab from feed_box before removing the Feed page
            feed_page = nested_nb.get_nth_page(1)  # Feed tab is page 1 (returns feed_box)
            if feed_page is not None and self._feed_tab is not None:
                feed_page.remove(self._feed_tab)

            # Remove pages using Notebook API (reverse order to keep indices valid)
            # Page 1 = Feed tab, Page 0 = File Tree tab
            n_pages = nested_nb.get_n_pages()
            for i in range(n_pages - 1, -1, -1):
                nested_nb.remove_page(i)

            # Now safe to remove the notebook from its parent
            self._projects_open_page.remove(nested_nb)
            nested_nb.unparent()
        self._projects_nested_notebook = None

        # 3. Reparent FileTree back into picker
        self._picker_box.append(self._file_tree)
        self._projects_stack.set_visible_child_name("picker")

        self._is_project_view_open = False

    def switch_to_feed_tab(self) -> None:
        """Switch the nested Notebook to the Feed sub-tab. Safe to call even if no project open."""
        if self._projects_nested_notebook is not None:
            self._projects_nested_notebook.set_current_page(1)

    def switch_to_file_tree_tab(self) -> None:
        """Switch the nested Notebook to the File Tree sub-tab. Safe to call even if no project open."""
        if self._projects_nested_notebook is not None:
            self._projects_nested_notebook.set_current_page(0)

    def _refresh_agents_list(self):
        """
        Rebuild the agents tab content from current _agent_names.
        Called on initial population and on subsequent refreshes.
        """
        # Find the agents tab page (index 1)
        notebook = self.get_first_child()
        if notebook is None:
            return

        agents_page_idx = 1
        agents_page = notebook.get_nth_page(agents_page_idx)
        if agents_page is None:
            return

        # Find the scroll container inside agents_page (may be placeholder or old scroll)
        scroll = agents_page.get_first_child()

        # If first child is not a scroll (it's the placeholder label), clear the container
        if scroll is not None and not isinstance(scroll, Gtk.ScrolledWindow):
            scroll.unparent()
            scroll = None

        # If no scroll yet, create one
        if scroll is None:
            scroll = Gtk.ScrolledWindow()
            scroll.set_vexpand(True)
            scroll.set_hexpand(True)
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            agents_page.append(scroll)

        # Remove existing list_box from scroll — GTK4 set_child() auto-unparents old child

        # List box for agent rows
        self._agents_list_box = Gtk.ListBox()
        self._agents_list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)

        # Load project members if a project is active
        project_members = []
        if self._active_project_name:
            project_members = load_members(self._active_project_name)

        # Use handler only if it has a populated agent_mgr, otherwise fall back to _agent_names
        # This matters on first connect: set_agents() fires before set_agent_mgr()
        if self._agent_list_handler and self._agent_list_handler.has_agent_mgr():
            sorted_agents = self._agent_list_handler.get_sorted_agents(project_members)
        else:
            # Fallback: group by name, prefer :main (old behavior)
            agents = {}
            for session_key, name in self._agent_names.items():
                if name not in agents:
                    agents[name] = session_key
                if ":main" in session_key:
                    agents[name] = session_key
            sorted_agents = [(sk, name, sk in project_members, sum(1 for _, n in self._agent_names.items() if n == name)) for name, sk in agents.items()]
        # Append special agents (Phase 1.4) — shown even without gateway connection
        if getattr(self, '_agent_runtime_handler', None):
            for sk, name in self._agent_runtime_handler.get_special_agents().items():
                sorted_agents.append((sk, name, sk in project_members, 1))

        if not sorted_agents:
            placeholder = Gtk.Label()
            placeholder.set_markup(
                '<span foreground="#6b6b7a" font_desc="Sans 11">'
                'No agents found</span>')
            placeholder.set_halign(Gtk.Align.CENTER)
            placeholder.set_valign(Gtk.Align.CENTER)
            placeholder.show()
            self._agents_list_box.append(placeholder)
        else:
            # Create Agent card (always first row)
            create_row = self._build_create_agent_row()
            self._agents_list_box.append(create_row)

            for session_key, name, in_project, session_count in sorted_agents:
                row = self._build_agent_row(session_key, name, in_project, session_count)
                self._agents_list_box.append(row)

        # Single-click activates (opens chat tab)
        self._agents_list_box.set_activate_on_single_click(False)
        self._agents_list_box.connect("row_activated", self._on_agent_row_activated)

        scroll.set_child(self._agents_list_box)
        self._agents_list_box.show()

    def _build_create_agent_row(self) -> Gtk.ListBoxRow:
        """Build the '+ Create Agent' card row. Follows _build_new_prompt_row() pattern."""
        row = Gtk.ListBoxRow()
        row._is_create_agent_card = True
        row.set_selectable(False)
        row.set_activatable(True)

        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        row_box.set_halign(Gtk.Align.FILL)
        row_box.set_margin_start(4)
        row_box.set_margin_end(4)
        row_box.set_margin_top(4)
        row_box.set_margin_bottom(4)
        row_box.add_css_class("agent-row")

        plus_lbl = Gtk.Label(label="+")
        plus_lbl.set_size_request(44, 44)
        plus_lbl.set_halign(Gtk.Align.CENTER)
        plus_lbl.set_valign(Gtk.Align.CENTER)
        plus_lbl.add_css_class("new-prompt-plus")

        text_lbl = Gtk.Label(label="Create Agent")
        text_lbl.set_halign(Gtk.Align.START)
        text_lbl.set_valign(Gtk.Align.CENTER)
        text_lbl.add_css_class("dim-label")

        row_box.append(plus_lbl)
        row_box.append(text_lbl)
        row.set_child(row_box)

        # Single-click gesture — list box activate_on_single_click is False
        # (agent rows need double-click to avoid accidental opens),
        # but this button-like card should respond immediately.
        click = Gtk.GestureClick()
        click.set_button(1)
        click.connect("pressed", lambda *_: self._on_create_agent_click())
        row.add_controller(click)

        return row

    def _on_create_agent_click(self) -> None:
        """Handle single-click on Create Agent card."""
        if self._on_create_agent:
            self._on_create_agent()

    def _build_agent_row(self, session_key, name, in_project=False, session_count=1):
        """
        Build a single agent avatar card row.

        Layout: [avatar] [name + tags column] [+/−] [Chat]
        Avatar uses render_agent_icon via the agent_list_handler (if set).
        Tags show source (Openclaw/DevelCakes) and session count.
        """
        row = Gtk.ListBoxRow()
        row._session_key = session_key
        row._agent_name = name

        # Get initials and color from handler (or compute defaults)
        if self._agent_list_handler:
            initials = self._agent_list_handler.compute_initials(name)
            color = self._agent_list_handler.get_agent_color(name)
        else:
            parts = name.split()
            initials = (parts[0][0] + parts[1][0]).upper() if len(parts) >= 2 else name[:2].upper()
            color = "#6366f1"  # fallback indigo

        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        row_box.set_halign(Gtk.Align.FILL)
        row_box.set_margin_start(4)
        row_box.set_margin_end(4)
        row_box.set_margin_top(4)
        row_box.set_margin_bottom(4)
        row_box.add_css_class("agent-row")

        # Avatar picture (44×44)
        avatar_picture = Gtk.Picture()
        avatar_picture.set_size_request(44, 44)
        avatar_picture.set_halign(Gtk.Align.CENTER)
        avatar_picture.set_valign(Gtk.Align.CENTER)
        avatar_picture.set_paintable(render_agent_icon(color, initials))

        # Name + tags column (vertical, like project cards)
        name_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        name_col.set_hexpand(True)
        name_col.set_halign(Gtk.Align.START)
        name_col.set_valign(Gtk.Align.CENTER)
        name_col.set_margin_start(8)

        name_lbl = Gtk.Label(label=name)
        name_lbl.set_halign(Gtk.Align.START)
        name_lbl.add_css_class("agent-name-label")

        # Tag line: source + session count
        is_special = session_key.startswith("special:")
        source_tag = "DevelCakes" if is_special else "Openclaw"
        session_word = "Session" if session_count == 1 else "Sessions"
        tag_text = f"{source_tag}: {session_count} {session_word}"
        tag_lbl = Gtk.Label(label=tag_text)
        tag_lbl.set_halign(Gtk.Align.START)
        tag_lbl.add_css_class("agent-tag-label")

        name_col.append(name_lbl)
        name_col.append(tag_lbl)

        name_lbl.show()
        tag_lbl.show()
        name_col.show()

        # Buttons box: +/− toggle (if project active)
        buttons_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        buttons_box.set_halign(Gtk.Align.END)

        # Toggle button: + (add) or − (remove) — only visible in project context
        toggle_btn = Gtk.Button()
        toggle_btn.set_size_request(28, 28)
        toggle_btn._agent_session_key = session_key
        toggle_btn._agent_name = name
        toggle_btn.connect("clicked", self._on_agent_toggle_clicked)
        if self._active_project_name:
            toggle_btn.add_css_class("agent-add-btn" if not in_project else "agent-remove-btn")
            toggle_btn.set_label("−" if in_project else "+")
            toggle_btn.set_visible(True)
            toggle_btn.show()
        else:
            toggle_btn.set_visible(False)

        # Right-click gesture for session switcher menu
        right_click = Gtk.GestureClick()
        right_click.set_button(3)  # right mouse button
        right_click.connect("pressed", self._on_agent_right_click, session_key, name)
        row_box.add_controller(right_click)

        avatar_picture.show()

        buttons_box.append(toggle_btn)
        row_box.append(avatar_picture)
        row_box.append(name_col)
        row_box.append(buttons_box)
        row.set_child(row_box)
        row.show()
        return row

    def _on_agent_row_activated(self, list_box, row):
        """Called when an agent row is clicked — open/create chat tab."""
        # Check if this is the Create Agent card
        if getattr(row, '_is_create_agent_card', False):
            if self._on_create_agent:
                self._on_create_agent()
            return

        if self._on_agent_selected is not None:
            self._on_agent_selected(row._session_key, row._agent_name)

    def _on_agent_right_click(self, gesture, n_press, x, y, session_key, name):
        """Show context menu on right-click over an agent row.

        For gateway agents with multiple sessions: show session switcher.
        For local (special) agents: show Edit/Delete options.
        """
        row_widget = gesture.get_widget()
        is_special = session_key.startswith("special:")

        if is_special:
            self._show_local_agent_menu(row_widget, name)
            return

        # Gateway agent — show session switcher if multiple sessions
        if self._agent_list_handler is None:
            return
        sessions = self._agent_list_handler.get_all_sessions_for_agent(name)
        if len(sessions) <= 1:
            return  # nothing to switch between
        current_active = (
            self._main_content.get_current_session_key()
            if hasattr(self, "_main_content") and self._main_content
            else None
        )
        show_session_menu(
            parent=row_widget,
            agent_name=name,
            sessions=sessions,
            on_select=lambda sk: self._on_agent_selected(sk, name),
            current_session_key=current_active,
        )

    def _show_local_agent_menu(self, parent: Gtk.Widget, name: str) -> None:
        """Show Edit/Delete popover for a local (special) agent."""
        popover = Gtk.Popover()
        popover.set_parent(parent)
        popover.set_position(Gtk.PositionType.BOTTOM)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        vbox.set_margin_start(8)
        vbox.set_margin_end(8)
        vbox.set_margin_top(6)
        vbox.set_margin_bottom(6)

        edit_btn = Gtk.Button(label="Edit")
        edit_btn.add_css_class("flat")
        edit_btn.connect("clicked", lambda *_: self._do_edit_agent(popover, name))
        vbox.append(edit_btn)

        delete_btn = Gtk.Button(label="Delete")
        delete_btn.add_css_class("flat")
        delete_btn.connect("clicked", lambda *_: self._do_delete_agent(popover, name))
        vbox.append(delete_btn)

        popover.set_child(vbox)
        popover.popup()

    def _do_edit_agent(self, popover: Gtk.Popover, name: str) -> None:
        popover.popdown()
        if self._on_edit_agent:
            self._on_edit_agent(name)

    def _do_delete_agent(self, popover: Gtk.Popover, name: str) -> None:
        popover.popdown()
        if self._on_delete_agent:
            self._on_delete_agent(name)

    def _on_agent_toggle_clicked(self, button):
        """Add or remove an agent from the active project via toggle_agent callback."""
        if not self._active_project_name:
            return

        # Resolve the session key: gateway agents via AgentManager, all others
        # (special agents, no-gateway) via the key stored on the button at row-build time.
        session_key = button._agent_session_key
        if self._agent_list_handler and self._agent_list_handler.has_agent_mgr():
            primary_sk = self._agent_list_handler.get_primary_session(button._agent_name)
            if primary_sk is not None:
                session_key = primary_sk

        if self._toggle_agent_callback:
            self._toggle_agent_callback(session_key)

    # ── Prompts tab ─────────────────────────────────────────────────────────

    def _build_prompts_tab(self):
        """Build the full Prompts tab: header box + scrollable list."""
        tab = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tab.set_vexpand(True)

        # Header: ["Prompts" title + search entry]
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header.set_margin_start(8)
        header.set_margin_end(8)
        header.set_margin_top(8)
        header.set_margin_bottom(4)
        header.set_spacing(6)

        title = Gtk.Label(label="Prompts")
        title.set_valign(Gtk.Align.CENTER)
        title.set_margin_end(4)

        search_entry = Gtk.SearchEntry()
        search_entry.set_placeholder_text("Search prompts...")
        search_entry.set_hexpand(True)
        search_entry.set_valign(Gtk.Align.CENTER)
        search_entry.connect("search-changed", self._on_prompt_search_changed)
        self._search_entry = search_entry

        header.append(title)
        header.append(search_entry)

        # Transient copy-status label (right-aligned in the header).
        status_label = Gtk.Label(label="")
        status_label.set_valign(Gtk.Align.CENTER)
        status_label.set_margin_start(4)
        status_label.set_margin_end(4)
        status_label.add_css_class("dim-label")
        status_label.set_xalign(1.0)
        header.append(status_label)
        self._prompt_copy_status_label = status_label

        self._prompts_tab_header = header

        # Scrollable prompt list
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._prompts_scroll = scroll

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        list_box.set_activate_on_single_click(False)
        list_box.connect("row_activated", self._on_new_prompt_row_activated)
        list_box.connect("row_activated", self._on_prompt_row_activated)
        self._prompts_list_box = list_box

        scroll.set_child(list_box)
        tab.append(header)
        tab.append(scroll)
        return tab

    def refresh_prompts(self):
        """Rebuild the prompts list from handler data. Called on init and after toggle/search."""
        if self._prompts_list_box is None:
            return
        list_box = self._prompts_list_box
        # Clear existing rows
        while True:
            row = list_box.get_row_at_index(0)
            if row is None:
                break
            list_box.remove(row)

        if self._prompts_handler is None:
            return

        # Prepend the '+' new prompt card row
        new_row = self._build_new_prompt_row()
        list_box.append(new_row)

        prompts = self._prompts_handler.load_prompts()
        for prompt in prompts:
            row = self._build_prompt_row(prompt)
            list_box.append(row)

        list_box.show()

    def _build_prompt_row(self, prompt: dict) -> Gtk.ListBoxRow:
        """Build a single prompt row with: [★] [name] [meta] [+/− toggle]."""
        row = Gtk.ListBoxRow()
        row._filepath = prompt['filepath']
        row._name = prompt['name']
        row._prompt_content = prompt['content']   # NEW — used by "Copy prompt" menu

        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        row_box.set_halign(Gtk.Align.FILL)
        row_box.set_margin_start(2)
        row_box.set_margin_end(2)
        row_box.set_margin_top(1)
        row_box.set_margin_bottom(1)
        row_box.add_css_class("lib-row")

        # Star button (★/☆)
        fav_btn = Gtk.Button()
        fav_btn.add_css_class("flat")
        fav_btn.set_size_request(24, 24)
        fav_btn.set_label("★" if prompt['is_favorite'] else "☆")
        if prompt['is_favorite']:
            fav_btn.add_css_class("lib-fav-star")
        fav_btn.connect("clicked", self._on_prompt_toggle_favorite, prompt['filepath'])

        # Name + meta column (vertical stack so meta is BELOW name)
        name_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        name_col.set_hexpand(True)
        name_col.set_halign(Gtk.Align.START)
        name_col.set_valign(Gtk.Align.CENTER)
        name_col.set_margin_start(4)

        # Name label
        name_lbl = Gtk.Label(label=prompt['name'], xalign=0)
        name_lbl.set_halign(Gtk.Align.START)
        name_lbl.set_valign(Gtk.Align.END)

        # Metadata: lines + size + last-used
        meta_parts = []
        if prompt.get('lines'):
            meta_parts.append(f"{prompt['lines']}L")
        if prompt.get('size'):
            size_kb = prompt['size'] // 1024
            meta_parts.append(f"{size_kb}KB" if size_kb > 0 else f"{prompt['size']}B")
        if prompt.get('last_used_str'):
            meta_parts.append(prompt['last_used_str'])
        meta_lbl = Gtk.Label(label=" · ".join(meta_parts) if meta_parts else "")
        meta_lbl.set_halign(Gtk.Align.START)
        meta_lbl.set_valign(Gtk.Align.START)
        meta_lbl.add_css_class("lib-tag")

        # Add button
        add_btn = Gtk.Button()
        add_btn.set_size_request(24, 24)
        add_btn.add_css_class("flat")
        add_btn.set_label("+")
        add_btn.connect("clicked", self._on_prompt_add_clicked, prompt)

        fav_btn.show()
        name_lbl.show()
        meta_lbl.show()
        name_col.show()
        add_btn.show()

        name_col.append(name_lbl)
        name_col.append(meta_lbl)

        row_box.append(fav_btn)
        row_box.append(name_col)
        row_box.append(add_btn)
        row.set_child(row_box)
        row.show()

        # Right-click gesture for copy menu — only on real prompt rows (has _filepath)
        right_ctrl = Gtk.GestureClick()
        right_ctrl.set_button(Gdk.BUTTON_SECONDARY)
        right_ctrl.connect("pressed", self._on_prompt_row_right_click, row)
        row.add_controller(right_ctrl)

        return row

    def _on_prompt_toggle_favorite(self, btn, filepath):
        """Toggle favorite star — handler does persistence; we just refresh."""
        if self._prompts_handler:
            self._prompts_handler.toggle_favorite(filepath)
            self.refresh_prompts()

    def _on_prompt_add_clicked(self, btn, prompt):
        """Load prompt into chat input (same as double-click)."""
        self._prompts_handler.on_prompt_activated(prompt['filepath'])

    def _on_prompt_search_changed(self, entry):
        """Filter prompts list on search-changed."""
        query = entry.get_text()
        if self._prompts_handler:
            self._prompts_handler.search(query)
            self.refresh_prompts()

    def _on_prompt_row_activated(self, list_box, row):
        """Called when a prompt row is double-clicked — load into chat input."""
        # Skip the "+" row
        if hasattr(row, '_is_new_prompt_card'):
            return
        if self._prompts_handler and hasattr(row, '_filepath'):
            self._prompts_handler.on_prompt_activated(row._filepath)

    def _build_new_prompt_row(self) -> Gtk.ListBoxRow:
        """Build the '+' new prompt card row. Follows the projects tab '+' card pattern."""
        row = Gtk.ListBoxRow()
        row._is_new_prompt_card = True
        row.set_selectable(False)
        row.set_activatable(True)

        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        row_box.set_halign(Gtk.Align.FILL)
        row_box.set_margin_start(4)
        row_box.set_margin_end(4)
        row_box.set_margin_top(4)
        row_box.set_margin_bottom(4)
        row_box.add_css_class("new-prompt-row")

        plus_lbl = Gtk.Label(label="+")
        plus_lbl.set_size_request(28, 28)
        plus_lbl.set_halign(Gtk.Align.CENTER)
        plus_lbl.set_valign(Gtk.Align.CENTER)
        plus_lbl.add_css_class("new-prompt-plus")

        text_lbl = Gtk.Label(label="Add Prompt")
        text_lbl.set_halign(Gtk.Align.START)
        text_lbl.set_valign(Gtk.Align.CENTER)
        text_lbl.add_css_class("dim-label")

        row_box.append(plus_lbl)
        row_box.append(text_lbl)
        row.set_child(row_box)
        return row

    def _on_new_prompt_row_activated(self, list_box, row):
        """Handle activation of the '+' row — open file picker."""
        if not hasattr(row, '_is_new_prompt_card'):
            return
        if self._prompts_handler is None:
            return
        self._open_import_dialog()

    def _open_import_dialog(self):
        """Open a GTK4 FileDialog to select a .md file for import."""
        dialog = Gtk.FileDialog()
        dialog.set_title("Select a prompt file")

        # Filter to .md only
        filter_md = Gtk.FileFilter()
        filter_md.set_name("Markdown files")
        filter_md.add_pattern("*.md")
        filter_list = Gio.ListStore.new(Gtk.FileFilter)
        filter_list.append(filter_md)
        dialog.set_filters(filter_list)

        # Get parent window
        root = self.get_root()
        if root is None:
            return
        dialog.open(root, None, self._on_import_file_selected)

    def _on_import_file_selected(self, dialog, result):
        """Handle file selection from the import dialog."""
        try:
            file = dialog.open_finish(result)
            if file is None:
                return
            source_path = file.get_path()
            if source_path and self._prompts_handler:
                new_path = self._prompts_handler.import_prompt(source_path)
                if new_path:
                    self.refresh_prompts()
        except GLib.Error:
            pass  # User cancelled the dialog

    def _on_prompt_row_right_click(self, ctrl, n_press, x, y, row) -> None:
        """
        Right-click on a prompt row — show the copy menu.

        Args:
            ctrl:    Gtk.GestureClick (sender, not used).
            n_press: int — number of presses (only respond to single click).
            x, y:    float — local click coordinates (unused; popover anchors to row).
            row:     Gtk.ListBoxRow — the right-clicked row (carries _filepath + _prompt_content).
        """
        if n_press != 1:
            return
        if not hasattr(row, "_filepath"):
            return  # defensive: skip non-prompt rows (e.g., the "+" row)

        popover = Gtk.Popover()
        popover.set_parent(row)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        vbox.set_margin_top(6)
        vbox.set_margin_bottom(6)
        vbox.set_margin_start(6)
        vbox.set_margin_end(6)

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)

        # Row 1: Copy path
        copy_path_row = Gtk.ListBoxRow()
        copy_path_row.set_activatable(True)
        copy_path_row.set_selectable(False)
        copy_path_row._action = "copy_path"
        copy_path_label = Gtk.Label(label="Copy path", xalign=0)
        copy_path_label.set_margin_top(4)
        copy_path_label.set_margin_bottom(4)
        copy_path_label.set_margin_start(8)
        copy_path_label.set_margin_end(8)
        copy_path_row.set_child(copy_path_label)
        list_box.append(copy_path_row)

        # Row 2: Copy prompt
        copy_content_row = Gtk.ListBoxRow()
        copy_content_row.set_activatable(True)
        copy_content_row.set_selectable(False)
        copy_content_row._action = "copy_prompt"
        copy_content_label = Gtk.Label(label="Copy prompt", xalign=0)
        copy_content_label.set_margin_top(4)
        copy_content_label.set_margin_bottom(4)
        copy_content_label.set_margin_start(8)
        copy_content_label.set_margin_end(8)
        copy_content_row.set_child(copy_content_label)
        list_box.append(copy_content_row)

        list_box.connect("row-activated", self._on_prompt_menu_row_activated, popover, row)
        vbox.append(list_box)
        popover.set_child(vbox)
        popover.connect("closed", lambda *_: popover.unparent())
        popover.popup()

    def _on_prompt_menu_row_activated(self, _lb, menu_row, popover, source_row) -> None:
        """
        One of "Copy path" / "Copy prompt" was clicked. Dispatch and dismiss the popover.

        The source_row is the original prompt row (carries _filepath and _prompt_content).
        We identify the action by reading the row's _action attribute (set at row build time),
        NOT by parsing the label text. This is robust to i18n: the displayed label can be
        translated without breaking dispatch.
        """
        popover.popdown()
        # Popover.unparent() also fires via the "closed" signal handler connected in
        # _on_prompt_row_right_click. The popdown() call schedules the close, so the
        # unparent happens shortly after. No need to call it explicitly here.
        action = getattr(menu_row, "_action", None)
        if action == "copy_path":
            self._on_copy_prompt_path(source_row)
        elif action == "copy_prompt":
            self._on_copy_prompt_content(source_row)
        # Unknown action → no-op. Defensive: if a future menu row is added without
        # _action, the click is silently ignored rather than crashing.

    def _on_copy_prompt_path(self, row) -> None:
        """Copy the absolute path of the right-clicked prompt's file to the clipboard."""
        filepath = getattr(row, "_filepath", None)
        if not filepath:
            return
        self._copy_text_to_clipboard(filepath)
        self._show_prompt_copy_status("Copied path")

    def _on_copy_prompt_content(self, row) -> None:
        """Copy the raw file contents of the right-clicked prompt to the clipboard."""
        content = getattr(row, "_prompt_content", None)
        if content is None:
            return
        self._copy_text_to_clipboard(content)
        self._show_prompt_copy_status("Copied prompt")

    def _copy_text_to_clipboard(self, text: str) -> None:
        """Copy text to the system clipboard using GTK4 clipboard API."""
        display = Gdk.Display.get_default()
        if display is None:
            return
        clipboard = display.get_clipboard()
        clipboard.set(text)

    def _show_prompt_copy_status(self, message: str) -> None:
        """Show a transient confirmation in the prompts tab header for ~2.5s."""
        if self._prompt_copy_status_label is None:
            return
        self._prompt_copy_status_label.set_text(message)
        # Cancel any pending clear, then schedule a new one.
        if self._prompt_copy_status_timeout_id is not None:
            try:
                GLib.source_remove(self._prompt_copy_status_timeout_id)
            except Exception:
                pass
            self._prompt_copy_status_timeout_id = None
        def _clear():
            if self._prompt_copy_status_label is not None:
                self._prompt_copy_status_label.set_text("")
            self._prompt_copy_status_timeout_id = None
            return GLib.SOURCE_REMOVE
        self._prompt_copy_status_timeout_id = GLib.timeout_add(2500, _clear)

