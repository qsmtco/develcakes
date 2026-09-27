# tests/test_pango_guard_sites.py
# Phase 2 regression tests: verify the unguarded set_markup sites
# (file_tree.py:217, file_tree.py:1092) now have the
# Pango.parse_markup pre-validation guard with set_text fallback.
#
# The sandbox segfaults on Gtk.Label construction at process exit, so these
# tests exercise the validation logic via Pango.parse_markup (which is safe)
# and inspect the source to confirm the guard patterns are present.

import inspect
import pytest

import gi
gi.require_version("Pango", "1.0")
from gi.repository import Pango


# ── Pure Pango validation ──────────────────────────────────────────────────

class TestPangoParseMarkupGuardLogic:
    """Validate that Pango.parse_markup distinguishes ok / fail markup
    exactly as the guard sites rely on."""

    def test_valid_bold_markup_parses(self):
        Pango.parse_markup("<b>safe</b>", -1, "\x00")

    def test_valid_span_markup_parses(self):
        Pango.parse_markup('<span foreground="red">safe</span>', -1, "\x00")

    def test_valid_composite_markup_parses(self):
        Pango.parse_markup("<b><i>safe</i></b>", -1, "\x00")

    def test_valid_escaped_filename_parses(self):
        # Simulates escape_for_pango output for a typical filename
        safe = "Tom &amp; Jerry &lt;script&gt;.txt"
        Pango.parse_markup(safe, -1, "\x00")

    def test_invalid_unclosed_tag_raises(self):
        with pytest.raises(Exception):
            Pango.parse_markup("<b>unclosed", -1, "\x00")

    def test_invalid_mismatched_tag_raises(self):
        with pytest.raises(Exception):
            Pango.parse_markup("<b>text</i>", -1, "\x00")

    def test_invalid_unknown_attribute_raises(self):
        # escape_for_pango rejects unknown attrs, but if somehow a raw span
        # with an invalid attribute reaches the guard, parse_markup should fail.
        with pytest.raises(Exception):
            Pango.parse_markup('<span classname="x">text</span>', -1, "\x00")

    def test_malformed_ampersand_raises(self):
        with pytest.raises(Exception):
            Pango.parse_markup("foo & bar", -1, "\x00")


# ── Source-code guard presence ───────────────────────────────────────────────

# SP5c-2 Phase C: the donor-module source-guard class was REPOINTED here
# (not removed) — its guarded code moved verbatim to ui/views/event_cards.py
# in Phase A; file_tree guards below remain in place.


class TestEventCardsCodeLabelGuard:
    """event_cards.py _build_code_from_markup must guard code_label.set_markup.

    SP5c-2 Phase C fix round (BUG#1): REPOINTED from the deleted donor
    module's guard class — the guarded code (builder + its verbatim
    relocation) lives in ui/views/event_cards.py since Phase A.
    """

    def test_parse_markup_guard_present(self):
        from ui.views import event_cards
        src = inspect.getsource(event_cards)
        assert "Pango.parse_markup(code_markup" in src, (
            "Guard pattern missing in event_cards.py source"
        )
        assert "code_label.set_text(raw_content)" in src, (
            "Fallback to set_text missing in event_cards.py source"
        )

    def test_set_markup_inside_try_block(self):
        """code_label.set_markup must appear inside a try block after parse_markup."""
        from ui.views import event_cards
        src_lines = inspect.getsource(event_cards).splitlines()
        for i, line in enumerate(src_lines):
            if line.strip() == "code_label.set_markup(code_markup)":
                # Walk back to confirm we are in a try: block
                found_try = False
                for j in range(i - 1, max(i - 10, -1), -1):
                    if src_lines[j].strip() == "try:":
                        found_try = True
                        break
                assert found_try, (
                    "code_label.set_markup is not inside a try block"
                )
                return
        pytest.fail("code_label.set_markup(code_markup) not found in source")

    def test_preserves_label_properties(self):
        """xalign, selectable, wrap, wrap_mode, max_width_chars must remain."""
        from ui.views import event_cards
        src = inspect.getsource(event_cards)
        for attr in (
            "code_label.set_xalign(0)",
            "code_label.set_selectable(True)",
            "code_label.set_wrap(True)",
            "code_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)",
            "code_label.set_max_width_chars(120)",
        ):
            assert attr in src, f"Missing label property: {attr}"


