# tests/test_chat_render_scroll.py — scroll/pill/pin tests for the FIX 3
# scroll seam (SP5a fix round 1) in their SPEC-17 SP1.3 shape.
#
# A chat surface scrolls its DOCUMENT (the page scrolls inside the web view
# — there is no outer ScrolledWindow anymore, SPEC-58 §2a). main_content.
# scroll_chat_to_bottom resolves the page's chat box's surface via
# handler.surface_for_box(chat_box) and delegates to `surface.scroll_to_latest()`.
# These tests pin that delegation with a local stand-in surface (no WebKit).

import gi

gi.require_version('Gtk', '4.0')
from gi.repository import Gtk

import ui.handlers.chat_render_handler as crh_module


class _SpySurface(Gtk.Box):
    """Minimal surface double: mountable in a chat box, records scroll
    delegation, and keeps the same ``_rows`` row list the render handler
    reads. Reproduces nothing else (no WebKit)."""

    def __init__(self, *a, **k):
        super().__init__()
        self._rows: list[dict] = []
        self.appended: list[dict] = []
        self.scroll_calls: list[str] = []

    def append_message(self, role, html_fragment, agent_name=None, agent_color=None):
        self._rows.append(
            {"role": role, "html": html_fragment, "agent": agent_name or "",
             "color": agent_color}
        )
        self.appended.append({"role": role, "html": html_fragment, "agent": agent_name})

    def stream_delta(self, session_key, text, agent_name=None):
        pass

    def end_stream(self, session_key, agent_name=None):
        pass

    def append_live(self, payload_html, agent_name=None, agent_color=None):
        pass

    def clear(self):
        self._rows.clear()

    def destroy(self):
        self._rows.clear()

    def scroll_to_latest(self):
        self.scroll_calls.append("latest")


class TestSurfaceScrollDelegation:
    """SPEC-17 SP1.3: scroll_chat_to_bottom delegates to the mounted
    surface's scroll_to_latest(); nothing drives a GTK adjustment."""

    def _make(self, monkeypatch):
        def factory(**kwargs):
            return _SpySurface()

        monkeypatch.setattr(crh_module, "create_chat_surface", factory)
        from ui.handlers.chat_render_handler import ChatRenderHandler

        return ChatRenderHandler()

    @staticmethod
    def _mc(handler, box):
        from ui.views.main_content import MainContent

        mc = MainContent.__new__(MainContent)
        mc._chat_notebook = type("NB", (), {"get_current_page": lambda self: 0})()
        mc._tab_chat_boxes = {0: box}
        mc._chat_render_handler = handler
        return mc

    def test_mount_is_direct_no_wrapper(self, monkeypatch):
        """The mount appends the surface DIRECTLY to the chat box — the
        box's child IS the surface (no ScrolledWindow wrapper between)."""
        box = Gtk.Box()
        handler = self._make(monkeypatch)
        handler.set_chat_container_getter(lambda sk: box)
        handler.render_sync("Agent", "hello", "sk")
        surface = handler._surfaces["sk"]
        assert box.get_first_child() is surface
        assert not isinstance(box.get_first_child(), Gtk.ScrolledWindow)

    def test_scroll_delegates_to_surface_scroll_to_latest(self, monkeypatch):
        """SPEC-17 SP1.3: a surface tab delegates to `scroll_to_latest()` —
        no outer page adjustment is driven (SPEC-58 §2a)."""
        box = Gtk.Box()
        handler = self._make(monkeypatch)
        handler.set_chat_container_getter(lambda sk: box)
        handler.render_sync("Agent", "line1", "sk")
        surface = handler._surfaces["sk"]
        assert surface.scroll_calls == []
        mc = self._mc(handler, box)
        mc.scroll_chat_to_bottom(0)
        assert surface.scroll_calls == ["latest"]  # delegated

    def test_scroll_without_surface_is_noop(self, monkeypatch):
        """No surface mounted in the box's chat box → early return — no
        crash, nothing driven (guard kept)."""
        handler = self._make(monkeypatch)
        mc = self._mc(handler, Gtk.Box())
        mc.scroll_chat_to_bottom(0)  # must not raise

    def test_scroll_noop_when_box_missing(self, monkeypatch):
        """Missing chat-box entry → early return, no crash."""
        handler = self._make(monkeypatch)
        mc = self._mc(handler, None)
        mc.scroll_chat_to_bottom(0)  # must not raise

