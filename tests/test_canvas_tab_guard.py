"""A canvas tab is not a conversation: send is ignored and welcome is skipped."""

import pytest

from ui.handlers.chat_handler import ChatHandler

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from ui.handlers.chat_render_handler import ChatRenderHandler
from ui.views.main_content import MainContent


class _Buffer:
    def __init__(self):
        self.read = False

    def get_buffer(self):
        self.read = True
        raise AssertionError("canvas send must not read the input buffer")


class _Main:
    def __init__(self, key):
        self._key = key
        self.user_input = _Buffer()

    def get_current_session_key(self):
        return self._key


def test_on_send_leaves_canvas_tab_alone():
    main = _Main("canvas:review")
    handler = ChatHandler(main, None, None)
    handler.on_send()
    assert main.user_input.read is False


class _SpySurface(Gtk.Box):
    """Local stand-in for the chat surface (no WebKit)."""

    def __init__(self, *a, **k):
        super().__init__()
        self._rows: list[dict] = []
        self.appended: list[dict] = []

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


def test_create_chat_tab_skips_welcome_for_canvas(monkeypatch):
    import ui.handlers.chat_render_handler as crh_module

    monkeypatch.setattr(crh_module, "create_chat_surface", lambda **_k: _SpySurface())
    handler = ChatRenderHandler()
    mc = MainContent()
    win = Gtk.Window()
    win.set_child(mc)
    mc.set_chat_render_handler(handler)
    try:
        mc.create_chat_tab("canvas:review", "review")
        assert "canvas:review" not in handler._welcome_shown
        mc.create_chat_tab("agent:wc", "Worker")
        assert "agent:wc" in handler._welcome_shown
    finally:
        win.destroy()
