# tests/test_presentation_injection.py
# Tests for Bug #4 + #9: dynamic values in hardcoded Pango wrappers must be
# fully escaped via xml_template/xml_escape_text, not escape_for_pango (which
# preserves known Pango tags).

import pytest


def _gtk_skip():
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk  # noqa: F401
    except (ImportError, ValueError):
        return True
    return False


class TestXmlTemplate:
    """Bug #9: xml_template helper escapes all markup in interpolated values."""

    def test_xml_template_escapes_bold(self):
        from utils.escaping import xml_template
        result = xml_template("<b>{x}</b>", x="<b>fake</b>")
        assert result == "<b>&lt;b&gt;fake&lt;/b&gt;</b>"

    def test_xml_template_escapes_ampersand(self):
        from utils.escaping import xml_template
        result = xml_template("{x}", x="a & b")
        assert result == "a &amp; b"

    def test_xml_template_preserves_literal_tags(self):
        from utils.escaping import xml_template
        result = xml_template("<b>{x}</b>", x="hello")
        assert result == "<b>hello</b>"

    def test_xml_template_multiple_kwargs(self):
        from utils.escaping import xml_template
        result = xml_template("{a} - {b}", a="<x>", b="<y>")
        assert result == "&lt;x&gt; - &lt;y&gt;"


class TestEventCardEscaping:
    """Bug #4: event card content fields must use xml_escape_text, not escape_for_pango."""

    def test_error_bubble_escapes_bold(self):
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import create_error_bubble
        widget = create_error_bubble("<b>not bold</b>")

        def find_label_with(w, needle):
            if hasattr(w, "get_label"):
                if needle in w.get_label():
                    return True
            child = w.get_first_child()
            while child:
                if find_label_with(child, needle):
                    return True
                child = child.get_next_sibling()
            return False

        assert find_label_with(widget, "&lt;b&gt;not bold&lt;/b&gt;"), (
            "escaped text not found in error bubble"
        )

    def test_file_card_path_escapes_bold(self):
        if _gtk_skip():
            pytest.skip("GTK not available in test environment")
        from ui.views.event_cards import create_file_card
        widget = create_file_card("<b>fake</b>")

        # Recursive walk to find any label containing the escaped text
        def find_label_with(w, needle):
            if hasattr(w, "get_label"):
                if needle in w.get_label():
                    return True
            child = w.get_first_child()
            while child:
                if find_label_with(child, needle):
                    return True
                child = child.get_next_sibling()
            return False

        assert find_label_with(widget, "&lt;b&gt;fake&lt;/b&gt;"), (
            "escaped path not found in file card"
        )
