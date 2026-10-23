# tests/test_main_content_improve_status.py
# Real-widget test for MainContent.set_improve_status (SPEC-55 §3d).
#
# SPEC-55 fix round — Test gap M5: fake-only coverage let the mutation
# (the status label never showing) survive. This constructs a REAL
# MainContent and asserts the label actually shows/hides. Needs a display —
# auto-skipped headless by conftest; run under `xvfb-run`.

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from ui.views.main_content import MainContent


class TestImproveStatusLabel:
    """Real-widget pins for the transient Improve status line."""

    def _make(self):
        mc = MainContent()
        win = Gtk.Window()
        win.set_child(mc)
        return mc, win

    def test_status_label_starts_hidden(self):
        mc, win = self._make()
        try:
            label = mc._improve_status_label
            assert label is not None
            assert label.get_visible() is False
            assert "improve-status" in label.get_css_classes()
        finally:
            win.destroy()

    def test_status_shows_for_text_and_hides_for_empty(self):
        mc, win = self._make()
        try:
            label = mc._improve_status_label
            mc.set_improve_status("Improve model not configured — Settings → Improve Model")
            assert label.get_visible() is True
            assert label.get_text() == "Improve model not configured — Settings → Improve Model"
            mc.set_improve_status("")
            assert label.get_visible() is False
            # None behaves like empty (clears).
            mc.set_improve_status("oops")
            assert label.get_visible() is True
            mc.set_improve_status(None)
            assert label.get_visible() is False
        finally:
            win.destroy()

    def test_input_edit_clears_status(self):
        mc, win = self._make()
        try:
            label = mc._improve_status_label
            mc.set_improve_status("something went wrong")
            assert label.get_visible() is True
            buffer = mc.user_input.get_buffer()
            buffer.set_text("the user starts typing")
            assert label.get_visible() is False
        finally:
            win.destroy()
