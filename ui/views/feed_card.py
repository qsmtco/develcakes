# ui/views/feed_card.py
# Feed card widget factories — Phase 1.
# Pure view — no business logic, no state mutations. All actions via callbacks.
# Architecture: the ONLY place that constructs feed card GTK widgets.
#
# Public API:
#   build_feed_card(card_data, on_review, on_accept, on_reject, on_copy) -> Gtk.Widget
#   build_empty_feed_widget() -> Gtk.Widget

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Pango

from typing import Callable
import os

from models.feed_card import FeedCardData, CardType
from models.conversation_snapshot import ConversationSnapshot
from utils.escaping import escape_for_pango, xml_template
from utils.config import get_project_root


# ─────────────────────────────────────────────────────────────────────────────
# Shared header builder (feed card variant)
# ─────────────────────────────────────────────────────────────────────────────

def _make_feed_card_header(
    title: str,
    card_type: str,
    card_data: FeedCardData,
    on_copy: Callable[[str], None],
) -> tuple[Gtk.Box, Gtk.Button]:
    """
    Build a feed card header bar with title + copy button.
    Color is driven by card type CSS class on the parent.

    Returns (header_box, copy_btn) — copy_btn is pre-wired.
    """
    header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    header.add_css_class("feed-card-header")

    title_label = Gtk.Label()
    title_label.set_text(title)
    title_label.set_halign(Gtk.Align.START)
    title_label.set_hexpand(True)
    title_label.set_ellipsize(Pango.EllipsizeMode.END)
    title_label.set_width_chars(20)

    copy_btn = Gtk.Button()
    copy_btn.add_css_class("flat")
    copy_btn.set_tooltip_text("Copy")
    copy_btn.set_size_request(22, 22)
    copy_btn.set_opacity(0.3)
    try:
        copy_btn.set_child(Gtk.Image.new_from_file(
            os.path.join(get_project_root(), "ui", "icons", "copy.svg")))
    except Exception:
        copy_btn.set_label("📋")
    copy_motion = Gtk.EventControllerMotion()
    copy_motion.connect("enter", lambda _c, _x, _y: copy_btn.set_opacity(1.0))
    copy_motion.connect("leave", lambda _c: copy_btn.set_opacity(0.3))
    copy_btn.add_controller(copy_motion)
    # Body text for copy
    body_for_copy = card_data.body or title
    copy_btn.connect("clicked", lambda _, t=body_for_copy: on_copy(t))

    # Sequence number badge (if assigned) (Phase 3)
    if card_data.seq_num is not None:
        seq_label = Gtk.Label(label=f"#{card_data.seq_num}")
        seq_label.add_css_class("feed-card-seq")
        header.append(seq_label)

    header.append(title_label)
    header.append(copy_btn)
    return header, copy_btn


def _format_timestamp(ts) -> str:
    """Format a datetime as a human-readable relative string."""
    from datetime import datetime, timezone
    if ts is None:
        return "just now"
    now = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = now - ts
    total_seconds = delta.total_seconds()
    if total_seconds < 60:
        return "just now"
    if total_seconds < 3600:
        mins = int(total_seconds / 60)
        return f"{mins}m ago"
    if total_seconds < 86400:
        hrs = int(total_seconds / 3600)
        return f"{hrs}h ago"
    days = int(total_seconds / 86400)
    return f"{days}d ago"


# ─────────────────────────────────────────────────────────────────────────────
# Card type body renderers
# ─────────────────────────────────────────────────────────────────────────────

