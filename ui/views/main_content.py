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

from ui.views.activity_pill import ActivityPillLabel
from ui.views.chat_input_toolbar import ChatInputToolbar
from ui.views.session_menu import show_project_menu
from utils.config import get_project_root

_PIP_CLASSES = (
    "project-bar-connect-off",
    "project-bar-connect-pending",
    "project-bar-connect-on",
    "project-bar-connect-error",
)

_CONNECT_TIPS = {
    "unconfigured": (
        "No transport (configure a bot token in Settings → Telegram Bridge)"
    ),
    "disconnected": (
        "Connect the Supervisor to your phone over Telegram "
        "(chat + exec approvals; configure a bot token in Settings)"
    ),
    "connecting": "Connecting…",
    "connected": "Disconnect the Telegram bridge",
    "error": "Offline — check the bot token and pairing in Settings",
}

_STOP_ALL_TIP = (
    "Halt every agent: cancel in-flight turns, kill spawned process "
    "groups, abort pending review checkpoints."
)

# Built-in fallback shown when assets/splash.html is missing (BUG #3):
# the Welcome tab must never stay permanently blank on a read error.
_WELCOME_FALLBACK_HTML = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<style>html,body{height:100%;margin:0;background:#121417;color:#f2f2f2;"
    "font-family:system-ui,-apple-system,'Segoe UI',sans-serif;display:flex;"
    "align-items:center;justify-content:center;} .card{text-align:center;}"
    "h1{font-size:2.2rem;letter-spacing:.5px;margin:0 0 .6rem;}"
    "p{color:#a8adb8;font-size:1rem;margin:0;}</style></head>"
    "<body><div class='card'><h1>DevelCakes</h1>"
    "<p>Welcome — this workspace tab is ready.</p></div></body></html>"
)


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
        self._top_box = top_box

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
        self._chat_render_handler = None  # injected via set_chat_render_handler()
        self._project_handler = None   # injected via set_project_handler()
        self._on_project_tab_closed = None  # callback(name) — set by window.py
        self._on_session_changed = None  # callback(session_key) — set by window.py for context meter update
        # Bulk-close guard: skip reindex until all removals are done
        self._bulk_closing = False
        # One-shot Welcome splash tab — created on fresh launch; once closed
        # it stays closed for the session (never respawns on project switch
        # or new tabs).
        self._welcome_closed = False
        # One-load guard (BUG #5): the raw splash load is issued at most once
        # per session — a duplicate create_welcome_tab call must not re-issue
        # the load (which would re-render over the live splash).
        self._welcome_loaded = False

        self._toolbar = ChatInputToolbar()

        # Top box minimum height — prevents it collapsing when notebook is empty
        top_box.set_size_request(-1, 120)

        top_box.append(self._chat_notebook)
        top_box.append(self._toolbar)

        # ── Project Settings Bar — floating OVER the chat scroll area only ────
        # Placed as overlay on the notebook's chat area (not the tab bar).
        # Opaque (UI-PILLBAR P1: rgba 0.75 → #1e1e28); chat content scrolls
        # underneath it. CSS .project-feed-bar provides background + radius.
        self._project_settings = Gtk.Box()
        self._project_settings.set_halign(Gtk.Align.FILL)
        self._project_settings.set_valign(Gtk.Align.START)
        self._project_settings.set_size_request(-1, 28)
        self._project_settings.set_margin_top(4)
        self._project_settings.set_margin_bottom(0)
        self._project_settings.add_css_class("project-feed-bar")
        self._project_settings.set_visible(False)

        # UI-PILLBAR Phase 1 — the SHARED activity-status pill lives at the
        # bar's right end (info_box is hexpand+START, so the pill lands at
        # the right edge). One pill for every tab. Re-appended after every
        # _clear_settings_bar().
        self._activity_pill = ActivityPillLabel()
        self._on_agent_cycle = None
        self._on_autoaccept_cycle = None
        self._on_connect_clicked = None
        self._on_stop_all_clicked = None
        self._build_transport_cluster()

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

        # Transient Improve status line (SPEC-55 §3d) — surfaces unconfigured /
        # error messages from the improve flow near the input. Cleared on the
        # next improve click or input edit.
        self._improve_status_label = Gtk.Label()
        self._improve_status_label.set_xalign(0.0)
        self._improve_status_label.set_wrap(True)
        self._improve_status_label.add_css_class("dim-label")
        self._improve_status_label.add_css_class("improve-status")
        self._improve_status_label.set_visible(False)
        bottom_box.append(self._improve_status_label)

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

        # Pill-only shape, visible, hosted above the notebook until the
        # first chat tab overlays it under the tab strip.
        self.update_project_settings("", 0, None, "off", None)
        self._top_box.prepend(self._project_settings)

    def _build_transport_cluster(self) -> None:
        """Icon connect, state pip, and stop-all. Built once and reparented."""
        self._transport_cluster = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self._transport_cluster.add_css_class("project-bar-transport")

        self._connect_btn = self._make_bar_icon(
            "network-transmit-receive-symbolic", 16
        )
        self._connect_btn.connect("clicked", self._on_transport_connect_clicked)

        self._connect_pip = Gtk.Button()
        self._connect_pip.add_css_class("project-bar-connect-pip")
        self._connect_pip.add_css_class("project-bar-connect-off")
        # FILL would stretch this to the 28px bar and turn the circle into an oval.
        self._connect_pip.set_halign(Gtk.Align.CENTER)
        self._connect_pip.set_valign(Gtk.Align.CENTER)
        self._connect_pip.set_size_request(10, 10)
        # Same as the Chat/Files buttons: grabbing focus from the chat
        # surface cancels the click gesture before "clicked" fires.
        self._connect_pip.set_focus_on_click(False)
        self._connect_pip.connect("clicked", self._on_transport_connect_clicked)

        self._stop_all_btn = self._make_bar_icon("process-stop-symbolic", 16)
        self._stop_all_btn.add_css_class("project-bar-stop")
        self._stop_all_btn.set_tooltip_text(_STOP_ALL_TIP)
        self._stop_all_btn.connect("clicked", self._on_transport_stop_clicked)

        tip = _CONNECT_TIPS["disconnected"]
        self._connect_btn.set_tooltip_text(tip)
        self._connect_pip.set_tooltip_text(tip)

        self._transport_cluster.append(self._connect_btn)
        self._transport_cluster.append(self._connect_pip)
        self._transport_cluster.append(self._stop_all_btn)

    @staticmethod
    def _make_bar_icon(icon_name: str, pixel_size: int) -> Gtk.Button:
        button = Gtk.Button()
        button.add_css_class("project-bar-icon")
        button.set_valign(Gtk.Align.CENTER)
        button.set_focus_on_click(False)
        image = Gtk.Image.new_from_icon_name(icon_name)
        image.set_pixel_size(pixel_size)
        # A targetable image is the pick target, and the button then treats
        # the release as outside itself and drops the click.
        image.set_can_target(False)
        button.set_child(image)
        return button

    def _on_transport_connect_clicked(self, *_args) -> None:
        if self._on_connect_clicked is not None:
            self._on_connect_clicked()

    def _on_transport_stop_clicked(self, *_args) -> None:
        if self._on_stop_all_clicked is not None:
            self._on_stop_all_clicked()

    def set_on_connect_clicked(self, cb) -> None:
        self._on_connect_clicked = cb

    def set_on_stop_all_clicked(self, cb) -> None:
        self._on_stop_all_clicked = cb

    def set_telegram_bridge_state(self, state: str) -> None:
        """Paint the connect pip. Unknown states match disconnected."""
        pip_class = {
            "unconfigured": "project-bar-connect-off",
            "disconnected": "project-bar-connect-off",
            "connecting": "project-bar-connect-pending",
            "connected": "project-bar-connect-on",
            "error": "project-bar-connect-error",
        }.get(state, "project-bar-connect-off")
        tip = _CONNECT_TIPS.get(state, _CONNECT_TIPS["disconnected"])
        for css_class in _PIP_CLASSES:
            self._connect_pip.remove_css_class(css_class)
        self._connect_pip.add_css_class(pip_class)
        self._connect_btn.set_tooltip_text(tip)
        self._connect_pip.set_tooltip_text(tip)

    def _detach(self, widget) -> None:
        """Unparent a singleton before the next bar rebuild parents it."""
        if widget.get_parent() is not None:
            widget.unparent()

    def _clear_settings_bar(self) -> None:
        """Remove all children from the settings bar box (sibling-walk safe)."""
        while self._project_settings.get_first_child() is not None:
            self._project_settings.remove(self._project_settings.get_first_child())

    def update_project_settings(self, project_name, member_count,
                                solo_target, auto_accept_level, branch_name):
        """Rebuild the settings bar with the latest per-project state.

        Called by window.py when project opens/closes, members change, solo
        target changes, auto-accept level changes, or branch refresh lands.

        Empty project (project_name falsy) -> the transport cluster, then
        the shared activity pill. Connect and Stop All are app-global, so
        they stay on the bar when there is no Git label.
        """
        if not project_name:
            self._clear_settings_bar()
            self._detach(self._transport_cluster)
            self._project_settings.append(self._transport_cluster)
            self._project_settings.append(self._activity_pill)
            self._project_settings.set_visible(True)
            return

        self._clear_settings_bar()
        self._detach(self._transport_cluster)
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
        info_box.append(self._transport_cluster)

        self._project_settings.append(info_box)
        # Re-append the shared pill — _clear_settings_bar() removed it.
        # (info_box is hexpand+START; the pill lands at the bar's right end.)
        self._project_settings.append(self._activity_pill)

    def activity_pill(self) -> ActivityPillLabel:
        """Read accessor for the SHARED activity pill (window.py's
        ActivityPillAdapter resolver targets this). One widget, one truth."""
        return self._activity_pill

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

    def set_on_agent_cycle(self, callback):
        self._on_agent_cycle = callback

    def set_on_autoaccept_cycle(self, callback):
        self._on_autoaccept_cycle = callback

    def set_on_buffer_changed(self, cb: callable) -> None:
        """Register callback for input buffer 'changed' events. cb(buffer)."""
        self._on_buffer_changed = cb

    def _on_input_buffer_changed(self, buf) -> None:
        """Fire the registered callback (if any). The actual buf.connect
        is in __init__; this is the indirection layer."""
        # Input edit clears any transient Improve status (SPEC-55 §3d).
        status = getattr(self, "_improve_status_label", None)
        if status is not None:
            status.set_visible(False)
        if self._on_buffer_changed is not None:
            self._on_buffer_changed(buf)

    def set_improve_status(self, text: str) -> None:
        """Show/hide the transient Improve status line (SPEC-55 §3d).

        Called by MediaHandler with the improve error string (or "" to
        clear). Empty/None hides it; the next improve click or input edit
        clears it again.
        """
        if text:
            self._improve_status_label.set_text(text)
            self._improve_status_label.set_visible(True)
        else:
            self._improve_status_label.set_visible(False)

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

    # ── Tab management ──────────────────────────────────────────────────────

    def create_chat_tab(self, session_key, agent_name):
        """
        Create a new chat tab for an agent.
        Returns the page index of the new tab, or existing tab index if already exists.
        """
        # The Welcome splash is a one-shot special tab — once closed it never
        # respawns for this session (project switches / new tabs don't revive it).
        if (
            isinstance(session_key, str)
            and session_key.startswith("welcome:")
            and self._welcome_closed
        ):
            return None
        # Check if tab already exists for this session_key
        for page_idx, sk in self._tab_sessions.items():
            if sk == session_key:
                self._chat_notebook.set_current_page(page_idx)
                return page_idx

        # The notebook page IS the chat box itself — no Gtk.Overlay, no
        # page-level ScrolledWindow (SPEC-58 §2a). The HTML surface owns its
        # document scroll; the project bar is a permanent _top_box row (it
        # never overlays tabs). The box EXPANDS (hexpand+vexpand) so the
        # mounted surface fills the pane.
        chat_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        chat_box.set_halign(Gtk.Align.FILL)
        chat_box.set_hexpand(True)
        chat_box.set_vexpand(True)

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

        # Special tabs (e.g. the one-shot Welcome splash) carry no agent-name /
        # session suffix — the label reads just the tab title.
        bare_label = isinstance(session_key, str) and session_key.startswith("welcome:")

        close_btn = Gtk.Button(label="×")
        close_btn.set_valign(Gtk.Align.CENTER)
        close_btn.set_has_frame(False)
        close_btn.set_focus_on_click(False)
        close_btn.set_size_request(20, -1)
        close_btn.set_hexpand(False)

        tab_label_box.append(tab_dot)
        tab_label_box.append(tab_name)
        if not bare_label:
            tab_label_box.append(tab_sep)
            tab_label_box.append(tab_session)
        tab_label_box.append(close_btn)

        # Append the new tab FIRST to get page_idx, then wire handlers
        page_idx = self._chat_notebook.append_page(chat_box, tab_label_box)

        # Store session_key on the tab label box so close handlers can look up
        # the CURRENT page index dynamically (avoids stale captured page_idx bug
        # when tabs are closed out of order and reindexing shifts pages).
        tab_label_box._session_key = session_key

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
        self._chat_notebook.set_show_tabs(True)
        self._chat_notebook.set_current_page(page_idx)
        # SPEC-06 SP5c-1 — the welcome is now the FIRST HTML-native surface
        # content: emitted through the render handler (SP5a ruling (a) —
        # handler owns mounting/emission), one row into the session's
        # surface, once per surface mount. Chosen reopen semantics (brief
        # constraint 4): re-emit IDEMPOTENTLY on a genuinely fresh surface
        # — v1 parity (the Pango bubble was part of tab creation); the
        # handler's flag mirrors surface lifetime (close clears it; a
        # live surface's reopen re-entry is suppressed by the flag, and
        # the early-return above stops duplicate tabs before this point).
        # The tombstone pop MUST precede emission (SP5a r3 FIX 3, moved
        # here): pop runs first, so the reopened key is un-tombstoned when
        # render_welcome checks it — otherwise the reopen welcome would
        # drop onto a blank surface. getattr-tolerant for test doubles.
        handler = self._chat_render_handler
        pop_box = getattr(handler, "pop_tombstones_for_box", None) if handler is not None else None
        if callable(pop_box):
            pop_box(session_key)
        if (
            handler is not None
            and hasattr(handler, "render_welcome")
            and not (
                isinstance(session_key, str)
                and (
                    session_key.startswith("canvas:")
                    or session_key.startswith("welcome:")
                )
            )
        ):
            handler.render_welcome(session_key)
        # Display-only project-chat placeholder: a project tab with no
        # conversation content shows the minimal splash page instead of the
        # welcome row — the same raw full-document load as the one-shot
        # Welcome tab (create_welcome_tab). Fires only for project keys; the
        # app-startup splash path (welcome:…) is untouched. The page's
        # ready()/splash:done JS is left unwired — display only, no behavior.
        if isinstance(session_key, str) and session_key.startswith("project:"):
            surface = None
            mount = getattr(handler, "surface_for_mount", None)
            if callable(mount):
                surface = mount(session_key)
            loader = getattr(surface, "load_unsanitized_full_document", None)
            rows = getattr(surface, "_rows", ())
            has_conversation = any(
                'class="welcome-row"' not in (row.get("html") or "")
                for row in rows
            )
            if callable(loader) and not has_conversation:
                splash_path = os.path.join(
                    get_project_root(), "assets", "splash-min.html")
                try:
                    with open(splash_path, "r", encoding="utf-8") as fh:
                        loader(fh.read())
                except OSError:
                    _logger.exception(
                        "project chat: could not read %s; keeping welcome placeholder",
                        splash_path,
                    )
        return page_idx

    def create_welcome_tab(self):
        """Create the one-shot Welcome splash tab (fresh launch only).

        A special ``welcome:`` session key (the ``canvas:`` prefix is the
        precedent). Content is ``assets/splash.html`` loaded as a RAW full
        HTML document through the surface's WebView load path (executes
        inline <script>) — NOT the sanitized welcome/markdown emitter.
        Returns the tab's page index, or None when already closed this
        session (it never respawns).
        """
        key = "welcome:splash"
        idx = self.create_chat_tab(key, "Welcome")
        if idx is None:
            return None
        # One-load guard (BUG #5): the raw load was already issued once this
        # session — a second call must not re-issue it (that would re-render
        # over the live splash). The tab is still found/activated above.
        if self._welcome_loaded:
            return idx
        surface = None
        handler = self._chat_render_handler
        if handler is not None:
            mount = getattr(handler, "surface_for_mount", None)
            if callable(mount):
                surface = mount(key)
        loader = getattr(surface, "load_unsanitized_full_document", None)
        if not callable(loader):
            _logger.warning("welcome: surface cannot raw-load a full document")
            return idx
        splash_path = os.path.join(get_project_root(), "assets", "splash.html")
        try:
            with open(splash_path, "r", encoding="utf-8") as fh:
                html = fh.read()
        except OSError:
            # BUG #3: never leave the Welcome tab permanently blank — fall
            # back to a tiny built-in inline document (centered heading + note).
            _logger.exception("welcome: could not read %s; using built-in fallback", splash_path)
            html = _WELCOME_FALLBACK_HTML
        loader(html)
        self._welcome_loaded = True
        return idx

    def _on_notebook_switch_page(self, notebook, _page, page_num):
        """Handle tab switch: scroll to bottom, notify session-change listeners."""
        self.scroll_chat_to_bottom(page_num)
        # Notify listeners of the session change.
        new_sk = self._tab_sessions.get(page_num)
        if new_sk and self._on_session_changed:
            self._on_session_changed(new_sk)

    # ── Unread dot management ──────────────────────────────────────────────

    @staticmethod
    def _extract_session_display_name(session_key: str) -> str:
        """Parse a session key like 'agent:qaster:telegram:direct:7478874934'
        and return a human-readable channel name like 'Telegram'."""
        if isinstance(session_key, str) and session_key.startswith("canvas:"):
            return "Canvas"
        if isinstance(session_key, str) and session_key.startswith("welcome:"):
            return "Welcome"
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
        # Once the one-shot Welcome tab is closed it stays closed for the
        # session — flag it so a later create_chat_tab cannot respawn it.
        if isinstance(session_key, str) and session_key.startswith("welcome:"):
            self._welcome_closed = True
        # SP5a-audit FIX (BUG #2): capture the tab's BOX before the pops too.
        # close_session resolves the dying key's box via the injected getter
        # (get_chat_box_for_session iterates _tab_sessions), which returns None
        # once the key's entry is popped — so passing the key alone made the
        # entire close fan-out dead. Handing the BOX ITSELF to close_session
        # lets it resolve the fan-out by BOX IDENTITY (the widget actually
        # being closed) instead of a session-key lookup. All entry points
        # (direct close, close_project_tab, _bulk_closing) resolve here —
        # they all funnel through _close_tab.
        closing_box = self._tab_chat_boxes.get(page_idx)
        self._chat_notebook.remove_page(page_idx)
        self._tab_sessions.pop(page_idx, None)
        self._tab_chat_boxes.pop(page_idx, None)
        # SPEC-06 SP5a (R2): release the session's chat surface — the SP3
        # destroy contract (pending-render cancel + guards) handles the rest.
        # Fires even during bulk close (stop-all spirit). Project tabs go
        # through here too: close_project_tab resolves to _close_tab, so the
        # wiring is single-point (no double-close).
        if session_key is not None and self._chat_render_handler is not None:
            self._chat_render_handler.close_session(
                session_key, box=closing_box
            )
        # Hide tab bar when empty to prevent GtkGizmo min-height < 0 warning.
        # The project bar never leaves _top_box — it is a permanent row
        # (SPEC-58 §2a), so no detach/prepend is needed here.
        if self._chat_notebook.get_n_pages() == 0:
            self._chat_notebook.set_show_tabs(False)
            self._project_settings.set_visible(True)
        if not self._bulk_closing:
            self._reindex_tabs()

    def _reindex_tabs(self):
        """
        Rebuild _tab_sessions and _tab_chat_boxes to reflect current notebook page order.

        Correct algorithm: iterate current GTK pages (which are the authoritative order),
        look up each page's widget (the chat box itself — the notebook page IS the
        box, SPEC-58 §2a), then find the matching session_key by scanning the saved
        snapshot. Keys the identity map on saved_chat_boxes (NOT an overlay/scroll
        map — those are gone).
        """
        # Snapshot current state before rebuilding
        saved_sessions = dict(self._tab_sessions)
        saved_chat_boxes = dict(self._tab_chat_boxes)

        # Build chat-box widget -> old_page_idx map from snapshot
        box_to_idx = {}
        for old_idx, box in saved_chat_boxes.items():
            box_to_idx[box] = old_idx

        new_sessions = {}
        new_chat_boxes = {}
        n_pages = self._chat_notebook.get_n_pages()
        for new_idx in range(n_pages):
            page_widget = self._chat_notebook.get_nth_page(new_idx)  # chat box
            old_idx = box_to_idx.get(page_widget)
            if old_idx is not None:
                new_sessions[new_idx] = saved_sessions[old_idx]
                new_chat_boxes[new_idx] = saved_chat_boxes[old_idx]

        self._tab_sessions = new_sessions
        self._tab_chat_boxes = new_chat_boxes

    def close_tabs(self, page_indices):
        """
        Close multiple tabs in one call, reindexing only once at the end.

        FIX 5 (SP5a round 2): delegates to _close_tab per index instead of
        replicating its dict-pops — the single-point wiring now ALSO fires
        close_session(sk) per tab, so bulk close (stop-all / workspace
        close) DRAINS the handler's per-session surfaces instead of leaking
        them (round 1 left _surfaces populated after bulk close). The
        _bulk_closing guard still suppresses per-tab reindexing; highest-
        index-first keeps lower indices stable during the loop.

        Args:
            page_indices: iterable of int page indices to close
        """
        if not page_indices:
            return
        self._bulk_closing = True
        try:
            for idx in sorted(page_indices, reverse=True):
                self._close_tab(idx)
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

    def _on_project_solo_selected(self, session_key, project_name, target_sk):
        """
        Handle project tab solo-DM target selection.
        target_sk = session_key of selected member, or None = All (group broadcast).
        """
        self._project_handler.set_solo_target(project_name, target_sk)

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
        """Scroll the active chat document to the bottom.

        SPEC-17 SP1.3: an HTML chat surface scrolls its DOCUMENT (the page
        scrolls inside the web view — there is no outer ScrolledWindow
        anymore, SPEC-58 §2a). Resolve the page's chat box, ask the render
        handler for the surface, and delegate to
        `surface.scroll_to_latest()`; nothing else to do.
        """
        if page_index is None:
            page_index = self._chat_notebook.get_current_page()
        chat_box = self._tab_chat_boxes.get(page_index)
        crh = self._chat_render_handler
        if chat_box is not None and crh is not None:
            surface = crh.surface_for_box(chat_box)
            if surface is not None:
                surface.scroll_to_latest()
                return

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