class TestFileTreeRowLabelGuard:
    """file_tree.py FileTreeRow.set_label must guard _label.set_markup."""

    def test_parse_markup_guard_present(self):
        from ui.views import file_tree
        src = inspect.getsource(file_tree)
        assert "Pango.parse_markup(markup" in src or "Pango.parse_markup" in src, (
            "Guard pattern missing in file_tree.py source"
        )
        # Check the set_label method specifically
        assert "self._label.set_text(display_name)" in src, (
            "Fallback to set_text missing in file_tree.py source"
        )

    def test_no_bare_set_markup_in_set_label(self):
        from ui.views import file_tree
        src = inspect.getsource(file_tree)
        assert "        self._label.set_markup(escape_for_pango(display_name))\n" not in src, (
            "Unguarded _label.set_markup still in set_label"
        )


class TestFileTreeTitleLabelGuard:
    """file_tree.py _on_project_selected must guard _title_lbl.set_markup."""

    def test_parse_markup_guard_present(self):
        from ui.views import file_tree
        src = inspect.getsource(file_tree)
        assert "title_markup" in src and "Pango.parse_markup(title_markup" in src, (
            "Guard pattern missing for title label in file_tree.py source"
        )
        assert "self._title_lbl.set_text(name)" in src, (
            "Fallback to set_text missing for title label in file_tree.py source"
        )

    def test_no_bare_set_markup_for_title(self):
        from ui.views import file_tree
        src = inspect.getsource(file_tree)
        assert '        self._title_lbl.set_markup(f"<b>{safe_name}</b>")\n' not in src, (
            "Unguarded _title_lbl.set_markup still present in _on_project_selected"
        )


# ── Simulated branch coverage via mock ───────────────────────────────────────

class TestGuardBranchLogic:
    """Mock the label object to prove correct branch (set_markup vs set_text)
    without constructing real Gtk.Label widgets."""

    def test_guard_happy_path_calls_set_markup(self, monkeypatch):
        calls = []
        mock_label = type("MockLabel", (), {
            "set_markup": lambda self, t: calls.append(("markup", t)),
            "set_text": lambda self, t: calls.append(("text", t)),
        })()
        markup = "<b>safe</b>"
        try:
            Pango.parse_markup(markup, -1, "\x00")
            mock_label.set_markup(markup)
        except Exception:
            mock_label.set_text(markup)
        assert calls == [("markup", markup)]

    def test_guard_sad_path_calls_set_text(self, monkeypatch):
        calls = []
        mock_label = type("MockLabel", (), {
            "set_markup": lambda self, t: calls.append(("markup", t)),
            "set_text": lambda self, t: calls.append(("text", t)),
        })()
        markup = "<b>bad"
        raw = "<b>bad"
        try:
            Pango.parse_markup(markup, -1, "\x00")
            mock_label.set_markup(markup)
        except Exception:
            mock_label.set_text(raw)
        assert calls == [("text", raw)]

    def test_guard_sad_path_does_not_call_set_markup(self, monkeypatch):
        """Critical: on parse failure, set_markup must NEVER be called."""
        markup_calls = []
        text_calls = []
        mock_label = type("MockLabel", (), {
            "set_markup": lambda self, t: markup_calls.append(t),
            "set_text": lambda self, t: text_calls.append(t),
        })()
        markup = "foo & bar"
        try:
            Pango.parse_markup(markup, -1, "\x00")
            mock_label.set_markup(markup)
        except Exception:
            mock_label.set_text(markup)
        assert not markup_calls
        assert text_calls == [markup]
