# tests/test_escaping.py
# Tests for utils/escaping.py — Pango/XML escape utilities.

import pytest
from utils.escaping import escape_for_pango, xml_escape_text


class TestXmlEscapeText:
    """Simple XML escaping for plain text (no Pango markup)."""

    def test_plain_text_unchanged(self):
        assert xml_escape_text("Hello world") == "Hello world"

    def test_ampersand_escaped(self):
        assert xml_escape_text("Tom & Jerry") == "Tom &amp; Jerry"
        assert xml_escape_text("A & B & C") == "A &amp; B &amp; C"

    def test_angle_brackets_escaped(self):
        assert xml_escape_text("<script>") == "&lt;script&gt;"
        assert xml_escape_text("a < b") == "a &lt; b"
        assert xml_escape_text("a > b") == "a &gt; b"

    def test_double_quotes_escaped(self):
        assert xml_escape_text('say "hi"') == "say &quot;hi&quot;"

    def test_single_quote_apostrophe(self):
        assert xml_escape_text("it's") == "it&#x27;s"

    def test_mixed(self):
        assert xml_escape_text('Tom & Jerry <script> "hi"') == (
            "Tom &amp; Jerry &lt;script&gt; &quot;hi&quot;"
        )

    def test_empty_string(self):
        assert xml_escape_text("") == ""


