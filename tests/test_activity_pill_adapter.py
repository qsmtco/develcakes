# tests/test_activity_pill_adapter.py — SPEC-07 SP1 (R4 feedbar removal).
#
# This file runs BARE — no display, no xvfb (proven: env -u DISPLAY green).
# Every surface is a pure fake; test 7 drives the real set_activity_status
# via TextViewFallback.__new__ (skips __init__, so NO widget is built —
# the segfault class is widget instantiation, not the gi namespace import);
# test 8's ChatRenderHandler imports gi but only calls pure-Python code
# (render_document/markdown → nh3 — no GTK calls on this path).
#
# Red-first protocol (steelFramedCodeWriter Step 3 + SP1 brief Edit 5): tests
# landed BEFORE implementation; the red run is pasted in the build report.
# Fix round (SPEC-07 SP1 audit): pill rules live in APP_CSS (ui/styles.py) —
# the pill label is a GTK widget, not part of the webview document.

import inspect
import re

from ui.handlers.chat_render_handler import ChatRenderHandler
from ui.styles import APP_CSS
from ui.views.chat_surface import (
    _ACTIVITY_STATE_TO_CSS,
    ActivityPillAdapter,
    TextViewFallback,
)


class FakeSurface:
    """Record set_activity_status calls — pure fake, no gi.

    set_activity_pill is NOT the recorded entry point — the adapter only
    ever calls set_activity_status; if it ever calls set_activity_pill
    instead, the "status" marker is absent and assertions fail (Rule 4:
    every test must be able to fail).
    """

    def __init__(self):
        self.calls: list[tuple] = []

    def set_activity_status(self, text, state=None):
        self.calls.append((text, state))

    def set_activity_pill(self, state):
        # Deliberately NOT routed through the same list — see docstring.
        self.calls.append(("PILL", state))


class FakeLabel:
    """Label-like fake recording the exact swap sequence.

    remove/add css_class and set_text are recorded in order so the test
    asserts the SAME remove→add→stash dance set_activity_pill performs
    (not just the end state).
    """

    def __init__(self):
        self._text = "Idle"
        self._css = "pill-idle"
        self.ops: list[tuple] = []

    def set_text(self, text):
        self._text = text
        self.ops.append(("set_text", text))

    def remove_css_class(self, name):
        self.ops.append(("remove", name))

    def add_css_class(self, name):
        self.ops.append(("add", name))


# ── Required coverage 1: status text lands on the resolved surface ────────


def test_status_text_lands_on_resolved_surface():
    surface = FakeSurface()
    adapter = ActivityPillAdapter(resolver=lambda: surface)
    adapter.set_status_text("Working", "reasoning")
    assert surface.calls == [("Working", "reasoning")]


# ── Required coverage 2: state=None passes through untouched ──────────────


def test_state_none_keeps_css_untouched():
    surface = FakeSurface()
    adapter = ActivityPillAdapter(resolver=lambda: surface)
    adapter.set_status_text("Working")
    # state=None must REACH the surface as None — the surface decides what
    # "no state" means (keeps its current class). Assert the exact shape.
    assert surface.calls == [("Working", None)]


# ── Required coverage 3: None surface is a silent no-op, cache updated ────


def test_none_surface_is_silent_noop():
    surface_holder: list = []

    def resolver():
        return surface_holder[0] if surface_holder else None

    adapter = ActivityPillAdapter(resolver=resolver)
    # Must not raise on the None surface.
    adapter.set_status_text("Reasoning hard", "reasoning")
    assert surface_holder == []  # nothing rendered — there was no surface
    # Cache was STILL updated: a surface appearing later gets the latest
    # status on its first resolved call.
    late = FakeSurface()
    surface_holder.append(late)
    adapter.set_status_text("Now streaming", "streaming")
    # New surface identity + cached text → catch-up apply of the CACHED
    # status first, then the new one.
    assert late.calls == [
        ("Reasoning hard", "reasoning"),  # catch-up (from cache)
        ("Now streaming", "streaming"),  # the live status
    ]


