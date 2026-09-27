# ui/views/event_cards.py
# Event-card widget factories — relocated VERBATIM from the retired Pango
# bubble module (donor deleted in SP5c-2 B.2; see git history for lineage).
# It is the Pango event-card pipeline's home (ChatRenderHandler.render_event_card
# — the five builders' only consumer).
#
# Relocated verbatim: build_role_bubble, create_file_card, create_edit_card,
# create_tool_card, create_error_bubble, build_streaming_bubble (test-only
# consumer), and their transitive private helpers (process_segments,
# _process_text_chunk, _add_action_buttons, _build_code_from_markup,
# _build_image_block, _build_segment_widget, _build_table_segment,
# _make_table_cell, _build_text_segment, _make_block_header,
# _build_quote_segment, _build_terminal_segment, _build_heading_segment,
# _build_task_segment, _build_crabcard_placeholder_segment, LOW-7 viewer
# helpers, _copy_to_clipboard).
#
# SP5c-2 B.0 (supervisor ruling): the _crabcards_registry machinery was
# DELETED here rather than relocated — crabcard interception wiring died with
# SP4, the registry had zero live consumers (grep-verified), and
# _set/_clear_crabcards_registry died with the donor file. The placeholder
# segment always renders its static fallback label (identical behavior).
#
# Security: No secrets, no network calls.
# Pure GTK widget construction; no GTK calls until after app activation.
# Image blocks use os.path.isfile() for validation and subprocess for
# click-to-open (LOW-7 root-scoping preserved verbatim).

import os

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Pango', '1.0')
from gi.repository import Gtk, Pango, Gdk

from utils.escaping import escape_for_pango, xml_escape_text, xml_template
from utils.config import get_project_root
from utils.markdown import format_markdown
from utils.block_parser import extract_blocks
from utils.gtk_safe_link import make_safe_label, on_activate_link  # HIGH-6: activate-link guard

# SP5c-2 B.0 (supervisor ruling): the crabcard placeholder registry is GONE.
# Crabcard interception wiring died with SP4; the placeholder segment below
# always renders its static fallback label (the registry was never populated
# in production — zero consumers, grep-verified; donor module deleted B.2).
from utils.syntax_highlight import highlight

import shutil
import subprocess

# LOW-7: paths outside these roots are rejected to prevent the LLM from
# tricking the user into opening /etc/passwd or similar sensitive files.
_ALLOWED_ROOTS_FALLBACK = (
    os.path.expanduser("~"),
    "/tmp",
)


def _get_allowed_roots() -> tuple[str, ...]:
    """LOW-7: compute the set of allowed root paths for the image viewer.

    Reads CRABCAKES_ACTIVE_PROJECT_PATH env var if set, plus the fallback
    roots (home + /tmp). The active project path is passed by the handler
    that owns the chat bubble, via os.environ (ui/handlers/ sets this when
    a project is active).
    """
    roots: list[str] = []
    project = os.environ.get("CRABCAKES_ACTIVE_PROJECT_PATH", "").strip()
    if project:
        roots.append(project)
    roots.extend(_ALLOWED_ROOTS_FALLBACK)
    return tuple(roots)


def _is_path_in_allowed_roots(file_path: str) -> bool:
    """LOW-7: return True if file_path is under one of the allowed roots.

    Resolves symlinks via realpath. If the resolved path is not under any
    allowed root, returns False.
    """
    try:
        resolved = os.path.realpath(file_path)
    except OSError:
        return False
    for root in _get_allowed_roots():
        try:
            root_resolved = os.path.realpath(root)
        except OSError:
            continue
        try:
            common = os.path.commonpath([resolved, root_resolved])
        except ValueError:
            # Different drives on Windows
            continue
        if common == root_resolved:
            return True
    return False


