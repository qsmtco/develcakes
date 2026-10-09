# SPEC-16 SP1 — status bar tracks the same agent name as the context meter.

import inspect

from ui.window import MainWindow


class _Label:
    def __init__(self, text=""):
        self.text = text

    def set_text(self, text):
        self.text = text


def test_update_agent_id_display_formats_name():
    win = MainWindow.__new__(MainWindow)
    win._agent_id_label = _Label()
    win.update_agent_id_display("Coder")
    assert win._agent_id_label.text == "Agent: Coder"


def test_update_agent_id_display_blank_when_unresolved():
    win = MainWindow.__new__(MainWindow)
    win._agent_id_label = _Label("Agent: Coder")
    win.update_agent_id_display("—")
    assert win._agent_id_label.text == "Agent: —"


def test_update_agent_id_display_noop_without_label():
    win = MainWindow.__new__(MainWindow)
    win.update_agent_id_display("Coder")  # must not raise


def test_build_wires_status_label_from_agent_display():
    """Both the resolved and unresolved arms of _update_agent_display write the label."""
    src = inspect.getsource(MainWindow._wire_feed)
    assert "self.update_agent_id_display(agent_name)" in src
    assert 'self.update_agent_id_display("—")' in src
    assert "update_agent_context_display" in src