# ── Required coverage 4: new surface gets cached status catch-up ──────────


def test_new_surface_gets_cached_status_catchup():
    surface_a = FakeSurface()
    surfaces = [surface_a]
    adapter = ActivityPillAdapter(resolver=lambda: surfaces[-1])

    adapter.set_status_text("Tool running", "tool_use")
    assert surface_a.calls == [("Tool running", "tool_use")]

    # Tab switch: resolver now hands back a FRESH surface (B starts "Idle").
    surface_b = FakeSurface()
    surfaces.append(surface_b)
    adapter.set_status_text("Done", "done")

    # B must receive the catch-up apply of the CACHED status FIRST, then
    # the new one — net effect: B shows the new status. Assert call ORDER.
    assert surface_b.calls == [
        ("Tool running", "tool_use"),  # catch-up
        ("Done", "done"),
    ]
    # A is untouched by the switch.
    assert surface_a.calls == [("Tool running", "tool_use")]


# ── Required coverage 5: the progress quartet are no-ops ──────────────────


def test_progress_quartet_are_noops():
    surface = FakeSurface()
    adapter = ActivityPillAdapter(resolver=lambda: surface)
    # Each of the four progress methods: runs, never touches the surface,
    # never raises — the pill has no progress element (SPEC-07 §2 AMENDED).
    adapter.set_progress_fraction(0.5)
    adapter.set_progress_hidden(True)
    adapter.set_progress_pulse(True)
    adapter.pulse_progress()
    assert surface.calls == []


# ── Required coverage 6: the 6+error → pill-CSS state map ─────────────────


def test_surface_state_map():
    # All 7 keys, each mapping into the pill CSS vocabulary.
    expected = {
        "idle": "pill-idle",
        "sending": "pill-thinking",
        "reasoning": "pill-thinking",
        "streaming": "pill-streaming",
        "tool_use": "pill-tool",
        "done": "pill-done",
        "error": "pill-error",
    }
    assert _ACTIVITY_STATE_TO_CSS == expected
    # Every mapped class must have a rule in APP_CSS — the GTK stylesheet
    # (the pill label is a GTK widget, NOT part of the webview document;
    # the rules' previous _BASE_CSS home styled only the webview, leaving
    # the widget theme-default — SP1 fix round BUG #1).
    for cls in _ACTIVITY_STATE_TO_CSS.values():
        assert f".{cls} {{" in APP_CSS


def _app_css_pill_rules() -> dict[str, str]:
    """Parse APP_CSS → {class: color} for the pill rules.

    Strict shape (`.<class> { color: <value>; }`) on purpose: the pin must
    FAIL if a rule is deleted, malformed, or loses its color — a laxer
    parse could pass on broken CSS.
    """
    rules: dict[str, str] = {}
    for m in re.finditer(
        r"\.(pill-[a-z]+)\s*\{\s*color:\s*(#[0-9a-fA-F]{6})\s*;\s*\}", APP_CSS
    ):
        rules[m.group(1)] = m.group(2)
    return rules


def test_app_css_pill_rules_complete_and_distinct():
    """The tooth: APP_CSS must carry a usable rule for EVERY mapped class.

    Can-fail proven (see build report + fix-round re-audit BUG #3): deleting
    any rule → red; duplicating a color among the 6 distinct classes → red.
    The leniency in the MAP is sending≡reasoning → pill-thinking (two states
    share one CLASS); every CLASS color must still be pairwise distinct —
    including pill-thinking (re-audit BUG #3: the first version excluded it).
    """
    rules = _app_css_pill_rules()
    expected_classes = set(_ACTIVITY_STATE_TO_CSS.values())
    assert set(rules) == expected_classes  # every mapped class, no extras

    colors_by_class = {cls: rules.get(cls, "") for cls in expected_classes}
    assert len(set(colors_by_class.values())) == len(expected_classes), (
        f"distinct pill classes share a color: {colors_by_class}"
    )
    assert all(c.startswith("#") for c in rules.values())  # non-empty, hex


