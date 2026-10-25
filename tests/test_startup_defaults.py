"""Surgical tests for the two startup defaults (PM-mandated minimal diff):

1. Fresh-launch Welcome tab — created at the startup path
   (MainContent.create_welcome_tab), titled exactly "Welcome" (bare label,
   no agent-name/session suffix), loads assets/splash.html as a RAW full
   HTML document via the surface's WebView load path
   (load_unsanitized_full_document),
   closable via the standard ×, and once closed stays closed for the session.
2. Left-panel notebook opens with Projects selected at startup.

Run ONLY this file (hard rule — no full-suite runs).
"""

import os
import sys

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ui.handlers.chat_handler import ChatHandler
from ui.handlers.chat_render_handler import ChatRenderHandler
from ui.views.left_panel import LeftPanel
from ui.views.main_content import MainContent


def _label_text(widget, skip_buttons: bool = True) -> str:
    """All Gtk.Label text under ``widget``, joined (label boxes carry
    dot/name/sep/session/close as separate children). ``skip_buttons=True``
    drops button labels (the tab close "×") — title text only."""
    parts = []

    def walk(w):
        if skip_buttons and isinstance(w, Gtk.Button):
            return
        if isinstance(w, Gtk.Label):
            parts.append(w.get_text())
        child = w.get_first_child()
        while child is not None:
            walk(child)
            child = child.get_next_sibling()

    walk(widget)
    return " ".join(parts).strip()


def _find_button(widget, label: str):
    if isinstance(widget, Gtk.Button) and widget.get_label() == label:
        return widget
    child = widget.get_first_child()
    while child is not None:
        found = _find_button(child, label)
        if found is not None:
            return found
        child = child.get_next_sibling()
    return None


class _SpySurface(Gtk.Box):
    """Local stand-in for the chat surface (no WebKit). Records raw full-
    document loads and keeps the same ``_rows`` row list the render handler
    reads, so welcome rows can be inspected without a WebView."""

    def __init__(self, *a, **k):
        super().__init__()
        self.loaded: list[str] = []
        self._rows: list[dict] = []
        self.appended: list[dict] = []

    def load_unsanitized_full_document(self, html: str) -> None:
        self.loaded.append(html)

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


@pytest.fixture
def welcome_env(monkeypatch):
    import ui.handlers.chat_render_handler as crh_module

    monkeypatch.setattr(crh_module, "create_chat_surface", lambda **_k: _SpySurface())
    handler = ChatRenderHandler()
    mc = MainContent()
    win = Gtk.Window()
    win.set_child(mc)
    mc.set_chat_render_handler(handler)
    yield mc, handler, win
    win.destroy()


