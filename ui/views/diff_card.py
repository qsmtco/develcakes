# ui/views/diff_card.py
# Diff card widget factories for display in project chat tabs.
# Pure view — no git calls, no state. All actions go through callbacks.

import os
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk

from utils.diff_parser import FileDiff, ParsedDiff, DiffHunk, DiffLine
from utils.escaping import xml_escape_text, xml_template
from utils.syntax_highlight import highlight


# Language mapping: file extension → Pygments lexer name
_EXTENSION_LANG_MAP: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".jsx": "javascript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".fish": "bash",
    ".ps1": "powershell",
    ".psm1": "powershell",
    ".sql": "sql",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    ".sass": "scss",
    ".less": "less",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".xml": "xml",
    ".toml": "toml",
    ".md": "markdown",
    ".markdown": "markdown",
    ".rst": "rst",
    ".tex": "latex",
    ".lua": "lua",
    ".r": "r",
    ".R": "r",
    ".clj": "clojure",
    ".cljs": "clojure",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".hrl": "erlang",
    ".fs": "fsharp",
    ".fsx": "fsharp",
    ".ml": "ocaml",
    ".mli": "ocaml",
    ".hs": "haskell",
    ".jl": "julia",
    ".nim": "nim",
    ".v": "verilog",
    ".vhd": "vhdl",
    ".vlog": "verilog",
    ".sv": "systemverilog",
    ".tf": "hcl",
    ".hcl": "hcl",
    ".dockerfile": "dockerfile",
    ".makefile": "makefile",
    ".mk": "makefile",
    ".cmake": "cmake",
    ".proto": "protobuf",
}


def get_lang_from_path(file_path: str | os.PathLike) -> str | None:
    """Infer language from file extension.

    Accepts str or os.PathLike (pathlib.Path).
    """
    # Guard: reject non-strings or empty input
    if isinstance(file_path, os.PathLike):
        file_path = os.fspath(file_path)
    if not isinstance(file_path, str) or not file_path:
        return None

    _, ext = os.path.splitext(file_path)
    if ext.lower() in _EXTENSION_LANG_MAP:
        return _EXTENSION_LANG_MAP[ext.lower()]

    basename = os.path.basename(file_path.rstrip("/\\")).lower()
    if basename in {"makefile", "gnumakefile"}:
        return "makefile"
    if basename == "dockerfile" or basename.startswith("dockerfile."):
        return "dockerfile"
    return None


def _build_diff_line(line_widget_box: Gtk.Box, line: DiffLine, lang: str | None) -> None:
    """Add a single diff line to the line box."""
    # Line number column (old)
    old_lbl = Gtk.Label(label=str(line.old_line_no) if line.old_line_no is not None else "")
    old_lbl.add_css_class("diff-line-number")
    old_lbl.set_halign(Gtk.Align.END)
    old_lbl.set_valign(Gtk.Align.CENTER)
    old_lbl.set_size_request(36, -1)
    old_lbl.set_margin_end(4)

    # Line number column (new)
    new_lbl = Gtk.Label(label=str(line.new_line_no) if line.new_line_no is not None else "")
    new_lbl.add_css_class("diff-line-number")
    new_lbl.set_halign(Gtk.Align.END)
    new_lbl.set_valign(Gtk.Align.CENTER)
    new_lbl.set_size_request(36, -1)
    new_lbl.set_margin_start(4)

    # Content
    # BUG #12: highlight raw content FIRST, then escape the highlighted output
    # (matching the event_cards code-segment pattern — highlight(raw, lang))
    if lang and line.type != "context":
        try:
            highlighted = highlight(line.content, lang)
            content_lbl = Gtk.Label(label=xml_escape_text(highlighted))
        except Exception:
            content_lbl = Gtk.Label(label=xml_escape_text(line.content))
    else:
        content_lbl = Gtk.Label(label=xml_escape_text(line.content))

    content_lbl.set_halign(Gtk.Align.START)
    content_lbl.set_valign(Gtk.Align.CENTER)
    content_lbl.set_selectable(True)
    content_lbl.set_xalign(0.0)

    line_widget_box.append(old_lbl)
    line_widget_box.append(new_lbl)
    line_widget_box.append(content_lbl)