def _open_in_viewer(file_path: str) -> None:
    """Open file_path in the system's default image viewer.

    LOW-7: only opens files inside an allowed root (active project, home, /tmp).
    Logs a warning if the path is rejected.
    """
    if not file_path or not os.path.isfile(file_path):
        return
    if not _is_path_in_allowed_roots(file_path):
        import logging
        logging.getLogger(__name__).warning(
            "LOW-7: refusing to open %r — not under any allowed root",
            file_path,
        )
        return
    opener = shutil.which("xdg-open") or shutil.which("open") or "xdg-open"
    try:
        # Use list-form (no shell), and pass only the file path (no extra args)
        # to avoid argument injection.
        subprocess.Popen([opener, file_path])
    except (OSError, ValueError):
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def process_segments(text: str) -> list[dict]:
    """
    Pre-process raw message text into a list of render-ready segment dicts.
    Pure Python — no GTK calls. Safe to run on a background thread.

    Each segment dict has a 'type' key and processed content:
      - text segments: 'markup' key with Pango-formatted string
      - code segments: 'code_markup' with highlighted Pango, 'raw_content' for copy
      - crabcard_placeholder segments: 'index' key — rendered as feed reference chip
      - other segments (quote, terminal, heading, task): 'content' passthrough

    Groups consecutive text segments for unified rendering (same as build_role_bubble).
    """
    # Phase 3: Pre-scan for crabcard placeholder markers (\x00CRABCARD_REF:N\x00)
    # These are embedded in cleaned_text by extract_crabcards(). We split them
    # out into dedicated segments so build_role_bubble can render feed chips.
    parts = text.split("\x00CRABCARD_REF:")
    processed = []

    # text_parts[0] is before first placeholder (no prefix to strip)
    # subsequent parts: "N\x00...rest" — strip N+trailing \x00, remainder is text

    for i, part in enumerate(parts):
        if i == 0:
            # First part — before any placeholder. Process normally.
            _process_text_chunk(part, processed)
            continue

        # Extract index from "N\x00..."
        null_pos = part.find("\x00")
        if null_pos < 0:
            # Malformed (no closing null) — treat entire part as text
            _process_text_chunk("\x00CRABCARD_REF:" + part, processed)
            continue

        try:
            card_index = int(part[:null_pos])
        except ValueError:
            _process_text_chunk("\x00CRABCARD_REF:" + part, processed)
            continue

        # Emit placeholder segment
        processed.append({"type": "crabcard_placeholder", "index": card_index})

        # Rest of part (after \x00) is normal text — process it
        rest = part[null_pos + 1:]
        _process_text_chunk(rest, processed)

    return processed


def _process_text_chunk(text_chunk: str, processed: list) -> None:
    """
    Extract blocks from a text chunk and append processed segments to `processed`.
    Merges consecutive text segments before appending.
    """
    if not text_chunk.strip():
        return

    segments = extract_blocks(text_chunk)
    text_buf: list[str] = []

    def flush_text():
        if not text_buf:
            return
        joined = "\n".join(text_buf)
        escaped = escape_for_pango(joined)
        formatted = format_markdown(escaped)
        processed.append({"type": "text", "markup": formatted})
        text_buf.clear()

    for seg in segments:
        seg_type = seg.get("type", "text")
        if seg_type == "text":
            text_buf.append(seg.get("content", ""))
        else:
            flush_text()
            if seg_type == "code":
                lang = seg.get("lang", "")
                raw = seg.get("content", "")
                if lang == "image":
                    # Image block — path is the content, no syntax highlighting
                    processed.append({
                        "type": "image",
                        "file_path": raw.strip(),
                    })
                else:
                    code_markup = highlight(raw, lang)
                    processed.append({
                        "type": "code",
                        "code_markup": code_markup,
                        "lang": lang,
                        "raw_content": raw,
                    })
            else:
                # quote, terminal, heading, task — pass through raw content
                # table — pass through structured data (headers, rows)
                if seg_type == "table":
                    processed.append(seg)
                else:
                    processed.append({
                        "type": seg_type,
                        "content": seg.get("content", ""),
                        **({"lang": seg["lang"]} if "lang" in seg else {}),
                        **({"level": seg["level"]} if "level" in seg else {}),
                    })
    flush_text()


