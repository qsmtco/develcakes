# tests/test_pango_guard_sites.py — DELETED (SPEC-58 §2i).
#
# Every Pango guard site this file pinned was deleted with the Pango surface:
# ui/views/event_cards.py (§2c), ui/views/file_tree.py's set_markup sites
# (file_tree still exists, but the surviving HTML surface carries the guard
# contract — see tests/test_html_guard_sites.py for the HTML-side catalog).
# The pure-Pango validation cases moved nowhere: Pango.parse_markup behavior
# is upstream-tested, not app behavior.
