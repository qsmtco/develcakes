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
# MICRO smart-scroll (2026-10-07): implemented — at-bottom follow / reading-
# position preserve. The restore is a PERMANENT `changed` handler gated on a
# pending capture (not a blind one-shot), so a pane RESIZE that fires
# `changed` with no render pending moves nothing (edge 2 rule); only a
# genuinely scrollable (upper > page_size) height CONSUMES the capture, which
# is what makes the mid-load collapse event harmless. Consumption is deferred
# one idle frame while the height settles (BUG#1: WebKit fires several
# `changed` events during layout; the first scrollable one is not final).
#
# Test fidelity note: the smart-scroll tests drive a REAL Gtk.Adjustment from
# the surface ScrolledWindow (real set_upper/set_value signal semantics —
# fires changed, never value-changed, no auto-clamp of value on set_upper).
# Do not replace with a fake — the real-semantics divergence is load-bearing.
#
# WebKit 6.0 preferred, 4.1 fallback (spec §2 edge table); if neither
# introspection namespace loads, TextViewFallback (same API, raw text) keeps
# the app usable (spec §7). Call sites import ChatSurface — it is the WebKit
# class when WebKit is present.
#
# CSS: token/color styling lives in _BASE_CSS classes (tok-*/lang-*/terminal/
# task-*/message-row) — no inline styles (SP2 contract). The classes
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

# DEVELCAKES_NO_WEBKIT=1 forces the TextViewFallback path (spec §7
# runtime-failure case). Needed on Ubuntu 24.04 with
# apparmor_restrict_unprivileged_userns=1: WebKit 6.0 IMPORTS fine but its
# bwrap sandbox + dbus-proxy cannot launch, so the first page render SIGTRAPs
# the whole app. The env var makes the import fail deliberately → the alias
# below routes to the fallback surface. Old CRABCAKES_ name rides the
# one-release fallback (utils.config.get_env — D2).
from utils.config import get_env

if get_env("NO_WEBKIT"):
    WebKit = None
    _WEBKIT_VERSION = None
else:
    try:
        gi.require_version("WebKit", "6.0")
        from gi.repository import WebKit

        _WEBKIT_VERSION = "6.0"
    except (ValueError, ImportError):  # pragma: no cover - environment-dependent
        # FIX 3 (SP3 audit BUG #3): no 4.1 fallback branch. The 4.x family's
        # introspection namespace is "WebKit2", NOT "WebKit" — the old branch
        # requested a nonexistent namespace (dead code). Register: a
        # WebKit2-4.1-only box would need an explicit `require_version("WebKit2",
        # "4.1") import here; unsupported until one matters.
        WebKit = None
        _WEBKIT_VERSION = None

# Spec §7 huge-message cap.
_MAX_ROW_BYTES = 512 * 1024
_TRUNCATION_MARKER = " [truncated]"

_BASE_CSS = """
body { background: #1a1b26; color: #a9b1d6; font-family: sans-serif;
       font-size: 14px; margin: 8px; }
.message-row { margin-bottom: 10px; }
.agent-name { color: #7aa2f7; font-weight: bold; font-size: 12px; }
.role-user .agent-name { color: #9ece6a; }
/* SPEC-14 §2a.4: the card frame replaces SPEC-12 SP1's border-left (now the
   chrome + card border carry the visual identity). The old SPEC-12 comment
   ("border-left becomes the card frame") is superseded here. */
.agent-box { border-left: none; margin-bottom: 14px; }
/* SP1-audit BUG#3 correction (SPEC-12): the box element ITSELF carries
   role-user, so the header span.agent-name is still a DESCENDANT of a
   .role-user element and the OLD `.role-user .agent-name` rule STILL matches.
   This box-level rule is therefore REDUNDANT (kept for explicitness/intent;
   either rule styles the user header green). */
.agent-box.role-user .agent-name { color: #9ece6a; }
/* SPEC-14 §2a.4 chrome defaults; an agent color overrides via inline style. */
.agent-chrome { display: flex; align-items: center; gap: 8px; padding: 6px 10px;
                background: #16161e; border-radius: 8px 8px 0 0; }
.agent-avatar { width: 24px; height: 24px; border-radius: 50%;
                background: #3b4261; color: #1a1b26; font-weight: bold;
                font-size: 12px; text-align: center; line-height: 24px;
                flex: none; }
.agent-card { border: 1px solid #3b4261; border-top: none;
              border-radius: 0 0 8px 8px; padding: 4px 10px 8px;
              overflow: hidden; } /* SP1 audit: full-bleed payload bgs clip at the corners */
.agent-box.role-user .agent-avatar { background: #9ece6a; }
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
/* SPEC-13 SP2: agent-payload defaults. The agent-author policy (§2a) admits
   div/section/article/header/footer/aside/nav/figure, img/video, and button;
   these defaults keep an UNSTYLED payload readable (block containers don't
   collapse inline; media can't overflow the pane; buttons aren't black-on-
   black). No JS: button/details are inert toggles only (surface JS stays off). */
div, section, article, header, footer, aside, nav, figure { display: block; }
img, video { max-width: 100%; height: auto; border-radius: 4px; }
button { background: #2f334d; color: #c0caf5; border: 1px solid #3b4261;
         border-radius: 6px; padding: 4px 10px; }
"""