def _render_body(card_data: FeedCardData) -> Gtk.Widget:
    """Render the body of a feed card based on card type."""
    ct = card_data.card_type
    # The render cap + reveal control apply to EVERY body-bearing card type
    # (UIRESP2-T2 §1) — including approval cards, whose command is the worst
    # offender in practice (22,714-char heredoc measured live).
    needs_approval = bool((card_data.metadata or {}).get("needs_approval"))

    if ct in ("diff", "git_commit"):
        # For Phase 1, render body as plain monospace text.
        # Actual diff rendering (hunks, syntax highlight) comes in Phase 4.
        # git_commit bodies carry raw `git` stdout (feed_handler._add_git_card),
        # so they are one of the genuinely unbounded producers.
        return _render_text_body(card_data.body, mono=True,
                                 needs_approval=needs_approval)

    elif ct in ("file_created", "file_modified", "file_deleted", "dir_created", "dir_deleted"):
        return _render_file_event_body(card_data)

    elif ct == "agent_action":
        return _render_text_body(card_data.body, mono=False,
                                 needs_approval=needs_approval)

    elif ct == "task":
        return _render_task_body(card_data)

    elif ct == "system":
        return _render_text_body(card_data.body, mono=True,
                                 needs_approval=needs_approval)

    else:
        return _render_text_body(card_data.body, mono=False,
                                 needs_approval=needs_approval)


# ─────────────────────────────────────────────────────────────────────────────
# Rendered body cap + reveal control (SPEC-UI-RESPONSIVENESS-2-T2 §1)
# ─────────────────────────────────────────────────────────────────────────────

# Render-side cap. STORAGE keeps the full body (models.feed_card.MAX_STORED_BODY,
# 200_000) — this limit is what Pango ever measures, because text measurement was
# 60.1% of main-thread burn with 16–39 KB approval bodies on screen.
RENDERED_BODY_LIMIT = 2000

# Tooltip on the reveal control for a card awaiting approval: the approver must
# be able to read the whole command BEFORE deciding, so the cap is never a
# reachability barrier — one click shows the full text.
REVEAL_TOOLTIP_PENDING_APPROVAL = "Full command text — view before approving"
REVEAL_TOOLTIP_DEFAULT = "Show the full text"


def _reveal_label(hidden: int) -> str:
    """Label for the reveal control — the EXACT number of hidden characters."""
    return f"… {hidden} more characters"


def _apply_reveal_tooltip(button: Gtk.Button, needs_approval: bool) -> None:
    button.set_tooltip_text(
        REVEAL_TOOLTIP_PENDING_APPROVAL if needs_approval else REVEAL_TOOLTIP_DEFAULT
    )


def _install_reveal_control(
    box: Gtk.Box, label: Gtk.Label, *, needs_approval: bool = False
) -> Gtk.Button:
    """Append the `… N more characters` control to `box` (additive sibling).

    The control is deliberately a SIBLING of the body label, inside the body
    box: `_text_label`/`_body_label` must stay the text label itself (Phase 4
    Part A's in-place update seam depends on it), and the action row must not be
    covered by a control the approver has to reach past.

    The click handler reads the box's CURRENT state (full text + label), so a
    later in-place update that swaps the body cannot leave the button revealing
    stale text.
    """
    button = Gtk.Button()
    button.add_css_class("feed-body-reveal")
    button.set_halign(Gtk.Align.START)
    button.set_valign(Gtk.Align.START)
    _apply_reveal_tooltip(button, needs_approval)

    def _on_clicked(_button: Gtk.Button) -> None:
        _apply_text_markup(label, getattr(box, "_body_full_text", "") or "")
        box._body_revealed = True
        _button.set_visible(False)

    button.connect("clicked", _on_clicked)
    box.append(button)
    box._reveal_button = button
    return button


def _set_body_text(
    box: Gtk.Box, label: Gtk.Label, text: str, *, needs_approval: bool = False
) -> bool:
    """Apply the render cap to `label` (+ reveal control) inside `box`.

    Returns True when the text was truncated for rendering. `box._body_full_text`
    always holds the STORED text (never the rendered slice) so the reveal action
    and the in-place update path both work from the real body.
    """
    text = text or ""
    box._body_full_text = text
    box._body_revealed = False
    button = getattr(box, "_reveal_button", None)

    if len(text) <= RENDERED_BODY_LIMIT:
        _apply_text_markup(label, text)
        if button is not None:
            button.set_visible(False)
        return False

    _apply_text_markup(label, text[:RENDERED_BODY_LIMIT])
    hidden = len(text) - RENDERED_BODY_LIMIT
    if button is None:
        button = _install_reveal_control(box, label, needs_approval=needs_approval)
    else:
        button.set_visible(True)
    button.set_label(_reveal_label(hidden))
    _apply_reveal_tooltip(button, needs_approval)
    return True