def _build_hunk_view(hunk: DiffHunk, lang: str | None) -> Gtk.Widget:
    """Build a view for a single hunk."""
    vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

    # Hunk header
    hunk_header_lbl = Gtk.Label()
    hunk_header_lbl.add_css_class("diff-hunk-header")
    hunk_header_lbl.set_text(hunk.header)
    hunk_header_lbl.set_halign(Gtk.Align.START)
    hunk_header_lbl.set_margin_start(12)
    hunk_header_lbl.set_margin_end(12)
    hunk_header_lbl.set_margin_top(4)
    hunk_header_lbl.set_margin_bottom(2)
    vbox.append(hunk_header_lbl)

    # Hunk lines
    for line in hunk.lines:
        line_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        if line.type == "add":
            line_box.add_css_class("diff-line-add")
        elif line.type == "remove":
            line_box.add_css_class("diff-line-remove")
        else:
            line_box.add_css_class("diff-line-context")
        _build_diff_line(line_box, line, lang)
        vbox.append(line_box)

    return vbox


def render_diff_hunks(hunks: list[DiffHunk], lang: str | None = None) -> Gtk.Box:
    """Render diff hunks as a Gtk.Box. Internal helper extracted for future reuse.

    Pure renderer — does NOT handle binary files. Caller must check
    FileDiff.is_binary before calling and render the "Binary file — not shown"
    label itself.

    Args:
        hunks: List of DiffHunk objects from parse_diff().
        lang: Language string for syntax highlighting (from get_lang_from_path).

    Returns:
        Gtk.Box containing rendered hunks.
    """
    vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    for hunk in hunks:
        vbox.append(_build_hunk_view(hunk, lang))
    return vbox


def build_file_diff_card(
    file_diff: FileDiff,
    on_accept_file=None,
    on_reject_file=None,
) -> Gtk.Widget:
    """
    Build a collapsible diff card for a single file.

    Args:
        file_diff: FileDiff from utils/diff_parser
        on_accept_file: Callable[[str], None] — file_path
        on_reject_file: Callable[[str], None] — file_path
    """
    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    card.add_css_class("diff-card")
    card.set_margin_start(8)
    card.set_margin_end(8)
    card.set_margin_top(4)
    card.set_margin_bottom(4)

    # Header row
    header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    header_box.add_css_class("diff-card-header")
    header_box.set_valign(Gtk.Align.CENTER)

    # File path label
    path_lbl = Gtk.Label()
    path_lbl.set_text(file_diff.display_path)
    path_lbl.set_halign(Gtk.Align.START)
    path_lbl.set_hexpand(True)

    # Badges
    badges = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
    badges.set_halign(Gtk.Align.END)

    if file_diff.is_new:
        badge_new = Gtk.Label(label="NEW")
        badge_new.add_css_class("diff-badge-new")
        badges.append(badge_new)
    if file_diff.is_deleted:
        badge_del = Gtk.Label(label="DELETED")
        badge_del.add_css_class("diff-badge-deleted")
        badges.append(badge_del)
    if file_diff.is_renamed:
        badge_ren = Gtk.Label(label="RENAMED")
        badge_ren.add_css_class("diff-badge-deleted")
        badges.append(badge_ren)

    if not file_diff.is_binary and (file_diff.additions > 0 or file_diff.deletions > 0):
        add_badge = Gtk.Label(label=f"+{file_diff.additions}")
        add_badge.add_css_class("diff-badge-add")
        del_badge = Gtk.Label(label=f"-{file_diff.deletions}")
        del_badge.add_css_class("diff-badge-remove")
        badges.append(add_badge)
        badges.append(del_badge)

    header_box.append(path_lbl)
    header_box.append(badges)

    # Collapsed toggle
    collapsed = [False]  # mutable flag for closure

    def toggle_collapse(*args):
        collapsed[0] = not collapsed[0]
        if collapsed[0]:
            card.add_css_class("diff-collapsed")
        else:
            card.remove_css_class("diff-collapsed")

    header_box.add_css_class("diff-card-header")
    header_box.set_can_target(True)
    gesture = Gtk.GestureClick()
    gesture.connect("pressed", toggle_collapse)
    header_box.add_controller(gesture)

    card.append(header_box)

    # Body (hunks)
    body_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    body_box.add_css_class("diff-card-body")

    if file_diff.is_binary:
        bin_lbl = Gtk.Label(label="  Binary file — not shown")
        bin_lbl.add_css_class("diff-line-context")
        body_box.append(bin_lbl)
    else:
        lang = get_lang_from_path(file_diff.display_path)
        body_box.append(render_diff_hunks(file_diff.hunks, lang))

    card.append(body_box)

    # Per-file action buttons (if callbacks provided)
    if on_accept_file or on_reject_file:
        actions_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        actions_box.set_halign(Gtk.Align.END)
        actions_box.set_margin_start(12)
        actions_box.set_margin_end(12)
        actions_box.set_margin_top(6)
        actions_box.set_margin_bottom(8)

        if on_accept_file:
            btn_accept = Gtk.Button(label="Accept File")
            btn_accept.add_css_class("diff-btn-accept-file")
            btn_accept.connect("clicked", lambda _, fp=file_diff.display_path: on_accept_file(fp))
            actions_box.append(btn_accept)

        if on_reject_file:
            btn_reject = Gtk.Button(label="Reject File")
            btn_reject.add_css_class("diff-btn-reject-file")
            btn_reject.connect("clicked", lambda _, fp=file_diff.display_path: on_reject_file(fp))
            actions_box.append(btn_reject)

        card.append(actions_box)

    return card


