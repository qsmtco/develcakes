# ui/views/chat_surface.py — WebKit-hosted chat transcript (SPEC-06 R2A).
#
# DEVIATION (JS-off-won, ruling R2 — supersedes the spec's JS-bridge sketch):
# the spec §2/§6 sketch assumed a JS bridge (run_javascript appends). JS is
# OFF for content (security ruling), so there is no bridge and no
# run_javascript. The WINDOWED DOM therefore lives on the PYTHON side:
# ChatSurface keeps a bounded deque (default 500) of rendered message rows
# and re-renders the whole document via WebView.load_html on append. Re-
# renders are COALESCED (idle_add + dirty flag — at most one pending render,
# ~10/s under streaming). Live nodes are bounded by construction: the deque
# IS the window. Spec §6's 10k-append test is satisfied without JS.
# Known cost (accepted): full-document reload resets scroll; SP-later may
# add scroll restore if the PM asks.
#
# WebKit 6.0 preferred, 4.1 fallback (spec §2 edge table); if neither
# introspection namespace loads, TextViewFallback (same API, raw text) keeps
# the app usable (spec §7). Call sites import ChatSurface — it is the WebKit
# class when WebKit is present.
#
# CSS: token/color styling lives in _BASE_CSS classes (tok-*/lang-*/terminal/
# task-*/message-row/pill-*) — no inline styles (SP2 contract). The classes
# survive because the sanitizer's class policy (render/sanitize.py, SP3
# ruling) allowlists exactly this vocabulary.

import html
import re
from collections import deque

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit

    _WEBKIT_VERSION = "6.0"
except (ValueError, ImportError):  # pragma: no cover - environment-dependent
    try:
        gi.require_version("WebKit", "4.1")
        from gi.repository import WebKit

        _WEBKIT_VERSION = "4.1"
    except (ValueError, ImportError):  # pragma: no cover
        WebKit = None
        _WEBKIT_VERSION = None

# Spec §7 huge-message cap.
_MAX_ROW_BYTES = 512 * 1024
_TRUNCATION_MARKER = " [truncated]"

_PILL_STATES = {
    "idle": "Idle",
    "thinking": "Thinking…",
    "tool": "Tool running…",
    "error": "Error",
}

_BASE_CSS = """
body { background: #1a1b26; color: #a9b1d6; font-family: sans-serif;
       font-size: 14px; margin: 8px; }
.message-row { margin-bottom: 10px; }
.agent-name { color: #7aa2f7; font-weight: bold; font-size: 12px; }
.role-user .agent-name { color: #9ece6a; }
pre { background: #16161e; padding: 6px; border-radius: 4px; }
code { font-family: monospace; }
pre.terminal { color: #c0caf5; }
.tok-kw { color: #c792ea; }
.tok-str { color: #c3e88d; }
.tok-com { color: #676e95; }
.tok-num { color: #f78c6c; }
.tok-op { color: #89ddff; }
.tok-fn { color: #82aaff; }
.tok-type { color: #ffcb6b; }
.pill-idle { color: #6b6b7a; }
.pill-thinking { color: #e0af68; }
.pill-tool { color: #7aa2f7; }
.pill-error { color: #f7768e; }
"""

_TAG_STRIP_RE = re.compile(r"<[^>]*>")


def _document(rows: list[dict]) -> str:
    """Render the row list as one full HTML document."""
    body = []
    for row in rows:
        name = (
            f'<span class="agent-name">{html.escape(row["agent"])}</span>'
            if row["agent"]
            else ""
        )
        body.append(
            f'<div class="message-row role-{row["role"]}">{name}'
            f'<div class="msg-body">{row["html"]}</div></div>'
        )
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<style>{_BASE_CSS}</style></head><body>"
        + "".join(body)
        + "</body></html>"
    )


def _cap_row_html(fragment: str) -> str:
    """Spec §7: truncate oversized rows with a marker."""
    if len(fragment.encode("utf-8", errors="replace")) > _MAX_ROW_BYTES:
        return fragment[:_MAX_ROW_BYTES] + _TRUNCATION_MARKER
    return fragment


