# tests/test_activity_pill_label.py — UI-PILLBAR Phase 1 (Edit 2 + Edit 5).
#
# ActivityPillLabel is the shared status pill hosted at the RIGHT END of the
# project feed bar (PM relocation 2026-10-02; SPEC-07 SP1 had placed the pill
# per-chat-surface). The 7-state→CSS map moved VERBATIM from chat_surface —
# the map pin below is the migration guard: Phase 2 deletes the chat_surface
# copy, after which this pin is the sole source of truth for the map shape.
#
# Two test layers, mirroring the established patterns:
#   - Recorder-label tests drive the REAL set_activity_status logic bare
#     (ActivityPillLabel.__new__ + method shadowing — the FakeLabel pattern
#     from test_activity_pill_adapter.py; widget construction segfaults
#     without a display, attribute setting does not).
#   - Construction tests build the real widget — REQUIRE a display (xvfb
#     battery). Gated by DISPLAY skipif (audit BUG#1/#2, 2026-10-02): an
#     ungated bare run SEGFAULTS the whole pytest process, so the gate is
#     load-bearing, not decorative. Recorder tests stay bare-safe.

import os

import pytest

gi = pytest.importorskip("gi")
pytest.importorskip("gi.repository.Gtk")

from ui.views.activity_pill import _ACTIVITY_STATE_TO_CSS, ActivityPillLabel

# ── Required coverage: the 7-key map, moved VERBATIM ───────────────────────


def test_state_map_moved_verbatim():
    """The exact 7-key map (idle/sending/reasoning/streaming/tool_use/done/
    error → pill vocabulary, sending≡reasoning→pill-thinking). Dict EQUALITY,
    not subset: a dropped or renamed key goes red here."""
    assert _ACTIVITY_STATE_TO_CSS == {
        "idle": "pill-idle",
        "sending": "pill-thinking",
        "reasoning": "pill-thinking",
        "streaming": "pill-streaming",
        "tool_use": "pill-tool",
        "done": "pill-done",
        "error": "pill-error",
    }
    assert len(_ACTIVITY_STATE_TO_CSS) == 7


# ── Recorder-label harness (bare-safe — drives the REAL method logic) ─────


class RecorderLabel:
    """Bare-safe stand-in for the label's own GTK methods.

    Shadowed onto an ActivityPillLabel created via __new__ (no widget
    construction). Records the exact op sequence so tests assert the SAME
    remove→add→stash dance the per-surface pill performed (not just end
    state) — copied from test_activity_pill_adapter.FakeLabel.
    """

    def __init__(self):
        self.ops: list[tuple[str, str]] = []

    def set_text(self, text):
        self.ops.append(("set_text", text))

    def remove_css_class(self, name):
        self.ops.append(("remove", name))

    def add_css_class(self, name):
        self.ops.append(("add", name))


def _bare_pill() -> tuple[ActivityPillLabel, RecorderLabel]:
    """An ActivityPillLabel with GTK widget calls shadowed by a recorder.

    __new__ skips the constructor (no widget build); the recorder shadows
    set_text/remove_css_class/add_css_class on the instance so the real
    set_activity_status body runs against recorded calls. _pill_css starts
    at the constructor's documented initial value ('pill-idle').
    """
    pill = ActivityPillLabel.__new__(ActivityPillLabel)
    recorder = RecorderLabel()
    pill.set_text = recorder.set_text
    pill.remove_css_class = recorder.remove_css_class
    pill.add_css_class = recorder.add_css_class
    pill._pill_css = "pill-idle"
    return pill, recorder