def build_diff_summary_card(
    parsed_diff: ParsedDiff,
    on_accept_all=None,
    on_reject_all=None,
) -> Gtk.Widget:
    """
    Build a summary card shown above all file diff cards.

    Args:
        parsed_diff: ParsedDiff from utils/diff_parser
        on_accept_all: Callable[[], None]
        on_reject_all: Callable[[], None]
    """
    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    card.add_css_class("diff-card")
    card.set_margin_start(8)
    card.set_margin_end(8)
    card.set_margin_top(4)
    card.set_margin_bottom(4)

    # Header
    header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    header_box.add_css_class("diff-card-header")

    summary_lbl = Gtk.Label()
    summary_lbl.set_text(f"📋 {parsed_diff.summary}")
    summary_lbl.set_halign(Gtk.Align.START)
    summary_lbl.set_hexpand(True)
    header_box.append(summary_lbl)
    card.append(header_box)

    # File list
    files_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    files_box.set_margin_start(12)
    files_box.set_margin_end(12)
    files_box.set_margin_top(4)
    files_box.set_margin_bottom(4)

    for f in parsed_diff.files:
        file_lbl = Gtk.Label()
        if f.is_new:
            file_lbl.set_markup(xml_template(
                "  • <b>{path}</b>  <span color='#10b981'>(new, +{additions})</span>",
                path=f.display_path, additions=str(f.additions),
            ))
        elif f.is_deleted:
            file_lbl.set_markup(xml_template(
                "  • <b>{path}</b>  <span color='#f43f5e'>(deleted)</span>",
                path=f.display_path,
            ))
        else:
            file_lbl.set_markup(xml_template(
                "  • <b>{path}</b>  <span color='#10b981'>+{additions}</span>/<span color='#f43f5e'>-{deletions}</span>",
                path=f.display_path, additions=str(f.additions), deletions=str(f.deletions),
            ))
        file_lbl.set_halign(Gtk.Align.START)
        files_box.append(file_lbl)

    card.append(files_box)

    # Action buttons
    if on_accept_all or on_reject_all:
        actions_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions_box.set_halign(Gtk.Align.CENTER)
        actions_box.set_margin_top(8)
        actions_box.set_margin_bottom(8)

        if on_accept_all:
            btn_accept = Gtk.Button(label="Accept All")
            btn_accept.add_css_class("diff-btn-accept-all")
            btn_accept.connect("clicked", lambda _: on_accept_all())
            actions_box.append(btn_accept)

        if on_reject_all:
            btn_reject = Gtk.Button(label="Reject All")
            btn_reject.add_css_class("diff-btn-reject-all")
            btn_reject.connect("clicked", lambda _: on_reject_all())
            actions_box.append(btn_reject)

        card.append(actions_box)

    return card