def build_role_bubble(role: str, text: str, on_forward_click=None, tight: bool = False, forwarded_from: str = None, session_key: str = None, agent_name: str = None, agent_color: str = None) -> Gtk.Widget:
    """
    Build a styled chat bubble for the given role and raw text.

    Handles both inline (Phase 1) and block-level (Phase 2) content:
      - Parses text into segments via extract_blocks()
      - Renders each segment with the appropriate widget factory
      - Wraps everything in a role-labeled bubble box

    Args:
        role: "You" for user messages (right-aligned), "Agent" otherwise
              (left-aligned, for agent responses)
        text: Raw message text — may contain markdown, code blocks, quotes, etc.)
        agent_name: Optional display name for the agent (used in header row).
                   If None, no header is shown.

    Returns:
        A Gtk.Widget (Gtk.Box) containing the bubble.
        Caller is responsible for adding it to the parent container.
    """
    # ── Outer container: sets horizontal alignment ─────────────────────
    container = Gtk.Box()
    container.set_halign(Gtk.Align.END if role == "You" else Gtk.Align.START)

    # ── Bubble box: vertical stack of segments + optional role label ───
    # Determine bubble CSS class: agent-colored if agent_color provided,
    # else fall back to role-based defaults.
    from models.colors import css_class_for_color
    if role == "System":
        bubble_css = "chat-bubble-System"
    elif role == "You":
        bubble_css = "chat-bubble-you"
    elif agent_color:
        bubble_css = css_class_for_color(agent_color, "agent-bg")
    else:
        bubble_css = "chat-bubble-agent"
    bubble = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        css_classes=[bubble_css],
    )
    bubble.set_margin_top(1 if tight else 4)
    bubble.set_margin_bottom(4)

    # Store role+text as custom attributes for conversation snapshot extraction.
    # FeedHandler._extract_messages_from_chat_box() reads these when walking the chat box children.
    # Attributes are on the container (the widget added to the chat box).
    container._crabcakes_role = role
    container._crabcakes_text = text

    # ── Header row: "Name ● HH:MM" ───────────────────────────────────
    # Always show for agents; show for You bubbles too (right-aligned)
    if agent_name or role == "You":
        from datetime import datetime
        timestamp = datetime.now().strftime("%H:%M")
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header.add_css_class("chat-bubble-header")
        header.set_spacing(4)
        header.set_margin_bottom(2)
        header.set_hexpand(role == "You")
        header.set_halign(Gtk.Align.END if role == "You" else Gtk.Align.START)

        display_name = agent_name if agent_name else "You"
        name_label = Gtk.Label(label=display_name)
        name_label.add_css_class("chat-bubble-header-name")
        name_label.set_halign(Gtk.Align.START)

        dot = Gtk.Box()
        dot.set_size_request(6, 6)
        dot_css = css_class_for_color(agent_color, "agent-dot") if agent_color and role == "Agent" else "chat-bubble-header-dot"
        dot.add_css_class(dot_css)
        dot.set_valign(Gtk.Align.CENTER)

        time_label = Gtk.Label(label=timestamp)
        time_label.add_css_class("chat-bubble-header-time")
        time_label.set_halign(Gtk.Align.START)

        header.append(name_label)
        header.append(dot)
        header.append(time_label)
        bubble.append(header)

    # ── Pre-process text (pure Python, can run off-thread) ────────────
    if forwarded_from:
        text = f"[Forwarded from {forwarded_from}]\n{text}"
    raw_text = text  # keep original for copy button
    processed = process_segments(text)

    # ── Assemble GTK widgets from processed segments ──────────────────
    for pseg in processed:
        seg_type = pseg.get("type", "text")
        if seg_type == "text":
            # Pre-formatted Pango markup — create label directly
            markup = pseg.get("markup", "")
            if not markup.strip():
                bubble.append(Gtk.Box())  # empty spacer
                continue
            label = make_safe_label(markup, css_class="chat-msg-label")
            bubble.append(label)
        elif seg_type == "code":
            # Build code widget using pre-highlighted markup
            code_markup = pseg.get("code_markup", "")
            lang = pseg.get("lang", "")
            raw_content = pseg.get("raw_content", "")
            block = _build_code_from_markup(lang, code_markup, raw_content)
            if block is not None:
                bubble.append(block)
        elif seg_type == "image":
            # Build image block — same container as code block, image instead of text
            file_path = pseg.get("file_path", "")
            block = _build_image_block(file_path)
            if block is not None:
                bubble.append(block)
        else:
            # quote, terminal, heading, task, table — use segment builders
            if seg_type == "table":
                # Table has structured data (headers, rows), pass through as-is
                seg_dict = pseg
            else:
                seg_dict = {"type": seg_type, "content": pseg.get("content", "")}
                if "lang" in pseg:
                    seg_dict["lang"] = pseg["lang"]
                if "level" in pseg:
                    seg_dict["level"] = pseg["level"]
            widget = _build_segment_widget(seg_dict)
            if widget is not None:
                bubble.append(widget)

    # ── Action buttons (hover-to-reveal) ───────────────────────────────
    _add_action_buttons(bubble, raw_text, on_forward_click, session_key)

    container.append(bubble)
    return container


