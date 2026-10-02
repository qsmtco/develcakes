# tests/test_activity_pill_adapter.py — SPEC-07 SP1 (R4 status-bar removal).
#
# This file runs BARE — no display, no xvfb (proven: env -u DISPLAY green).
# Every surface is a pure fake. (UI-PILLBAR P2: SP1's test 7 — the real
# set_activity_status driven via TextViewFallback.__new__ — retired with the
# per-surface pill; its contract lives in tests/test_activity_pill_label.py.
# test 8's ChatRenderHandler imports gi but only calls pure-Python code
# (render_document/markdown → nh3 — no GTK calls on this path).
#
# Red-first protocol (steelFramedCodeWriter Step 3 + SP1 brief Edit 5): tests
# landed BEFORE implementation; the red run is pasted in the build report.
# Fix round (SPEC-07 SP1 audit): pill rules live in APP_CSS (ui/styles.py) —
# the pill label is a GTK widget, not part of the webview document.

import inspect
import re
from unittest.mock import MagicMock

import pytest

from ui.handlers.activity_handler import ActivityHandler
from ui.handlers.chat_render_handler import ChatRenderHandler
from ui.styles import APP_CSS
from ui.views.activity_pill import (
    _ACTIVITY_STATE_TO_CSS,
    ActivityPillAdapter,
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


# ── SP1 coverage 7: retired in UI-PILLBAR P2 ──────────────────────────────
# test_set_activity_status_unknown_state_keeps_class RETIRED with its
# subject: it drove TextViewFallback.set_activity_status via __new__, and
# that method no longer exists (per-surface pill retired; the shared bar
# pill owns the logic). The contract it pinned (known state → swap dance;
# unknown/None → text lands, class kept) is pinned VERBATIM by
# tests/test_activity_pill_label.py TestSetActivityStatus — same recorder
# pattern, same assertions, against ActivityPillLabel. Nothing lost.


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


# ── SPEC-07 SP2 Edit 4: project-tab resolver ruling pin ───────────────────


class FakeChatBox:
    """Minimal append-only box — the mount target for the pin test."""

    def __init__(self):
        self.children = []

    def append(self, widget):
        self.children.append(widget)


def test_project_tab_resolver_picks_per_key(monkeypatch):
    """SP2 Edit 4 ruling: on a project tab, surface_for_key('project:<name>')
    returns the project's PRIMARY (welcome) surface, while the agent key
    returns the agent's own surface — two keys, two distinct surfaces. The
    window resolver (surface_for_key(get_current_session_key())) must pick
    per-key: the project pill renders project-tab activity; agent surfaces'
    pills stay untouched. Multi-surface display (N pills on a project tab)
    is the SP3/SP4 REGISTER item — this pin fixes the RESOLVER contract, not
    the display question."""
    import ui.handlers.chat_render_handler as crh

    handler = crh.ChatRenderHandler()
    project_box = FakeChatBox()

    def getter(key):
        return project_box if key == "project:alpha" else None

    handler.set_chat_container_getter(getter)

    created: list = []

    def factory():
        s = RegisteredFakeSurface()
        created.append(s)
        return s

    monkeypatch.setattr(crh, "create_chat_surface", factory)

    # Project tab opens → welcome renders → PRIMARY surface keyed project:alpha.
    handler.render_welcome("project:alpha")
    # An agent reply routes into the project tab (mount_key) but its surface
    # is cached under the AGENT key.
    handler.render_sync("Agent", "working the task", "agent:coder",
                        mount_key="project:alpha")

    assert len(created) == 2  # one surface per key — no cross-key reuse
    project_surface = handler.surface_for_key("project:alpha")
    agent_surface = handler.surface_for_key("agent:coder")
    assert project_surface is created[0]
    assert agent_surface is created[1]
    assert project_surface is not agent_surface  # per-key resolution


# ── SPEC-07 SP2 fix round BUG #1: the (text, state) mapping pin ───────────


class RecordingTarget:
    """Records (text, state) pairs — the args-level fake BUG #1 demands."""

    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []

    def set_status_text(self, text, state=None):
        self.calls.append((text, state))

    # Progress quartet: no-ops — the handler may or may not call them;
    # neither is an error (the adapter keeps the 5-method duck-type).
    def set_progress_fraction(self, f):
        pass

    def set_progress_hidden(self, b):
        pass

    def set_progress_pulse(self, e):
        pass

    def pulse_progress(self):
        pass


_STREAMING_TEXT_RE = r"^⬇ Generating… · \d+ tokens · \d+ tok/s · \d+\.\d+s$"


@pytest.mark.parametrize(
    ("state", "expected_text"),
    [
        ("idle", "● Idle"),
        ("sending", "⬡ Pre Flight Check"),
        ("reasoning", "◉ Reasoning…"),
        ("tool_use", "⚙ read_file"),
        ("done", "✓ Done"),
        ("streaming", _STREAMING_TEXT_RE),  # matched via re.match
    ],
)
def test_update_status_pins_text_and_state(state, expected_text, fake_glib):
    """BUG #1: the SP2 deliverable — the 6-state (text, state) map — pinned
    at the ARGS level. Drives the REAL handler through _set_state (the render
    trigger); the LAST recorded call must be exactly (expected_text, state):
    text from the verbatim body, state the EXACT state string (MUT A drops
    the arg → None; MUT C pins 'idle' — both must go red here)."""
    target = RecordingTarget()
    h = ActivityHandler(
        status_target=target, main_content=MagicMock(), GLib_module=fake_glib
    )

    if state == "tool_use":
        h._current_tool_name = "read_file"  # read at render time
    if state == "streaming":
        h._streaming_token_count = 800

    # _set_state is THE render trigger. From the initial 'idle' any other
    # state is a real transition → renders. For 'idle' itself the same-state
    # early-return does NOT render, so enter via a real transition
    # (reasoning → idle) — the _enter_idle pattern from test_uirsp3_phase2.
    if state == "idle":
        h._set_state("reasoning", None)
    h._set_state(state, None)

    assert target.calls, f"{state}: _set_state must render via set_status_text"
    last_text, last_state = target.calls[-1]
    if state == "streaming":
        assert re.match(_STREAMING_TEXT_RE, last_text), (
            f"streaming label shape drifted: {last_text!r}"
        )
    else:
        assert last_text == expected_text, (
            f"{state}: text drifted: {last_text!r} != {expected_text!r}"
        )
    assert last_state == state, (
        f"{state}: state arg must be the exact state string, got {last_state!r}"
    )