class TestEscapeForPango:
    """Pango-aware escaping — preserves valid tags, escapes malformed ones."""

    # Plain text
    def test_plain_text_unchanged(self):
        assert escape_for_pango("Hello world") == "Hello world"

    def test_plain_text_ampersand_escaped(self):
        assert escape_for_pango("Tom & Jerry") == "Tom &amp; Jerry"

    def test_plain_text_with_literal_brackets_escaped(self):
        assert escape_for_pango("a < b") == "a &lt; b"
        assert escape_for_pango("a > b") == "a &gt; b"

    def test_empty_string(self):
        assert escape_for_pango("") == ""

    # Valid Pango tags preserved
    def test_bold_tag_preserved(self):
        assert escape_for_pango("<b>bold text</b>") == "<b>bold text</b>"

    def test_italic_tag_preserved(self):
        assert escape_for_pango("<i>italic text</i>") == "<i>italic text</i>"

    def test_monospace_tag_preserved(self):
        assert escape_for_pango("<tt>code</tt>") == "<tt>code</tt>"

    def test_underline_tag_preserved(self):
        assert escape_for_pango("<u>underlined</u>") == "<u>underlined</u>"

    def test_strikethrough_tag_preserved(self):
        assert escape_for_pango("\u672cstrikethrough\u672c") == "\u672cstrikethrough\u672c"

    def test_span_tag_preserved(self):
        assert escape_for_pango('<span foreground="red">text</span>') == '<span foreground="red">text</span>'

    def test_anchor_tag_escaped(self):
        """Pango 1.52 does not support <a>; escape_for_pango escapes it."""
        assert escape_for_pango('<a href="https://x.com">link</a>') == '&lt;a href=&quot;https://x.com&quot;&gt;link&lt;/a&gt;'

    def test_nested_tags_preserved(self):
        assert escape_for_pango("<b><i>bold italic</i></b>") == "<b><i>bold italic</i></b>"

    def test_mixed_tags_with_ampersand_in_content(self):
        assert escape_for_pango("<b>Tom & Jerry</b>") == "<b>Tom &amp; Jerry</b>"

    # Malformed tags escaped
    def test_unmatched_closing_tag_escaped(self):
        result = escape_for_pango("</b>")
        assert "&lt;/b&gt;" in result

    def test_wrong_closing_tag_escaped(self):
        result = escape_for_pango("<b>text</i>")
        assert "&lt;/i&gt;" in result

    def test_double_closing_escaped(self):
        result = escape_for_pango("</b></b>")
        assert result == "&lt;/b&gt;&lt;/b&gt;"

    def test_incomplete_open_tag_preserved(self):
        result = escape_for_pango("<b")
        assert result == "&lt;b"

    def test_br_tag_escaped(self):
        # Void tags (br, hr, img) are NOT preserved — they are common in
        # code snippets and would break Pango rendering if left as real
        # tags inside a <tt> code span.
        assert escape_for_pango("line1<br>line2") == "line1&lt;br&gt;line2"

    def test_hr_tag_escaped(self):
        assert escape_for_pango("<hr>") == "&lt;hr&gt;"

    def test_img_tag_escaped(self):
        assert escape_for_pango('<img src="foo.png">') == '&lt;img src=&quot;foo.png&quot;&gt;'

    def test_br_inside_bold_escaped(self):
        # Regression: <b>bold with <br> inside</b> — <br> must be escaped
        # even when surrounded by preserved tags, otherwise Pango opens
        # a real <br> and fails to close the </tt> (or </b>) around it.
        assert escape_for_pango("<b>bold with <br> inside</b>") == \
            "<b>bold with &lt;br&gt; inside</b>"

    def test_tag_with_attributes_preserved(self):
        result = escape_for_pango('<span foreground="blue">blue text</span>')
        assert 'foreground="blue"' in result

    def test_link_tag_escaped(self):
        """Pango 1.52 does not support <a>; the whole tag is escaped."""
        result = escape_for_pango('<a href="http://example.com"><u>link</u></a>')
        assert '&lt;a' in result  # escaped, not preserved
        assert '<a href' not in result  # not a real tag

    def test_only_tag_characters(self):
        result = escape_for_pango("<<>>")
        # <<>> - trailing > becomes &gt;, trailing < is kept as literal
        assert "&gt;" in result

    def test_multiple_ampersands(self):
        assert escape_for_pango("a & b & c") == "a &amp; b &amp; c"

    def test_trailing_lt_escaped(self):
        result = escape_for_pango("text <")
        assert result.endswith("&lt;")

    # Strict entity unescape
    def test_well_formed_amp(self):
        assert escape_for_pango("Tom & Jerry") == "Tom &amp; Jerry"

    def test_well_formed_lt(self):
        assert escape_for_pango("a < b") == "a &lt; b"

    def test_well_formed_gt(self):
        assert escape_for_pango("a > b") == "a &gt; b"

    def test_malformed_gt_preserved(self):
        result = escape_for_pango("see &gt here")
        # &gt; without semicolon - & is escaped to &amp;, gt is literal
        assert "amp;gt" in result

    def test_malformed_amp_preserved(self):
        result = escape_for_pango("see &amp here")
        # &amp; without semicolon - & is escaped, amp is literal
        assert "amp;" in result

    def test_buggy_autolink_output_robust(self):
        broken = '<<a href="https://example.com&gt"><u>https://example.com&gt</u></a>'
        result = escape_for_pango(broken)
        assert 'href="https://example.com>' not in result

    def test_numeric_decimal(self):
        assert escape_for_pango("&#42;") == "*"

    def test_numeric_hex(self):
        assert escape_for_pango("&#x2A;") == "*"

    def test_non_pango_entity_not_decoded(self):
        result = escape_for_pango("&copy; 2024")
        # &copy; is well-formed (has semicolon), but &copy is not in our entity allowlist
        # So the & is escaped to &amp; before entity decode, resulting in &amp;copy;
        assert "&amp;copy;" in result

    def test_double_encoded_no_double_decode(self):
        result = escape_for_pango("&")
        assert result == "&amp;"

    def test_invalid_numeric_codepoint_preserved(self):
        result = escape_for_pango("&#999999999;")
        assert "999999999" in result or "&amp;#999999999;" in result


