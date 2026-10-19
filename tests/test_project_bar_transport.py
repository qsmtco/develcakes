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

    def test_bar_moves_onto_the_first_tab_and_back(self):
        mc = MainContent()
        assert mc._project_settings.get_parent() is mc._top_box
        mc.create_chat_tab("agent:1", "Agent")
        assert mc._project_settings.get_parent() is not mc._top_box
        mc._close_tab(0)
        assert mc._project_settings.get_parent() is mc._top_box
        assert mc._project_settings.get_visible() is True

    def test_connect_tooltip_is_telegram(self):
        mc = MainContent()
        tip = (mc._connect_btn.get_tooltip_text() or "").lower()
        assert "telegram" in tip
        assert "post-mvp" not in tip
        assert "gateway" not in tip