def _build_code_from_markup(lang: str, code_markup: str, raw_content: str) -> Gtk.Widget | None:
    """Build a code block widget from pre-highlighted Pango markup."""
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    outer.add_css_class("code-block")

    header, _ = _make_block_header(
        lang or "code",
        raw_content,
        "code-block-header",
    )
    outer.append(header)

    # Content box — expands to natural height, no scroll cap
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    content.add_css_class("code-block-content")

    code_label = Gtk.Label()
    # Pre-validate markup before set_markup to avoid Gtk-WARNING terminal spam
    # and empty-label content loss. If Pango rejects the markup, fall back to
    # set_text so the raw text is visible. See commit 898062a post-mortem:
    # docs/post-mortems/2026-07-31-PANGO-MARKUP-GUARD-POST-MORTEM.md
    try:
        Pango.parse_markup(code_markup, -1, "\x00")
        code_label.set_markup(code_markup)
    except Exception:
        code_label.set_text(raw_content)
    code_label.set_xalign(0)
    code_label.set_selectable(True)
    code_label.set_can_focus(False)
    code_label.set_wrap(True)
    code_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    code_label.set_max_width_chars(120)  # bound natural width — 120 chars

    content.append(code_label)
    outer.append(content)
    return outer


def _build_image_block(file_path: str) -> Gtk.Widget | None:
    """Build an image block widget — same container as code blocks, image instead of text.

    Uses the same code-block header (shows 'image') and content area structure.
    Content area contains a Gtk.Image instead of syntax-highlighted text.
    Click to open in system viewer.
    """
    if not file_path or not os.path.isfile(file_path):
        return None

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    outer.add_css_class("code-block")

    header, _ = _make_block_header(
        "image",
        file_path,
        "code-block-header",
    )
    outer.append(header)

    # Content box — same style as code blocks
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    content.add_css_class("code-block-content")

    img = Gtk.Image.new_from_file(file_path)
    img.add_css_class("chat-image")
    img.set_size_request(-1, 280)
    img.set_tooltip_text(os.path.basename(file_path))
    # Click to open in system viewer
    controller = Gtk.GestureClick()
    controller.connect("pressed", lambda _c, _n, _x, _y: _open_in_viewer(file_path))
    img.add_controller(controller)

    content.append(img)
    outer.append(content)
    return outer


def _add_action_buttons(bubble: Gtk.Box, raw_text: str, on_forward_click, session_key: str = None) -> None:
    """Add Copy + Forward action buttons to a bubble."""
    actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    actions.add_css_class("chat-bubble-actions")
    actions.set_spacing(4)

    copy_btn = Gtk.Button()
    copy_btn.add_css_class("flat")
    copy_btn.set_tooltip_text("Copy message")
    copy_btn.set_size_request(22, 22)
    copy_btn.set_opacity(0.3)
    try:
        copy_btn.set_child(Gtk.Image.new_from_file(
            os.path.join(get_project_root(), "ui", "icons", "copy.svg")))
    except Exception:
        copy_btn.set_label("📋")
    copy_btn.connect("clicked", lambda _, t=raw_text: _copy_to_clipboard(t))
    copy_motion = Gtk.EventControllerMotion()
    copy_motion.connect("enter", lambda _c, _x, _y: copy_btn.set_opacity(1.0))
    copy_motion.connect("leave", lambda _c: copy_btn.set_opacity(0.3))
    copy_btn.add_controller(copy_motion)

    fwd_btn = Gtk.Button()
    fwd_btn.add_css_class("flat")
    fwd_btn.set_tooltip_text("Forward to another agent")
    fwd_btn.set_size_request(22, 22)
    fwd_btn.set_opacity(0.3)
    try:
        fwd_btn.set_child(Gtk.Image.new_from_file(
            os.path.join(get_project_root(), "ui", "icons", "forward.svg")))
    except Exception:
        fwd_btn.set_label("↗")
    if on_forward_click:
        fwd_btn.connect("clicked", lambda btn, t=raw_text, sk=session_key: on_forward_click(t, btn, sk))
    else:
        # REACHABLE (SP5c-2 audit BUG #2 correction): CRH:851's thinking branch
        # builds role bubbles with no forward handler. The [event_cards]
        # constant rename from Phase A was grep hygiene, not dead-code cleanup.
        fwd_btn.connect("clicked", lambda _: print("[event_cards] forward (no handler)"))
    fwd_motion = Gtk.EventControllerMotion()
    fwd_motion.connect("enter", lambda _c, _x, _y: fwd_btn.set_opacity(1.0))
    fwd_motion.connect("leave", lambda _c: fwd_btn.set_opacity(0.3))
    fwd_btn.add_controller(fwd_motion)

    actions.append(copy_btn)
    actions.append(fwd_btn)
    bubble.append(actions)


# ─────────────────────────────────────────────────────────────────────────────
# Segment widget factories
# ─────────────────────────────────────────────────────────────────────────────