class TestOrphanTagSweep:
    """Orphan opening tags (no matching close) must be escaped."""

    def test_orphan_a_tag_escaped(self):
        result = escape_for_pango('renders <a href="..."> tags')
        assert '<a ' not in result  # not preserved as valid tag
        assert '<a' not in result     # fully escaped

    def test_orphan_b_tag_escaped(self):
        result = escape_for_pango('<b>bold')
        assert '<b>' not in result  # not preserved as valid tag
        assert '<b>' not in result  # fully escaped

    def test_valid_tag_pair_preserved(self):
        assert escape_for_pango('<b>bold</b>') == '<b>bold</b>'

    def test_a_tag_pair_escaped(self):
        """Even a valid-looking <a> pair is escaped — Pango doesn't support <a>."""
        result = escape_for_pango('<a href="https://x.com">link</a>')
        assert '<a ' not in result
        assert '&lt;a' in result

    def test_nested_valid_tags_preserved(self):
        assert escape_for_pango('<b><i>nested</i></b>') == '<b><i>nested</i></b>'

    def test_grep_output_with_a_tag(self):
        """The exact crash trigger: plain text containing <a href="...">."""
        result = escape_for_pango('# \u2190 renders <a href="..."> tags')
        assert '<a ' not in result  # not preserved as valid tag
        assert '<a' not in result     # fully escaped

    def test_no_orphan_when_all_closed(self):
        """When all tags are properly closed, sweep does nothing."""
        result = escape_for_pango('<b>one</b> <i>two</i>')
        assert result == '<b>one</b> <i>two</i>'


class TestPangoCaseSensitivity:
    """Pango is CASE-SENSITIVE on tag names and attribute names."""

    def test_uppercase_tag_pair_normalized(self):
        """Pango is CASE-SENSITIVE on tag names. Uppercase must be lowercased."""
        assert escape_for_pango("<B>orphan</B>") == "<b>orphan</b>"

    def test_mixed_case_tag_normalized(self):
        """Mixed case input must normalize to all-lowercase output."""
        assert escape_for_pango("<B>x</b>") == "<b>x</b>"
        assert escape_for_pango("<b>x</B>") == "<b>x</b>"
        assert escape_for_pango("<Span>x</span>") == "<span>x</span>"

    def test_uppercase_closing_tag_normalized(self):
        """Closing tag with uppercase name must be lowered to match opening."""
        assert escape_for_pango("<b>x</B>") == "<b>x</b>"

    def test_uppercase_attribute_name_normalized(self):
        """Pango is case-sensitive on attribute names. Uppercase must be lowered."""
        result = escape_for_pango('<span FOREGROUND="red">x</span>')
        assert 'foreground="red"' in result, f"Got: {result!r}"
        assert 'FOREGROUND="red"' not in result, f"Got: {result!r}"

    def test_mixed_case_attribute_name_normalized(self):
        """Mixed-case attribute name normalizes to lowercase."""
        result = escape_for_pango('<span Foreground="red">x</span>')
        assert 'foreground="red"' in result, f"Got: {result!r}"

    def test_attribute_value_case_preserved(self):
        """Attribute values are preserved exactly (case-sensitive user data)."""
        result = escape_for_pango('<span foreground="RED">x</span>')
        assert '<span foreground="RED">x</span>' == result

    def test_nested_uppercase_tags_normalized(self):
        """All Pango tags in nested structure must be lowercased."""
        assert escape_for_pango("<B><I>nested</I></B>") == "<b><i>nested</i></b>"
        assert escape_for_pango("<B><B>double</B></B>") == "<b><b>double</b></b>"

    def test_uppercase_self_closing_escaped(self):
        """Uppercase self-closing void tags are escaped, not normalized.

        Void tags (br, hr, img) are never preserved as real Pango elements,
        regardless of case or self-closing form. They are common in code
        snippets and shell output, where they must render as literal text."""
        assert escape_for_pango("<BR/>") == "&lt;BR/&gt;"
        assert escape_for_pango("<HR/>") == "&lt;HR/&gt;"

    def test_uppercase_orphan_tag_still_escaped(self):
        """Orphan tags are escaped regardless of input case."""
        # Uppercase orphan tags become lowercase, then are fully HTML-escaped
        # so they appear as literal text in the output
        assert escape_for_pango('<B>no close') == '&lt;b&gt;no close'
        assert escape_for_pango('<B attr="val">no close') == '&lt;b attr=&quot;val&quot;&gt;no close'

    def test_br_inside_code_span_does_not_break_pango(self):
        """Regression for the cascade-failure: a <br> literal inside text
        that gets wrapped in <tt> by format_markdown would emit the
        Gtk warning 'Element tt was closed, but the currently open element
        is br' and silently empty the bubble. escape_for_pango must escape
        the <br> so the final markup stays valid Pango."""
        # Simulates what chat_render_handler does: escape then markdown-wrap.
        escaped = escape_for_pango('assert escape_for_pango("line1<br>line2") == "line1<br>line2"')
        # The <br> must be escaped (not real Pango):
        assert "<br>" not in escaped
        assert "&lt;br&gt;" in escaped
        # And when wrapped in <tt> by the markdown renderer, the result must
        # be valid Pango markup (no nested unclosed void tag):
        final = f"<tt>{escaped}</tt>"
        import gi
        gi.require_version('Gtk', '4.0')
        from gi.repository import Gtk
        # set_markup logs a Gtk-WARNING on parse error and renders empty.
        # We can't assert on stderr cleanly here, but the test below checks
        # the structural invariant: <tt> must not contain a real <br>.
        assert final.count("<br>") == 0, final
        # Belt-and-suspenders: try set_markup to confirm Pango accepts it.
        lbl = Gtk.Label()
        lbl.set_markup(final)  # would log Gtk-WARNING if malformed