def _refresh_body_text(
    box: Gtk.Box, label: Gtk.Label, text: str, *, needs_approval: bool = False
) -> bool:
    """Re-evaluate the render cap on an in-place update (Edit B).

    Sticky-reveal applies to the SAME text only: an already-revealed card that
    receives the identical body stays expanded, while a NEW oversized body
    resets to truncated with a fresh hidden count. Returns True when truncated.
    """
    text = text or ""
    if (getattr(box, "_body_revealed", False)
            and getattr(box, "_body_full_text", None) == text):
        return len(text) > RENDERED_BODY_LIMIT
    return _set_body_text(box, label, text, needs_approval=needs_approval)


def _apply_text_markup(label: Gtk.Label, text: str) -> None:
    """Set body text on a Gtk.Label through the Pango markup guard.

    Shared by the initial render (`_render_text_body`) and the Phase 4
    in-place update path (`update_card_in_place`) so a refreshed card body
    goes through exactly the same validation/fallback behaviour as a
    freshly built one.
    """
    escaped = escape_for_pango(text)
    # Pre-validate markup before set_markup to avoid Gtk-WARNING terminal spam
    # and empty-label content loss. If Pango rejects the markup (unknown attrs
    # from JSX/TSX that escape_for_pango intentionally escaped), fall back to
    # set_text so the raw text is visible. See commit 898062a post-mortem:
    # docs/post-mortems/2026-07-31-PANGO-MARKUP-GUARD-POST-MORTEM.md
    try:
        Pango.parse_markup(escaped, -1, "\x00")
        label.set_markup(escaped)
    except Exception:
        # Trade-off: for a single text block (not composite markup), falling
        # back to set_text is acceptable — we lose Pango formatting but the
        # content is still readable and not silently destroyed.
        label.set_text(text)


def _render_text_body(text: str, mono: bool, *,
                      needs_approval: bool = False) -> Gtk.Widget:
    """Render body text as a label (monospace or normal).

    Rendered through `_set_body_text`, so the body is capped at
    `RENDERED_BODY_LIMIT` with a reveal control when longer (UIRESP2-T2 §1).
    """
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.add_css_class("feed-card-body")

    if not text or not text.strip():
        spacer = Gtk.Label()
        spacer.set_text(" ")
        spacer.set_size_request(-1, 8)
        box.append(spacer)
        # Phase 4 Part A: expose the body label for in-place updates.
        box._text_label = spacer
        # A placeholder label carries no wrap config, so it cannot host a real
        # body in place — update_card_in_place rebuilds instead (Edit B).
        box._body_is_placeholder = True
        box._body_full_text = ""
        return box

    label = Gtk.Label()
    label.set_xalign(0)
    label.set_wrap(True)
    label.set_wrap_mode(1)  # Pango.WrapMode.WORD_CHAR
    label.set_selectable(True)
    label.set_can_focus(False)
    if mono:
        label.add_css_class("feed-body-mono")
    box.append(label)
    _set_body_text(box, label, text, needs_approval=needs_approval)
    # Phase 4 Part A: expose the body label for in-place updates.
    box._text_label = label
    return box