def _build_crabcard_placeholder_segment(seg: dict) -> Gtk.Widget | None:
    """
    Render a crabcard placeholder as a small static feed-reference chip:
    "📋 [card added to feed]" — non-interactive (no click handling).

    SP5c-2 B.0 (supervisor ruling): this ALWAYS renders the static fallback
    label. The crabcard interception wiring (module-level card registry +
    ChatRenderHandler's render-time extraction) died with SP4; the registry
    was never populated in production, so the static label is identical to
    the pre-deletion behavior.
    """
    card_index = seg.get("index")  # kept for segment-shape compatibility
    _ = card_index
    # Static fallback — no card lookup (registry deleted, B.0 ruling).
    label = Gtk.Label()
    label.set_text("📋 [card added to feed]")
    label.add_css_class("chat-msg-label")
    return label


def _build_segment_widget(seg: dict) -> Gtk.Widget | None:
    """Route a segment dict to the appropriate widget factory."""
    seg_type = seg.get("type", "text")

    if seg_type == "text":
        return _build_text_segment(seg)
    elif seg_type == "quote":
        return _build_quote_segment(seg)
    elif seg_type == "terminal":
        return _build_terminal_segment(seg)
    elif seg_type == "heading":
        return _build_heading_segment(seg)
    elif seg_type == "task":
        return _build_task_segment(seg)
    elif seg_type == "crabcard_placeholder":
        return _build_crabcard_placeholder_segment(seg)
    elif seg_type == "table":
        return _build_table_segment(seg)
    else:
        return None


def _build_table_segment(seg: dict) -> Gtk.Widget:
    """Render a markdown table as a GTK Grid with styled cells.

    Args:
        seg: Table segment with 'headers' (list[str]) and 'rows' (list[list[str]])

    Returns:
        Gtk.Box wrapping the grid with proper styling.
    """
    headers = seg.get("headers", [])
    rows = seg.get("rows", [])
    num_cols = len(headers)

    if num_cols == 0:
        return Gtk.Box()

    # Outer container
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    outer.add_css_class("table-block")

    grid = Gtk.Grid()
    grid.add_css_class("table-grid")
    grid.set_column_spacing(1)
    grid.set_row_spacing(1)

    # Header row
    for col, header_text in enumerate(headers):
        cell = _make_table_cell(header_text, is_header=True)
        grid.attach(cell, col, 0, 1, 1)

    # Data rows
    for row_idx, row_data in enumerate(rows):
        for col, cell_text in enumerate(row_data):
            is_odd = (row_idx % 2 == 1)
            cell = _make_table_cell(cell_text, is_header=False, is_odd_row=is_odd)
            grid.attach(cell, col, row_idx + 1, 1, 1)

    outer.append(grid)
    return outer


def _make_table_cell(text: str, is_header: bool = False, is_odd_row: bool = False) -> Gtk.Widget:
    """Create a single table cell widget.

    Supports inline markdown (bold, italic, code, links) in cell text.
    """
    label = Gtk.Label()
    # Apply inline markdown formatting (escape first, then format)
    escaped = escape_for_pango(text)
    formatted = format_markdown(escaped)
    # HIGH-6: use make_safe_label so links are gated on scheme allowlist
    label = make_safe_label(formatted)
    label.set_valign(Gtk.Align.CENTER)

    # Styling
    if is_header:
        label.add_css_class("table-cell-header")
    else:
        label.add_css_class("table-cell")
        if is_odd_row:
            label.add_css_class("table-cell-alt")

    # Wrap in a box for padding control
    box = Gtk.Box()
    box.append(label)
    box.add_css_class("table-cell-box")
    return box


def _build_text_segment(seg: dict) -> Gtk.Widget:
    """Render a plain text segment with inline markdown formatting."""
    raw = seg.get("content", "")
    if not isinstance(raw, str):
        raw = ""
    if not raw.strip():
        return Gtk.Box()  # empty spacer

    # Order: 1. escape, 2. markdown.  Escape first so that literal < > in the
    # original text don't corrupt the Pango tags that format_markdown produces.
    escaped = escape_for_pango(raw)
    formatted = format_markdown(escaped)
    # HIGH-6: make_safe_label wires the activate-link handler so non-allowlisted
    # schemes (javascript:, file://, custom URIs, etc.) cannot be opened.
    label = make_safe_label(formatted, css_class="chat-msg-label")
    return label


# ── Block widget helpers ────────────────────────────────────────────────────────

