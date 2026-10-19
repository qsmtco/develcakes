# tests/test_canvas_top_spacer.py — canvas tab top spacer (toolbar overlap fix).
#
# The canvas tab mounts its document UNDER the chat input toolbar, so the
# first line of an agent-authored document was clipped by the overlay. The
# window's canvas callback now prepends a fixed-height spacer div before the
# document reaches either mount path:
#
#   live     -> surface.append_live(html)
#   degraded -> surface.append_message("agent", sanitize_agent_html(html))
#
# These pins drive the REAL callback installed by
# MainWindow._wire_group_tool_callbacks() through a fake surface, using the
# established MainWindow.__new__ shell pattern (no GTK widget build) and a
# synchronous GLib.idle_add replacement so the UI hop runs inline.

from unittest.mock import MagicMock

import pytest

import ui.window as wm

DOC = "<!DOCTYPE html><html><body><p>chart</p></body></html>"


class FakeSurface:
    """Minimal canvas surface: records what each mount path received."""

    def __init__(self, live: bool = True):
        self._live = live
        self.live_payloads: list[str] = []
        self.message_payloads: list[tuple[str, str, str | None]] = []
        self.cleared = 0

    def clear(self) -> None:
        self.cleared += 1

    def is_live_enabled(self) -> bool:
        return self._live

    def append_live(self, html: str, agent_name: str | None = None) -> str:
        self.live_payloads.append(html)
        return f"live-{len(self.live_payloads)}"

    def append_message(self, role: str, html: str, agent_name: str | None = None):
        self.message_payloads.append((role, html, agent_name))
        return MagicMock()


class FakeGLib:
    """Runs idle_add callbacks inline — the loop never spins in tests."""

    @staticmethod
    def idle_add(func, *args, **kwargs):
        func()
        return 0


@pytest.fixture
def mount(monkeypatch):
    """Return (canvas_callback, surface) for a shell MainWindow.

    Captures the callback the window installs instead of mutating the real
    group-tool globals, so test order cannot leak a stale canvas callback.
    """
    captured: dict = {}
    monkeypatch.setattr(
        "agent.tools.set_canvas_callback",
        lambda cb: captured.__setitem__("canvas", cb),
    )
    monkeypatch.setattr("agent.tools.set_talk_callback", lambda cb: None)
    monkeypatch.setattr("agent.tools.set_agent_context_callback", lambda cb: None)
    monkeypatch.setattr(wm, "GLib", FakeGLib)

    def _make(live: bool = True) -> FakeSurface:
        s = FakeSurface(live)
        win = wm.MainWindow.__new__(wm.MainWindow)
        win._main_content = MagicMock()
        win._chat_render_handler = MagicMock()
        win._chat_render_handler.surface_for_mount.return_value = s
        win._agent_runtime_handler = MagicMock()
        win._wire_group_tool_callbacks()
        captured["surface"] = s
        return s

    captured["make"] = _make
    return captured


def _canvas(mount) -> object:
    return mount["canvas"]


def test_live_path_prepends_top_spacer(mount):
    """Live mount: append_live receives the document behind the spacer."""
    surface = mount["make"](live=True)

    result = _canvas(mount)("session:1", "chart", DOC)

    assert result.success is True
    assert surface.cleared == 1
    assert len(surface.live_payloads) == 1
    payload = surface.live_payloads[0]
    assert payload.startswith(wm._CANVAS_TOP_SPACER_HTML)
    assert payload == wm._CANVAS_TOP_SPACER_HTML + DOC


def test_degraded_path_prepends_top_spacer(mount):
    """Degraded (scripts off) mount: the sanitized document keeps the spacer."""
    surface = mount["make"](live=False)

    result = _canvas(mount)("session:1", "chart", DOC)

    assert result.success is True
    assert surface.live_payloads == []
    assert len(surface.message_payloads) == 1
    role, payload, agent_name = surface.message_payloads[0]
    assert role == "agent"
    assert agent_name == "chart"
    assert payload.startswith(wm._CANVAS_TOP_SPACER_HTML)


def test_payload_already_spaced_is_not_doubled(mount):
    """An idempotent document is passed through with exactly one spacer."""
    surface = mount["make"](live=True)
    already = wm._CANVAS_TOP_SPACER_HTML + DOC

    result = _canvas(mount)("session:1", "chart", already)

    assert result.success is True
    payload = surface.live_payloads[0]
    assert payload == already
    assert payload.count(wm._CANVAS_TOP_SPACER_HTML) == 1
