# tests/test_presentation_injection.py
# Tests for Bug #9: xml_template escapes all markup in interpolated values.
#
# SPEC-58 §2i: class TestEventCardEscaping deleted with its subject —
# create_error_bubble / create_file_card died with ui/views/event_cards.py.
# The surviving class pins utils/escaping.py xml_template, which is live.

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