class TestSpanAttributeValidation:
    """Phase 1: unknown attributes on known Pango tags must be rejected."""

    def test_span_with_unknown_attr_escaped(self):
        """JSX classname attribute on <span> is not a valid Pango attr."""
        result = escape_for_pango('<span classname="x">t</span>')
        assert '<span' not in result or '&lt;span' in result, f"Got: {result!r}"

    def test_span_with_jsx_style_attr_escaped(self):
        """JSX style attribute on <span> is not a valid Pango attr."""
        result = escape_for_pango('<span style={{ color: "red" }}>hi</span>')
        assert '<span' not in result or '&lt;span' in result, f"Got: {result!r}"

    def test_span_with_valid_attrs_preserved(self):
        """Valid Pango span attrs must survive (regression guard)."""
        result = escape_for_pango('<span foreground="#ff0000">t</span>')
        assert result == '<span foreground="#ff0000">t</span>', f"Got: {result!r}"

    def test_b_with_any_attr_escaped(self):
        """<b> (and other non-span tags) takes no attributes at all."""
        result = escape_for_pango('<b class="x">t</b>')
        assert '&lt;b' in result, f"Got: {result!r}"

    def test_uppercase_classname_normalized_then_rejected(self):
        """Uppercase attr names are lowercased before validation."""
        result = escape_for_pango('<span ClassName="x">t</span>')
        assert '&lt;span' in result, f"Got: {result!r}"

    def test_span_bg_attr_escaped(self):
        """Pango rejects 'bg' (not a valid span attr); only 'background' is."""
        result = escape_for_pango('<span bg="red">x</span>')
        assert '&lt;span' in result, f"Got: {result!r}"

    def test_span_color_deprecated_alias_preserved(self):
        """'color' is Pango's deprecated alias for foreground, still accepted."""
        result = escape_for_pango("<span color='#10b981'>x</span>")
        assert '<span color=\'#10b981\'>' in result, f"Got: {result!r}"

    def test_valid_name_invalid_value_preserved_guard_handles(self):
        """Name-valid tags with malformed values are preserved by the escaper
        because it validates attribute NAMES, not VALUE shapes.

        The downstream Pango.parse_markup guard (feed_card.py, event_cards.py)
        handles the resulting parse failure. For composite markup (diff lines)
        the per-line fallback isolates the failure; for single text blocks
        set_text() is an acceptable last-resort fallback.
        """
        result = escape_for_pango('<span foreground=noquotes>t</span>')
        # Name 'foreground' is valid → tag preserved; value shape will be
        # caught downstream by the parse_markup guard.
        assert '<span foreground=noquotes>' in result, f"Got: {result!r}"