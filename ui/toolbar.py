# ui/toolbar.py
# Top toolbar — horizontal bar across the top of the window

import gi

gi.require_version('Gtk', '4.0')
from gi.repository import Gtk


class Toolbar(Gtk.Box):
    """
    Top toolbar widget.
    A horizontal bar that will contain app-level actions.
    Layout: [Stream toggle | ⚙ Settings]  ←—expanding spacer—→  [status label | Connect button]
    Extends Gtk.Box with horizontal orientation.
    """

    def __init__(self, on_connect_clicked=None, *, on_settings_clicked=None, on_stop_all_clicked=None):
        # Initialize as a horizontal box — children lay out left to right
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        # Fixed height of 40 pixels, width stretches to fill window (-1 = stretch)
        self.set_size_request(-1, 40)

        # Store the connect button callback
        self._on_connect_clicked = on_connect_clicked
        self._on_settings_clicked = on_settings_clicked
        # SPEC-09 SP3: stop-all — wired by window.py to the ARH aggregation
        # (lazy resolve via closure: the handler is built after the toolbar).
        self._on_stop_all_clicked = on_stop_all_clicked

        # Spacer — expands to push everything after it to the right
        spacer = Gtk.Box()
        spacer.set_hexpand(True)

        # Right-aligned box containing toolbar buttons
        right_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)

        # Connection status label
        self._status_label = Gtk.Label()
        self._status_label.set_halign(Gtk.Align.END)
        self._status_label.set_valign(Gtk.Align.CENTER)
        self._status_label.set_margin_end(8)
        self._status_label.set_markup(
            '<span foreground="#6b6b7a" font_desc="Sans 10">● No transport</span>')

        # Connect button
        self._connect_btn = Gtk.Button(label="Connect")
        self._connect_btn.add_css_class("suggested-action")
        self._connect_btn.set_size_request(90, -1)
        self._connect_btn.set_tooltip_text(
            "Toggle remote transport (none configured — Telegram arrives post-MVP)")
        self._connect_btn.connect("clicked", self._on_connect_click)

        # Settings button + red status dot
        self._settings_btn = Gtk.Button(label="⚙ Settings")
        self._settings_btn.add_css_class("settings-toolbar-btn")
        self._settings_btn.set_size_request(110, -1)
        self._settings_btn.connect("clicked", self._on_settings_click)

        # Wrap settings button in an overlay to show a red dot
        overlay = Gtk.Overlay()
        overlay.set_child(self._settings_btn)
        self._status_dot = Gtk.Label(label="●")
        self._status_dot.add_css_class("toolbar-status-dot")
        self._status_dot.set_halign(Gtk.Align.END)
        self._status_dot.set_valign(Gtk.Align.START)
        self._status_dot.set_visible(False)  # hidden until needed
        overlay.add_overlay(self._status_dot)

        # Left-aligned box: Settings (the Stream toggle was removed 2026-10-07 —
        # its flag's only reader was the retired remote-event path)
        left_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        left_box.append(overlay)

        # Right-aligned box: status + Connect
        right_box.set_spacing(6)
        right_box.append(self._status_label)
        right_box.append(self._connect_btn)

        # SPEC-09 SP3: ■ Stop All — destructive, halts every agent (turns +
        # process groups + in-flight review checkpoints). Callback is the
        # window's confirm+dispatch, wired via on_stop_all_clicked.
        self._stop_all_btn = Gtk.Button(label="■ Stop All")
        self._stop_all_btn.add_css_class("destructive-action")
        self._stop_all_btn.set_size_request(100, -1)
        self._stop_all_btn.set_tooltip_text(
            "Halt every agent: cancel in-flight turns, kill spawned process "
            "groups, abort pending review checkpoints.")
        self._stop_all_btn.connect("clicked", self._on_stop_all_click)
        right_box.append(self._stop_all_btn)

        # Assemble: left cluster | spacer | right content
        self.append(left_box)
        self.append(spacer)
        self.append(right_box)

    def _on_connect_click(self, *args):
        """Called when Connect button is clicked. Delegates to window's callback.

        SPEC-15 SP2: the window now drives the button label + status markup
        from the bridge's state signals (set_telegram_bridge_state). The old
        unconditional post-click label overwrite was removed — it clobbered
        the 'connecting' state the callback had just set.
        """
        if self._on_connect_clicked is not None:
            self._on_connect_clicked()

    def _on_settings_click(self, *args):
        """Called when ⚙ Settings button is clicked. Delegates to window's callback."""
        if self._on_settings_clicked is not None:
            self._on_settings_clicked()

    def _on_stop_all_click(self, *args):
        """Called when ■ Stop All is clicked. Delegates to the window's
        confirm-and-dispatch callback (window owns the dialog + ARH resolve)."""
        if self._on_stop_all_clicked is not None:
            self._on_stop_all_clicked()

    def set_settings_status(self, has_verified_provider: bool) -> None:
        """Show/hide the red dot. Window calls this on startup and after providers change."""
        self._status_dot.set_visible(not has_verified_provider)

    def set_telegram_bridge_state(self, state: str) -> None:
        """SPEC-15 Telegram bridge states (distinct from the legacy generic
        transport states, which predate the bridge).

        state: "unconfigured" | "disconnected" | "connecting" | "connected"
               | "error"
        """
        if state == "unconfigured":
            self._connect_btn.set_label("Connect")
            self._connect_btn.remove_css_class("destructive-action")
            self._connect_btn.add_css_class("suggested-action")
            self._status_label.set_markup(
                '<span foreground="#6b6b7a" font_desc="Sans 10">'
                '● No transport (configure in Settings)</span>')
        elif state == "connecting":
            self._connect_btn.set_label("Connecting…")
            self._status_label.set_markup(
                '<span foreground="#f59e0b" font_desc="Sans 10">'
                '● Connecting…</span>')
        elif state == "connected":
            self._connect_btn.set_label("Disconnect")
            self._connect_btn.remove_css_class("suggested-action")
            self._connect_btn.add_css_class("destructive-action")
            self._status_label.set_markup(
                '<span foreground="#22c55e" font_desc="Sans 10">'
                '● Telegram bridge</span>')
        elif state == "error":
            self._connect_btn.set_label("Connect")
            self._connect_btn.remove_css_class("destructive-action")
            self._connect_btn.add_css_class("suggested-action")
            self._status_label.set_markup(
                '<span foreground="#ef4444" font_desc="Sans 10">'
                '● Offline</span>')
        else:  # "disconnected"
            self._connect_btn.set_label("Connect")
            self._connect_btn.remove_css_class("destructive-action")
            self._connect_btn.add_css_class("suggested-action")
            self._status_label.set_markup(
                '<span foreground="#6b6b7a" font_desc="Sans 10">'
                '● Telegram bridge off</span>')

    # ── State update methods ─────────────────────────────────────────────────

    def update_connection_state(self, state):
        """
        Update button label and status label based on connection state.
        state: "disconnected" | "connecting" | "connected" | "offline"
        """
        if state == "connecting":
            self._connect_btn.set_label("Connecting…")
            self._status_label.set_markup(
                '<span foreground="#f59e0b" font_desc="Sans 10">● Connecting</span>')
        elif state == "connected":
            self._connect_btn.set_label("Disconnect")
            self._connect_btn.remove_css_class("suggested-action")
            self._connect_btn.add_css_class("destructive-action")
            self._status_label.set_markup(
                '<span foreground="#22c55e" font_desc="Sans 10">● Connected</span>')
        elif state == "offline":
            self._connect_btn.set_label("Connect")
            self._connect_btn.remove_css_class("destructive-action")
            self._connect_btn.add_css_class("suggested-action")
            self._status_label.set_markup(
                '<span foreground="#8b8ba0" font_desc="Sans 10">● Offline — local agents available</span>')
        elif state == "disconnected":
            self._connect_btn.set_label("Connect")
            self._connect_btn.remove_css_class("destructive-action")
            self._connect_btn.add_css_class("suggested-action")
            self._status_label.set_markup(
                '<span foreground="#6b6b7a" font_desc="Sans 10">● Not connected</span>')