# ── Required coverage 7: unknown state keeps the current CSS class ────────


def test_set_activity_status_unknown_state_keeps_class():
    label = FakeLabel()
    # Drive the REAL method via a minimal duck-typed host — set_activity_status
    # only touches self._pill_label/self._pill_css, no gi needed.
    host = TextViewFallback.__new__(TextViewFallback)
    host._pill_label = label
    host._pill_css = "pill-idle"

    host.set_activity_status("Thinking…", "reasoning")  # known → swap
    assert label.ops == [
        ("set_text", "Thinking…"),
        ("remove", "pill-idle"),
        ("add", "pill-thinking"),
    ]
    assert host._pill_css == "pill-thinking"

    label.ops.clear()
    host.set_activity_status("Doing something odd", "martian_state")  # unknown
    assert label.ops == [("set_text", "Doing something odd")]  # text only
    assert host._pill_css == "pill-thinking"  # class KEPT (fail-quiet)

    label.ops.clear()
    host.set_activity_status("Plain", None)  # None → class untouched too
    assert label.ops == [("set_text", "Plain")]
    assert host._pill_css == "pill-thinking"


# ── Required coverage 8: surface_for_key is READ-ONLY ─────────────────────


class RegisteredFakeSurface:
    """Pure fake standing in for a surface entry — no gi, no display.

    Replaces SP1's SpySurface(TextViewFallback): a real TextViewFallback
    instantiates real GTK widgets, which segfaults under `env -u DISPLAY`
    (fix round BUG #2). The render path only needs the surface to accept
    append_message; this records the same evidence bare.
    """

    def __init__(self):
        self.appended: list[dict] = []

    def append_message(self, role, html_fragment, agent_name=None):
        self.appended.append(
            {"role": role, "html": html_fragment, "agent_name": agent_name}
        )

    def get_parent(self):
        # Mount-path guard in _mount_surface calls this on every render —
        # None = unmounted (the real unmounted surface's answer).
        return None


def test_surface_for_key_readonly(monkeypatch):
    handler = ChatRenderHandler()

    created: list = []

    def factory():
        s = RegisteredFakeSurface()
        created.append(s)
        return s

    monkeypatch.setattr(
        __import__("ui.handlers.chat_render_handler", fromlist=["x"]),
        "create_chat_surface",
        factory,
    )

    # MISS: read-only lookup returns None and creates NOTHING.
    assert handler.surface_for_key("no-such-key") is None
    assert len(handler._surfaces) == 0  # no surface created on the miss

    # A real render creates + registers a surface (the normal path).
    handler.render_sync("Agent", "hello", "real-key")
    assert len(created) == 1
    assert created[0].appended and "hello" in created[0].appended[0]["html"]

    # HIT: returns exactly the registered surface object.
    assert handler.surface_for_key("real-key") is handler._surfaces["real-key"]


# ── Required coverage 9: adapter satisfies the handler's 5-method surface ─


def test_adapter_satisfies_handler_call_surface():
    """SP1 pin (stays green through SP2): the adapter has all five methods
    ActivityHandler calls today (activity_handler.py:683-871). SP2's rebind
    compiles against THIS shape; a future rename breaks this test first."""
    required = {
        "set_status_text",
        "set_progress_fraction",
        "set_progress_hidden",
        "set_progress_pulse",
        "pulse_progress",
    }
    present = {name for name in required if hasattr(ActivityPillAdapter, name)}
    assert present == required
    # Signature shape: set_status_text carries (text, state=None) — the
    # state parameter is the SP1 seam the handler's markup path migrates to.
    sig = inspect.signature(ActivityPillAdapter.set_status_text)
    assert list(sig.parameters) == ["self", "text", "state"]
    assert sig.parameters["state"].default is None