def _render_file_event_body(card_data: FeedCardData) -> Gtk.Widget:
    """Render body for file_created/file_deleted/dir_created events."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.add_css_class("feed-card-body")

    # File/dir path with icon
    path = card_data.file_path or ""
    icon = "📁" if card_data.card_type == "dir_created" else "📄"
    path_label = Gtk.Label()
    path_label.set_markup(xml_template(f"{icon} <b>{{path}}</b>", path=path))
    path_label.set_xalign(0)
    path_label.set_wrap(False)
    path_label.set_ellipsize(Pango.EllipsizeMode.START)
    path_label.set_max_width_chars(40)
    path_label.set_selectable(True)
    path_label.set_can_focus(False)
    path_label.add_css_class("feed-body-path")
    box.append(path_label)

    # Body description if present
    if card_data.body and card_data.body.strip():
        desc_label = Gtk.Label()
        desc_label.set_xalign(0)
        desc_label.set_wrap(True)
        desc_label.set_wrap_mode(1)
        desc_label.set_selectable(True)
        desc_label.set_can_focus(False)
        desc_label.set_margin_top(4)
        desc_label.add_css_class("feed-body-desc")
        box.append(desc_label)
        # Same render cap as text bodies (UIRESP2-T2 §1). The box DOES expose
        # `_text_label` (set below) when a non-empty body exists at build time,
        # so update_card() can refresh the description in place; an empty-body
        # file-event card leaves no seam and still falls back to rebuild
        # (MEMRATCHET §2.3).
        _set_body_text(
            box, desc_label, card_data.body,
            needs_approval=bool((card_data.metadata or {}).get("needs_approval")),
        )
        box._text_label = desc_label

    return box


def _render_task_body(card_data: FeedCardData) -> Gtk.Widget:
    """Render body for task cards."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.add_css_class("feed-card-body")

    # Task title
    title_label = Gtk.Label()
    title_label.set_markup(xml_template("<b>📋 {title}</b>", title=card_data.title))
    title_label.set_xalign(0)
    title_label.set_wrap(True)
    title_label.set_wrap_mode(1)
    title_label.set_selectable(True)
    title_label.set_can_focus(False)
    box.append(title_label)

    # Body (task description or status)
    if card_data.body and card_data.body.strip():
        body_label = Gtk.Label()
        body_label.set_xalign(0)
        body_label.set_wrap(True)
        body_label.set_wrap_mode(1)
        body_label.set_selectable(True)
        body_label.set_can_focus(False)
        body_label.set_margin_top(4)
        box.append(body_label)
        # `body=` sweep: the work-unit status line is generated (bounded today)
        # but funnels through the same cap so a future long body cannot slip past.
        _set_body_text(box, body_label, card_data.body)
        # MEMRATCHET §2.3: expose the seam when a non-empty body exists at build
        # time, so update_card() refreshes the task body in place; an empty-body
        # task card leaves no seam and falls back to rebuild.
        box._text_label = body_label

    # Task ID if present
    if card_data.task_id:
        id_label = Gtk.Label()
        id_label.set_markup(xml_template("<span foreground='#9b9bab'>ID: {task_id}</span>", task_id=card_data.task_id))
        id_label.set_xalign(0)
        id_label.set_margin_top(2)
        box.append(id_label)

    return box


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
            # Render diff lines with +/- color coding
            # Single label approach — see below for rendering

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


# ─────────────────────────────────────────────────────────────────────────────
# Main card factory
# ─────────────────────────────────────────────────────────────────────────────