def _make_block_header(
    label_text: str,
    content_for_copy: str,
    header_css: str,
    copy_btn_css: str = "code-copy-btn",
) -> tuple[Gtk.Box, Gtk.Button]:
    """
    Shared header bar factory for code/terminal block widgets.

    Returns (header_box, copy_btn) so callers can optionally suppress or
    re-style the copy button. The button is pre-wired — callers just ignore
    the reference if they don't need it.

    Args:
        label_text:       Text for the left-side label (e.g. "python" or "$ terminal")
        content_for_copy: String passed to _copy_to_clipboard when Copy is clicked
        header_css:      CSS class for the header row (e.g. "code-block-header")
        copy_btn_css:    CSS class for the copy button (default "code-copy-btn")

    Returns:
        (header_box, copy_btn) — header is fully built, copy_btn is wired
    """
    header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    header.add_css_class(header_css)

    label = Gtk.Label()
    label.set_text(f"\u2007{label_text}")  #   = figure space ≈ 1 char width
    label.set_xalign(0)
    label.set_hexpand(True)
    label.add_css_class("code-lang-label")

    copy_btn = Gtk.Button(label="Copy")
    copy_btn.add_css_class(copy_btn_css)
    copy_btn.connect("clicked", lambda _: _copy_to_clipboard(content_for_copy))

    header.append(label)
    header.append(copy_btn)
    return header, copy_btn


# ── Segment widget factories ──────────────────────────────────────────────────

# _build_code_segment removed — code blocks now rendered via _build_code_from_markup()
# in the process_segments() → build_role_bubble() pipeline (commit 37bc5cc refactor).


def _build_quote_segment(seg: dict) -> Gtk.Widget:
    """Render a blockquote with left border and italic muted text."""
    content = seg.get("content", "")
    if not isinstance(content, str):
        content = ""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.add_css_class("blockquote")

    # Order: 1. escape, 2. markdown.  Escape first so that literal < > in
    # the original text don't corrupt the Pango tags that format_markdown produces.
    escaped = escape_for_pango(content)
    formatted = format_markdown(escaped)
    # HIGH-6 (Phase 6.1): use make_safe_label so links are gated on scheme
    # allowlist. The blockquote path was missed in Phase 6 commit 593391e.
    label = make_safe_label(formatted, css_class="blockquote-text")
    box.append(label)
    return box


def _build_terminal_segment(seg: dict) -> Gtk.Widget:
    """
    Render a terminal block with amber left border and $ prefix on lines.

    Each line is split into a prompt label (hardcoded markup, no user input)
    and a content label (routed through make_safe_label for HIGH-6 link safety
    and inline markdown support). This prevents the Bug #8 HIGH-6 regression
    where adding format_markdown to per-line content without make_safe_label
    would make javascript: links clickable with no guard.
    """
    content = seg.get("content", "")
    if not isinstance(content, str):
        content = ""
    if not content.strip():
        return Gtk.Box()  # empty spacer

    block = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    block.add_css_class("terminal-block")

    # Header with amber styling — uses shared header factory
    header, _copy_btn = _make_block_header(
        label_text="$ terminal",
        content_for_copy=content,
        header_css="terminal-header",
    )
    block.append(header)

    # Content lines — each prefixed with $ (or plain for continuation)
    content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    content_box.add_css_class("terminal-content")

    for line in content.split('\n'):
        # Line row: prompt label + content label, side by side
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)

        # Prompt: hardcoded markup (no user input) — safe to set_markup directly
        prompt = Gtk.Label()
        prompt.set_markup("<tt><span foreground=\"#e5c07b\">$</span></tt>")
        prompt.set_xalign(0)
        prompt.add_css_class("terminal-prompt")
        row.append(prompt)

        # Content: route through escape + format_markdown + make_safe_label.
        # This enables inline markdown (bold/italic/code/links) in terminal
        # output AND keeps the HIGH-6 activate-link guard connected (Bug #8).
        escaped_line = escape_for_pango(line)
        formatted_line = format_markdown(escaped_line)
        line_widget = make_safe_label(formatted_line, css_class="terminal-line")
        row.append(line_widget)

        content_box.append(row)

    block.append(content_box)
    return block