class TestSetActivityStatus:
    def test_known_state_swaps_class_in_order(self):
        """Known state → set_text, then remove old, add new, stash. The op
        ORDER is pinned: remove BEFORE add (GTK requires it for a clean
        single-class swap)."""
        pill, rec = _bare_pill()
        pill.set_activity_status("Thinking…", "reasoning")
        assert rec.ops == [
            ("set_text", "Thinking…"),
            ("remove", "pill-idle"),
            ("add", "pill-thinking"),
        ]
        assert pill._pill_css == "pill-thinking"

    def test_same_state_swaps_nothing(self):
        """Text always lands; a no-op class change (same class) must NOT
        emit remove/add — the guard is `new_css != self._pill_css`."""
        pill, rec = _bare_pill()
        pill.set_activity_status("Still going", "reasoning")
        pill.set_activity_status("Still going 2", "reasoning")
        assert rec.ops == [
            ("set_text", "Still going"),
            ("remove", "pill-idle"),
            ("add", "pill-thinking"),
            ("set_text", "Still going 2"),
        ]
        assert pill._pill_css == "pill-thinking"

    def test_unknown_state_keeps_class(self):
        """Unknown state → text lands, class KEPT (fail-quiet, not crash).
        Mirrors chat_surface's contract verbatim — the adapter can forward
        states this map does not know."""
        pill, rec = _bare_pill()
        pill.set_activity_status("Doing something odd", "martian_state")
        assert rec.ops == [("set_text", "Doing something odd")]
        assert pill._pill_css == "pill-idle"

    def test_none_state_keeps_class(self):
        """state=None → text lands, class untouched (the ActivityPillAdapter
        forwards None when the handler supplies no state)."""
        pill, rec = _bare_pill()
        pill.set_activity_status("Plain text")
        assert rec.ops == [("set_text", "Plain text")]
        assert pill._pill_css == "pill-idle"

    def test_text_without_state_after_swap_keeps_swapped_class(self):
        """A stateless update must not fall back to pill-idle — the class
        rides the LAST known state (the per-surface pill's behavior)."""
        pill, rec = _bare_pill()
        pill.set_activity_status("Working", "tool_use")
        pill.set_activity_status("Working more")
        assert pill._pill_css == "pill-tool"
        assert rec.ops[-1] == ("set_text", "Working more")


# ── Construction (real widget — runs under the xvfb battery) ──────────────


class TestConstruction:
    # Audit BUG#1: real widget construction needs a display — skip (don't
    # segfault the whole pytest process) when running bare.
    @pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="Gtk.Label construction requires a display (xvfb-run)")
    def test_idle_defaults(self):
        """Fresh pill: 'Idle' text, pill-idle class, right-aligned, 8px end
        margin (the bar's right-end hosting contract)."""
        pill = ActivityPillLabel()
        assert pill.get_text() == "Idle"
        assert "pill-idle" in pill.get_css_classes()
        assert pill.get_halign() == gi.repository.Gtk.Align.END
        assert pill.get_margin_end() == 8

    @pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="Gtk.Label construction requires a display (xvfb-run)")
    def test_is_gtk_label(self):
        """ActivityPillLabel IS a Gtk.Label — the .pill-* APP_CSS rules
        (ui/styles.py) apply to any Gtk.Label with the class; the subclass
        must not break that."""
        assert isinstance(ActivityPillLabel(), gi.repository.Gtk.Label)


# ── UI-PILLBAR P2: the retirement contract ────────────────────────────────


def test_bar_pill_is_sole_pill():
    """P2 retirement contract: the per-surface pill is GONE from both chat
    surface classes — the shared bar pill (ActivityPillLabel in this module)
    is the ONLY pill. A regression re-adding set_activity_pill or
    set_activity_status to either class fails here. Bare-safe: hasattr on
    class objects constructs no widgets.

    NOTE: ChatSurface here is the module-level binding — on a WebKit-less
    box the import-time alias makes it TextViewFallback, collapsing the two
    assertions to one class. Under xvfb (the mandated battery) both real
    classes are pinned."""
    from ui.views import chat_surface

    for cls in (chat_surface.ChatSurface, chat_surface.TextViewFallback):
        assert not hasattr(cls, "set_activity_pill"), (
            f"{cls.__name__} re-acquired set_activity_pill — the per-surface "
            "pill regression the P2 retirement forbids"
        )
        assert not hasattr(cls, "set_activity_status"), (
            f"{cls.__name__} re-acquired set_activity_status — pill logic "
            "belongs in ActivityPillLabel (ui/views/activity_pill.py)"
        )
