# ui/toolbar.py
# Top toolbar — horizontal bar across the top of the window

import gi
# Require GTK 4.0 — must be called before importing Gtk
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk

import ui.constants

class Toolbar(Gtk.Box):
    """
    Top toolbar widget.
    A horizontal bar that will contain app-level actions.
    Layout: [Stream toggle | ⚙ Settings]  ←—expanding spacer—→  [status label | Connect button]
    Extends Gtk.Box with horizontal orientation.
    """

    def __init__(self, on_connect_clicked=None, *, on_settings_clicked=None):
        # Initialize as a horizontal box — children lay out left to right
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        # Fixed height of 40 pixels, width stretches to fill window (-1 = stretch)
        self.set_size_request(-1, 40)

        # Store the connect button callback
        self._on_connect_clicked = on_connect_clicked
        self._on_settings_clicked = on_settings_clicked

        # Stream toggle — left side of toolbar
        self._stream_btn = Gtk.ToggleButton(label="Stream: OFF")
        self._stream_btn.set_size_request(100, -1)
        self._stream_btn.set_active(ui.constants.STREAMING_ENABLED)
        self._update_stream_label()
        self._stream_btn.connect("toggled", self._on_stream_toggled)

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

        # Left-aligned box: Stream + Settings
        left_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        left_box.append(self._stream_btn)
        left_box.append(overlay)

        # Right-aligned box: status + Connect
        right_box.set_spacing(6)
        right_box.append(self._status_label)
        right_box.append(self._connect_btn)

        # Assemble: left cluster | spacer | right content
        self.append(left_box)
        self.append(spacer)
        self.append(right_box)

    def _on_connect_click(self, *args):
        """Called when Connect button is clicked. Delegates to window's callback.

        No transport is configured in MVP (SPEC-05 R1 strip; Telegram arrives
        post-MVP) — surface the honest state instead of toggling.
        """
        if self._on_connect_clicked is not None:
            self._on_connect_clicked()
        self._status_label.set_markup(
            '<span foreground="#6b6b7a" font_desc="Sans 10">● No transport</span>')

    def _on_stream_toggled(self, button):
        """Toggle streaming on/off and update the button label."""
        ui.constants.STREAMING_ENABLED = button.get_active()
        self._update_stream_label()

    def _update_stream_label(self):
        """Update stream button label to reflect current state."""
        self._stream_btn.set_label(
            "Stream: ON" if ui.constants.STREAMING_ENABLED else "Stream: OFF"
        )

    def _on_settings_click(self, *args):
        """Called when ⚙ Settings button is clicked. Delegates to window's callback."""
        if self._on_settings_clicked is not None:
            self._on_settings_clicked()

    def set_settings_status(self, has_verified_provider: bool) -> None:
        """Show/hide the red dot. Window calls this on startup and after providers change."""
        self._status_dot.set_visible(not has_verified_provider)

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
