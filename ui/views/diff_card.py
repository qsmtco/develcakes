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
    # (matching the code-segment pattern — highlight(raw, lang))
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


# SPEC-58 §2c: build_file_diff_card / build_diff_summary_card are deleted.
# The file-tree drawer imports only the two survivors above.