_TAG_STRIP_RE = re.compile(r"<[^>]*>")

# SPEC-14 §2a.2 security gate. NOTE the deviation: the spec writes the gate as
# `^#[0-9a-fA-F]{6}$`, but Python's `$` also matches BEFORE a trailing newline,
# so `"#16a34a\n"` would slip through a `.match()` + `$` form. The intent is
# "ONLY an exact six-hex-digit value may reach a style attribute" — `fullmatch`
# preserves that with no trailing-newline loophole.
_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")


def _sanitize_color(value) -> str:
    """SPEC-14 §2a.2 — the color security gate.

    ONLY an exact six-hex-digit color may ever reach a style attribute:
    None/empty → "" ; ``#[0-9a-fA-F]{6}`` (whole string) → lowercased ;
    anything else → "" (drops to the CSS default). Agent colors are platform
    hex, but the surface must never embed an arbitrary attribute-sourced
    string into HTML.
    """
    if not value:
        return ""
    if _COLOR_RE.fullmatch(value):
        return value.lower()
    return ""


def _document(rows: list[dict]) -> str:
    """Render rows as grouped agent boxes — consecutive rows with the same
    agent collapse under ONE header (SPEC-12). Rows with no agent name render
    bare (system/welcome)."""
    blocks = []
    i = 0
    n = len(rows)
    while i < n:
        row = rows[i]
        agent = row.get("agent") or ""
        if not agent:
            blocks.append(
                f'<div class="message-row role-{row.get("role") or "system"}">'
                f'<div class="msg-body">{row["html"]}</div></div>'
            )
            i += 1
            continue
        # collect the run of same-agent rows — key on (agent, role) so an
        # agent displaying the literal name "You" (role "agent") can never
        # merge with the user's own rows (role "user") (SP1-audit BUG#1).
        role = row.get("role") or "system"
        j = i
        while (j < n and (rows[j].get("agent") or "") == agent
               and (rows[j].get("role") or "system") == role):
            j += 1
        body = "".join(
            f'<div class="message-row role-{rows[k].get("role") or "system"}">'
            f'<div class="msg-body">{rows[k]["html"]}</div></div>'
            for k in range(i, j)
        )
        name = html.escape(agent)
        # SP1-audit BUG#1: derive the box class from the row's ROLE, not the
        # display-name string — the name is not a user/agent discriminator.
        box_class = "role-user" if role == "user" else "role-agent"
        # SPEC-14 §2a.3: the PLATFORM draws the card chrome (avatar + name
        # header + card frame); the agent payload stays inside .agent-card.
        # Color rides the group — first non-empty row color wins (defensive).
        # User rows get NO inline color: `.role-user` supplies the green
        # identity via _BASE_CSS (agent-gate color never touches user DOM).
        color = ""
        if role != "user":
            for k in range(i, j):
                if rows[k].get("color"):
                    color = rows[k]["color"]
                    break
        initial = html.escape((agent[:1] or "?").upper())
        card_style = f' style="border-bottom-color:{color}"' if color else ""
        avatar_style = f' style="background-color:{color}"' if color else ""
        name_style = f' style="color:{color}"' if color else ""
        blocks.append(
            f'<div class="agent-box {box_class}">'
            f'<div class="agent-chrome">'
            f'<span class="agent-avatar"{avatar_style}>{initial}</span>'
            f'<span class="agent-name"{name_style}>{name}</span>'
            f'</div>'
            f'<div class="agent-card"{card_style}>{body}</div></div>'
        )
        i = j
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<style>{_BASE_CSS}</style></head><body>"
        + "".join(blocks) + "</body></html>"
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
        # FIX 3 (SP5a audit): the surface owns its scroll — when mounted
        # (directly, no wrapper) it must fill the pane.
        self._scroll = _make_owned_scroll()
        # MICRO smart-scroll: social-feed scroll behavior. Full-document
        # load_html collapses content height mid-load → the adjustment clamps
        # → the view snaps to top on EVERY append (PM bug 2026-10-07; this
        # closes SPEC-06 register #8 without a JS bridge). We track at-bottom
        # on the surface's OWN adjustment and restore the user's intent once
        # the new document's height lands.
        self._was_at_bottom = True  # fresh surface: first render lands at bottom
        self._pending_restore: tuple[bool, float] | None = None
        # BUG#1 (fix round): WebKit fires MULTIPLE `changed` events as layout
        # settles; the restore must wait for a STABLE height, so consumption is
        # deferred one idle frame (see _on_adjustment_changed / _settle_restore).
        self._restore_settle_source = None
        self._restore_upper = 0.0
        vadj = self._scroll.get_vadjustment()
        if vadj is not None:
            self._bottom_adj = vadj
            self._bottom_handler_id = vadj.connect(
                "value-changed", self._on_scroll_value_changed
            )
            self._restore_handler_id = vadj.connect(
                "changed", self._on_adjustment_changed
            )
        else:  # pragma: no cover - Gtk.ScrolledWindow always has an adjustment
            self._bottom_adj = None
            self._bottom_handler_id = 0
            self._restore_handler_id = 0
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

    # ── smart-scroll (MICRO 2026-10-07) ──
    _BOTTOM_THRESHOLD = 80.0  # px — same threshold as main_content's button

    def _on_scroll_value_changed(self, vadj) -> None:
        """Track whether the user is at/near the bottom (social-feed rule).

        A user-driven value change (scrollbar drag, wheel, the scroll-to-
        bottom button) re-arms the intent that the next restored render uses.
        Programmatic set_value from the restore ALSO fires this — idempotent:
        after a restore the value is at the restored spot, which re-derives
        the SAME at-bottom verdict.
        """
        self._was_at_bottom = (
            (vadj.get_upper() - vadj.get_page_size() - vadj.get_value())
            <= self._BOTTOM_THRESHOLD
        )

    def _on_adjustment_changed(self, vadj) -> None:
        """Fired when upper/page_size change — for a real load, this is the
        new DOM height landing. Schedule the deferred restore.

        BUG#1 (fix round): real WebKit fires MULTIPLE `changed` events as the
        layout settles — the FIRST event with `upper > page_size` is NOT the
        final height (audit Probe D: captured 300 → the premature consume read
        100). So we do NOT consume here; we defer one idle frame and only
        restore once `upper` is STABLE across that frame (see _settle_restore).

        Guard 1 (edge 2): no pending capture (e.g. a pane resize) → nothing.
        Guard 2 (load-bearing, kept): `upper <= page_size` is the mid-load
        COLLAPSE — not scrollable yet, so leave the capture pending and do not
        even schedule (the real height's own `changed` will).
        """
        pending = self._pending_restore
        if pending is None or self._destroyed:
            return
        if vadj.get_upper() <= vadj.get_page_size():
            return  # collapse / nothing to scroll yet — keep the capture
        # Defer: a settled restore is applied on the next idle frame only if
        # the height is unchanged across it (a still-changing height
        # re-schedules itself from its own `changed`).
        self._schedule_restore(vadj)

    def _schedule_restore(self, vadj) -> None:
        """Arm a one-frame settle check (idempotent — one pending at a time)."""
        if self._restore_settle_source is not None:
            return  # already scheduled; the running settle re-reads upper
        self._restore_upper = vadj.get_upper()
        self._restore_settle_source = GLib.idle_add(self._settle_restore)

    def _settle_restore(self) -> bool:
        """Idle callback: consume the capture only once `upper` is stable.

        Stable == same `upper` as when this check was armed. If the height
        changed again, re-arm (the next idle re-checks) instead of consuming.
        """
        self._restore_settle_source = None
        if self._destroyed:
            return GLib.SOURCE_REMOVE
        pending = self._pending_restore
        vadj = self._bottom_adj
        if pending is None or vadj is None:
            return GLib.SOURCE_REMOVE
        current_upper = vadj.get_upper()
        if current_upper != self._restore_upper:
            # Height moved again during the frame — re-check, don't consume.
            self._schedule_restore(vadj)
            return GLib.SOURCE_REMOVE
        if vadj.get_upper() <= vadj.get_page_size():
            return GLib.SOURCE_REMOVE  # collapsed again — leave pending
        self._pending_restore = None  # consume BEFORE set_value (see below)
        was_at_bottom, captured_value = pending
        if was_at_bottom:
            vadj.set_value(vadj.get_upper() - vadj.get_page_size())
        else:
            vadj.set_value(captured_value)  # set_value clamps to [lower, max]
        # NOTE: consume-before-set_value is deliberate — set_value fires
        # `value-changed` (re-arming _was_at_bottom) but NOT `changed`, so it
        # cannot re-trigger this handler.
        return GLib.SOURCE_REMOVE

    def _do_render(self) -> bool:
        """Coalesced render — runs at most once per idle cycle."""
        self._render_pending = False
        if self._destroyed:
            self._dirty = False
            return GLib.SOURCE_REMOVE
        if self._dirty:
            self._dirty = False
            self._rebuild_count += 1
            # MICRO smart-scroll: capture the intent BEFORE the load collapses
            # content height. One slot — the LAST capture before a drain wins
            # (rapid coalesced appends collapse to one render); a capture with
            # no rows yet is the fresh-surface bottom-first case.
            vadj = self._scroll.get_vadjustment()
            if vadj is not None:
                self._pending_restore = (self._was_at_bottom, vadj.get_value())
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
    def append_message(self, role: str, html_fragment: str, agent_name: str | None = None,
                       agent_color: str | None = None) -> None:
        """Append one rendered (sanitized) HTML row; schedules a re-render.

        SPEC-14 §2a.1: `agent_color` is gated + FROZEN into the row at append
        time (deque rows re-render verbatim; `_document` stays pure — no
        color-map lookup at render time).
        """
        if self._destroyed:
            return
        self._rows.append(
            {
                "role": role if role in ("user", "agent", "system") else "system",
                "html": _cap_row_html(html_fragment),
                "agent": agent_name or "",
                "color": _sanitize_color(agent_color),
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
        # MICRO smart-scroll (edge 7): cancel any pending settle idle and
        # disconnect the adjustment handlers so a height change landing AFTER
        # destroy cannot restore on a dead surface (and no restore fires from
        # a stale capture).
        if self._restore_settle_source is not None:
            try:
                GLib.source_remove(self._restore_settle_source)
            except RuntimeError:
                logger.debug("settle source already fired — nothing to cancel")
            self._restore_settle_source = None
        if self._bottom_adj is not None:
            try:
                if self._bottom_handler_id:
                    self._bottom_adj.disconnect(self._bottom_handler_id)
                if self._restore_handler_id:
                    self._bottom_adj.disconnect(self._restore_handler_id)
            except (TypeError, ValueError):
                # handler already gone / invalid id — non-fatal (idempotent
                # destroy contract).
                logger.debug("adjustment handler already disconnected")
            self._bottom_adj = None
            self._bottom_handler_id = 0
            self._restore_handler_id = 0
        # NOTE: `_pending_restore` is intentionally NOT cleared here — on a
        # destroyed surface the capture is inert, and leaving it means the
        # DISCONNECT is the sole guard (so test_destroy_mid_load_no_restore
        # isolates disconnection; clearing it would mask a missing disconnect).
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

    def append_message(self, role: str, html_fragment: str, agent_name: str | None = None,
                       agent_color: str | None = None) -> None:
        """SPEC-14 §2a.5: color is accepted for signature parity and stored on
        the row, but is INERT in plain-text mode (no text medium for color;
        the `[name]` prefix is the fallback's identity marker)."""
        self._rows.append(
            {
                "role": role,
                "html": _cap_row_html(html_fragment),
                "agent": agent_name or "",
                "color": _sanitize_color(agent_color),
            }
        )
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