def _build_heading_segment(seg: dict) -> Gtk.Widget:
    """Render a heading with scaled font size and inline markdown."""
    # Guard level field against non-int types (None, str, list crash min()).
    try:
        level = int(seg.get("level", 1))
    except (TypeError, ValueError):
        level = 1
    level = min(max(level, 1), 4)  # clamp to [1, 4]
    content = seg.get("content", "")
    if not isinstance(content, str):
        content = ""
    if not content.strip():
        return Gtk.Box()  # empty spacer

    # Order: 1. escape, 2. markdown.  Same pattern as _build_text_segment.
    escaped = escape_for_pango(content)
    formatted = format_markdown(escaped)
    # HIGH-6: make_safe_label wires activate-link handler so non-allowlisted
    # schemes (javascript:, file://, custom URIs, etc.) cannot be opened
    # by clicking a [link](url) inside a heading.
    return make_safe_label(
        formatted,
        css_classes=["chat-heading", f"chat-heading-{level}"],
    )


def _build_task_segment(seg: dict) -> Gtk.Widget:
    """Render a task list item with checkbox character and inline markdown."""
    content = seg.get("content", "")
    if not isinstance(content, str):
        content = ""
    if not content.strip():
        return Gtk.Box()  # empty spacer
    # Replace [ ] / [x] with ☐ / ☑ checkbox characters
    content = content.replace('[ ]', '☐').replace('[x]', '☑').replace('[X]', '☑')
    # Order: 1. escape, 2. markdown.  Same pattern as _build_text_segment.
    escaped = escape_for_pango(content)
    formatted = format_markdown(escaped)
    # HIGH-6: make_safe_label wires activate-link handler for link safety.
    return make_safe_label(formatted, css_class="task-item")


# ─────────────────────────────────────────────────────────────────────────────
# Streaming bubble factory (Phase 3)
# ─────────────────────────────────────────────────────────────────────────────

def build_streaming_bubble(agent_name: str = "Agent") -> Gtk.Widget:
    """
    Build a streaming response bubble with an inline cursor (▍).

    The bubble starts with just the cursor and text is appended incrementally
    via update_streaming(). When streaming ends, the cursor is removed and
    the bubble is re-rendered as a final message.

    Args:
        agent_name: Display name for the agent role label.

    Returns:
        A tuple (container, label) where container goes in the chat box
        and label is the mutable text label the caller updates.
    """
    container = Gtk.Box()
    container.set_halign(Gtk.Align.START)

    bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    bubble.add_css_class("chat-bubble-pending")
    bubble.add_css_class("chat-bubble-agent")
    bubble.set_margin_top(4)
    bubble.set_margin_bottom(4)

    label = Gtk.Label()
    label.set_markup("<tt>▍</tt>")
    label.set_xalign(0)
    label.set_wrap(True)
    label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    label.set_max_width_chars(120)
    label.set_can_focus(False)
    label.set_selectable(True)
    label.add_css_class("chat-msg-label")
    # HIGH-6 (Bug #10): connect activate-link guard so javascript: links in
    # streamed markdown cannot be clicked during the live-streaming window.
    label.connect("activate-link", on_activate_link)
    bubble.append(label)

    container.append(bubble)
    return container, label


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Event card factories (Phase 4)
# ─────────────────────────────────────────────────────────────────────────────

def create_file_card(file_path: str, snippet: str = "", line_range: str = "") -> Gtk.Widget:
    """
    Build a file-read event card with 📄 icon, filename, optional line range, and snippet.

    """
    container = Gtk.Box()
    container.set_halign(Gtk.Align.START)

    bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    bubble.add_css_class("chat-bubble-agent")
    bubble.add_css_class("bubble-file-read")
    bubble.set_margin_top(4)
    bubble.set_margin_bottom(4)

    # Header: 📄 icon + file path + optional line range
    header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    header.add_css_class("code-block-header")

    icon_label = Gtk.Label(label="<b>📄 File read</b>")
    icon_label.set_markup("<b>📄 File read</b>")
    icon_label.set_xalign(0)
    path_label = Gtk.Label()
    path_label.set_markup(xml_template("<b>{file_path}</b>", file_path=file_path))
    path_label.set_xalign(0)
    path_label.set_hexpand(True)
    if line_range:
        lr_label = Gtk.Label(label=f"  {line_range}")
        lr_label.set_markup(xml_template("  <span foreground=\"#9b9bab\">{line_range}</span>", line_range=line_range))
        header.append(icon_label)
        header.append(path_label)
        header.append(lr_label)
    else:
        header.append(icon_label)
        header.append(path_label)
    bubble.append(header)

    # Snippet if provided
    if snippet:
        snippet_code = Gtk.Label()
        snippet_code.set_markup(xml_escape_text(snippet))
        snippet_code.set_xalign(0)
        snippet_code.set_wrap(True)
        snippet_code.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        snippet_code.set_max_width_chars(120)
        snippet_code.set_can_focus(False)
        snippet_code.set_selectable(True)
        snippet_code.add_css_class("code-block-content")
        snippet_code.set_margin_start(12)
        bubble.append(snippet_code)

    container.append(bubble)
    return container


