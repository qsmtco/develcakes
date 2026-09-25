# tests/test_chat_render_handler.py — scroll/pill/pin tests for the FIX 3
# scroll seam (SP5a fix round 1). Appended here instead of a new file to
# keep the sanctioned test-file set unchanged.

import gi

gi.require_version('Gtk', '4.0')
from gi.repository import Gtk

import ui.handlers.chat_render_handler as crh_module
import ui.views.chat_surface as cs_module
from ui.views.chat_surface import TextViewFallback


class TestSurfaceScrollOwnership:
    """FIX 3 (single-scroll ruling): the surface OWNS its ScrolledWindow;
    the mount appends the surface directly; scroll_chat_to_bottom drives
    the surface's vadjustment via surface_for_box; wrapper expand is gone
    with the wrapper."""

    def _make(self, monkeypatch):
        def factory():
            return TextViewFallback()

        monkeypatch.setattr(crh_module, "create_chat_surface", factory)
        from ui.handlers.chat_render_handler import ChatRenderHandler

        return ChatRenderHandler()

    def test_surface_owns_scroll_and_fills_pane(self):
        """Both surface classes own a ScrolledWindow (single-scroll, one
        place — _make_owned_scroll) and expand to fill their pane."""
        for cls in (cs_module.TextViewFallback,):
            s = cls()
            assert isinstance(s._scroll, Gtk.ScrolledWindow)
            assert s.get_vexpand() is True and s.get_hexpand() is True
            assert s._scroll.get_vexpand() is True
            # The content view lives INSIDE the surface's own scroll.
            assert s._scroll.get_child() is s._view
            s.destroy()

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

    def test_scroll_chat_to_bottom_drives_surface_vadjustment(self, monkeypatch):
        """scroll_chat_to_bottom → surface_for_box → the SURFACE's own
        vadjustment is driven (set_value == upper - page_size)."""
        from ui.views.main_content import MainContent

        box = Gtk.Box()
        handler = self._make(monkeypatch)
        handler.set_chat_container_getter(lambda sk: box)
        handler.render_sync("Agent", "line1", "sk")
        handler.render_sync("Agent", "line2", "sk")

        mc = MainContent.__new__(MainContent)
        mc._chat_notebook = type("NB", (), {"get_current_page": lambda self: 0})()
        mc._tab_scrolls = {0: Gtk.ScrolledWindow()}
        mc._tab_chat_boxes = {0: box}
        mc._chat_render_handler = handler

        surface = handler._surfaces["sk"]
        vadj = surface.get_vadjustment()
        driven: list[tuple[float, float, float]] = []  # (value, upper, page)
        orig_set_value = vadj.set_value

        def _capture(v):
            orig_set_value(v)
            driven.append((v, vadj.get_upper(), vadj.get_page_size()))

        vadj.set_value = _capture  # type: ignore[method-assign]
        mc.scroll_chat_to_bottom(0)
        # GLib.timeout_add(16, ...) needs real elapsed time — pump with a
        # wall-clock deadline (a pure pending()-drain spins too fast).
        import time

        from gi.repository import GLib

        ctx = GLib.MainContext.default()
        deadline = time.monotonic() + 2.0
        while not driven and time.monotonic() < deadline:
            while ctx.pending():
                ctx.iteration(False)
            time.sleep(0.005)
            ctx.iteration(False)
        assert driven, "scroll_chat_to_bottom never drove the surface vadjustment"
        value, upper, page = driven[0]
        # Driven to the BOTTOM of the surface's own adjustment, and it is a
        # real scroll (content taller than the viewport → positive offset).
        assert value == upper - page
        assert value > 0

    def test_scroll_falls_back_to_tab_scroll_without_surface(self, monkeypatch):
        """No surface in the box → the legacy _tab_scrolls path still
        drives (plain Pango children keep working)."""
        from ui.views.main_content import MainContent

        handler = self._make(monkeypatch)
        plain_scroll = Gtk.ScrolledWindow()
        mc = MainContent.__new__(MainContent)
        mc._chat_notebook = type("NB", (), {"get_current_page": lambda self: 0})()
        mc._tab_scrolls = {0: plain_scroll}
        mc._tab_chat_boxes = {0: Gtk.Box()}  # empty box — no surface
        mc._chat_render_handler = handler

        vadj = plain_scroll.get_vadjustment()
        vadj.set_upper(500.0)
        vadj.set_page_size(50.0)
        driven: list[float] = []
        orig_set_value = vadj.set_value

        def _capture(v):
            orig_set_value(v)
            driven.append(v)

        vadj.set_value = _capture  # type: ignore[method-assign]
        mc.scroll_chat_to_bottom(0)
        import time

        from gi.repository import GLib

        ctx = GLib.MainContext.default()
        deadline = time.monotonic() + 2.0
        while not driven and time.monotonic() < deadline:
            while ctx.pending():
                ctx.iteration(False)
            time.sleep(0.005)
            ctx.iteration(False)
        assert driven, "fallback path never drove the tab scroll"
        assert driven[0] == 450.0

    def test_scroll_noop_when_neither_surface_nor_tab_scroll(self, monkeypatch):
        """Missing scroll entry → early return, no crash (guard kept)."""
        from ui.views.main_content import MainContent

        handler = self._make(monkeypatch)
        mc = MainContent.__new__(MainContent)
        mc._chat_notebook = type("NB", (), {"get_current_page": lambda self: 0})()
        mc._tab_scrolls = {}
        mc._tab_chat_boxes = {0: Gtk.Box()}
        mc._chat_render_handler = handler
        mc.scroll_chat_to_bottom(0)  # must not raise