def build_feed_card(
    card_data: FeedCardData,
    *,
    on_review: Callable[[str, Gtk.Widget], None],
    on_accept: Callable[[str], None],
    on_reject: Callable[[str], None],
    on_copy: Callable[[str], None],
) -> Gtk.Widget:
    """
    Build a complete feed card widget from FeedCardData.

    Returns a Gtk.Box containing:
      - Header: colored bar with title + copy button (via css_class_for_type)
      - Body: content area with card-specific rendering
      - Footer: author • timestamp + Review/Accept/Reject buttons
      - Context panel: expandable section (if snapshot present, hidden by default)

    on_review callback receives (card_id: str, card_widget: Gtk.Widget).
    All other callbacks receive (card_id: str) except on_copy which receives (text: str).
    """
    card_id = card_data.card_id or ""

    # Root card box
    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    card.add_css_class("feed-card")
    card.add_css_class(FeedCardData.css_class_for_type(card_data.card_type))

    # Set accepted/rejected visual state
    if card_data.accepted is True:
        card.add_css_class("feed-card-accepted")
    elif card_data.accepted is False:
        card.add_css_class("feed-card-rejected")

    # ── Header ─────────────────────────────────────────────────────────
    header, _copy_btn = _make_feed_card_header(
        card_data.title,
        card_data.card_type,
        card_data,
        on_copy,
    )
    card.append(header)

    # ── Body ───────────────────────────────────────────────────────────
    body_widget = _render_body(card_data)
    card.append(body_widget)
    # Phase 4 Part A (spec §2.4): expose the body label so
    # update_card_in_place() can mutate the live widget instead of
    # rebuilding the whole card. Set by every body renderer that has a single
    # text label AND a non-empty body at build time (status/text, chat,
    # file-event, task); an empty-body file-event/task card yields no seam,
    # and an empty TEXT body yields a placeholder seam
    # (`_body_is_placeholder`) that update_card_in_place refuses — both
    # fall back to rebuild (MEMRATCHET §2.3).
    card._body_label = getattr(body_widget, "_text_label", None)
    # UIRESP2-T2 Edit B: the body CONTAINER is the seam that hosts the reveal
    # control, so the in-place path can re-evaluate truncation.
    card._body_box = body_widget

    # ── Footer: author • timestamp ─────────────────────────────────────
    footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    footer.add_css_class("feed-card-footer")

    ts_str = _format_timestamp(card_data.timestamp)
    meta_text = f"{card_data.author} • {ts_str}"
    meta_label = Gtk.Label(label=meta_text)
    meta_label.set_halign(Gtk.Align.START)
    meta_label.set_hexpand(True)

    footer.append(meta_label)

    # Accepted/rejected badge
    badge = None
    if card_data.accepted is True:
        badge = Gtk.Label(label="ACCEPTED")
        badge.add_css_class("feed-accepted-badge")
        footer.append(badge)
    elif card_data.accepted is False:
        badge = Gtk.Label(label="REJECTED")
        badge.add_css_class("feed-rejected-badge")
        footer.append(badge)

    card.append(footer)
    # Phase 4 Part A: expose the status badge (None when undecided) for
    # in-place updates via update_card_badge()/update_card_in_place().
    card._status_label = badge

    # ── Action buttons (conditional visibility) ────────────────────
    is_resolved = card_data.accepted is not None
    is_actionable = FeedCardData.is_actionable(
        card_data.card_type, card_data.metadata
    )
    is_informational = FeedCardData.is_informational(
        card_data.card_type, card_data.metadata
    )

    if is_actionable and not is_resolved:
        # Show full action row (Review + Accept/Reject or Approve/Deny)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        actions.add_css_class("feed-card-actions")
        actions.set_spacing(6)

        btn_review = Gtk.Button(label="Review")
        btn_review.add_css_class("feed-btn-review")
        btn_review.connect("clicked", lambda _, cid=card_id, w=card: on_review(cid, w))
        actions.append(btn_review)

        # For approval cards: "Approve" / "Deny" labels
        # For file-change cards: "Accept" / "Reject" labels
        btn_accept = Gtk.Button(label="Approve" if card_data.metadata.get("needs_approval") else "Accept")
        btn_accept.add_css_class("feed-btn-accept")
        btn_accept.connect("clicked", lambda _, cid=card_id: on_accept(cid))
        actions.append(btn_accept)

        btn_reject = Gtk.Button(label="Deny" if card_data.metadata.get("needs_approval") else "Reject")
        btn_reject.add_css_class("feed-btn-reject")
        btn_reject.connect("clicked", lambda _, cid=card_id: on_reject(cid))
        actions.append(btn_reject)

        card.append(actions)

    elif is_actionable and is_resolved:
        # Resolved actionable card: show Review button only
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        actions.add_css_class("feed-card-actions")
        actions.set_spacing(6)

        btn_review = Gtk.Button(label="Review")
        btn_review.add_css_class("feed-btn-review")
        btn_review.connect("clicked", lambda _, cid=card_id, w=card: on_review(cid, w))
        actions.append(btn_review)

        card.append(actions)

    # Informational cards: NO action buttons at all

    # ── Sub-state CSS classes for agent_action cards ────────────────────
    if card_data.card_type == "agent_action":
        if card_data.metadata.get("needs_approval"):
            card.add_css_class("feed-card-approval")
        elif card_data.metadata.get("status") == "running":
            card.add_css_class("feed-card-running")
        elif card_data.metadata.get("status") == "complete":
            card.add_css_class("feed-card-complete")
        elif card_data.metadata.get("status") == "error":
            card.add_css_class("feed-card-error")

    # ── Context panel (expandable, hidden by default) ────────────────────
    if card_data.conversation_snapshot is not None:
        context_panel = build_context_panel(card_data.conversation_snapshot)
    else:
        # Placeholder panel — will be populated by _finalize_snapshot via idle_add
        context_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        context_panel.add_css_class("feed-context-panel")
    context_panel.set_visible(False)
    card.append(context_panel)
    card._context_panel = context_panel

    return card