def create_edit_card(file_path: str, diff: str = "") -> Gtk.Widget:
    """
    Build an edit-proposal event card with ✏️ icon, filename, and diff content.
    """
    container = Gtk.Box()
    container.set_halign(Gtk.Align.START)

    bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    bubble.add_css_class("chat-bubble-agent")
    bubble.add_css_class("bubble-edit-proposal")
    bubble.set_margin_top(4)
    bubble.set_margin_bottom(4)

    # Header: ✏️ icon + file path
    header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    header.add_css_class("code-block-header")
    icon_label = Gtk.Label()
    icon_label.set_markup("<b>✏️ Edit proposal</b>")
    icon_label.set_xalign(0)
    path_label = Gtk.Label()
    path_label.set_markup(xml_template("<b>{file_path}</b>", file_path=file_path))
    path_label.set_xalign(0)
    path_label.set_hexpand(True)
    header.append(icon_label)
    header.append(path_label)
    bubble.append(header)

    # Diff content
    if diff:
        diff_label = Gtk.Label()
        diff_label.set_markup(xml_escape_text(diff))
        diff_label.set_xalign(0)
        diff_label.set_wrap(True)
        diff_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        diff_label.set_max_width_chars(120)
        diff_label.set_can_focus(False)
        diff_label.set_selectable(True)
        diff_label.add_css_class("code-block-content")
        diff_label.set_margin_start(12)
        bubble.append(diff_label)

    container.append(bubble)
    return container


def create_tool_card(tool_name: str, detail: str = "") -> Gtk.Widget:
    """
    Build a tool-call event card with 🔧 icon and tool name.
    """
    container = Gtk.Box()
    container.set_halign(Gtk.Align.START)

    bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    bubble.add_css_class("chat-bubble-agent")
    bubble.add_css_class("bubble-tool-call")
    bubble.set_margin_top(4)
    bubble.set_margin_bottom(4)

    # Header: 🔧 icon + tool name
    header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    header.add_css_class("code-block-header")
    icon_label = Gtk.Label()
    icon_label.set_markup("<b>🔧 Tool call</b>")
    icon_label.set_xalign(0)
    name_label = Gtk.Label()
    name_label.set_markup(xml_template("<b>{tool_name}</b>", tool_name=tool_name))
    name_label.set_xalign(0)
    name_label.set_hexpand(True)
    header.append(icon_label)
    header.append(name_label)
    bubble.append(header)

    # Detail if provided
    if detail:
        detail_label = Gtk.Label()
        detail_label.set_markup(xml_escape_text(detail))
        detail_label.set_xalign(0)
        detail_label.set_wrap(True)
        detail_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        detail_label.set_max_width_chars(120)
        detail_label.set_can_focus(False)
        detail_label.set_selectable(True)
        detail_label.add_css_class("code-block-content")
        detail_label.set_margin_start(12)
        bubble.append(detail_label)

    container.append(bubble)
    return container


def create_error_bubble(error_msg: str) -> Gtk.Widget:
    """
    Build an error bubble with ❌ icon and error message.
    """
    container = Gtk.Box()
    container.set_halign(Gtk.Align.START)

    bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    bubble.add_css_class("chat-bubble-agent")
    bubble.add_css_class("bubble-error")
    bubble.set_margin_top(4)
    bubble.set_margin_bottom(4)


    # Header: ❌ icon + error label
    header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    header.add_css_class("code-block-header")
    icon_label = Gtk.Label()
    icon_label.set_markup("<b>❌ Error</b>")
    icon_label.set_xalign(0)
    header.append(icon_label)
    bubble.append(header)

    # Error message
    msg_label = Gtk.Label()
    msg_label.set_markup(xml_escape_text(error_msg))
    msg_label.set_xalign(0)
    msg_label.set_wrap(True)
    msg_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    msg_label.set_max_width_chars(120)
    msg_label.set_can_focus(False)
    msg_label.set_selectable(True)
    msg_label.set_margin_start(12)
    bubble.append(msg_label)

    container.append(bubble)
    return container

def _copy_to_clipboard(text: str):
    """Copy text to the system clipboard using GTK4 clipboard API."""
    display = Gdk.Display.get_default()
    if display is None:
        return
    clipboard = display.get_clipboard()
    clipboard.set(text)