class ChatSurface(Gtk.Box):
    """WebKit-hosted chat transcript. One per chat tab, lazy webview.

    JS is OFF; windowing is a Python-side bounded deque with coalesced
    full-document re-renders (see module docstring for the JS-off-won
    deviation).
    """

    def __init__(self, window_max: int = 500) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._window_max = window_max
        self._rows: deque = deque(maxlen=window_max)
        self._stream_buffers: dict[str, list[str]] = {}
        self._webview = None
        self._dirty = False
        self._render_pending = False
        self._rebuild_count = 0
        self._pill_label = Gtk.Label(label=_PILL_STATES["idle"])
        self._pill_label.set_halign(Gtk.Align.END)
        self._pill_css = "pill-idle"
        self._pill_label.add_css_class(self._pill_css)
        self.append(self._pill_label)

    # ── internals ──
    def _ensure_webview(self):
        """Lazy: the webview is created on the first append."""
        if self._webview is None:
            self._webview = WebKit.WebView()
            settings = self._webview.get_settings()
            settings.set_enable_javascript(False)  # JS OFF (ruling R2)
            self.append(self._webview)
        return self._webview

    def _load_html(self, doc: str) -> None:
        """The single load path (monkeypatch target for tests)."""
        self._ensure_webview().load_html(doc, "about:blank")

    def _do_render(self) -> bool:
        """Coalesced render — runs at most once per idle cycle."""
        self._render_pending = False
        if self._dirty:
            self._dirty = False
            self._rebuild_count += 1
            self._load_html(_document(list(self._rows)))
        return GLib.SOURCE_REMOVE

    def _schedule_render(self) -> None:
        """Coalesce: at most one pending render at any time."""
        self._dirty = True
        if self._render_pending:
            return
        self._render_pending = True
        GLib.idle_add(self._do_render)

    def _drain_renders(self) -> None:
        """Test hook: run the pending coalesced render synchronously."""
        if self._render_pending:
            self._do_render()

    # ── public API ──
    def append_message(self, role: str, html_fragment: str, agent_name: str | None = None) -> None:
        """Append one rendered (sanitized) HTML row; schedules a re-render."""
        self._rows.append(
            {
                "role": role if role in ("user", "agent", "system") else "system",
                "html": _cap_row_html(html_fragment),
                "agent": agent_name or "",
            }
        )
        self._schedule_render()

    def stream_delta(self, session_key: str, text: str, agent_name: str | None = None) -> None:
        """Buffer a streaming delta — NOTHING renders until end_stream."""
        self._stream_buffers.setdefault(session_key, []).append(text)

    def end_stream(self, session_key: str, agent_name: str | None = None) -> None:
        """Flush the buffered stream as ONE atomic message row."""
        chunks = self._stream_buffers.pop(session_key, None)
        if not chunks:
            return
        joined = html.escape("".join(chunks)).replace("\n", "<br>")
        self.append_message("agent", joined, agent_name=agent_name)

    def set_activity_pill(self, state: str) -> None:
        """Pill text + CSS class swap (idle|thinking|tool|error)."""
        text = _PILL_STATES.get(state, _PILL_STATES["idle"])
        self._pill_label.set_text(text)
        new_css = f"pill-{_PILL_STATES.get(state) and state or 'idle'}"
        if new_css != self._pill_css:
            self._pill_label.remove_css_class(self._pill_css)
            self._pill_label.add_css_class(new_css)
            self._pill_css = new_css

    def destroy(self) -> None:
        """Drop the webview (idempotent — twice-safe)."""
        self._stream_buffers.clear()
        self._rows.clear()
        if self._webview is not None:
            self.remove(self._webview)
            self._webview = None


class TextViewFallback(Gtk.Box):
    """Spec §7: plain-text fallback with the same API — app stays usable
    without WebKit (raw text, no HTML rendering)."""

    def __init__(self, window_max: int = 500) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._window_max = window_max
        self._rows: deque = deque(maxlen=window_max)
        self._stream_buffers: dict[str, list[str]] = {}
        self._pill_label = Gtk.Label(label=_PILL_STATES["idle"])
        self._pill_label.set_halign(Gtk.Align.END)
        self._pill_css = "pill-idle"
        self._pill_label.add_css_class(self._pill_css)
        self.append(self._pill_label)
        self._view = Gtk.TextView()
        self._view.set_editable(False)
        self._view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.append(self._view)

    def _text(self) -> str:
        buf = self._view.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    def _append_line(self, line: str) -> None:
        buf = self._view.get_buffer()
        buf.insert(buf.get_end_iter(), line + "\n")

    def append_message(self, role: str, html_fragment: str, agent_name: str | None = None) -> None:
        self._rows.append({"role": role, "html": _cap_row_html(html_fragment), "agent": agent_name or ""})
        plain = html.unescape(_TAG_STRIP_RE.sub("", _cap_row_html(html_fragment)))
        prefix = f"[{agent_name}] " if agent_name else ""
        self._append_line(f"{prefix}{plain}")

    def stream_delta(self, session_key: str, text: str, agent_name: str | None = None) -> None:
        self._stream_buffers.setdefault(session_key, []).append(text)

    def end_stream(self, session_key: str, agent_name: str | None = None) -> None:
        chunks = self._stream_buffers.pop(session_key, None)
        if not chunks:
            return
        self.append_message("agent", "".join(chunks), agent_name=agent_name)

    def set_activity_pill(self, state: str) -> None:
        self._pill_label.set_text(_PILL_STATES.get(state, _PILL_STATES["idle"]))
        new_css = f"pill-{state if state in _PILL_STATES else 'idle'}"
        if new_css != self._pill_css:
            self._pill_label.remove_css_class(self._pill_css)
            self._pill_label.add_css_class(new_css)
            self._pill_css = new_css

    def destroy(self) -> None:
        self._stream_buffers.clear()
        self._rows.clear()