# ─────────────────────────────────────────────────────────────────────────────
# Empty state widget
# ─────────────────────────────────────────────────────────────────────────────

def build_empty_feed_widget() -> Gtk.Widget:
    """
    Build the empty state widget shown when the feed has no cards.

    Returns a Gtk.Box with centered text: "No activity yet"
    CSS class: .feed-empty
    """
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.add_css_class("feed-empty")
    box.set_valign(Gtk.Align.CENTER)
    box.set_halign(Gtk.Align.CENTER)

    icon = Gtk.Label(label="📭")
    icon.set_valign(Gtk.Align.CENTER)
    icon.set_halign(Gtk.Align.CENTER)
    icon.set_margin_bottom(12)
    icon.set_size_request(48, 48)

    msg = Gtk.Label()
    msg.set_text("No activity yet")
    msg.set_valign(Gtk.Align.CENTER)
    msg.set_halign(Gtk.Align.CENTER)
    msg.add_css_class("feed-empty-text")

    hint = Gtk.Label()
    hint.set_text("Agent actions will appear here")
    hint.set_valign(Gtk.Align.CENTER)
    hint.set_halign(Gtk.Align.CENTER)
    hint.set_margin_top(4)
    hint.add_css_class("feed-empty-hint")

    box.append(icon)
    box.append(msg)
    box.append(hint)

    return box


# ─────────────────────────────────────────────────────────────────────────────
# Post-construction badge update
# ─────────────────────────────────────────────────────────────────────────────

def update_card_badge(card_widget: Gtk.Widget, accepted: bool | None) -> None:
    """
    Update the ACCEPTED/REJECTED badge on an existing feed card widget.

    Called by FeedHandler after accept/reject actions to inject the badge
    into the footer without rebuilding the entire card.

    Args:
        card_widget: The root Gtk.Box returned by build_feed_card().
        accepted:    True → ACCEPTED badge, False → REJECTED badge, None → no badge.
    """
    # Card structure: index 0=header, 1=body, 2=footer, 3=actions
    footer = card_widget.get_first_child()  # header
    if footer is None:
        return
    footer = footer.get_next_sibling()  # body
    if footer is None:
        return
    footer = footer.get_next_sibling()  # footer
    if footer is None:
        return

    # Remove any existing badge (last child of footer if it has a badge CSS class)
    child = footer.get_first_child()
    while child is not None:
        next_child = child.get_next_sibling()
        css_classes = child.get_css_classes()
        if "feed-accepted-badge" in css_classes or "feed-rejected-badge" in css_classes:
            footer.remove(child)
        child = next_child

    # Add new badge if applicable
    badge = None
    if accepted is True:
        badge = Gtk.Label(label="ACCEPTED")
        badge.add_css_class("feed-accepted-badge")
        footer.append(badge)
    elif accepted is False:
        badge = Gtk.Label(label="REJECTED")
        badge.add_css_class("feed-rejected-badge")
        footer.append(badge)
    # Phase 4 Part A: keep the exposed status-badge ref in sync so a later
    # in-place update sees the badge that is actually in the footer.
    card_widget._status_label = badge


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4 Part A — in-place card update
# ─────────────────────────────────────────────────────────────────────────────

