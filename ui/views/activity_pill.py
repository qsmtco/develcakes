# ui/views/activity_pill.py — the shared activity-status pill + its adapter
# (UI-PILLBAR Phase 1 Edit 2; adapter relocated here in Phase 2).
#
# SPEC-07 SP1 placed the status pill PER-CHAT-SURFACE (a private Gtk.Label in
# ChatSurface/TextViewFallback, resolved per tab by ActivityPillAdapter's
# surface resolver). PM relocation 2026-10-02: the pill moves to a single
# shared widget hosted at the RIGHT END of the project feed bar
# (main_content.py) — one pill, visible on every tab including project-less
# ones. Phase 2 retires the per-surface pill; this module is now the pill's
# ONLY home (logic + styling contract), and ActivityPillAdapter — the
# duck-type between ActivityHandler and the pill — moved alongside it
# (window.py imports it from here). chat_surface.py keeps NO pill code.
#
# Styling contract: the pill is a Gtk.Label and the .pill-* rules live in
# APP_CSS (ui/styles.py, GTK side) — they apply to ANY Gtk.Label carrying the
# class, so the subclass needs no CSS of its own (SPEC-07 SP1 fix round).

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

# SPEC-07 SP1: activity-machine state → pill CSS class (6 handler states +
# error → the 4-class pill vocabulary, extended with streaming/done).
# MOVED VERBATIM from ui/views/chat_surface.py in Phase 1 (Phase 2 deleted
# that copy — this is the single source of truth; the map pin lives in
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


class ActivityPillAdapter:
    """SPEC-07 SP1: old status-bar duck-type → the activity pill.

    RELOCATED VERBATIM from ui/views/chat_surface.py in UI-PILLBAR Phase 2
    (the per-surface pill retired; this adapter is the seam between
    ActivityHandler and the pill — its only home is now next to the pill
    itself). window.py imports it from here.

    The ActivityHandler calls five methods today (verified at HEAD 3972ac9e:
    activity_handler.py:683-871): set_status_text, set_progress_fraction,
    set_progress_hidden, set_progress_pulse, pulse_progress. The pill has no
    progress element, so the progress quartet are documented no-ops.
    set_status_text carries (text, state); the adapter re-applies the last
    status when the resolver returns a NEW target (identity-change catch-up —
    inert since Phase 1, where the resolver returns the one stable bar pill,
    but kept: it is the duck-type the handler speaks and the tests pin it).

    The resolver is our own lambda in window.py (SP2 wiring) and MUST NOT
    throw — exceptions propagate deliberately (no catch-and-None: a swallowed
    resolver bug would silently strand the pill on stale status).
    """

    def __init__(self, resolver) -> None:
        self._resolver = resolver
        self._last_surface = None
        self._last_text: str | None = None
        self._last_state: str | None = None

    def set_status_text(self, text: str, state: str | None = None) -> None:
        """Status → the resolved target's set_activity_status, with catch-up
        on identity change. None target → cache-and-noop (the pill catches
        up when a target appears)."""
        surface = self._resolver()
        if surface is None:
            self._last_text = text
            self._last_state = None if state is None else state
            self._last_surface = None
            return
        if surface is not self._last_surface and self._last_text is not None:
            # Fresh target starts "Idle" — apply the CACHED status first
            # (catch-up), then the new one. Net effect: it shows the new
            # status.
            surface.set_activity_status(self._last_text, self._last_state)
        surface.set_activity_status(text, state)
        self._last_surface = surface
        self._last_text = text
        self._last_state = None if state is None else state

    def set_progress_fraction(self, f: float) -> None:
        # no-op: pill has no progress element (SPEC-07 §2 AMENDED)
        pass

    def set_progress_hidden(self, b: bool) -> None:
        # no-op: pill has no progress element (SPEC-07 §2 AMENDED)
        pass

    def set_progress_pulse(self, e: bool) -> None:
        # no-op: pill has no progress element (SPEC-07 §2 AMENDED)
        pass

    def pulse_progress(self) -> None:
        # no-op: pill has no progress element (SPEC-07 §2 AMENDED)
        pass
