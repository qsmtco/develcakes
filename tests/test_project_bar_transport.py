# tests/test_project_bar_transport.py
# Connect pip + stop icon on the project bar. Replaces tests/test_toolbar.py.

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from ui.views.main_content import MainContent


def _labels(button) -> str:
    child = button.get_child()
    if child is None:
        return button.get_label() or ""
    if hasattr(child, "get_label"):
        return child.get_label() or ""
    return ""


class TestTransportCluster:
    def test_icons_have_no_text_labels(self):
        mc = MainContent()
        forbidden = {"Connect", "Disconnect", "Connecting…", "■ Stop All"}
        for button in (mc._connect_btn, mc._connect_pip, mc._stop_all_btn):
            assert _labels(button) not in forbidden

    def test_connected_paints_green_pip(self):
        mc = MainContent()
        mc.set_telegram_bridge_state("connected")
        classes = set(mc._connect_pip.get_css_classes())
        assert "project-bar-connect-on" in classes
        assert "project-bar-connect-off" not in classes

    def test_connecting_and_error_and_unknown(self):
        mc = MainContent()
        mc.set_telegram_bridge_state("connecting")
        assert "project-bar-connect-pending" in mc._connect_pip.get_css_classes()
        mc.set_telegram_bridge_state("error")
        classes = set(mc._connect_pip.get_css_classes())
        assert "project-bar-connect-error" in classes
        assert "project-bar-connect-pending" not in classes
        mc.set_telegram_bridge_state("unconfigured")
        assert "project-bar-connect-off" in mc._connect_pip.get_css_classes()
        assert "No transport" in (mc._connect_btn.get_tooltip_text() or "")
        mc.set_telegram_bridge_state("offline")
        assert "project-bar-connect-off" in mc._connect_pip.get_css_classes()

    def test_click_does_not_clobber_connecting(self):
        mc = MainContent()
        fired = []

        def _on_connect():
            fired.append(True)
            mc.set_telegram_bridge_state("connecting")

        mc.set_on_connect_clicked(_on_connect)
        mc._on_transport_connect_clicked()
        mc._connect_pip.emit("clicked")
        assert fired == [True, True]
        assert "project-bar-connect-pending" in mc._connect_pip.get_css_classes()

    def test_stop_click_and_missing_callbacks(self):
        mc = MainContent()
        mc._on_transport_connect_clicked()
        mc._on_transport_stop_clicked()
        fired = []
        mc.set_on_stop_all_clicked(lambda: fired.append(True))
        mc._on_transport_stop_clicked()
        assert fired == [True]
        tip = mc._stop_all_btn.get_tooltip_text() or ""
        assert "Halt every agent" in tip

    def test_bar_never_leaves_top_box_across_tab_lifecycle(self):
        """SPEC-58 §2a: the project bar is a PERMANENT _top_box row — the
        old overlay reparenting (bar hops onto the active tab, back on last
        close) is DELETED. Pin the new invariant: the bar never leaves
        _top_box across create, tab switch, MIDDLE-tab close, and last-tab
        close; it stays visible after the last close; the tab bar itself
        hides at 0 pages (GtkGizmo min-height guard)."""
        mc = MainContent()
        bar = mc._project_settings
        assert bar.get_parent() is mc._top_box

        # CREATE — three agent tabs; the bar keeps its seat in _top_box.
        mc.create_chat_tab("agent:1", "One")
        assert bar.get_parent() is mc._top_box
        assert mc._chat_notebook.get_show_tabs() is True
        mc.create_chat_tab("agent:2", "Two")
        mc.create_chat_tab("agent:3", "Three")
        assert bar.get_parent() is mc._top_box

        # SWITCH — moving between tabs must not reparent it either.
        mc._chat_notebook.set_current_page(0)
        assert bar.get_parent() is mc._top_box
        mc._chat_notebook.set_current_page(2)
        assert bar.get_parent() is mc._top_box

        # MIDDLE-TAB CLOSE — page 1 is neither first nor last; the bar is
        # unmoved and still visible, and the remaining two pages reindex.
        mc._close_tab(1)
        assert bar.get_parent() is mc._top_box
        assert bar.get_visible() is True
        assert mc._chat_notebook.get_n_pages() == 2

        # LAST-TAB CLOSE — the bar keeps its seat and its visibility; only
        # the (now empty) tab strip hides.
        mc._close_tab(0)
        assert bar.get_parent() is mc._top_box
        mc._close_tab(0)
        assert bar.get_parent() is mc._top_box
        assert bar.get_visible() is True
        assert mc._chat_notebook.get_n_pages() == 0
        assert mc._chat_notebook.get_show_tabs() is False

    def test_connect_tooltip_is_telegram(self):
        mc = MainContent()
        tip = (mc._connect_btn.get_tooltip_text() or "").lower()
        assert "telegram" in tip
        assert "post-mvp" not in tip
        assert "gateway" not in tip
