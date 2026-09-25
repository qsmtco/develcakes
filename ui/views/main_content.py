# ui/views/main_content.py
# Main content area — right side of the split view
# Contains: notebook (chat tabs), chat control bar, user input + buttons
# Resizable paned divider between top (notebook+bar) and bottom (input+buttons)

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gdk, GLib, Gio
import logging
import os
from typing import Callable

_logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

from ui.views.chat_input_toolbar import ChatInputToolbar
from ui.views.session_menu import show_session_menu, show_project_menu
from utils.escaping import escape_for_pango
from utils.config import get_project_root

class MainContent(Gtk.Box):
    """
    Main content area widget.
    Vertical split:
      - Top: Gtk.Notebook with chat tabs (one per agent)
      - Bottom: User input box + button bar (scrollable, resizable)
    """

    @property
    def user_input(self):
        """Expose the user_input TextView for external access."""
        return self._user_input

    @property
    def toolbar(self):
        """Public accessor for the input toolbar view (find/replace, spell check, etc.)."""
        return self._toolbar

    @property
    def send_button(self):
        """Expose the send button for external signal wiring."""
        return self._send_button

    @property
    def notebook(self):
        """Expose the chat notebook for external tab management."""
        return self._chat_notebook

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        # --- TOP: Notebook (with chat scroll overlaid by project settings bar) ---
        top_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self._chat_notebook = Gtk.Notebook()
        self._chat_notebook.set_show_tabs(True)
        self._chat_notebook.set_scrollable(True)
        self._chat_notebook.connect("switch-page", self._on_notebook_switch_page)
        # Track which session_key each tab belongs to.
        # KEY INSIGHT: page_index is NOT stable across tab additions/removals — GTK
        # reuses and shifts indices. Always look up by session_key via _find_page_by_session()
        # or rebuild via _reindex_tabs(). Never capture page_idx in closures.
        self._tab_sessions = {}  # page_index -> session_key
        # Track chat boxes per page_index so we can append to them
        self._tab_chat_boxes = {}  # page_index -> chat_box widget
        self._tab_scrolls = {}   # page_index -> ScrolledWindow widget
        self._tab_overlays = {}  # page_index -> chat_overlay widget (top-level page widget)
        # Phase 5b: scroll-to-bottom button — single floating button overlay on current tab
        self._scroll_to_bottom_btn = None
        self._scroll_to_bottom_overlay = None  # per-tab overlay holding the button
        self._chat_render_handler = None  # injected via set_chat_render_handler()
        self._project_handler = None   # injected via set_project_handler()
        self._on_project_tab_closed = None  # callback(name) — set by window.py
        self._on_session_changed = None  # callback(session_key) — set by window.py for context meter update
        # Bulk-close guard: skip reindex until all removals are done
        self._bulk_closing = False
        # Unread tab tracking — session_keys with unseen messages
        self._unread_tabs: set[str] = set()

        # Phase A — Context meter widget (Claude Code style).
        # These are instance attributes so set_context_meter() can find them.
        self._context_meter = Gtk.ProgressBar()
        self._context_meter.set_size_request(80, 6)
        self._context_meter.set_show_text(False)
        self._context_meter.set_fraction(0.0)
        self._context_meter.add_css_class("context-meter")
        self._context_meter_label = Gtk.Label(label="")
        self._context_meter_label.add_css_class("context-meter-label")
        self._context_meter_label.set_visible(False)  # hidden — meter bar color conveys zone
        self._context_pct_label = Gtk.Label(label="0%")
        self._context_pct_label.add_css_class("context-meter-pct")
        self._context_caption_label = Gtk.Label(label="context")
        self._context_caption_label.add_css_class("context-meter-caption")
        meter_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        meter_box.append(self._context_pct_label)
        meter_box.append(self._context_meter)
        meter_box.append(self._context_caption_label)

        # Agent avatar + name display (left of meter in button bar)
        self._context_avatar = Gtk.Picture()
        self._context_avatar.set_size_request(28, 28)
        self._context_avatar.add_css_class("context-avatar")
        self._context_name_label = Gtk.Label(label="")
        self._context_name_label.add_css_class("context-agent-name")
        self._agent_context_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._agent_context_box.append(self._context_avatar)
        self._agent_context_box.append(self._context_name_label)
        self._agent_context_box.add_css_class("agent-context-box")

        self._toolbar = ChatInputToolbar()

        # Top box minimum height — prevents it collapsing when notebook is empty
        top_box.set_size_request(-1, 120)

        top_box.append(self._chat_notebook)
        top_box.append(self._toolbar)

        # ── Project Settings Bar — floating OVER the chat scroll area only ────
        # Placed as overlay on the notebook's chat area (not the tab bar).
        # Semi-transparent; chat content scrolls underneath it.
        # CSS .project-feed-bar provides rgba background + border-radius.
        self._project_settings = Gtk.Box()
        self._project_settings.set_halign(Gtk.Align.FILL)
        self._project_settings.set_valign(Gtk.Align.START)
        self._project_settings.set_size_request(-1, 28)
        self._project_settings.set_margin_top(4)
        self._project_settings.set_margin_bottom(0)
        self._project_settings.add_css_class("project-feed-bar")
        self._project_settings.set_visible(False)  # hidden until a project is opened
        _feed_lbl = Gtk.Label()
        _feed_lbl.set_halign(Gtk.Align.END)
        _feed_lbl.set_margin_start(8)
        _feed_lbl.set_margin_end(8)
        _feed_lbl.set_markup('<span foreground="#6b6b7a" font_desc="Sans 10">Project Settings</span>')
        self._project_settings.append(_feed_lbl)

        # Singleton gear button (⚙) — right-aligned, opens the Settings dialog.
        # Round 2 BUG #5: this widget is re-appended by update_project_settings()
        # and BOTH legacy text setters after _clear_settings_bar() removes all
        # children, so it is never lost across bar rebuilds (SPEC-...-FIX-3 §2.1).
        # GTK4: set_has_frame(False), NOT set_relief()/ReliefStyle (removed in GTK4).
        self._settings_btn = Gtk.Button(label="⚙")
        self._settings_btn.set_has_frame(False)
        self._settings_btn.set_focus_on_click(False)
        self._settings_btn.add_css_class("project-bar-gear")
        self._settings_btn.set_margin_end(8)
        self._settings_btn.connect("clicked", self._on_settings_btn_clicked)
        self._on_settings_clicked = None
        self._on_agent_cycle = None
        self._on_autoaccept_cycle = None

        # Phase 5b: scroll-to-bottom floating button
        self._scroll_btn = Gtk.Button(label="↓")
        self._scroll_btn.add_css_class("scroll-to-bottom-btn")
        self._scroll_btn.set_opacity(0)  # hidden by default
        self._scroll_btn.connect("clicked", self._on_scroll_to_bottom_clicked)
        self._scroll_btn_box = Gtk.Box()
        self._scroll_btn_box.set_halign(Gtk.Align.END)
        self._scroll_btn_box.set_valign(Gtk.Align.END)
        self._scroll_btn_box.set_hexpand(False)
        self._scroll_btn_box.set_vexpand(False)
        self._scroll_btn_box.append(self._scroll_btn)
        self._scroll_btn_box.set_size_request(48, 36)

        # --- BOTTOM: User input area + button bar ---
        bottom_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Scrollable text view for typing prompts/commands
        input_scroll = Gtk.ScrolledWindow()
        input_scroll.set_vexpand(True)
        input_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._user_input = Gtk.TextView()
        self._user_input.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._user_input.set_editable(True)
        self._user_input.set_cursor_visible(True)
        self._user_input.set_hexpand(True)
        self._user_input.set_vexpand(True)
        self._user_input.set_left_margin(8)
        self._user_input.set_right_margin(8)
        self._user_input.set_top_margin(6)
        self._user_input.set_bottom_margin(6)
        self._user_input.add_css_class("input-bubble")
        # Spell-check context menu via Gtk.TextView.set_extra_menu().
        # This is the GTK4-native way to add items to the TextView's built-in
        # right-click menu. It avoids the GestureClick grab conflicts that
        # cause "Tried to map a grabbing popup with a non-top-most parent"
        # and UI freezes on Wayland.
        # Pattern from pygtkspellcheck library.
        self._spell_extra_menu = Gio.Menu()
        self._user_input.set_extra_menu(self._spell_extra_menu)
        # Action group for spell suggestion actions
        self._spell_action_group = Gio.SimpleActionGroup()
        self._user_input.insert_action_group("spell", self._spell_action_group)
        # Right-click handler: move cursor to click position and rebuild menu.
        # The TextView itself handles showing the menu — no manual popover.
        right_click = Gtk.GestureClick()
        right_click.set_button(Gdk.BUTTON_SECONDARY)
        right_click.connect("pressed", self._on_input_right_click_internal)
        self._user_input.add_controller(right_click)
        input_scroll.set_child(self._user_input)

        # Buffer-changed signal — let subscribers react to typing/edits.
        # Mirrors the pattern used by project_settings / feed_bar in this class.
        buf = self._user_input.get_buffer()
        self._on_buffer_changed: callable | None = None
        self._on_input_right_click: callable | None = None
        buf.connect("changed", self._on_input_buffer_changed)

        # Button bar — right-justified buttons below the input
        button_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        button_bar.set_halign(Gtk.Align.END)
        button_bar.set_valign(Gtk.Align.CENTER)
        button_bar.set_size_request(-1, 36)

        self._prompt_button = Gtk.Button()
        self._prompt_button.add_css_class("btn-prompt")

        prompt_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        mic_img = Gtk.Image.new_from_file(
            os.path.join(get_project_root(), "icons", "emoji", "mic_modern.png")
        )
        mic_img.set_pixel_size(16)
        self._prompt_mic_img = mic_img
        self._prompt_label = Gtk.Label(label="Prompt")
        prompt_box.append(self._prompt_label)
        prompt_box.append(mic_img)
        self._prompt_button.set_child(prompt_box)
        self._improve_button = Gtk.Button(label="Improve ✦")
        self._improve_button.add_css_class("btn-improve")
        self._send_button = Gtk.Button(label="Send  ↵")
        self._send_button.add_css_class("suggested-action")

        button_bar.set_spacing(6)
        button_bar.append(self._agent_context_box)
        button_bar.append(meter_box)
        button_bar.append(self._prompt_button)
        button_bar.append(self._improve_button)
        button_bar.append(self._send_button)

        # STT state
        self._on_stt_start_stop = None
        self._on_stt_partial = None
        self._stt_state = "idle"  # "idle" | "recording"

        # Improve state
        self._on_improve_click = None

        # Agent manager reference — set via set_agent_manager()
        self._agent_mgr = None

        # Agent runtime handler reference — set via set_agent_runtime_handler()
        # Used for fallback name lookup when _agent_mgr is None (offline mode)
        self._agent_runtime_handler = None

        self._prompt_button.connect("clicked", self._on_prompt_clicked)
        self._improve_button.connect("clicked", self._on_improve_clicked)

        bottom_box.append(input_scroll)
        bottom_box.append(button_bar)

        # --- VERTICAL PANED SPLIT ---
        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        paned.set_start_child(top_box)
        paned.set_end_child(bottom_box)
        paned.set_resize_start_child(True)
        paned.set_resize_end_child(True)
        paned.set_shrink_start_child(False)
        paned.set_shrink_end_child(False)
        paned.set_position(400)

        self.append(paned)

    def _clear_settings_bar(self) -> None:
        """Remove all children from the settings bar box (sibling-walk safe)."""
        while self._project_settings.get_first_child() is not None:
            self._project_settings.remove(self._project_settings.get_first_child())

    def update_project_settings(self, project_name, member_count,
                                solo_target, auto_accept_level, branch_name):
        """Rebuild the settings bar with the latest per-project state.

        Called by window.py when project opens/closes, members change, solo
        target changes, auto-accept level changes, or branch refresh lands.

        Empty project (project_name falsy) -> hide the bar and return.
        """
        if not project_name:
            self._project_settings.set_visible(False)
            self._clear_settings_bar()
            return

        self._clear_settings_bar()
        self._project_settings.set_visible(True)

        info_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        info_box.set_hexpand(True)
        info_box.set_halign(Gtk.Align.START)

        # Project name + member count. BUG #6: xml_escape_text() the UNTRUSTED
        # project name (escape_for_pango preserves <b> etc -> injection).
        from utils.escaping import xml_escape_text
        safe_name = xml_escape_text(project_name)
        name_label = Gtk.Label()
        name_label.set_markup(
            f'<span font_desc="Sans 10"><b>{safe_name}</b>  ·  '
            f'{member_count} member{"s" if member_count != 1 else ""}</span>'
        )
        name_label.set_margin_start(8)
        info_box.append(name_label)

        # Agent name (green) — clickable to cycle. Pango markup drives the
        # font (Sans 10, matching the Git/branch label) and splits the prefix
        # ("Chat:") into a brighter color from the value.
        agent_text = self._resolve_agent_display_name(solo_target) if solo_target else "ALL"
        agent_label = Gtk.Button()
        agent_label.set_has_frame(False)   # GTK4 — set_relief()/ReliefStyle do NOT exist
        agent_label.set_focus_on_click(False)
        agent_label.add_css_class("project-bar-agent")
        _agent_lbl = Gtk.Label()
        _agent_lbl.set_markup(
            f'<span font_desc="Sans 10" foreground="#cfd8e8">Chat:</span> '
            f'<span font_desc="Sans 10" foreground="#4ade80">{xml_escape_text(agent_text)}</span>'
        )
        agent_label.set_child(_agent_lbl)
        agent_label.connect("clicked", lambda _b: self._on_agent_label_clicked(solo_target))
        info_box.append(agent_label)

        # Auto-accept level (file changes) — clickable, cycles off->diffs->files->all
        level_labels = {
            "off": "off",
            "diffs": "diffs",
            "files": "files",
            "all": "all",
        }
        auto_label = Gtk.Button()
        auto_label.set_has_frame(False)
        auto_label.set_focus_on_click(False)
        auto_label.add_css_class("project-bar-autoaccept")
        _auto_lbl = Gtk.Label()
        _auto_lbl.set_markup(
            f'<span font_desc="Sans 10" foreground="#cfd8e8">Files:</span> '
            f'<span font_desc="Sans 10" foreground="#facc15">{xml_escape_text(level_labels.get(auto_accept_level, "off"))}</span>'
        )
        auto_label.set_child(_auto_lbl)
        auto_label.connect("clicked", lambda _b: self._on_autoaccept_label_clicked(auto_accept_level))
        info_box.append(auto_label)

        # Git branch (read-only) — interpolated into markup, must be xml_escape_text'd.
        branch_text = branch_name or "—"
        branch_label = Gtk.Label()
        branch_label.set_markup(
            f'<span font_desc="Sans 10" foreground="#cfd8e8">Git:</span> '
            f'<span font_desc="Sans 10" foreground="#6b9bd8">⎇ {xml_escape_text(branch_text)}</span>'
        )
        branch_label.set_margin_start(4)
        info_box.append(branch_label)

        self._project_settings.append(info_box)
        # Always re-append the singleton gear — _clear_settings_bar() removed it.
        self._project_settings.append(self._settings_btn)

    def _resolve_agent_display_name(self, session_key: str) -> str:
        """Resolve a member session_key to a human-readable name.

        Ordered fallback (mirrors existing _on_tab_right_click logic):
          1. _agent_mgr.get_name(sk)  (gateway/connected AgentManager)
          2. _agent_runtime_handler.get_special_agents()[sk]  (offline special agents)
          3. session_key as-is.

        Uses the dict returned by ARTH.get_special_agents() — NO reliance on a
        SpecialAgentDef.session_key attribute (keyed by conv_id_prefix).

        Round 3 BUG #7: use .get() + truthiness, NOT `if sk in special` — an
        entry whose value is "" or None must fall through to session_key
        rather than return a blank label.
        """
        if self._agent_mgr is not None:
            name = self._agent_mgr.get_name(session_key)
            if name:
                return name
        if self._agent_runtime_handler is not None:
            special = self._agent_runtime_handler.get_special_agents()
            name = special.get(session_key)
            if name:
                return name
        return session_key

    def _on_agent_label_clicked(self, current_solo):
        if self._on_agent_cycle is None:
            return
        self._on_agent_cycle(current_solo)

    def _on_autoaccept_label_clicked(self, current_level):
        if self._on_autoaccept_cycle is None:
            return
        self._on_autoaccept_cycle(current_level)

    def _on_settings_btn_clicked(self, _btn):
        if self._on_settings_clicked:
            self._on_settings_clicked()

    def set_on_settings_clicked(self, callback):
        self._on_settings_clicked = callback

    def set_on_agent_cycle(self, callback):
        self._on_agent_cycle = callback

    def set_on_autoaccept_cycle(self, callback):
        self._on_autoaccept_cycle = callback

    def set_project_settings_text(self, text: str):
        """Set text or markup on the project settings bar. Handles Pango markup correctly.

        Backward compat: still clears the bar, appends the label, then re-appends the
        singleton gear button so it is never lost (BUG #5). Uses the sibling-walk
        _clear_settings_bar() helper (not `for child in list(box)`). A later
        update_project_settings() call fully rebuilds the row.
        """
        self._clear_settings_bar()
        lbl = Gtk.Label()
        lbl.set_halign(Gtk.Align.END)
        lbl.set_margin_start(8)
        lbl.set_margin_end(8)
        if text.startswith("<"):
            lbl.set_markup(text)
        else:
            lbl.set_text(text)
        self._project_settings.append(lbl)
        self._project_settings.append(self._settings_btn)

    def set_on_buffer_changed(self, cb: callable) -> None:
        """Register callback for input buffer 'changed' events. cb(buffer)."""
        self._on_buffer_changed = cb

    def _on_input_buffer_changed(self, buf) -> None:
        """Fire the registered callback (if any). The actual buf.connect
        is in __init__; this is the indirection layer."""
        if self._on_buffer_changed is not None:
            self._on_buffer_changed(buf)

    def set_on_input_right_click(self, cb: callable) -> None:
        """Register callback for right-click on input TextView.

        cb(n_press, x, y, menu: Gio.Menu, action_group: Gio.SimpleActionGroup)
        — called on right-click. The callback should populate the menu with
        spell-check suggestion items and add corresponding actions to the
        action group. The TextView handles showing the menu itself.
        """
        self._on_input_right_click = cb

    def _on_input_right_click_internal(self, gesture, n_press, x, y) -> None:
        """Internal: rebuild spell-check extra menu based on the word at (x, y).

        The TextView handles showing the menu itself — we just populate it.
        """
        if n_press != 1:
            return
        # Clear the menu and action group from the previous right-click
        self._spell_extra_menu.remove_all()
        # Remove all previous spell suggestion actions
        for action in self._spell_action_group.list_actions():
            self._spell_action_group.remove_action(action)
        # Build new menu if we have a callback that returns suggestion data
        if self._on_input_right_click is not None:
            self._on_input_right_click(n_press, x, y, self._spell_extra_menu, self._spell_action_group)

    def set_on_project_settings_update(self, cb):
        """Set callback for project settings updates. cb(project_name, member_count)."""
        self._on_feed_bar_update = cb

    def _update_project_settings_from_project(self, project_name: str, member_count: int):
        """Internal — called by window to refresh the project settings bar."""
        if project_name:
            self._project_settings.set_visible(True)
            # MED-9: escape interpolated values to prevent Pango markup injection
            safe_name = escape_for_pango(project_name)
            self.set_project_settings_text(
                f'<span font_desc="Sans 10"><b>{safe_name}</b>  ·  {member_count} member{"s" if member_count != 1 else ""}</span>'
            )
        else:
            self._project_settings.set_visible(False)
            self.set_project_settings_text('Project Settings')

    def set_feed_bar_text(self, text):
        """Update the project feed bar with a status message (legacy).

        Same gear-preservation as set_project_settings_text() (BUG #5): uses the
        sibling-walk _clear_settings_bar() helper and re-appends the singleton
        gear button so it is never lost.
        """
        self._clear_settings_bar()
        if text:
            lbl = Gtk.Label()
            lbl.set_halign(Gtk.Align.END)
            lbl.set_margin_start(8)
            lbl.set_margin_end(8)
            if text.startswith("<"):
                lbl.set_markup(text)
            else:
                lbl.set_text(text)
            self._project_settings.append(lbl)
        self._project_settings.append(self._settings_btn)

    # ── Tab management ──────────────────────────────────────────────────────

    def create_chat_tab(self, session_key, agent_name):
        """
        Create a new chat tab for an agent.
        Returns the page index of the new tab, or existing tab index if already exists.
        """
        # Check if tab already exists for this session_key
        for page_idx, sk in self._tab_sessions.items():
            if sk == session_key:
                self._chat_notebook.set_current_page(page_idx)
                return page_idx

        # Scrollable container for the chat content — wrapped in Overlay so the
        # project settings bar can float over it (below the tab row).
        chat_scroll = Gtk.ScrolledWindow()
        chat_scroll.set_vexpand(True)
        chat_scroll.set_hexpand(True)
        chat_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        chat_overlay = Gtk.Overlay()
        chat_overlay.set_vexpand(True)
        chat_overlay.set_child(chat_scroll)
        # Only add singleton overlays to the FIRST tab.
        # On subsequent tabs, they'll be moved via _on_notebook_switch_page.
        if len(self._tab_sessions) == 0:
            chat_overlay.add_overlay(self._project_settings)
            chat_overlay.add_overlay(self._scroll_btn_box)

        # Vertical box for chat messages
        chat_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        chat_box.set_halign(Gtk.Align.FILL)
        chat_box.set_valign(Gtk.Align.END)
        chat_scroll.set_child(chat_box)

        # Tab label = [dot] [agent name bold] [•] [session name] [spacer] [×]
        tab_label_box = Gtk.Box(spacing=4)
        tab_label_box.set_valign(Gtk.Align.CENTER)
        tab_label_box.set_halign(Gtk.Align.START)
        tab_label_box.set_margin_start(8)

        # Status dot — green (idle) by default, yellow when unread
        tab_dot = Gtk.Box()
        tab_dot.add_css_class("tab-dot")
        tab_dot.add_css_class("tab-dot-idle")
        tab_dot.set_valign(Gtk.Align.CENTER)

        # Agent name (bold)
        tab_name = Gtk.Label()
        tab_name.set_markup(f"<b>{GLib.markup_escape_text(agent_name)}</b>")
        tab_name.set_valign(Gtk.Align.CENTER)
        tab_name.add_css_class("tab-label-name")

        # Bullet separator
        tab_sep = Gtk.Label(label="•")
        tab_sep.set_valign(Gtk.Align.CENTER)
        tab_sep.add_css_class("tab-label-separator")

        # Session/channel name
        session_display = self._extract_session_display_name(session_key)
        tab_session = Gtk.Label(label=session_display)
        tab_session.set_valign(Gtk.Align.CENTER)
        tab_session.add_css_class("tab-label-session")

        close_btn = Gtk.Button(label="×")
        close_btn.set_valign(Gtk.Align.CENTER)
        close_btn.set_has_frame(False)
        close_btn.set_focus_on_click(False)
        close_btn.set_size_request(20, -1)
        close_btn.set_hexpand(False)

        tab_label_box.append(tab_dot)
        tab_label_box._dot = tab_dot  # explicit reference avoids GTK4 get_first_child() ordering issue
        tab_label_box.append(tab_name)
        tab_label_box.append(tab_sep)
        tab_label_box.append(tab_session)
        tab_label_box.append(close_btn)

        # Append the new tab FIRST to get page_idx, then wire handlers
        page_idx = self._chat_notebook.append_page(chat_overlay, tab_label_box)

        # Store session_key on the tab label box so close handlers can look up
        # the CURRENT page index dynamically (avoids stale captured page_idx bug
        # when tabs are closed out of order and reindexing shifts pages).
        tab_label_box._session_key = session_key
        chat_scroll._session_key = session_key

        close_btn.connect("clicked", self._on_tab_close_clicked)

        # Middle-click on the label also closes the tab
        click_ctrl = Gtk.GestureClick()
        click_ctrl.set_button(Gdk.BUTTON_MIDDLE)
        click_ctrl.connect("pressed", self._on_tab_middle_click)
        tab_label_box.add_controller(click_ctrl)

        # Right-click on the label shows session switch menu
        right_ctrl = Gtk.GestureClick()
        right_ctrl.set_button(Gdk.BUTTON_SECONDARY)
        right_ctrl.connect("pressed", self._on_tab_right_click, session_key)
        tab_label_box.add_controller(right_ctrl)
        self._tab_sessions[page_idx] = session_key
        self._tab_chat_boxes[page_idx] = chat_box
        self._tab_scrolls[page_idx] = chat_scroll
        self._tab_overlays[page_idx] = chat_overlay
        # Phase 5b: wire scroll adjustment callback for scroll-to-bottom button
        vadj = chat_scroll.get_vadjustment()
        if vadj is not None:
            vadj.connect("value-changed", self._on_vadjustment_changed, page_idx)
        self._chat_notebook.set_show_tabs(True)
        self._chat_notebook.set_current_page(page_idx)
        # Welcome bubble — centered logo at bottom of new chat tabs.
        # Scrolled away naturally as messages arrive.
        from ui.views.chat_bubble import build_welcome_bubble
        welcome = build_welcome_bubble()
        if welcome is not None:
            chat_box.append(welcome)
        return page_idx

    def _on_notebook_switch_page(self, notebook, _page, page_num):
        """Handle tab switch: move overlays, scroll to bottom, clear unread."""
        # Move singleton overlays (project settings, scroll btn) to the new tab.
        # Detach them from whatever overlay they're currently on first to avoid
        # GTK4 "Can't set new parent" warnings when the widget already has a parent.
        overlay = self._tab_overlays.get(page_num)
        if overlay is not None:
            for widget in (self._project_settings, self._scroll_btn_box):
                # Gtk.Overlay has no .remove() method (it is not a Gtk.Container).
                # Use Gtk.Widget.unparent() — works regardless of parent type.
                # See session_menu.py, left_panel.py for the same pattern.
                if widget.get_parent() is not None:
                    widget.unparent()
                overlay.add_overlay(widget)
        self.scroll_chat_to_bottom(page_num)
        # Clear unread state for the tab being switched to
        tab_label = self._chat_notebook.get_tab_label(notebook.get_nth_page(page_num))
        if tab_label and hasattr(tab_label, '_session_key'):
            self.clear_unread(tab_label._session_key)
        # Notify listeners of the session change (for context meter avatar/name update)
        new_sk = self._tab_sessions.get(page_num)
        if new_sk and self._on_session_changed:
            self._on_session_changed(new_sk)

    # ── Unread dot management ──────────────────────────────────────────────

    @staticmethod
    def _extract_session_display_name(session_key: str) -> str:
        """Parse a session key like 'agent:qaster:telegram:direct:7478874934'
        and return a human-readable channel name like 'Telegram'."""
        parts = session_key.split(":")
        if len(parts) >= 3:
            channel = parts[2].lower()
            channel_map = {
                "telegram": "Telegram",
                "discord": "Discord",
                "signal": "Signal",
                "whatsapp": "WhatsApp",
                "slack": "Slack",
                "cli": "CLI",
                "web": "Web",
            }
            return channel_map.get(channel, channel.title())
        return session_key

    def increment_unread(self, session_key: str) -> None:
        """Mark a tab as having unread messages; update dot to yellow."""
        _logger.debug("[tab-dot] increment_unread(%r), currently unread=%r", session_key, self._unread_tabs)
        self._unread_tabs.add(session_key)
        self._update_tab_dot(session_key)

    def clear_unread(self, session_key: str) -> None:
        """Clear unread state when user switches to a tab; update dot to green."""
        _logger.debug("[tab-dot] clear_unread(%r), currently unread=%r", session_key, self._unread_tabs)
        self._unread_tabs.discard(session_key)
        self._update_tab_dot(session_key)

    def _update_tab_dot(self, session_key: str) -> None:
        """Update the dot color on a tab label based on unread state."""
        _logger.debug("[tab-dot] _update_tab_dot called with session_key=%r, _unread_tabs=%r", session_key, self._unread_tabs)
        page_idx = self._find_page_by_session(session_key)
        _logger.debug("[tab-dot] _find_page_by_session(%r) -> page_idx=%r", session_key, page_idx)
        if page_idx is None:
            _logger.warning("[tab-dot] No tab found for session_key=%r — cannot update dot", session_key)
            return
        tab_label_box = self._chat_notebook.get_tab_label(
            self._chat_notebook.get_nth_page(page_idx)
        )
        if tab_label_box is None:
            return
        # Dot stored directly on tab_label_box at creation time — avoids GTK4
        # get_first_child() ordering ambiguity (TabLabelBox children may not be
        # in insertion order after layout).
        dot = getattr(tab_label_box, '_dot', None)
        if dot is None:
            return
        has_unread = session_key in self._unread_tabs
        if has_unread:
            dot.remove_css_class("tab-dot-idle")
            dot.add_css_class("tab-dot-unread")
        else:
            dot.remove_css_class("tab-dot-unread")
            dot.add_css_class("tab-dot-idle")

    def _update_tab_session_label(self, page_idx: int, new_session_key: str) -> None:
        """Update the session/channel label in the tab for a given page.

        Traversal: dot(0) → name(1) → sep(2) → session_label(3).
        Called from _switch_tab_session() after updating _tab_sessions.
        """
        page = self._chat_notebook.get_nth_page(page_idx)
        if page is None:
            return
        tab_label_box = self._chat_notebook.get_tab_label(page)
        if tab_label_box is None:
            return
        dot = getattr(tab_label_box, '_dot', None)
        if dot is None:
            return
        name = dot.get_next_sibling()
        sep = name.get_next_sibling() if name else None
        session_label = sep.get_next_sibling() if sep else None
        if session_label is None:
            return
        session_label.set_label(self._extract_session_display_name(new_session_key))

    def _find_page_by_session(self, session_key):
        """Return the current page index for a session_key by scanning notebook tab labels.

        The _session_key attribute is stored on the tab_label_box (the tab),
        not the page widget. Use get_tab_label() to access it.
        """
        n_pages = self._chat_notebook.get_n_pages()
        _logger.debug("[tab-dot] _find_page_by_session scanning %d tabs for session_key=%r", n_pages, session_key)
        for idx in range(n_pages):
            page_widget = self._chat_notebook.get_nth_page(idx)
            tab_label = self._chat_notebook.get_tab_label(page_widget)
            tab_sk = getattr(tab_label, '_session_key', None) if tab_label else None
            _logger.debug("[tab-dot]   tab[%d]._session_key=%r (looking for %r)", idx, tab_sk, session_key)
            if tab_label and getattr(tab_label, '_session_key', None) == session_key:
                _logger.debug("[tab-dot]   MATCH at index %d", idx)
                return idx
        _logger.debug("[tab-dot]   no match found")
        return None

    def _close_tab(self, page_idx):
        """Remove a tab by page index and clean up tracking dicts."""
        session_key = self._tab_sessions.get(page_idx)  # SP5a: capture BEFORE pop
        self._chat_notebook.remove_page(page_idx)
        self._tab_sessions.pop(page_idx, None)
        self._tab_chat_boxes.pop(page_idx, None)
        self._tab_scrolls.pop(page_idx, None)
        self._tab_overlays.pop(page_idx, None)
        # SPEC-06 SP5a (R2): release the session's chat surface — the SP3
        # destroy contract (pending-render cancel + guards) handles the rest.
        # Fires even during bulk close (stop-all spirit). Project tabs go
        # through here too: close_project_tab resolves to _close_tab, so the
        # wiring is single-point (no double-close).
        if session_key is not None and self._chat_render_handler is not None:
            self._chat_render_handler.close_session(session_key)
        # Hide tab bar when empty to prevent GtkGizmo min-height < 0 warning
        if self._chat_notebook.get_n_pages() == 0:
            self._chat_notebook.set_show_tabs(False)
        if not self._bulk_closing:
            self._reindex_tabs()

    def _reindex_tabs(self):
        """
        Rebuild _tab_sessions and _tab_chat_boxes to reflect current notebook page order.

        Correct algorithm: iterate current GTK pages (which are the authoritative order),
        look up each page's widget (the GtkOverlay), then find the matching session_key
        by scanning the saved snapshot. Uses _tab_overlays (page widget = GtkOverlay)
        for the lookup, NOT _tab_chat_boxes (child of ScrolledWindow inside the overlay).
        """
        # Snapshot current state before rebuilding
        saved_sessions = dict(self._tab_sessions)
        saved_chat_boxes = dict(self._tab_chat_boxes)
        saved_scrolls = dict(self._tab_scrolls)
        saved_overlays = dict(self._tab_overlays)

        # Build overlay widget -> old_page_idx map from snapshot
        # get_nth_page() returns the GtkOverlay (top-level page widget),
        # so we must key by overlay, not by chat_box.
        overlay_to_idx = {}
        for old_idx, overlay in saved_overlays.items():
            overlay_to_idx[overlay] = old_idx

        new_sessions = {}
        new_chat_boxes = {}
        new_scrolls = {}
        new_overlays = {}
        n_pages = self._chat_notebook.get_n_pages()
        for new_idx in range(n_pages):
            page_widget = self._chat_notebook.get_nth_page(new_idx)  # GtkOverlay
            old_idx = overlay_to_idx.get(page_widget)
            if old_idx is not None:
                new_sessions[new_idx] = saved_sessions[old_idx]
                new_chat_boxes[new_idx] = saved_chat_boxes[old_idx]
                new_scrolls[new_idx] = saved_scrolls[old_idx]
                new_overlays[new_idx] = page_widget

        self._tab_sessions = new_sessions
        self._tab_chat_boxes = new_chat_boxes
        self._tab_scrolls = new_scrolls
        self._tab_overlays = new_overlays

    def close_tabs(self, page_indices):
        """
        Close multiple tabs in one call, reindexing only once at the end.

        Tabs are closed highest-index-first so that lower indices remain stable
        for the duration of the loop.

        Args:
            page_indices: iterable of int page indices to close
        """
        if not page_indices:
            return
        self._bulk_closing = True
        try:
            for idx in sorted(page_indices, reverse=True):
                self._chat_notebook.remove_page(idx)
                self._tab_sessions.pop(idx, None)
                self._tab_chat_boxes.pop(idx, None)
                self._tab_scrolls.pop(idx, None)
                self._tab_overlays.pop(idx, None)
        finally:
            self._bulk_closing = False
        self._reindex_tabs()

    def _on_tab_close_clicked(self, _btn):
        """× button clicked on a tab — close it."""
        tab_label_box = _btn.get_parent()
        session_key = getattr(tab_label_box, "_session_key", None) if tab_label_box else None
        if session_key is None:
            return
        page_idx = self._find_page_by_session(session_key)
        if page_idx is None:
            return

        # If this is a project tab, notify window so it can close the project properly
        if session_key.startswith("project:") and self._on_project_tab_closed:
            project_name = session_key.replace("project:", "", 1)
            self._on_project_tab_closed(project_name)
        else:
            self._close_tab(page_idx)

    def _on_tab_middle_click(self, ctrl, n_press, x, y):
        """Middle-click on tab label — close it."""
        if n_press != 1:
            return
        tab_label_box = ctrl.get_widget()
        session_key = getattr(tab_label_box, "_session_key", None) if tab_label_box else None
        if session_key is None:
            return
        page_idx = self._find_page_by_session(session_key)
        if page_idx is not None:
            # If this is a project tab, notify window so it can close the project properly
            if session_key.startswith("project:") and self._on_project_tab_closed:
                project_name = session_key.replace("project:", "", 1)
                self._on_project_tab_closed(project_name)
            else:
                self._close_tab(page_idx)

    def _on_tab_right_click(self, ctrl, n_press, x, y, session_key):
        """Right-click on tab label — show appropriate menu."""
        if n_press != 1:
            return
        tab_widget = ctrl.get_widget()

        # Project tab — show project solo-DM menu (works offline via AgentRuntimeHandler)
        if session_key.startswith("project:"):
            project_name = session_key.replace("project:", "", 1)
            if self._project_handler is None:
                return
            members = self._project_handler.get_project_members(project_name)
            if not members:
                return
            # Build (session_key, display_name) pairs — use _agent_mgr if available,
            # otherwise fall back to AgentRuntimeHandler (offline mode)
            member_names = []
            for sk in members:
                if self._agent_mgr is not None:
                    name = self._agent_mgr.get_name(sk) or sk
                elif (self._agent_runtime_handler is not None
                      and sk in self._agent_runtime_handler.get_special_agents()):
                    name = self._agent_runtime_handler.get_special_agents()[sk]
                else:
                    name = sk
                member_names.append((sk, name))
            current_solo = self._project_handler.get_solo_target(project_name)
            show_project_menu(
                tab_widget,
                f"Project: {project_name}",
                member_names,
                current_solo,
                lambda target_sk: self._on_project_solo_selected(session_key, project_name, target_sk),
            )
            return

        # Agent tab — show session switcher (requires _agent_mgr, gateway-only)
        if self._agent_mgr is None:
            return
        agent_name = self._agent_mgr.get_name(session_key)
        if not agent_name:
            return
        sessions = self._agent_mgr.get_sessions(agent_name)
        page_idx = self._find_page_by_session(session_key)
        if page_idx is None:
            return
        show_session_menu(
            tab_widget,
            agent_name,
            sessions,
            lambda sk: self._switch_tab_session(page_idx, sk),
            current_session_key=self._tab_sessions.get(page_idx, session_key),
        )

    def _on_project_solo_selected(self, session_key, project_name, target_sk):
        """
        Handle project tab solo-DM target selection.
        target_sk = session_key of selected member, or None = All (group broadcast).
        """
        self._project_handler.set_solo_target(project_name, target_sk)

    def _switch_tab_session(self, page_idx, new_session_key):
        """Switch an existing tab to a new session key.

        Note: this only updates _tab_sessions — it does NOT create a new tab,
        switch to it, or change the visible label. The tab keeps showing its
        original agent_name. The session_key used for routing incoming messages
        is what changes. Designed for switching between sessions of the same agent.

        The tab label box._session_key is also kept in sync so that
        _find_page_by_session (which reads the widget attribute) finds the
        correct tab on subsequent gateway events."""
        self._tab_sessions[page_idx] = new_session_key
        self._update_tab_session_label(page_idx, new_session_key)
        # Keep tab_label_box._session_key in sync so _find_page_by_session
        # (which reads the widget attribute) finds this tab on subsequent events.
        page = self._chat_notebook.get_nth_page(page_idx)
        if page is not None:
            tab_label_box = self._chat_notebook.get_tab_label(page)
            if tab_label_box is not None:
                tab_label_box._session_key = new_session_key

    def get_current_session_key(self):
        """Return the session_key for the currently active tab, or None."""
        current = self._chat_notebook.get_current_page()
        return self._tab_sessions.get(current)

    def get_chat_box(self, page_index=None):
        """Return the chat box widget for a given page index (default: current page)."""
        if page_index is None:
            page_index = self._chat_notebook.get_current_page()
        return self._tab_chat_boxes.get(page_index)

    def get_chat_box_for_session(self, session_key: str):
        """
        Return the chat box widget for the tab matching session_key.

        Iterates over _tab_sessions to find the page_index with the matching
        session_key, then returns the corresponding chat box.
        Returns None if no tab exists for that session_key.
        """
        for page_idx, sk in self._tab_sessions.items():
            if sk == session_key:
                return self._tab_chat_boxes.get(page_idx)
        return None

    def set_chat_render_handler(self, handler):
        """Inject ChatRenderHandler instance. Called by window.py._build().

        SPEC-06 SP5a (R1a): ALSO injects the session→chat-box getter so the
        handler can mount surfaces at first create (the single wiring point
        for the transcript-blank-window fix). Tolerates handlers without
        the setter (test doubles)."""
        self._chat_render_handler = handler
        setter = getattr(handler, "set_chat_container_getter", None)
        if setter is not None:
            setter(self.get_chat_box_for_session)

    def set_project_handler(self, handler):
        """Inject ProjectHandler instance. Called by window.py._build()."""
        self._project_handler = handler

    def set_file_tree(self, file_tree) -> None:
        """Inject FileTree instance for integrated diff navigation."""
        self._file_tree = file_tree

    def toggle_file_diff(self, file_path: str) -> None:
        """Toggle file diff drawer open/closed for a path via FileTree."""
        ft = getattr(self, '_file_tree', None)
        if ft is not None:
            ft.toggle_drawer_for_file(file_path)

    def set_on_project_tab_closed(self, callback):
        """Set callback for when a project tab is closed via (X) or middle-click.
        Signature: callback(project_name: str). Called by window.py._build().
        """
        self._on_project_tab_closed = callback

    def set_on_session_changed(self, callback):
        """Set callback for when the active chat tab changes.
        Signature: callback(session_key: str). Called by window.py._build()
        to update the context meter avatar/name display.
        """
        self._on_session_changed = callback

    def close_project_tab(self, project_name: str):
        """Remove the project tab from the notebook. Called by window._close_project_tab()."""
        session_key = f"project:{project_name}"
        page_idx = self._find_page_by_session(session_key)
        if page_idx is not None:
            self._close_tab(page_idx)

    def scroll_chat_to_bottom(self, page_index=None):
        """Scroll the chat ScrolledWindow to the bottom."""
        if page_index is None:
            page_index = self._chat_notebook.get_current_page()
        scroll = self._tab_scrolls.get(page_index)
        if scroll is None:
            return
        vadj = scroll.get_vadjustment()
        if vadj is None:
            return
        # Defer scroll to next frame — widget layout must recalculate first
        def _do_scroll():
            vadj.set_value(vadj.get_upper() - vadj.get_page_size())
            return False  # don't repeat
        from gi.repository import GLib
        # Use timeout_add(16) to wait one frame (~16ms at 60fps) — this gives GTK
        # time to allocate the new child widget and update vadjustment values before
        # we read them. idle_add/timeout_add(0) can race with the layout phase.
        GLib.timeout_add(16, _do_scroll)

    def _on_scroll_to_bottom_clicked(self, *args):
        """Handle scroll-to-bottom button click — scroll current tab to bottom."""
        self.scroll_chat_to_bottom()
        self._scroll_btn.set_opacity(0)

    def _on_vadjustment_changed(self, adjustment, page_index):
        """
        Show scroll-to-bottom button when user scrolls up away from bottom.
        Hide it when user is at or near the bottom of the chat.
        """
        current_page = self._chat_notebook.get_current_page()
        if page_index != current_page:
            return  # only track current page
        upper = adjustment.get_upper()
        page_size = adjustment.get_page_size()
        current_value = adjustment.get_value()
        # distance_from_bottom: how far the scroll is from the bottom
        distance_from_bottom = upper - page_size - current_value
        if distance_from_bottom > 80:
            self._scroll_btn.set_opacity(1)
        else:
            self._scroll_btn.set_opacity(0)

    # ── Context meter (Phase A) ──────────────────────────────────────────

    def update_agent_context_display(self, agent_name: str, agent_color: str) -> None:
        """Update the small avatar + name display next to the context meter.

        Args:
            agent_name: Display name of the active agent (e.g. "Coder").
            agent_color: Hex color string (e.g. "#6366f1").
        """
        from utils.icons import render_agent_icon
        # Compute initials from name
        parts = agent_name.split() if agent_name else []
        if len(parts) >= 2:
            initials = (parts[0][0] + parts[1][0]).upper()
        elif agent_name:
            initials = agent_name[:2].upper()
        else:
            initials = "??"
        texture = render_agent_icon(agent_color or "#6366f1", initials, size=28)
        if texture is not None:
            self._context_avatar.set_paintable(texture)
        self._context_name_label.set_text(agent_name or "")

    def set_context_meter(
        self, session_key: str, usage_percent: float
    ) -> None:
        """Update the context-meter widget for the active ``session_key``.

        Called from window.py when AgentRuntimeHandler emits a breakdown.
        ``usage_percent`` is 0.0-100.0. Negative values reset to idle.
        """
        if usage_percent is None or not isinstance(usage_percent, (int, float)):
            self._context_meter.set_fraction(0.0)
            self._context_meter_label.set_text("")
            return
        if usage_percent != usage_percent:  # NaN check
            self._context_meter.set_fraction(0.0)
            self._context_meter_label.set_text("")
            return
        if usage_percent < 0:
            self._context_meter.set_fraction(0.0)
            self._context_meter_label.set_text("")
            return
        self._context_meter.set_fraction(min(usage_percent / 100.0, 1.0))
        # Pick CSS class per zone (green < 70%, yellow 70-90%, red >= 90%).
        existing_classes = [
            c for c in self._context_meter.get_css_classes()
            if c.startswith("context-meter-")
        ]
        for c in existing_classes:
            self._context_meter.remove_css_class(c)
        if usage_percent >= 90.0:
            self._context_meter.add_css_class("context-meter-high")
            color_label = "high"
        elif usage_percent >= 70.0:
            self._context_meter.add_css_class("context-meter-medium")
            color_label = "med"
        else:
            self._context_meter.add_css_class("context-meter-low")
            color_label = "ok"
        self._context_meter_label.set_text(f"{color_label} {usage_percent:.0f}%")
        self._context_pct_label.set_text(f"{usage_percent:.0f}%")

    # ── STT (Speech-to-Text) ───────────────────────────────────────────────
    # State machine: idle → click → recording → click → idle.
    # update_stt_state() drives the button label/style.
    # append_stt_text() appends partial transcripts as they arrive.

    def set_on_stt_click(self, cb):
        """Set callback for when the Prompt (STT) button is clicked."""
        self._on_stt_start_stop = cb

    def set_on_stt_partial(self, cb):
        """Set callback for partial STT results — append to input buffer."""
        self._on_stt_partial = cb

    def update_stt_state(self, state):
        """
        Update the Prompt button appearance to reflect STT state.
        state: "idle" | "recording"
        """
        self._stt_state = state
        if state == "recording":
            self._prompt_label.set_text("■ Stop")
            self._prompt_label.add_css_class("recording-stop")
            self._prompt_mic_img.set_visible(False)
            self._prompt_button.add_css_class("destructive-action")
        else:
            self._prompt_label.set_text("Prompt")
            self._prompt_label.remove_css_class("recording-stop")
            self._prompt_mic_img.set_visible(True)
            self._prompt_button.remove_css_class("destructive-action")

    def append_stt_text(self, text):
        """
        Insert STT transcript text at the current cursor position in the input buffer.
        If text is selected, it is replaced. Cursor ends up after the inserted text.
        """
        buf = self.user_input.get_buffer()
        buf.insert_at_cursor(text)
        self.user_input.grab_focus()

    def set_agent_manager(self, agent_mgr):
        """Set the AgentManager for session lookup (used by session switch menu)."""
        self._agent_mgr = agent_mgr

    def set_agent_runtime_handler(self, handler):
        """
        Set the AgentRuntimeHandler for offline name lookup.

        Used by _on_tab_right_click to resolve member display names when
        _agent_mgr is None (offline mode, before gateway has connected).
        """
        self._agent_runtime_handler = handler

    def _on_prompt_clicked(self, *args):
        """Forward button click to the registered STT callback."""
        if self._on_stt_start_stop:
            self._on_stt_start_stop()

    def set_on_improve_click(self, cb):
        """Set callback for when the Improve button is clicked."""
        self._on_improve_click = cb

    def set_on_send_click(self, cb):
        """Set callback for send button click. Called in addition to ChatHandler.on_send_clicked."""
        self._send_button.connect("clicked", lambda _: cb())

    def _on_improve_clicked(self, *args):
        """Forward button click to the registered improve callback."""
        if self._on_improve_click:
            self._on_improve_click()

    def replace_input_text(self, text):
        """Replace the entire input buffer with improved text."""
        buf = self.user_input.get_buffer()
        buf.set_text(text)
        end_iter = buf.get_end_iter()
        buf.place_cursor(end_iter)
        self.user_input.grab_focus()

    def set_review_bar(self, bar: Gtk.Widget | None):
        """
        Add or remove a review bar widget above the chat area.

        Called by ReviewHandler when a review session starts or ends.
        """
        # Remove existing review bar if any
        if hasattr(self, '_review_bar') and self._review_bar is not None:
            old_bar = self._review_bar
            old_bar.unparent()
            self._review_bar = None

        self._review_bar = bar
        if bar is None:
            return

        # Insert the bar at the top of the top_box (above the notebook).
        # MainContent's direct child is a vertical Gtk.Paned.
        # The paned's start child is the top_box containing the notebook.
        paned = self.get_first_child()
        if paned is None:
            return
        top_box = paned.get_start_child()
        if top_box is None:
            return
        # Insert at the very top of top_box (above the notebook).
        # GTK4: use prepend() — pack_start() was removed in GTK4.
        top_box.prepend(bar)

    def get_review_bar(self) -> Gtk.Widget | None:
        """Return the current review bar widget, or None.


        Used by ReviewHandler and ActivityHandler to read the current bar
        without accessing MainContent internal state.
        """
        return getattr(self, '_review_bar', None)

    # ── Diff Viewer Slot (Phase 2a) ─────────────────────────────────────────

    def show_diff_viewer(self, viewer_widget: Gtk.Widget) -> None:
        """Insert diff viewer above the chat notebook, below the review bar.

        Follows the same insert/remove pattern as set_review_bar().
        The diff viewer sits between the review bar (if present) and the chat notebook.
        Chat notebook remains visible — PM can see both diff and chat.
        """
        # Remove existing diff viewer if any (M21 fix: dispose old viewer)
        self.hide_diff_viewer()

        self._diff_viewer = viewer_widget

        paned = self.get_first_child()
        if paned is None:
            return
        top_box = paned.get_start_child()
        if top_box is None:
            return

        # Insert after review bar (if present), before chat notebook
        review_bar = getattr(self, '_review_bar', None)
        if review_bar is not None:
            # L1 sanity: verify review_bar is actually in top_box
            if review_bar.get_parent() == top_box:
                top_box.insert_after(viewer_widget, review_bar)
            else:
                top_box.prepend(viewer_widget)
        else:
            top_box.prepend(viewer_widget)

    def hide_diff_viewer(self) -> None:
        """Remove diff viewer from main_content if present.

        M21 fix: marks the old viewer as disposed before unparenting,
        so its background threads' idle_add callbacks become no-ops.
        """
        viewer = getattr(self, '_diff_viewer', None)
        if viewer is not None:
            # H3/M21 fix: mark disposed before removing from widget tree
            if hasattr(viewer, '_disposed'):
                viewer._disposed = True
            viewer.unparent()
            self._diff_viewer = None

    def get_diff_viewer(self) -> Gtk.Widget | None:
        """Return the current diff viewer widget, or None."""
        return getattr(self, '_diff_viewer', None)
