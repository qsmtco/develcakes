# ui/views/activity_pill.py — the shared activity-status pill (UI-PILLBAR
# Phase 1, Edit 2).
#
# SPEC-07 SP1 placed the status pill PER-CHAT-SURFACE (a private Gtk.Label in
# ChatSurface/TextViewFallback, resolved per tab by ActivityPillAdapter's
# surface resolver). PM relocation 2026-10-02: the pill moves to a single
# shared widget hosted at the RIGHT END of the project feed bar
# (main_content.py) — one pill, visible on every tab including project-less
# ones. This module gives the relocated pill a real home instead of leaving
# its logic inside chat_surface; Phase 2 deletes chat_surface's copy (the
# per-surface label + set_activity_status bodies) and repoints tests here.
#
# Styling contract: the pill is a Gtk.Label and the .pill-* rules live in
# APP_CSS (ui/styles.py, GTK side) — they apply to ANY Gtk.Label carrying the
# class, so the subclass needs no CSS of its own (SPEC-07 SP1 fix round).

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

# SPEC-07 SP1: activity-machine state → pill CSS class (6 handler states +
# error → the 4-class pill vocabulary, extended with streaming/done).
# MOVED VERBATIM from ui/views/chat_surface.py (Phase 2 deletes that copy —
# this is now the single source of truth; the map pin lives in
# tests/test_activity_pill_label.py).
_ACTIVITY_STATE_TO_CSS = {
    "idle": "pill-idle",
    "sending": "pill-thinking",
    "reasoning": "pill-thinking",
    "streaming": "pill-streaming",
    "tool_use": "pill-tool",
    "done": "pill-done",
    "error": "pill-error",
}


class ActivityPillLabel(Gtk.Label):
    """Status pill for the project bar — text + state-driven CSS class.

    Owns the 7-state→class map (moved verbatim from chat_surface) and the
    swap dance (remove old, add new, stash). set_activity_status(text, state)
    is the ActivityPillAdapter contract; state None/unknown keeps the class.
    """

    def __init__(self) -> None:
        super().__init__(label="Idle")
        self._pill_css = "pill-idle"
        self.add_css_class(self._pill_css)
        # Right end of the project bar: info_box is hexpand+START, so this
        # label lands at the bar's right edge with a small inset.
        self.set_halign(Gtk.Align.END)
        self.set_margin_end(8)

    def set_activity_status(self, text: str, state: str | None = None) -> None:
        """Activity-machine status → pill text + CSS class.

        Contract moved verbatim from chat_surface.set_activity_status:
        text always lands on the label (plain text — callers must NOT send
        Pango markup); state (one of the ActivityHandler 6 + error) drives
        the CSS class; None keeps the current class. Unknown state → keep
        current class (fail-quiet, not crash).
        """
        self.set_text(text)
        if state is not None and state in _ACTIVITY_STATE_TO_CSS:
            new_css = _ACTIVITY_STATE_TO_CSS[state]
            if new_css != self._pill_css:
                self.remove_css_class(self._pill_css)
                self.add_css_class(new_css)
                self._pill_css = new_css
