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
import json
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

# ── SPEC-17 SP1: document scroll (app-side evaluate_javascript) ───────────
#
# C-API PROBE (2026-10-07, xvfb, WebKit 6.0/GI 3.48.2): with
# `settings.set_enable_javascript(False)` (the ruling, kept), calling
# `evaluate_javascript(script, len, world_name=None, ...)` raises
# `WebKitJavascriptError: Cannot execute JavaScript in this document (699)`.
# The spec's "world_name may be None" was WRONG on this box — a None world
# resolves to the MAIN world, which is disabled. In a NAMED ISOLATED WORLD
# the app-side script DOES run while page content stays non-scriptable: a
# probe page with an inline `<script>window.__pwn=...</script>` reported
# `{pwn: null}` and `el.scrollTop = el.scrollHeight` moved the shared DOM
# (`{y:7408,max:7408}`). So the fix keeps JS off for CONTENT and evaluates in
# this isolated world. (Deviation from spec §SP1.1 recorded in the report.)
_DOC_WORLD = "develcakes-scroll"

# SPEC-17 SP1.2: the at-bottom apply script. `scrollingElement` is the
# document's scroll container; `scrollHeight` is its full height, so this
# lands on the newest row (the LAST body element — order is unchanged).
_BOTTOM_SCRIPT = (
    "(function () {"
    "  var el = document.scrollingElement || document.documentElement;"
    "  el.scrollTop = el.scrollHeight;"
    "})();"
)

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
        # deferred across idle frames. CROSS-FRAME (audit Finding 1): a single
        # stable frame is NOT enough — WebKit's layout lands across frames, so
        # an intermediate height can be stable for one frame and consume the
        # capture prematurely. We require N=2 CONSECUTIVE stable checks.
        self._restore_settle_source = None
        self._restore_upper = 0.0
        self._restore_stable_count = 0
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
        # SPEC-17 SP1: the DOCUMENT-scroll pending intent (the fix). SINGLE
        # slot — `(at_bottom: bool, y: float)`. Depends on the DOCUMENT
        # position, captured by a pre-load READ (SP1.1) and consumed by the
        # FINISHED handler (SP1.2) after each full-document reload. It is
        # DELIBERATELY separate from `_pending_restore` above (the legacy GTK
        # machinery, retained but not the fix).
        self._pending_scroll: tuple[bool, float] | None = None
        # SPEC-17 SP1.1 coalescing: True between issuing the pre-load READ and
        # its callback. The load is deferred to the callback, so an append
        # that arrives meanwhile must NOT issue a second read+load — it leaves
        # `_dirty` set, and the callback's FINISHED kick re-renders ("coalesced
        # appends still produce one load").
        self._read_in_flight = False
        # BUG#1 (audit fix, SPEC-17): True between issuing a full-document load
        # and its terminal event (FINISHED, or the failure signal). While set,
        # `_do_render` defers (sets `_dirty`) instead of issuing another
        # read+load — the queued-load race: a second load's read overwrites the
        # single `_pending_scroll` slot, so the FIRST load's FINISHED consumes
        # the SECOND's intent and the SECOND's FINISHED finds None → the reader
        # strands at the top of the newest document. Set in ONE place
        # (`_issue_load`); cleared on FINISHED and on `load-failed`.
        self._load_in_flight = False
        # ROUND 4 (spin latch): one-shot retry latch for the raising-load
        # re-queue (see `_issue_load`); cleared on a successful load, reset
        # on destroy.
        self._load_retry_pending = False
        self._load_changed_handler_id = 0
        self._load_failed_handler_id = 0
        # FIX ROUND 2 (Issue #3): a `scroll_to_latest()` that lands while a
        # pre-load READ is in flight must WIN over the stale position that read
        # captured. Set only while `_read_in_flight`; consumed (cleared) by the
        # read callback, which then keeps the (True, 0.0) bottom intent instead
        # of the stale y. Cleared on destroy too. (O(1) override flag — no new
        # state class.)
        self._scroll_override = False
        # FIX ROUND 2 (BUG#1b): `web-process-terminated` handler id (the crash
        # signal — reaches NEITHER FINISHED nor load-failed).
        self._web_process_handler_id = 0
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
            # SPEC-17 SP1.2: connect `load-changed` ONCE, on this web view.
            # FINISHED is when the document is laid out and the post-load
            # scroll can be issued (SP1.2).
            self._load_changed_handler_id = self._webview.connect(
                "load-changed", self._on_load_changed
            )
            # BUG#1 (audit fix): the real failure signal clears the
            # `_load_in_flight` gate (WebKit 6.0 has no LoadEvent.FAILED —
            # probe-verified; `load-failed` is the failure mechanism).
            self._load_failed_handler_id = self._webview.connect(
                "load-failed", self._on_load_failed
            )
            # FIX ROUND 2 (BUG#1b): a web-process CRASH (or OOM kill) fires
            # `web-process-terminated` and reaches NEITHER `load-failed` NOR
            # FINISHED — without this handler `_load_in_flight` sticks True and
            # the surface defers every later render forever. Signal arity is
            # `(view, reason)` (probe-verified: WebKitWebView, reason is a
            # WebKit.WebProcessTerminationReason).
            self._web_process_handler_id = self._webview.connect(
                "web-process-terminated", self._on_web_process_terminated
            )
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
    # Finding 1: consecutive stable idle checks required before consuming the
    # capture (WebKit layout lands across frames).
    _RESTORE_STABLE_FRAMES = 2

    def _on_scroll_value_changed(self, vadj) -> None:
        """Track whether the user is at/near the bottom (social-feed rule).

        A user-driven value change (scrollbar drag, wheel, the scroll-to-
        bottom button) re-arms the intent that the next restored render uses.
        Programmatic set_value from the restore ALSO fires this — idempotent:
        after a restore the value is at the restored spot, which re-derives
        the SAME at-bottom verdict.

        Finding 2 guard: when the content is NOT scrollable (upper <= page_size
        — the mid-load COLLAPSE clamps value→0 and fires value-changed), the
        clamp is a LAYOUT artifact, NOT user intent. Updating the tracker here
        would wrongly re-arm was_at_bottom=True while the user reads up top.
        Early-return: there is nothing to scroll, so there is no intent.
        """
        if vadj.get_upper() <= vadj.get_page_size():
            return  # collapsed / not scrollable — do not touch the tracker
        self._was_at_bottom = (
            (vadj.get_upper() - vadj.get_page_size() - vadj.get_value())
            <= self._BOTTOM_THRESHOLD
        )

    def _on_adjustment_changed(self, vadj) -> None:
        """Fired when upper/page_size change — for a real load, this is the
        new DOM height landing. Schedule the deferred restore.

        BUG#1 + Finding 1: real WebKit fires MULTIPLE `changed` events and the
        layout lands ACROSS FRAMES — an intermediate height can look stable for
        one frame and must NOT consume the capture (audit: captured 300 → the
        premature consume read the intermediate 50 ≈ top). We therefore defer
        and require N=2 CONSECUTIVE stable idle checks before consuming.

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
        self._schedule_restore(vadj)

    def _schedule_restore(self, vadj) -> None:
        """Arm a settle check (idempotent — one pending at a time).

        Resets the consecutive-stability counter: any new `changed` means the
        height moved, so the run of stable frames restarts.
        """
        self._restore_stable_count = 0
        if self._restore_settle_source is not None:
            return  # already scheduled; the running settle re-reads upper
        self._restore_upper = vadj.get_upper()
        self._restore_settle_source = GLib.idle_add(self._settle_restore)

    def _settle_restore(self) -> bool:
        """Idle callback: consume the capture only after N consecutive stable
        checks (Finding 1 — cross-frame settle).

        Each call compares the current upper to the last-seen upper. If it
        moved, restart the stable run and re-arm. If unchanged, increment the
        counter; consume only when it reaches _RESTORE_STABLE_FRAMES.
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
            # Height moved — restart the stable run and re-check next frame.
            self._restore_upper = current_upper
            self._restore_stable_count = 0
            self._restore_settle_source = GLib.idle_add(self._settle_restore)
            return GLib.SOURCE_REMOVE
        if vadj.get_upper() <= vadj.get_page_size():
            return GLib.SOURCE_REMOVE  # collapsed again — leave pending
        self._restore_stable_count += 1
        if self._restore_stable_count < self._RESTORE_STABLE_FRAMES:
            # Stable so far, but not for enough consecutive frames yet.
            self._restore_settle_source = GLib.idle_add(self._settle_restore)
            return GLib.SOURCE_REMOVE
        # N consecutive stable frames — the layout has settled; consume.
        self._pending_restore = None  # consume BEFORE set_value (see below)
        was_at_bottom, captured_value = pending
        if was_at_bottom:
            # BUG#7: re-read the LIVE tracker immediately before driving the
            # adjustment. A user-grab during the cross-frame settle window
            # fires value-changed and re-arms _was_at_bottom=False; that fresh
            # intent supersedes the stale capture, so abort the at-bottom
            # action rather than yanking the reader back down. (The reader was
            # at-bottom so there is no reading position to restore.)
            if not self._was_at_bottom:
                return GLib.SOURCE_REMOVE
            vadj.set_value(vadj.get_upper() - vadj.get_page_size())
        else:
            vadj.set_value(captured_value)  # set_value clamps to [lower, max]
        # NOTE: consume-before-set_value is deliberate — set_value fires
        # `value-changed` (re-arming _was_at_bottom) but NOT `changed`, so it
        # cannot re-trigger this handler.
        return GLib.SOURCE_REMOVE

    def _do_render(self) -> bool:
        """Coalesced render — runs at most once per idle cycle.

        SPEC-17 SP1.1: BEFORE the load, capture the reader's DOCUMENT intent
        (at-bottom + y) into the single pending slot `_pending_scroll`, then
        load. The legacy GTK capture (`_pending_restore`) is retained
        unchanged — it is not the fix.
        """
        self._render_pending = False
        if self._destroyed:
            self._dirty = False
            return GLib.SOURCE_REMOVE
        if not self._dirty:
            return GLib.SOURCE_REMOVE
        if self._read_in_flight or self._load_in_flight:
            # A read OR a load is already in flight; its completion re-renders
            # this append:
            #   - `_read_in_flight`: the read callback loads the CURRENT rows
            #     (which include this append) and clears `_dirty`.
            #   - `_load_in_flight` (BUG#1): the FINISHED handler's dirty-kick
            #     re-renders. Issuing another read/load here would overwrite
            #     the single `_pending_scroll` slot (the queued-load race).
            # Leave `_dirty` set so this append is not dropped.
            self._dirty = True
            return GLib.SOURCE_REMOVE
        self._dirty = False
        self._rebuild_count += 1
        # Legacy GTK capture (MICRO smart-scroll, retained, NOT the fix):
        # capture the adjustment intent before the load collapses the height.
        vadj = self._scroll.get_vadjustment()
        if vadj is not None:
            self._pending_restore = (self._was_at_bottom, vadj.get_value())
        # SPEC-17 SP1.1: capture the DOCUMENT intent.
        if self._webview is None:
            # No web view yet — a fresh surface's first paint lands at the
            # bottom. Store at-bottom and load (no document to read).
            self._pending_scroll = (True, 0.0)
            self._issue_load(_document(list(self._rows)))
            return GLib.SOURCE_REMOVE
        # A document is already loaded — read its position FIRST; the read
        # callback stores the intent and THEN loads (spec: do not load until
        # the read finishes or fails).
        self._read_in_flight = True
        self._document_eval(self._read_scroll_script(), self._on_scroll_read)
        return GLib.SOURCE_REMOVE

    def _issue_load(self, doc: str) -> None:
        """BUG#1 (audit fix): the ONE place that arms `_load_in_flight` before
        a REAL load. Set here, NOT inside `_load_html` (the test monkeypatch
        seam), so the flag semantics survive monkeypatching. Every document
        load routes through this method.

        FIX ROUND 2 (BUG#1a): `_load_html` can RAISE (probe: `load_html(None,
        ...)` raises). The flag is armed BEFORE the call, so an escaping
        exception would leave it set forever → deterministic wedge. Catch it,
        clear the gate, log (house idle-callback discipline: an exception here
        would otherwise kill the render loop), and kick a re-render if a row is
        waiting so the append is not stranded.
        """
        self._load_in_flight = True
        try:
            self._load_html(doc)
            # ROUND 4 (spin latch): success clears the one-shot retry latch,
            # so the NEXT failure gets its own single retry.
            self._load_retry_pending = False
        except Exception:
            logger.exception("chat surface: document load raised")
            self._load_in_flight = False
            # ROUND 4 (spin latch): the re-queue is a ONE-SHOT retry. Round 3
            # re-queued UNCONDITIONALLY — a persistently-raising load re-fed
            # itself (~4,500 idle iterations/sec, probe-verified). Round 2's
            # `if self._dirty` never fired (call sites clear `_dirty` before
            # this call) and STRANDED the row instead. Both wrong. The latch
            # caps self-triggered retries at one; NEW rows arriving later
            # still re-queue normally (the latch only gates the except
            # branch's own kick).
            if not self._load_retry_pending:
                self._load_retry_pending = True
                self._schedule_render()

    def _on_web_process_terminated(self, view, reason) -> None:
        """FIX ROUND 2 (BUG#1b, HIGH): a web-process crash/OOM-kill fires
        `web-process-terminated`, which reaches NEITHER `load-failed` NOR
        FINISHED (probe-verified: 2/5 robust trials wedged). Clear the gate so
        later renders are not deferred forever, and clear the stale intent — a
        crashed web process consumed nothing. Kick a re-render if a row is
        waiting (the WebView recovers on the next fresh load — probe-verified).

        Signal arity `(view, reason)`; `reason` is a
        `WebKit.WebProcessTerminationReason` (unused here).
        """
        self._load_in_flight = False
        self._pending_scroll = None
        if self._destroyed:
            return
        if self._dirty:
            self._schedule_render()

    # ── SPEC-17 SP1: document scroll ──
    def _read_scroll_script(self) -> str:
        """SP1.1 pre-load read. The threshold is `_BOTTOM_THRESHOLD`,
        interpolated as a NUMBER (`:g` → "80") — never a second literal."""
        return (
            "(function () {"
            "  var el = document.scrollingElement || document.documentElement;"
            "  var y = el.scrollTop || 0;"
            "  var max = Math.max(0, el.scrollHeight - el.clientHeight);"
            f"  var atBottom = (max - y) <= {self._BOTTOM_THRESHOLD:g};"
            "  return JSON.stringify({y: y, atBottom: atBottom});"
            "})();"
        )

    def _scroll_to_script(self, y: float) -> str:
        """SP1.2 reading-preserve apply. `y` is formatted by PYTHON as a
        number — never a string interpolated from the page."""
        return (
            "(function () {"
            "  var el = document.scrollingElement || document.documentElement;"
            f"  el.scrollTop = {y!r};"
            "})();"
        )

    @staticmethod
    def _parse_scroll_read(text) -> tuple[bool, float]:
        """SP1.1: parse the read payload. ANY exception, timeout (missing
        callback → text None), or malformed payload → (True, 0.0)."""
        try:
            data = json.loads(text)
            return (bool(data["atBottom"]), float(data["y"]))
        except (TypeError, ValueError, KeyError):
            return (True, 0.0)

    def _on_scroll_read(self, value_text) -> None:
        """Read callback (SP1.1): store the intent, THEN load.

        Clears `_dirty` at LOAD time (mirroring `_do_render`'s synchronous
        path): the load below includes every row currently in `self._rows`,
        including any appended during the async read gap. Only an append
        AFTER this load (before FINISHED) re-sets `_dirty` and gets the
        SP1.2 re-render kick — so a coalesced batch still produces one load.

        FIX ROUND 2 (Issue #3): if `scroll_to_latest()` was pressed while this
        read was in flight, the read's (stale) position must NOT clobber the
        user's "go to latest" intent — honour the override and keep the armed
        `(True, 0.0)` bottom intent (the next FINISHED then applies
        `_BOTTOM_SCRIPT`).
        """
        self._read_in_flight = False
        if self._scroll_override:
            self._scroll_override = False  # consumed — keep the armed intent
            # ROUND 3 (closure audit BUG#1): the override protects an intent
            # that a crash may have cleared (`_on_web_process_terminated`
            # clears `_pending_scroll` but not the override). Self-heal to the
            # bottom intent — the button always wins (SP1.3 ruling).
            if self._pending_scroll is None:
                self._pending_scroll = (True, 0.0)
        else:
            self._pending_scroll = self._parse_scroll_read(value_text)
        if self._destroyed:
            return
        self._dirty = False
        self._issue_load(_document(list(self._rows)))

    def _on_load_changed(self, view, event) -> None:
        """SP1.2: apply the captured intent when the load FINISHES.

        We use the intent CAPTURED BEFORE the load — never the position read
        now (the load has already forced the document to the top; reading it
        here is exactly the bug).
        """
        if event != WebKit.LoadEvent.FINISHED:
            return
        if self._destroyed:
            return
        # BUG#1: the load reached its terminal event — release the gate so a
        # deferred append can re-render. Cleared BEFORE the dirty-kick so the
        # kicked render is not itself gated.
        self._load_in_flight = False
        pending = self._pending_scroll
        if pending is not None:
            at_bottom, y = pending
            script = _BOTTOM_SCRIPT if at_bottom else self._scroll_to_script(y)
            self._document_eval(script, lambda _text: None)
            self._pending_scroll = None
        # BUG#2 (audit fix round 2): the dirty-kick must run REGARDLESS of the
        # intent's presence — with `pending=None, dirty=True` (e.g. a
        # load-failed that never applied an intent) the row would otherwise
        # render only on a FUTURE append (delayed, not lost).
        if self._dirty:
            self._schedule_render()

    def _on_load_failed(self, view, event, uri, error) -> None:
        """BUG#1 wedge guard: a load that FAILS never reaches FINISHED.

        Without releasing the gate here, one load error leaves `_load_in_flight`
        True forever and every later render defers → the surface wedges (no new
        content ever renders).

        NOTE (deviation from the phase instructions): the instructions name
        `WebKit.LoadEvent.FAILED`, but WebKit 6.0 has NO such LoadEvent member
        (only STARTED/REDIRECTED/COMMITTED/FINISHED — probe-verified). The real
        failure mechanism is this `load-failed` SIGNAL, connected once in
        `_ensure_webview`.

        FIX ROUND 2 (BUG#1c): do NOT clear `_pending_scroll` here. On the real
        API a failed *navigation*'s `load-failed` is immediately followed by
        FINISHED (probe CASE B), which must still consume the pre-load intent;
        clearing it here made that FINISHED apply NOTHING (probe D: intent
        `(True, 900)` → applies=0). The gate is still cleared (harmless, and
        correct for a load that genuinely never finishes).
        """
        self._load_in_flight = False
        if self._destroyed:
            return
        if self._dirty:
            self._schedule_render()

    def _document_eval(self, script: str, callback) -> None:
        """SP1 seam — the ONE place that touches the WebKit C API.

        Runs `script` in a NAMED ISOLATED WORLD (`_DOC_WORLD`) and hands
        `callback` the value's string form, or None on any failure. Page
        JavaScript stays OFF (`set_enable_javascript(False)`); the isolated
        world is what makes app-side evaluation legal (C-API probe note at
        the top of this module). Tests monkeypatch THIS method.
        """
        def _finish(view, result, _data):
            try:
                text = view.evaluate_javascript_finish(result).to_string()
            except Exception:  # noqa: BLE001 — any failure is a failed read
                text = None
            callback(text)

        try:
            view = self._ensure_webview()
            view.evaluate_javascript(
                script, len(script.encode("utf-8")), _DOC_WORLD,
                None, None, _finish, None,
            )
        except Exception:
            logger.debug("evaluate_javascript failed", exc_info=True)
            callback(None)

    def scroll_to_latest(self) -> None:
        """SP1.3: the scroll-to-bottom button/API seam.

        ALWAYS arms the follow intent (the user pressed "go to latest" — the
        next append follows; a reader who wants to stop follows by scrolling
        up, which re-arms the tracker non-follow). Runs the bottom script now
        if a document is loaded; otherwise only stores the intent so the next
        FINISHED lands at the bottom.
        """
        self._was_at_bottom = True
        self._pending_scroll = (True, 0.0)
        if self._read_in_flight:
            # FIX ROUND 2 (Issue #3): a pre-load READ is in flight and will
            # overwrite `_pending_scroll` with the STALE position it captured
            # when it completes. Arm the override so that callback keeps the
            # (True, 0.0) intent instead — the next FINISHED then applies
            # `_BOTTOM_SCRIPT` (the user's "go to latest" wins).
            self._scroll_override = True
        if self._webview is not None:
            self._document_eval(_BOTTOM_SCRIPT, lambda _text: None)

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
            # SPEC-17 SP1.2: disconnect load-changed so a late FINISHED (mid-
            # load destroy) cannot issue a scroll script on a dead surface.
            if self._load_changed_handler_id:
                try:
                    self._webview.disconnect(self._load_changed_handler_id)
                except (TypeError, ValueError):  # already gone — non-fatal
                    logger.debug("load-changed handler already disconnected")
                self._load_changed_handler_id = 0
            # BUG#1: likewise disconnect load-failed.
            if self._load_failed_handler_id:
                try:
                    self._webview.disconnect(self._load_failed_handler_id)
                except (TypeError, ValueError):  # already gone — non-fatal
                    logger.debug("load-failed handler already disconnected")
                self._load_failed_handler_id = 0
            # FIX ROUND 2 (BUG#1b): disconnect web-process-terminated too — a
            # late crash signal on a dead surface must not schedule a render.
            if self._web_process_handler_id:
                try:
                    self._webview.disconnect(self._web_process_handler_id)
                except (TypeError, ValueError):  # already gone — non-fatal
                    logger.debug("web-process-terminated handler already disconnected")
                self._web_process_handler_id = 0
            self._scroll.set_child(None)
            self._webview = None
        # SPEC-17 SP1: the pending document intent is inert on a destroyed
        # surface (all its consumers guard on _destroyed) — clear for hygiene.
        self._pending_scroll = None
        self._read_in_flight = False
        self._load_in_flight = False
        self._scroll_override = False
        self._load_retry_pending = False


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
        # MICRO-SMART-SCROLL BUG#2 fix (2026-10-07): the fallback must keep
        # the SAME social-feed scroll contract as ChatSurface — follow new
        # rows only when the reader is already at the bottom, otherwise
        # preserve their reading position. The 18-site sweep removed the
        # handler-side force-scroll that used to cover this path; the surface
        # owns its scroll now, so the tracking lives HERE.
        #
        # The fallback appends IN PLACE (no full-document reload), so there
        # is no mid-load height collapse and NO capture/restore machinery —
        # just track at-bottom and conditionally scroll after the new line
        # has been laid out (GTK updates `upper` a frame later, so the follow
        # is idle-deferred).
        self._was_at_bottom = True
        self._follow_source = None
        vadj = self._scroll.get_vadjustment()
        if vadj is not None:
            self._bottom_adj = vadj
            self._bottom_handler_id = vadj.connect(
                "value-changed", self._on_scroll_value_changed
            )
        else:  # pragma: no cover — ScrolledWindow always has an adjustment
            self._bottom_adj = None
            self._bottom_handler_id = 0

    _BOTTOM_THRESHOLD = 80.0  # px — parity with ChatSurface / main_content

    def _on_scroll_value_changed(self, vadj) -> None:
        """Track at-bottom on the surface's OWN vadjustment (same 80px
        threshold as ChatSurface). Mirrors ChatSurface's guard: when the
        content is not scrollable (upper <= page_size) the value change is a
        layout artifact, not user intent — do not touch the tracker."""
        if vadj.get_upper() <= vadj.get_page_size():
            return
        self._was_at_bottom = (
            (vadj.get_upper() - vadj.get_page_size() - vadj.get_value())
            <= self._BOTTOM_THRESHOLD
        )

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
        # MICRO-SMART-SCROLL BUG#2: only follow if the reader was already at
        # the bottom. GTK recomputes `upper` one frame after the buffer
        # change lands, so scroll on the next idle (idempotent — one pending
        # follow at a time).
        if self._was_at_bottom:
            self._schedule_follow_bottom()

    def _schedule_follow_bottom(self) -> None:
        """Arm a deferred follow-to-bottom (idempotent — one pending at a
        time). The append already landed, but GTK updates the vadjustment's
        `upper` on the NEXT frame; scrolling synchronously would read the
        stale height and stop short of the true bottom."""
        if self._follow_source is not None:
            return
        self._follow_source = GLib.idle_add(self._follow_to_bottom)

    def _follow_to_bottom(self) -> bool:
        """Idle callback: drive the adjustment to the bottom, once.

        If the height has not landed yet (upper <= page_size) there is
        nothing to scroll to; we do NOT re-arm — the at-bottom tracker stays
        True, so the NEXT append arms a fresh follow once layout exists
        (avoids an unbounded idle spin on a never-allocated surface)."""
        self._follow_source = None
        vadj = self._bottom_adj
        if vadj is None:
            return GLib.SOURCE_REMOVE
        if vadj.get_upper() <= vadj.get_page_size():
            return GLib.SOURCE_REMOVE
        # BUG#7: re-read the LIVE tracker right before driving the adjustment.
        # A user-grab during the idle gap fires value-changed and re-arms
        # _was_at_bottom=False; that fresh intent supersedes the arm-time
        # capture, so abort the follow instead of yanking them back down.
        if not self._was_at_bottom:
            return GLib.SOURCE_REMOVE
        vadj.set_value(vadj.get_upper() - vadj.get_page_size())
        return GLib.SOURCE_REMOVE

    def scroll_to_latest(self) -> None:
        """SPEC-17 SP1.3: parity with ChatSurface.scroll_to_latest.

        The fallback appends in place (no document), so there is no script
        to run: arm the follow intent and drive the adjustment to the bottom.
        As with append, the drive is idle-deferred ("today's behavior" — GTK
        updates `upper` a frame after the layout, so a synchronous read would
        stop short); `_schedule_follow_bottom` is the existing one-place
        mechanism and re-reads the (True) intent before moving."""
        self._was_at_bottom = True
        self._schedule_follow_bottom()

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
        # MICRO-SMART-SCROLL BUG#2: cancel a pending follow and disconnect the
        # at-bottom tracker so a late height change cannot scroll a dead
        # surface (parity with ChatSurface's destroy contract).
        if self._follow_source is not None:
            try:
                GLib.source_remove(self._follow_source)
            except Exception:  # noqa: BLE001 — already gone: idempotent destroy
                logger.debug("fallback follow source already removed")
            self._follow_source = None
        if self._bottom_adj is not None:
            try:
                if self._bottom_handler_id:
                    self._bottom_adj.disconnect(self._bottom_handler_id)
            except (TypeError, ValueError):  # handler already gone — non-fatal
                logger.debug("fallback adjustment handler already disconnected")
            self._bottom_adj = None
            self._bottom_handler_id = 0


# FIX 4 (SP3 audit BUG #4): on a WebKit-less box the real ChatSurface would
# crash on first append (WebView is None) — the fallback IS the surface
# there. Same API, zero call-site branching.
if WebKit is None:
    ChatSurface = TextViewFallback  # deliberate module-level alias


def create_chat_surface(window_max: int = 500):
    """Call-site factory: resolves the surface class at CALL time (tests
    monkeypatch the module's WebKit binding; the import-time alias above
    covers genuinely WebKit-less boxes). This is the PRODUCTION entry:
    chat_render_handler._finalize mounts a surface through here. It is also
    a supported test monkeypatch seam (tests patch this symbol to inject a
    fake surface)."""
    if WebKit is None:
        return TextViewFallback(window_max)
    return ChatSurface(window_max)
