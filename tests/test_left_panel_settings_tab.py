# tests/test_left_panel_settings_tab.py
# Fourth notebook page: Settings, with the unverified-provider dot.

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from ui.views.left_panel import LeftPanel


def _label_text(widget) -> str:
    if isinstance(widget, Gtk.Label):
        return widget.get_text()
    child = widget.get_first_child()
    parts = []
    while child is not None:
        if isinstance(child, Gtk.Label):
            parts.append(child.get_text())
        child = child.get_next_sibling()
    return " ".join(parts)


class TestSettingsTab:
    def test_fourth_page_is_settings(self):
        panel = LeftPanel()
        notebook = panel._PAP_notebook
        assert notebook.get_n_pages() == 4
        last = notebook.get_tab_label(notebook.get_nth_page(3))
        assert "Settings" in _label_text(last)

    def test_status_dot_follows_verified_flag(self):
        panel = LeftPanel()
        panel.set_settings_status(False)
        assert panel._settings_dot.get_visible() is True
        panel.set_settings_status(True)
        assert panel._settings_dot.get_visible() is False

    def test_hide_callback_skips_the_settings_page(self):
        panel = LeftPanel()
        fired = []
        panel.set_on_settings_hidden(lambda: fired.append(True))
        panel._on_pap_switch_page(None, panel._settings_page, 3)
        assert fired == []
        panel._on_pap_switch_page(None, Gtk.Box(), 0)
        assert fired == [True]
