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
import logging
import re
from collections import deque

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

logger = logging.getLogger(__name__)

try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit

    _WEBKIT_VERSION = "6.0"
except (ValueError, ImportError):  # pragma: no cover - environment-dependent
    # FIX 3 (SP3 audit BUG #3): no 4.1 fallback branch. The 4.x family's
    # introspection namespace is "WebKit2", NOT "WebKit" — the old branch
    # requested a nonexistent namespace (dead code). Register: a
    # WebKit2-4.1-only box would need an explicit `require_version("WebKit2",
    # "4.1")` import here; unsupported until one matters.
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
    """Spec §7: truncate oversized rows to a BYTE budget with a marker.

    FIX 5 (SP3 audit BUG #5): slice BYTES, not chars — a char-slice of
    multi-byte content can exceed the budget ~4x; encode-slice-decode
    guarantees the cap (errors="ignore" drops a torn trailing codepoint).

    FIX D (SP3 audit r2): the marker's bytes are RESERVED inside the budget —
    slice + marker is ≤ _MAX_ROW_BYTES exactly, never the marker over it.
    """
    raw = fragment.encode("utf-8", errors="replace")
    if len(raw) > _MAX_ROW_BYTES:
        budget = _MAX_ROW_BYTES - len(_TRUNCATION_MARKER.encode("utf-8"))
        return raw[:budget].decode("utf-8", errors="ignore") + _TRUNCATION_MARKER
    return fragment


def _make_owned_scroll() -> Gtk.ScrolledWindow:
    """FIX 3 (SP5a audit): the ONE ScrolledWindow a surface owns.

    Single-scroll ruling: the surface wraps its content widget itself —
    no mount-time wrapper in the render handler, no second scrollbar.
    Both surface classes build it here (parity, one place).
    """
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.set_vexpand(True)
    scroll.set_hexpand(True)
    return scroll


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
        self._render_source = None
        self._destroyed = False
        self._pill_label = Gtk.Label(label=_PILL_STATES["idle"])
        self._pill_label.set_halign(Gtk.Align.END)
        self._pill_css = "pill-idle"
        self._pill_label.add_css_class(self._pill_css)
        self.append(self._pill_label)
        # FIX 3 (SP5a audit): the surface owns its scroll — when mounted
        # (directly, no wrapper) it must fill the pane.
        self._scroll = _make_owned_scroll()
        self.set_vexpand(True)
        self.set_hexpand(True)
        self.append(self._scroll)

    # ── internals ──
    def _ensure_webview(self):
        """Lazy: the webview is created on the first append."""
        if self._webview is None:
            self._webview = WebKit.WebView()
            settings = self._webview.get_settings()
            settings.set_enable_javascript(False)  # JS OFF (ruling R2)
            self._scroll.set_child(self._webview)
        return self._webview

    def _load_html(self, doc: str) -> None:
        """The single load path (monkeypatch target for tests)."""
        self._ensure_webview().load_html(doc, "about:blank")

    def get_vadjustment(self) -> Gtk.Adjustment | None:
        """FIX 3 (SP5a audit): the surface's own scroll adjustment — the
        seam main_content.scroll_chat_to_bottom drives (single-scroll
        ruling: one active scroll per pane, owned by the surface)."""
        return self._scroll.get_vadjustment()

    def _do_render(self) -> bool:
        """Coalesced render — runs at most once per idle cycle."""
        self._render_pending = False
        if self._destroyed:
            self._dirty = False
            return GLib.SOURCE_REMOVE
        if self._dirty:
            self._dirty = False
            self._rebuild_count += 1
            self._load_html(_document(list(self._rows)))
        return GLib.SOURCE_REMOVE

    def _schedule_render(self) -> None:
        """Coalesce: at most one pending render at any time."""
        if self._destroyed:
            return
        self._dirty = True
        if self._render_pending:
            return
        self._render_pending = True
        self._render_source = GLib.idle_add(self._do_render)

    def _drain_renders(self) -> None:
        """Test hook: run the pending coalesced render synchronously."""
        if self._render_pending:
            self._do_render()

    # ── public API ──
    def append_message(self, role: str, html_fragment: str, agent_name: str | None = None) -> None:
        """Append one rendered (sanitized) HTML row; schedules a re-render."""
        if self._destroyed:
            return
        self._rows.append(
            {
                "role": role if role in ("user", "agent", "system") else "system",
                "html": _cap_row_html(html_fragment),
                "agent": agent_name or "",
            }
        )
        self._schedule_render()

    def stream_delta(self, session_key: str, text: str, agent_name: str | None = None) -> None:
        """Buffer a streaming delta — NOTHING renders until end_stream.

        REGISTER (SP3 audit r2, accepted class): pre-flush chunks accumulate
        unbounded until end_stream (P11 register item).
        """
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
        """Drop the webview (idempotent — twice-safe).

        FIX 2 (SP3 audit BUG #2): cancel the pending idle render — a queued
        rebuild fired AFTER destroy() in probe (recreating a webview via
        _ensure_webview inside the render path).

        FIX C (SP3 audit r2): source_remove is NON-RAISING on stale ids
        (GLib logs a critical warning and returns — probe-verified); the
        try/except below is belt-and-braces, not a RuntimeError shield.
        """
        self._destroyed = True
        if self._render_source is not None:
            try:
                GLib.source_remove(self._render_source)
            except RuntimeError:
                logger.debug("render source already fired — nothing to cancel")
            self._render_source = None
        self._render_pending = False
        self._dirty = False
        self._stream_buffers.clear()
        self._rows.clear()
        if self._webview is not None:
            self._scroll.set_child(None)
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
        # FIX 3 (SP5a audit): scroll parity with the WebKit class — the
        # surface owns its ScrolledWindow; TextView goes inside it.
        self._scroll = _make_owned_scroll()
        self.set_vexpand(True)
        self.set_hexpand(True)
        self.append(self._scroll)
        self._view = Gtk.TextView()
        self._view.set_editable(False)
        self._view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._scroll.set_child(self._view)

    def get_vadjustment(self) -> Gtk.Adjustment | None:
        """FIX 3 (SP5a audit): scroll seam — see ChatSurface.get_vadjustment."""
        return self._scroll.get_vadjustment()

    def _text(self) -> str:
        buf = self._view.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    def _append_line(self, line: str) -> None:
        """Append one line; trim from the TOP past window_max (FIX 7 —
        P11: every append-driven surface is bounded-memory; this buffer IS
        the fallback's window)."""
        buf = self._view.get_buffer()
        buf.insert(buf.get_end_iter(), line + "\n")
        excess = buf.get_line_count() - self._window_max
        if excess > 0:
            # GTK4 out-param API: get_iter_at_line returns (ok, iter).
            start = buf.get_start_iter()
            _, end = buf.get_iter_at_line(excess)
            buf.delete(start, end)

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


# FIX 4 (SP3 audit BUG #4): on a WebKit-less box the real ChatSurface would
# crash on first append (WebView is None) — the fallback IS the surface
# there. Same API, zero call-site branching.
if WebKit is None:
    ChatSurface = TextViewFallback  # deliberate module-level alias


def create_chat_surface(window_max: int = 500):
    """Call-site factory: resolves the surface class at CALL time (tests
    monkeypatch the module's WebKit binding; the import-time alias above
    covers genuinely WebKit-less boxes). REGISTER (SP3 audit r2): test
    scaffolding today — no production caller yet."""
    if WebKit is None:
        return TextViewFallback(window_max)
    return ChatSurface(window_max)