class TestStartupWelcomeTab:
    def test_create_welcome_tab_builds_bare_label_and_raw_loads_splash(self, welcome_env):
        mc, handler, _win = welcome_env
        idx = mc.create_welcome_tab()
        assert idx == 0
        page = mc._chat_notebook.get_nth_page(idx)
        label = mc._chat_notebook.get_tab_label(page)
        # Tab reads just "Welcome" — no agent-name/session suffix.
        assert _label_text(label) == "Welcome"
        # Startup shows the Welcome tab.
        assert mc._chat_notebook.get_current_page() == idx
        # Splash reached the surface through the RAW full-document loader
        # (not the sanitized welcome/markdown emitter): exact file content,
        # inline <script> intact.
        surface = handler.surface_for_key("welcome:splash")
        assert surface is not None
        assert len(surface.loaded) == 1
        assert "<!DOCTYPE" in surface.loaded[0]
        assert "<script" in surface.loaded[0]
        assert "develcakesSplash" in surface.loaded[0]
        # No sanitized welcome row was emitted for the special key.
        assert "welcome:splash" not in handler._welcome_shown

    def test_welcome_tab_closes_via_x_and_never_respawns(self, welcome_env):
        mc, _handler, _win = welcome_env
        idx = mc.create_welcome_tab()
        label = mc._chat_notebook.get_tab_label(mc._chat_notebook.get_nth_page(idx))
        close_btn = _find_button(label, "×")
        assert close_btn is not None
        close_btn.emit("clicked")
        assert mc._chat_notebook.get_n_pages() == 0
        assert mc._welcome_closed is True
        # Subsequent create attempts (project switch / new tabs) cannot respawn it.
        assert mc.create_chat_tab("welcome:splash", "Welcome") is None
        assert mc._chat_notebook.get_n_pages() == 0
        assert mc.create_welcome_tab() is None
        assert mc._chat_notebook.get_n_pages() == 0

    def test_create_welcome_tab_twice_loads_splash_once(self, welcome_env):
        """BUG #5: calling create_welcome_tab twice never re-issues the raw
        load — the second call finds the existing tab and returns it without
        reloading (a reload would re-render over the live splash)."""
        mc, handler, _win = welcome_env
        mc.create_welcome_tab()
        mc.create_welcome_tab()
        surface = handler.surface_for_key("welcome:splash")
        assert surface is not None
        assert len(surface.loaded) == 1
        # The tab is still the (single) active page.
        assert mc._chat_notebook.get_n_pages() == 1

    def test_create_welcome_tab_oseerror_loads_inline_fallback(self, welcome_env, monkeypatch):
        """BUG #3: when assets/splash.html is unreadable the Welcome tab must
        not be left blank — the built-in inline fallback is loaded instead."""
        import ui.views.main_content as mc_module

        monkeypatch.setattr(mc_module, "get_project_root", lambda: "/nonexistent/develcakes-root")
        mc, handler, _win = welcome_env
        idx = mc.create_welcome_tab()
        assert idx == 0
        surface = handler.surface_for_key("welcome:splash")
        assert surface is not None
        assert len(surface.loaded) == 1
        html = surface.loaded[0]
        assert "DevelCakes" in html
        assert "<script" not in html

    def test_extract_session_display_name_welcome_prefix(self):
        """BUG #7: the welcome: session-key prefix maps to 'Welcome'."""
        assert MainContent._extract_session_display_name("welcome:splash") == "Welcome"
        assert MainContent._extract_session_display_name("canvas:default") == "Canvas"

    def test_normal_tab_still_has_session_suffix(self, welcome_env):
        mc, _handler, _win = welcome_env
        idx = mc.create_chat_tab("agent:wc:telegram:direct:1", "Worker")
        label = mc._chat_notebook.get_tab_label(mc._chat_notebook.get_nth_page(idx))
        text = _label_text(label)
        assert "Worker" in text
        assert "Telegram" in text


class TestStartupLeftPanelDefault:
    def test_left_panel_opens_with_projects_selected(self):
        panel = LeftPanel()
        assert panel._PAP_notebook.get_n_pages() == 4
        # Prompts=0, Agents=1, Projects=2, Settings=3 → Projects active at startup.
        assert panel._PAP_notebook.get_current_page() == 2
        label = panel._PAP_notebook.get_tab_label(
            panel._PAP_notebook.get_nth_page(2)
        )
        assert "Projects" in _label_text(label)


class TestSurfaceRawLoad:
    def test_load_unsanitized_full_document_uses_raw_path_and_resets_state(self, monkeypatch):
        from ui.views.chat_surface import ChatSurface

        surf = ChatSurface()
        captured: list[str] = []
        monkeypatch.setattr(surf, "_load_html", lambda doc: captured.append(doc))
        doc = (
            "<!DOCTYPE html><html><body>"
            "<script>window.develcakesSplash={}</script>"
            "</body></html>"
        )
        surf.load_unsanitized_full_document(doc)
        # The FULL document goes verbatim through the raw load path.
        assert captured == [doc]
        # No python-side transcript rows remain.
        assert list(surf._rows) == []
        surf.destroy()


class _SendBuffer:
    """Input-buffer stand-in: read access records and fails the test."""

    def __init__(self):
        self.read = False

    def get_buffer(self):
        self.read = True
        raise AssertionError("welcome send must not read the input buffer")


class _SendMain:
    """Minimal main-content stand-in for ChatHandler.on_send."""

    def __init__(self, key):
        self._key = key
        self.user_input = _SendBuffer()

    def get_current_session_key(self):
        return self._key


class TestWelcomeSendGuard:
    """The Welcome tab is a document, not a conversation — sending must
    short-circuit in ChatHandler.on_send before touching the input buffer
    (same pattern as the existing canvas-tab guard)."""

    def test_on_send_leaves_welcome_tab_alone(self):
        main = _SendMain("welcome:splash")
        handler = ChatHandler(main, None, None)
        handler.on_send()
        assert main.user_input.read is False

    def test_welcome_send_guard_is_prefix_based(self):
        main = _SendMain("welcome:")
        handler = ChatHandler(main, None, None)
        handler.on_send()
        assert main.user_input.read is False
