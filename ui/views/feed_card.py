# ui/views/feed_card.py
# Feed card view helpers.
# Pure view — no business logic, no state mutations.
#
# Public API:
#   build_context_panel(snapshot) -> Gtk.Widget
#   RENDERED_BODY_LIMIT
#
# The GTK card factories (build_feed_card, update_card_badge,
# update_card_in_place, build_empty_feed_widget) were deleted with the GTK
# feed path (WU feed-surface chunk 3b). The feed is a WebKit document built
# by render/feed_html.py; there is no widget-list fallback (NO_WEBKIT ruling).
# `build_context_panel` survives because ui/feed/snapshots.py still imports it
# from live code; `RENDERED_BODY_LIMIT` survives as the render-side cap that
# render/feed_html.py pins against (tests/test_feed_html.py).

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Pango

from models.conversation_snapshot import ConversationSnapshot
from utils.escaping import escape_for_pango, xml_template


# Render-side cap. STORAGE keeps the full body (models.feed_card.MAX_STORED_BODY,
# 200_000) — this limit is what the document renders per card, because the HTML
# surface mirrors the Pango-era measurement budget. render/feed_html.py keeps
# its own FEED_RENDERED_BODY_LIMIT (it must not import this GTK module) and a
# test pins the two equal.
RENDERED_BODY_LIMIT = 2000


# ─────────────────────────────────────────────────────────────────────────────
# Context panel (conversation snapshot)
# ─────────────────────────────────────────────────────────────────────────────

def build_context_panel(
    snapshot: ConversationSnapshot,
) -> Gtk.Widget:
    """
    Build the expandable context panel for a feed card.

    For snapshot_type="conversation":
      - Vertical box of compact mini-bubbles (role + text)
      - User messages: right-aligned, muted background (.feed-context-mini-bubble-user)
      - Agent messages: left-aligned, default background (.feed-context-mini-bubble-agent)
      - If truncated, shows "Showing last N of M messages"

    For snapshot_type="diff":
      - Monospace label with diff text
      - Red/green styling for -/+ lines
      - CSS class: .feed-context-diff

    Returns a Gtk.Box with CSS class "feed-context-panel".
    Initially hidden (set_visible(False)). Toggled by Review button.
    """
    panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    panel.add_css_class("feed-context-panel")

    # Header label
    if snapshot.snapshot_type == "conversation":
        header_text = "▼ Review Context"
        if snapshot.total_messages > len(snapshot.messages):
            header_text += f" (showing last {len(snapshot.messages)} of {snapshot.total_messages})"
    else:
        header_text = "▼ Diff Context"

    header = Gtk.Label(label=header_text)
    header.add_css_class("feed-context-header")
    header.set_xalign(0)
    header.set_margin_bottom(4)
    panel.append(header)

    if snapshot.snapshot_type == "conversation":
        if not snapshot.messages:
            empty = Gtk.Label(label="No conversation context available.")
            empty.add_css_class("feed-context-empty")
            empty.set_halign(Gtk.Align.CENTER)
            panel.append(empty)
        else:
            for msg in snapshot.messages:
                bubble = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
                is_user = msg.role == "You" or msg.role == "User"
                bubble.add_css_class("feed-context-mini-bubble")
                bubble.add_css_class(
                    "feed-context-mini-bubble-user" if is_user
                    else "feed-context-mini-bubble-agent"
                )

                role_label = Gtk.Label()
                role_label.set_markup(xml_template("<b>{role}:</b>", role=msg.role))
                role_label.set_xalign(0)
                role_label.set_valign(Gtk.Align.START)

                text_label = Gtk.Label()
                text_label.set_markup(xml_template("{text}", text=msg.text))
                text_label.set_xalign(0)
                text_label.set_wrap(True)
                text_label.set_wrap_mode(1)  # Pango.WrapMode.WORD_CHAR
                text_label.set_selectable(True)
                text_label.set_can_focus(False)
                text_label.set_hexpand(True)

                bubble.append(role_label)
                bubble.append(text_label)
                panel.append(bubble)

    elif snapshot.snapshot_type == "diff":
        if not snapshot.diff_text:
            empty = Gtk.Label(label="No diff available.")
            empty.add_css_class("feed-context-empty")
            empty.set_halign(Gtk.Align.CENTER)
            panel.append(empty)
        else:
            # Single label with full diff text for multi-line selection.
            # Color coding via Pango spans (not CSS classes) so one label
            # can show + in green, - in red, context in default.
            markup_lines = []
            for line in snapshot.diff_text.split("\n"):
                escaped = escape_for_pango(line)
                if line.startswith("+") and not line.startswith("+++"):
                    line_markup = f'<span foreground="#6a9955"><tt>{escaped}</tt></span>'
                elif line.startswith("-") and not line.startswith("---"):
                    line_markup = f'<span foreground="#f44747"><tt>{escaped}</tt></span>'
                else:
                    line_markup = f'<tt>{escaped}</tt>'
                # Validate each diff line individually so one malformed line
                # cannot cascade and erase color markup on valid lines.
                # If a line fails Pango.parse_markup, fall back to the raw
                # escaped text (loss of color styling for that line only).
                try:
                    Pango.parse_markup(line_markup, -1, "\x00")
                    markup_lines.append(line_markup)
                except Exception:
                    markup_lines.append(escaped)
            diff_markup = "\n".join(markup_lines)

            diff_label = Gtk.Label()
            diff_label.set_markup(diff_markup)
            diff_label.set_xalign(0)
            diff_label.set_selectable(True)
            diff_label.set_can_focus(False)
            diff_label.set_wrap(True)
            diff_label.set_wrap_mode(Pango.WrapMode.CHAR)
            diff_label.add_css_class("feed-context-diff")
            panel.append(diff_label)

    # Start hidden
    panel.set_visible(False)
    return panel