# State CSS classes build_feed_card derives from card_data (accepted +
# agent_action metadata). Kept as module constants so the in-place refresh
# and the initial build cannot drift apart silently.
_ACCEPTED_STATE_CLASSES = ("feed-card-accepted", "feed-card-rejected")
_AGENT_ACTION_STATE_CLASSES = (
    "feed-card-approval",
    "feed-card-running",
    "feed-card-complete",
    "feed-card-error",
)


def _refresh_card_state_classes(card_widget: Gtk.Widget, card_data: FeedCardData) -> None:
    """Re-apply the CSS classes build_feed_card derives from card_data.

    Removes every state class first so a resolved/complete card cannot keep
    a stale "running"/"accepted" class. Mirrors the class logic in
    build_feed_card exactly.
    """
    for css in _ACCEPTED_STATE_CLASSES + _AGENT_ACTION_STATE_CLASSES:
        card_widget.remove_css_class(css)

    if card_data.accepted is True:
        card_widget.add_css_class("feed-card-accepted")
    elif card_data.accepted is False:
        card_widget.add_css_class("feed-card-rejected")

    if card_data.card_type != "agent_action":
        return

    metadata = card_data.metadata or {}
    if metadata.get("needs_approval"):
        card_widget.add_css_class("feed-card-approval")
    elif metadata.get("status") == "running":
        card_widget.add_css_class("feed-card-running")
    elif metadata.get("status") == "complete":
        card_widget.add_css_class("feed-card-complete")
    elif metadata.get("status") == "error":
        card_widget.add_css_class("feed-card-error")


def update_card_in_place(card_widget: Gtk.Widget, card_data: FeedCardData) -> bool:
    """Refresh an existing feed card widget in place from updated card data.

    Phase 4 Part A (SPEC-UI-RESPONSIVENESS-2 §2.4): mutate the body label,
    the accepted/rejected badge, and the state CSS classes by reference
    instead of rebuilding the whole card (build_feed_card + FeedTab
    remove/insert on the main thread).

    UIRESP2-T2 Edit B: the body is written through the render cap, so the
    refreshed card is truncated + revealable exactly like a freshly built one,
    and a card that was revealed resets to truncated when a NEW body arrives.

    Must be called on the GTK main thread.

    Args:
        card_widget: The root Gtk.Box returned by build_feed_card().
        card_data:   The updated card data.

    Returns:
        True when the widget exposes the body seam (`_body_label`) and was
        refreshed in place. The body renderers set `_text_label` whenever a
        non-empty body exists at build time (status/text, chat, file-event,
        task), so `_body_label` is non-None for those. Empty-body
        file-event/task cards yield no seam; an empty TEXT body yields a
        placeholder seam (`_body_is_placeholder`) which this function
        refuses — both fall back to a full rebuild (MEMRATCHET §2.3).
    """
    body_label = getattr(card_widget, "_body_label", None)
    if body_label is None:
        return False

    body_box = getattr(card_widget, "_body_box", None)
    new_text = card_data.body or ""
    needs_approval = bool((card_data.metadata or {}).get("needs_approval"))

    if body_box is None or not hasattr(body_box, "_text_label"):
        # No container seam to host a reveal control: only safe for text short
        # enough that no control is needed (otherwise rebuild — Edit B).
        if len(new_text) > RENDERED_BODY_LIMIT:
            return False
        _apply_text_markup(body_label, new_text)
    elif getattr(body_box, "_body_is_placeholder", False) and new_text.strip():
        # An empty body rendered a spacer label with no wrap/select config;
        # hosting a real body there would render it as one unwrapped line.
        return False
    else:
        _refresh_body_text(body_box, body_label, new_text,
                           needs_approval=needs_approval)

    _refresh_card_state_classes(card_widget, card_data)
    update_card_badge(card_widget, card_data.accepted)
    return True