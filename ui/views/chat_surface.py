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
from html.parser import HTMLParser

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

# SPEC-19 SP2 §2.1: the whole-message ```live fence detector (pure — no
# rendering, no sanitizer changes). Imported here because the SURFACE owns the
# live tier decision (see `append_message`).
from render.html import live_fence

logger = logging.getLogger(__name__)

# DEVELCAKES_NO_WEBKIT=1 forces the TextViewFallback path (spec §7
# runtime-failure case). Needed on Ubuntu 24.04 with
# apparmor_restrict_unprivileged_userns=1: WebKit 6.0 IMPORTS fine but its
# bwrap sandbox + dbus-proxy cannot launch, so the first page render SIGTRAPs
# the whole app. The env var makes the import fail deliberately → the alias
# below routes to the fallback surface. Old CRABCAKES_ name rides the
# one-release fallback (utils.config.get_env — D2).
from utils.config import get_env
from utils.live_bridge import READ_ONLY_METHODS, LiveBridge
from utils.live_guard import LiveGuard
from utils.live_reader import read_local_image

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

# DEVELCAKES_LIVE_JS kill-switch (SPEC-19 SP2 — DEFAULT ON; =0 disables).
# When ON: the transcript view runs with JS enabled + the LiveGuard enforcement
# boundary attached, and appends become INCREMENTAL DOM injection via
# evaluate_javascript (the F1 decision) instead of a full-document load_html;
# a ```live fence appends a LIVE SECTION (T3).
# When OFF (=0): the pre-SP2 posture (JS off, full-document rebuild) and a
# ```live fence degrades to T2 static via sanitize_agent_html.
_LIVE_JS_FLAG = "LIVE_JS"


def _live_js_enabled() -> bool:
    """SPEC-19 §4 kill-switch — SP2 default flips ON.

    Live JS is ENABLED by default (`DEVELCAKES_LIVE_JS=1` is now redundant);
    `DEVELCAKES_LIVE_JS=0` disables it. Disabled → the chat surface keeps the
    SPEC-06 ruling (JS off, full-document rebuild) and a ```live fence degrades
    to T2 static (sanitize_agent_html — see chat_render_handler).

    "Set" is presence, not truthiness (utils.config.get_env D2): ONLY the
    exact string "0" disables; anything else (unset, "", "1", "true") is ON.
    Preserving the old-name fallback lets `CRABCAKES_LIVE_JS=0` still kill it.
    """
    try:
        return get_env(_LIVE_JS_FLAG) != "0"
    except Exception:  # noqa: BLE001 — flag read must never break surface construction
        return True


# SPEC-19 §3 E3 (SP1 stub): the injection IIFE. `json.dumps(html)` guarantees
# the payload is a SAFE JS string literal — markup is NEVER interpolated raw.
#
# FOLLOW TRANSACTION (SPEC-17 × SPEC-19 re-derivation, 2026-10-08): injection
# is ONE atomic scroll transaction in a single JS execution — measure
# at-bottom BEFORE the append, pin AFTER it (no async read/apply gap → no
# queued-append race). If the reader was at the bottom, a bounded rAF "settle
# tail" keeps the bottom pinned through post-append height growth
# (live-section animations — F10); it aborts the moment the reader scrolls
# away (never fights the reader) and is doubly bounded: exits after
# `_FOLLOW_SETTLE_FRAMES` frames with no height change, hard cap
# `_FOLLOW_TAIL_CAP` frames. The threshold is `_BOTTOM_THRESHOLD`
# interpolated as a NUMBER (`:g` → "80") — never a second literal.
# The SAME tail runs on the load-path bottom apply (`_bottom_and_settle_script`)
# — resurrection re-runs live-section timers and regrows heights AFTER the
# FINISHED apply (probe R7: a bottom reader drifted 960px without it).
_FOLLOW_SETTLE_FRAMES = 30  # ~0.5 s at 60 fps with no change → settled
_FOLLOW_TAIL_CAP = 420      # ~7 s — the tail must never run away


def _settle_tail_js() -> str:
    """The bounded rAF settle tail (shared by the injection transaction and
    the load-path bottom apply). Assumes `el` and `atBottom()` are in scope.
    Re-pins on every height change while the reader stays at the bottom;
    aborts the moment the reader scrolls away (never fights the reader);
    exits after `_FOLLOW_SETTLE_FRAMES` stable frames or the hard cap."""
    return (
        "  var lastH = el.scrollHeight;"
        "  var frames = 0;"
        "  var stable = 0;"
        "  (function tail() {"
        "    frames += 1;"
        f"    if (frames > {_FOLLOW_TAIL_CAP}) {{ return; }}"
        "    if (!atBottom()) { return; }"
        "    var h = el.scrollHeight;"
        "    if (h !== lastH) { lastH = h; stable = 0; el.scrollTop = h; }"
        f"    else {{ stable += 1; if (stable > {_FOLLOW_SETTLE_FRAMES}) {{ return; }} }}"
        "    window.requestAnimationFrame(tail);"
        "  })();"
    )


def _bottom_and_settle_script(threshold: float) -> str:
    """Bottom pin + settle tail — for applies onto a document whose LIVE
    sections will re-animate height AFTER this executes (resurrection re-runs
    their timers; probe R7: the reader at bottom drifted 960px without it)."""
    return (
        "(function () {"
        f"  var T = {threshold:g};"
        "  var el = document.scrollingElement || document.documentElement;"
        "  function max() { return Math.max(0, el.scrollHeight - el.clientHeight); }"
        "  function atBottom() { return (max() - (el.scrollTop || 0)) <= T; }"
        "  el.scrollTop = el.scrollHeight;"
        + _settle_tail_js()
        + "})();"
    )


def _inject_script(row_html: str, threshold: float) -> str:
    """Build the append-injection script for one row's HTML (json.dumps-safed).

    Atomic follow transaction (see the block comment above). Returns
    'followed' (reader was at bottom — pinned + settle tail armed) or 'held'
    (reader was up — position preserved, nothing pinned); probes read the
    value, the eval callback ignores it.
    """
    return (
        "(function () {"
        f"  var T = {threshold:g};"
        "  var el = document.scrollingElement || document.documentElement;"
        "  var host = document.getElementById('transcript');"
        "  if (!host) { host = document.documentElement; }"
        "  function max() { return Math.max(0, el.scrollHeight - el.clientHeight); }"
        "  function atBottom() { return (max() - (el.scrollTop || 0)) <= T; }"
        "  var wasBottom = atBottom();"
        "  var tpl = document.createElement('template');"
        f"  tpl.innerHTML = {json.dumps(row_html)};"
        "  host.appendChild(tpl.content);"
        "  if (!wasBottom) { return 'held'; }"
        "  el.scrollTop = el.scrollHeight;"
        + _settle_tail_js()
        + "  return 'followed';"
        "})();"
    )


# ── SPEC-19 SP2: the live-section machinery ──────────────────────────────
#
# A live section is `<section class="live-section" data-live-id="N">`. The
# agent payload is carried in an INERT JSON data island
# (`<script type="application/json" class="dc-live-src">JSON</script>`):
# markup embedded this way never executes — not via `template.innerHTML`
# (probe: SP2 probe row 2/4) and not via load_html (a JSON script is not a
# JS script; probe row A). Resurrection is an EXPLICIT, scoped pass that
# parses the island and re-creates only the inline scripts (E3).
#
# Why not embed the raw markup directly? Because the FULL-document rebuild
# path uses `load_html`, which DOES execute inline `<script>` in the document
# (probe row 1). The island keeps the rebuild inert so resurrection is the
# single execution point in every path.
#
# PROBE-PINNED (SP2, WebKit 2.52.6 / xvfb) — .debug/spec19_sp2_probe*.py:
#   * template.innerHTML <script> is inert; a RE-CREATED node executes.
#   * a JSON-MIME script never executes (island is safe in load_html).
#   * setTimeout/setInterval/rAF can be wrapped and the ORIGINAL clear
#     functions stop exactly the attributed handles (A stopped, B kept).
#   * stripping on* attributes makes a synthetic .click() a no-op.

_LIVE_SECTION_CAP = 10

# SPEC-19 SP2: an incremental live surface may accumulate orphaned DOM rows
# beyond the deque window (the deque evicts; the DOM keeps append-only). Once
# the DOM exceeds the deque by this slack, force the compaction rebuild so the
# DOM re-converges on the window (and evicted live sections flatten). Keeps the
# DOM bounded (P11 invariant) without a reload per append.
_DOM_COMPACT_SLACK = 100

# Timer shims (F9) — installed ONCE per WebView (window-guarded). Handles are
# attributed to the section whose scripts are currently executing: the
# resurrection wrapper sets `window.__dcCurrentLive = id` immediately before
# appending that section's scripts and clears it after. Shims read it.
_LIVE_SHIM_JS = (
    "if (!window.__dcTimersInstalled) {"
    "  window.__dcTimersInstalled = true;"
    "  window.__dcTimers = {handles: {}};"
    "  window.__dcCurrentLive = null;"
    "  var _st = window.setTimeout.bind(window);"
    "  var _si = window.setInterval.bind(window);"
    "  var _raf = window.requestAnimationFrame.bind(window);"
    "  window.__dcOrig = {"
    "    clearTimeout: window.clearTimeout.bind(window),"
    "    clearInterval: window.clearInterval.bind(window),"
    "    cancelAnimationFrame: window.cancelAnimationFrame.bind(window)"
    "  };"
    "  window.__dcRecord = function (id, kind) {"
    "    var live = window.__dcCurrentLive;"
    "    if (live === null) { return; }"
    "    var h = window.__dcTimers.handles;"
    "    var b = h[live] || (h[live] = {timeout: [], interval: [], raf: []});"
    "    b[kind].push(id);"
    "  };"
    "  window.setTimeout = function (fn, ms) {"
    "    var extra = [].slice.call(arguments, 2);"
    "    var id = _st(function () { fn.apply(null, extra); }, ms);"
    "    window.__dcRecord(id, 'timeout');"
    "    return id;"
    "  };"
    "  window.setInterval = function (fn, ms) {"
    "    var extra = [].slice.call(arguments, 2);"
    "    var id = _si(function () { fn.apply(null, extra); }, ms);"
    "    window.__dcRecord(id, 'interval');"
    "    return id;"
    "  };"
    "  window.requestAnimationFrame = function (fn) {"
    "    var id = _raf(function (ts) { fn(ts); });"
    "    window.__dcRecord(id, 'raf');"
    "    return id;"
    "  };"
    "}"
)

# SPEC-19 SP4: the page-side bridge API, installed ONCE per document (window-
# guarded, same init slot as the timer shims). `call(method, params)` returns a
# Promise that resolves ONLY when the native side resolves the call after human
# approval (F6: no timeout — approvals take minutes). Native picks calls up by
# polling __dcBridgeQueue (see _LIVE_BRIDGE_POLL_MS + the surface's poll);
# resolution arrives as a `develcakes:result` CustomEvent.
_LIVE_BRIDGE_INIT_JS = (
    "if (!window.develcakes) {"
    "  window.develcakes = {"
    "    _pending: {},"
    "    _seq: 0,"
    "    call: function (method, params) {"
    "      window.develcakes._seq++;"
    "      var id = 'dc' + window.develcakes._seq;"
    "      var p = new Promise(function (resolve) {"
    "        window.develcakes._pending[id] = resolve;"
    "      });"
    "      window.__dcBridgeQueue = window.__dcBridgeQueue || [];"
    "      window.__dcBridgeQueue.push({method: method, params: params || {}, id: id});"
    "      return p;"
    "    }"
    "  };"
    "}"
)

# Native-side resolution: resolve the page Promise for `ID` with `STATUS`.
# __DC_RESOLVE__ is replaced with a json.dumps payload (id/status/data) — never
# interpolated raw (json.dumps makes it a safe JS literal).
_LIVE_BRIDGE_RESOLVE_JS = (
    "(function () {"
    "  var msg = __DC_RESOLVE__;"
    "  var resolve = window.develcakes && window.develcakes._pending[msg.id];"
    "  if (!resolve) { return; }"
    "  delete window.develcakes._pending[msg.id];"
    "  try { resolve(msg); } catch (e) {}"
    "  document.dispatchEvent(new CustomEvent('develcakes:result',"
    "    {detail: msg}));"
    "})();"
)
# with `src` (removed — never appended); preserves `type`; IIFE-wraps the body
# so a section's top-level declarations cannot collide with another's.
_LIVE_RESURRECT_JS = (
    "(function () {"
    "  __SHIMS__"
    "  var ids = __IDS__;"
    "  for (var a = 0; a < ids.length; a++) {"
    "    var id = ids[a];"
    "    var secs = document.querySelectorAll("
    "      'section.live-section[data-live-id=\"' + id + '\"]');"
    "    for (var b = 0; b < secs.length; b++) {"
    "      var sec = secs[b];"
    "      var island = sec.querySelector('script.dc-live-src');"
    "      if (!island) { continue; }"
    "      var payload;"
    "      try { payload = JSON.parse(island.textContent); }"
    "      catch (e) { payload = ''; }"
    "      var tpl = document.createElement('template');"
    "      tpl.innerHTML = payload;"
    "      var scripts = [];"
    "      var nodes = [].slice.call(tpl.content.childNodes);"
    "      for (var c = 0; c < nodes.length; c++) {"
    "        var nd = nodes[c];"
    "        if (nd.nodeType === 1 && nd.tagName === 'SCRIPT') {"
    "          if (nd.getAttribute('src')) { continue; }"
    "          scripts.push(nd);"
    "        } else {"
    "          sec.appendChild(nd);"
    "        }"
    "      }"
    "      if (island.parentNode) { island.parentNode.removeChild(island); }"
    "      window.__dcCurrentLive = id;"
    "      for (var d = 0; d < scripts.length; d++) {"
    "        var s = document.createElement('script');"
    "        var t = scripts[d].getAttribute('type');"
    "        if (t) { s.setAttribute('type', t); }"
    "        s.text = '(function(){\\n' + scripts[d].textContent + '\\n})();';"
    "        sec.appendChild(s);"
    "      }"
    "      window.__dcCurrentLive = null;"
    "    }"
    "  }"
    "})();"
)

# F8/F9 flatten: remove scripts, strip on* attributes, clear EXACTLY this
# section's timers via the ORIGINAL clear functions.
_LIVE_FLATTEN_JS = (
    "(function () {"
    "  var ids = __IDS__;"
    "  var t = window.__dcTimers;"
    "  var orig = window.__dcOrig;"
    "  for (var a = 0; a < ids.length; a++) {"
    "    var id = ids[a];"
    "    var secs = document.querySelectorAll("
    "      'section.live-section[data-live-id=\"' + id + '\"]');"
    "    for (var b = 0; b < secs.length; b++) {"
    "      var sec = secs[b];"
    "      var sc = sec.querySelectorAll('script');"
    "      for (var c = 0; c < sc.length; c++) {"
    "        sc[c].parentNode.removeChild(sc[c]);"
    "      }"
    "      var all = sec.querySelectorAll('*');"
    "      for (var d = 0; d < all.length; d++) {"
    "        var at = all[d].attributes;"
    "        for (var e = at.length - 1; e >= 0; e--) {"
    "          if (/^on/i.test(at[e].name)) { all[d].removeAttribute(at[e].name); }"
    "        }"
    "      }"
    "    }"
    "    if (t && t.handles[id]) {"
    "      var h = t.handles[id];"
    "      for (var f = 0; f < h.timeout.length; f++) { orig.clearTimeout(h.timeout[f]); }"
    "      for (var g = 0; g < h.interval.length; g++) { orig.clearInterval(h.interval[g]); }"
    "      for (var i = 0; i < h.raf.length; i++) { orig.cancelAnimationFrame(h.raf[i]); }"
    "      delete t.handles[id];"
    "    }"
    "  }"
    "})();"
)


def _live_ids_js(ids) -> str:
    """JSON array of int ids — safe to interpolate (ints only)."""
    return json.dumps([int(i) for i in ids])


def _live_resurrect_js(ids) -> str:
    return _LIVE_RESURRECT_JS.replace(
        "__SHIMS__", _LIVE_SHIM_JS + _LIVE_BRIDGE_INIT_JS
    ).replace(
        "__IDS__", _live_ids_js(ids)
    )


def _live_flatten_js(ids) -> str:
    return _LIVE_FLATTEN_JS.replace("__IDS__", _live_ids_js(ids))


def _live_section_island_html(live_id: int, payload_html: str) -> str:
    """The section markup used by the rebuild/inject path: the payload in an
    inert JSON data island (never executes; resurrected explicitly).

    `<` is escaped to `\\u003c` (JSON string escape) so the island's text can
    never be closed early by a `</script>` inside the payload.
    """
    enc = json.dumps(payload_html).replace("<", "\\u003c")
    return (
        f'<section class="live-section" data-live-id="{int(live_id)}">'
        f'<script type="application/json" class="dc-live-src">{enc}</script>'
        "</section>"
    )


class _LiveHtmlFlattener(HTMLParser):
    """Python-side flatten mirror (linear, not a regex): drop `<script>`
    elements entirely and strip `on*` attributes. Used to emit a NEUTRALIZED
    section on a document rebuild for a section no longer in the live
    registry. Fail-closed: any parser error yields "" (never un-flattened
    payload)."""

    _VOID = frozenset({
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip_depth = 0

    def text(self) -> str:
        return "".join(self._out)

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._out.append(html.escape(data, quote=False))

    def handle_starttag(self, tag, attrs) -> None:
        if tag == "script":
            self._skip_depth += 1
            return
        if not self._skip_depth:
            self._out.append(self._start_tag(tag, attrs))

    def handle_startendtag(self, tag, attrs) -> None:
        if tag != "script" and not self._skip_depth:
            self._out.append(self._start_tag(tag, attrs))

    def handle_endtag(self, tag) -> None:
        if tag == "script":
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if not self._skip_depth and tag not in self._VOID:
            self._out.append(f"</{tag}>")

    @staticmethod
    def _start_tag(tag, attrs) -> str:
        kept = []
        for k, v in attrs:
            if k.lower().startswith("on"):
                continue
            if v is None:
                kept.append(f" {k}")
            else:
                kept.append(f' {k}="{html.escape(v, quote=True)}"')
        return f"<{tag}{''.join(kept)}>"


def _flatten_live_html(payload_html: str) -> str:
    """Neutralize a live payload for static re-emission (scripts + on* gone)."""
    parser = _LiveHtmlFlattener()
    try:
        parser.feed(payload_html)
        parser.close()
        return parser.text()
    except Exception:
        logger.debug("live flatten parse failed", exc_info=True)
        return ""


def _live_flattened_section_html(live_id: int, payload_html: str) -> str:
    return (
        f'<section class="live-section" data-live-id="{int(live_id)}">'
        f"{_flatten_live_html(payload_html)}</section>"
    )


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


def _blocks_html(rows: list[dict]) -> str:
    """Render a row list to grouped agent-box HTML (the inner DOM of
    #transcript). Shared by the full document build (`_document`) and the
    SPEC-19 incremental injection path — one markup source, no fork."""
    blocks = []
    i = 0
    n = len(rows)
    while i < n:
        row = rows[i]
        # SPEC-19 SP2: a live section is emitted VERBATIM as a TOP-LEVEL
        # child of #transcript (the spec's injection shape) — never wrapped in
        # the agent-box chrome. `row["html"]` holds either the inert island
        # markup (still live) or the flattened markup (evicted/flattened).
        if row.get("live_id") is not None:
            blocks.append(row["html"])
            i += 1
            continue
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
    return "".join(blocks)


def _document(rows: list[dict]) -> str:
    """Render rows as one full HTML document (the full-load path). The inner
    DOM is shared with the SPEC-19 injection path via `_blocks_html`."""
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<style>{_BASE_CSS}</style></head><body>"
        "<div id=\"transcript\">" + _blocks_html(rows) + "</div></body></html>"
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

    def __init__(self, window_max: int = 500,
                 live_bridge_approver=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._window_max = window_max
        self._rows: deque = deque(maxlen=window_max)
        self._stream_buffers: dict[str, list[str]] = {}
        # SPEC-19 SP2 §2.4: streamed live-fence placeholders (session_key → the
        # deque row dict shown as a code block until completion replaces it).
        self._stream_placeholders: dict[str, dict] = {}
        self._webview = None
        self._dirty = False
        self._render_pending = False
        self._rebuild_count = 0
        self._render_source = None
        self._destroyed = False
        # SPEC-19 SP1: live-JS prototype state (default OFF — see _live_js_enabled).
        self._live_js = _live_js_enabled()
        self._live_guard = None
        # SPEC-19 SP4: the two-phase action bridge. `_live_bridge` is the pure
        # LiveBridge (approver = the injected callback → window wires it to the
        # exec-approval card). Consequential calls are never executed here.
        # SPEC-20a: read_file is the read-only registry — the reader validates
        # and reads; the bridge only routes.
        self._live_bridge = LiveBridge(approver=live_bridge_approver)
        self._live_bridge.set_reader(read_local_image)
        # id → (was the call still pending?) used to route disk-backed results
        # to the page. The poll drains __dcBridgeQueue.
        self._bridge_poll_source = None
        # Rows already injected into the live #transcript (incremental append);
        # the full-document load_html path re-renders from scratch instead.
        # Monotonic (SP2): `_row_seq` counts ALL rows ever appended; the deque
        # window can evict old rows, so an INDEX into it stalls once saturated.
        # `_injected_seq` is the watermark of the newest row present in the
        # live DOM; `_dom_row_count` bounds DOM growth (compaction trigger).
        self._row_seq = 0
        self._injected_seq = 0
        self._dom_row_count = 0
        self._needs_compact = False
        # SPEC-19 SP2 live-section registry: live_id (int, monotonic) → entry
        # {"row": <the deque row dict>, "payload": <raw live payload>}. Insertion
        # order = oldest-first, which the cap uses to evict. A row's `html`
        # holds its CURRENT markup (island while live, flattened once evicted).
        self._live_sections: dict[int, dict] = {}
        self._next_live_id = 1
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
            # SPEC-19 SP1 (F1): with the live-JS flag ON the transcript runs
            # with JS enabled + the enforcement boundary attached; OFF keeps
            # the SPEC-06 ruling (JS off) byte-for-byte.
            live = self._live_js
            settings.set_enable_javascript(live)
            if live:
                self._live_guard = LiveGuard()
                self._live_guard.compile(self._on_live_guard_compiled)
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

    def _on_live_guard_compiled(self, content_filter) -> None:
        """SPEC-19 SP1: attach the enforcement boundary once the compiled
        filter is ready. FAIL-CLOSED: no filter → the guard refuses to attach;
        we log and leave the surface with JS on but NO boundary — so we must
        disable JS instead (never run live without the boundary)."""
        if content_filter is None:
            logger.error(
                "live_guard: content-filter compile FAILED — disabling live JS "
                "(never run the transcript without the enforcement boundary)"
            )
            if self._webview is not None:
                try:
                    self._webview.get_settings().set_enable_javascript(False)
                except Exception:
                    logger.debug("live_guard: could not disable JS", exc_info=True)
            self._live_guard = None
            return
        if self._live_guard is not None and self._webview is not None:
            self._live_guard.attach(self._webview)

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
        # SPEC-19 SP2 §2.3: a DOM/Python divergence (placeholder edit/removal,
        # or a flattened-evicted live section) forces a full-document reload —
        # the incremental path can only APPEND. Flatten evicted live sections
        # FIRST so the emitted document is already neutralized.
        if self._needs_compact:
            self._needs_compact = False
            self._reap_evicted_live()
            self._rebuild_live_document()
            return GLib.SOURCE_REMOVE
        # SPEC-19 SP1 (F1): with live JS ON and a document ALREADY loaded,
        # appends are INCREMENTAL — inject only the new row(s) via eval, no
        # full-document reload (live state survives). The initial load and the
        # compaction rebuild still use load_html (below / _on_scroll_read).
        if (self._live_js and self._webview is not None
                and self._injected_seq < self._row_seq):
            self._inject_new_rows()
            return GLib.SOURCE_REMOVE
        # Legacy GTK capture (MICRO smart-scroll, retained, NOT the fix):
        # capture the adjustment intent before the load collapses the height.
        vadj = self._scroll.get_vadjustment()
        if vadj is not None:
            self._pending_restore = (self._was_at_bottom, vadj.get_value())
        # SPEC-17 SP1.1: capture the DOCUMENT intent.
        if self._webview is None or self._live_js:
            # No web view yet, OR the live path's FIRST load — a fresh
            # surface's first paint lands at the bottom; no document to read.
            self._pending_scroll = (True, 0.0)
            self._injected_seq = self._row_seq
            self._dom_row_count = len(self._rows)
            self._issue_load(_document(list(self._rows)))
            return GLib.SOURCE_REMOVE
        # A document is already loaded — read its position FIRST; the read
        # callback stores the intent and THEN loads (spec: do not load until
        # the read finishes or fails).
        self._read_in_flight = True
        self._document_eval(self._read_scroll_script(), self._on_scroll_read)
        return GLib.SOURCE_REMOVE

    def _rebuild_live_document(self) -> None:
        """Full-document reload for a live surface (compaction). The document
        emits inert islands for live sections and flattened markup for
        evicted ones; FINISHED then resurrects the STILL-LIVE sections.

        FOLLOW (SPEC-17 model, injection regime): the reader's DOCUMENT
        position is CAPTURED (read → `_on_scroll_read`) before the reload and
        applied at FINISHED — a compaction must not yank a mid-history reader
        to the bottom. This path is hit by streaming live-fence placeholder
        refreshes (`_update_stream_placeholder`), `end_stream` placeholder
        removal, and eviction/orphan compactions — all must honour the
        captured intent, never assume bottom.
        """
        vadj = self._scroll.get_vadjustment()
        if vadj is not None:
            self._pending_restore = (self._was_at_bottom, vadj.get_value())
        self._injected_seq = self._row_seq
        self._dom_row_count = len(self._rows)
        if self._webview is not None:
            # Capture the DOCUMENT intent FIRST; the read callback issues the
            # load (SP1.1: do not load until the read finishes or fails).
            self._read_in_flight = True
            self._document_eval(self._read_scroll_script(), self._on_scroll_read)
            return
        # No document yet (placeholder churn before the first paint) — a
        # fresh first load lands at the bottom by construction.
        self._pending_scroll = (True, 0.0)
        self._issue_load(_document(list(self._rows)))

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

    def _inject_new_rows(self) -> None:
        """SPEC-19 SP1 F1: append the not-yet-injected rows incrementally.

        Only rows with `seq > _injected_seq` are injected; the document is NOT
        reloaded, so live DOM state survives appends. HTML is passed as a
        json.dumps string literal (never interpolated raw into JS). Injection
        is in the MAIN world (the loaded document's world) — live scripts run.

        Monotonic model (SP2): `_injected_seq` is a watermark over `seq`, NOT
        an index into the (evictable) deque — indices stall once the window
        saturates. If the deque evicted rows that were never injected (DOM
        would permanently retain orphan rows), force a compaction instead. A
        bounded DOM (`_dom_row_count`) triggers compaction too (bounded-memory
        invariant under the live path).

        SPEC-19 SP2: any live SECTIONS in the delta are injected as inert JSON
        islands; after injection lands, resurrection runs SCOPED to exactly
        those new ids (their inline scripts execute).
        """
        rows = list(self._rows)
        new_rows = [r for r in rows if r.get("seq", 0) > self._injected_seq]
        if not new_rows:
            self._injected_seq = self._row_seq
            return
        # Orphan guard: the oldest surviving row's seq must be exactly the
        # next-to-inject seq. Otherwise the deque dropped un-injected rows and
        # an incremental append cannot represent the DOM → compact. Also
        # compact once the DOM accumulates a SLACK of orphaned rows beyond the
        # deque window (bounded-memory invariant: DOM ≤ window + slack).
        oldest_seq = rows[0].get("seq", 0)
        if (oldest_seq > self._injected_seq + 1
                or self._dom_row_count > len(rows) + _DOM_COMPACT_SLACK):
            self._needs_compact = True
            self._schedule_render()
            return
        html = _blocks_html(new_rows)
        self._injected_seq = new_rows[-1].get("seq", self._row_seq)
        self._dom_row_count += len(new_rows)
        live_ids = [r["live_id"] for r in new_rows if r.get("live_id") is not None]

        def _after_inject(_t):
            if live_ids and not self._destroyed:
                self._document_eval_main(
                    _live_resurrect_js(live_ids), lambda _x: None)
                # SPEC-19 SP4: a live section exists → start the bounded poll
                # (and drain immediately in case the section already called).
                self._schedule_bridge_poll()
                self._drain_bridge_queue()

        self._document_eval_main(
            _inject_script(html, self._BOTTOM_THRESHOLD), _after_inject)


    def _document_eval_main(self, script: str, callback) -> None:
        """Evaluate `script` in the document's MAIN world (live content world).
        Distinct from `_document_eval`, which runs in the isolated scroll world.
        """
        def _finish(view, result, _data):
            try:
                text = view.evaluate_javascript_finish(result).to_string()
            except Exception:  # noqa: BLE001 — any failure is a failed eval
                text = None
            callback(text)

        try:
            view = self._ensure_webview()
            view.evaluate_javascript(
                script, len(script.encode("utf-8")), None,
                None, None, _finish, None,
            )
        except Exception:
            logger.debug("chat surface: injection eval failed", exc_info=True)
            callback(None)

    # ── SPEC-19 SP4: the two-phase action bridge ──
    _LIVE_BRIDGE_POLL_MS = 500  # bounded poll while live sections exist

    def _drain_bridge_queue(self, _text=None) -> None:
        """Pull page-initiated bridge calls and dispatch them (no execution).

        Runs after each injection/resurrection eval AND on a bounded idle poll
        (500ms) while a live section exists. Polling (not a script-message
        handler) is chosen deliberately: no new WebKit surface is needed, and
        the queue is tiny (page-driven).
        """
        if not self._live_js or self._webview is None or self._destroyed:
            return

        def _got(text):
            if self._destroyed:
                return
            ids = []
            try:
                queued = json.loads(text) if text else []
            except (TypeError, ValueError):
                queued = []
            if not isinstance(queued, list):
                queued = []
            for item in queued:
                if not isinstance(item, dict):
                    continue
                method = item.get("method")
                params = item.get("params") or {}
                call_id = item.get("id")
                if not isinstance(method, str) or not isinstance(call_id, str):
                    continue
                # Consequential: {status:pending}; the page Promise stays open
                # until resolve_bridge_call. Read-only (SPEC-20a): the result
                # is already final and rides the SAME develcakes:result seam.
                result = self._live_bridge.dispatch(method, params)
                if method in READ_ONLY_METHODS:
                    self._dispatch_bridge_result(result)
                ids.append(call_id)
            if ids:
                self._schedule_bridge_poll()

        self._document_eval_main(
            "JSON.stringify(window.__dcBridgeQueue ? "
            "(function(){var q=window.__dcBridgeQueue;window.__dcBridgeQueue=[];"
            "return q;})() : [])",
            _got,
        )

    def _schedule_bridge_poll(self) -> None:
        """One bounded idle poll (idempotent — one pending at a time)."""
        if self._bridge_poll_source is not None or self._destroyed:
            return
        self._bridge_poll_source = GLib.timeout_add(
            self._LIVE_BRIDGE_POLL_MS, self._bridge_poll_tick
        )

    def _bridge_poll_tick(self) -> bool:
        self._bridge_poll_source = None
        if self._destroyed or not self._live_js:
            return GLib.SOURCE_REMOVE
        if not self._live_sections:
            return GLib.SOURCE_REMOVE  # no live sections → stop polling
        self._drain_bridge_queue()
        # Re-arm only while sections remain (self-limiting).
        if self._live_sections and not self._destroyed:
            self._schedule_bridge_poll()
        return GLib.SOURCE_REMOVE

    def resolve_bridge_call(self, call_id: str, ok: bool, data=None) -> None:
        """SPEC-19 SP4 resolution path: called by the window when the human
        resolves the approval card. Records the outcome and dispatches the
        `develcakes:result` event so the page's Promise resolves."""
        def _on_result(result):
            self._dispatch_bridge_result(result)

        self._live_bridge.resolve(call_id, ok, data, on_result=_on_result)

    def _dispatch_bridge_result(self, result: dict) -> None:
        """Eval the resolution into the page (CustomEvent + Promise resolve)."""
        if self._webview is None or self._destroyed:
            return
        script = _LIVE_BRIDGE_RESOLVE_JS.replace(
            "__DC_RESOLVE__", json.dumps(result)
        )
        self._document_eval_main(script, lambda _t: None)

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
        # SPEC-19 SP2: a full-document (re)load re-emits live sections as
        # INERT JSON islands — resurrect every STILL-LIVE section now (the
        # load wiped the previous document's scripts + timer registry). Runs
        # in the MAIN world; scoped to the live registry's ids.
        if self._live_js and self._live_sections:
            self._document_eval_main(
                _live_resurrect_js(list(self._live_sections.keys())),
                lambda _t: None,
            )
            # SPEC-19 SP4: live sections present after reload → keep polling.
            self._schedule_bridge_poll()
        pending = self._pending_scroll
        if pending is not None:
            at_bottom, y = pending
            if at_bottom and self._live_js and self._live_sections:
                # Resurrection (above) re-runs section timers → heights
                # re-animate AFTER this apply. Keep the bottom pinned through
                # the regrowth window (probe R7 — 960px drift without this).
                script = _bottom_and_settle_script(self._BOTTOM_THRESHOLD)
            else:
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
            # With live sections present the document may re-animate height
            # right after this — arm the settle tail together with the pin.
            script = (_bottom_and_settle_script(self._BOTTOM_THRESHOLD)
                      if (self._live_js and self._live_sections) else _BOTTOM_SCRIPT)
            self._document_eval(script, lambda _text: None)

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
        self._append_row({
            "role": role if role in ("user", "agent", "system") else "system",
            "html": _cap_row_html(html_fragment),
            "agent": agent_name or "",
            "color": _sanitize_color(agent_color),
        })

    def _append_row(self, row: dict) -> None:
        """Assign a monotonic seq and append (single append chokepoint)."""
        self._row_seq += 1
        row["seq"] = self._row_seq
        self._rows.append(row)
        self._schedule_render()

    # ── SPEC-19 SP2: the live tier ──
    def is_live_enabled(self) -> bool:
        """True when this surface runs the live tier (JS on + guard attached).

        The render handler branches on this to choose between the live append
        (T3) and the T2-static degrade (sanitize_agent_html) for a ```live
        fence. The flag is resolved once at construction.
        """
        return bool(self._live_js)

    def append_live(self, payload_html: str, agent_name: str | None = None,
                    agent_color: str | None = None) -> int | None:
        """SPEC-19 SP2 §2.2 T3: append an agent ```live payload as a LIVE
        SECTION of #transcript.

        The payload travels as an inert JSON data island; the section is
        registered and (if the document is loaded) resurrected immediately.
        Unlike `append_message`, a live row carries `live_id` and its `html`
        is the island markup, re-emitted verbatim by `_blocks_html`.

        Cap (§4): appending past `_LIVE_SECTION_CAP` flattens the OLDEST live
        section FIRST (its scripts/on*/timers neutralized), then registers the
        new one. Bounded-memory invariant holds by construction.

        Returns the assigned live id (or None if the surface is destroyed).
        """
        if self._destroyed:
            return None
        live_id = self._next_live_id
        self._next_live_id += 1
        payload_html = _cap_row_html(payload_html)
        row = {
            "role": "agent",
            "html": _live_section_island_html(live_id, payload_html),
            "agent": agent_name or "",
            "color": _sanitize_color(agent_color),
            "live_id": live_id,
        }
        self._append_row(row)
        self._live_sections[live_id] = {"row": row, "payload": payload_html}
        # Cap overflow: flatten oldest-first until within the cap.
        while len(self._live_sections) > _LIVE_SECTION_CAP:
            oldest = next(iter(self._live_sections))
            self._flatten_live_section(oldest, dom=True)
        return live_id

    def _reap_evicted_live(self) -> None:
        """Compaction: a live row that fell out of the deque window is no
        longer present in the rebuilt document — flatten its DOM section +
        deregister (it becomes plain static DOM)."""
        rows = list(self._rows)

        def _present(row) -> bool:
            return any(r is row for r in rows)

        for lid in list(self._live_sections.keys()):
            if not _present(self._live_sections[lid]["row"]):
                self._flatten_live_section(lid, dom=True)

    def _flatten_live_section(self, live_id: int, dom: bool = True) -> None:
        """F8/F9 full neutralization of ONE section.

        - JS (dom=True): remove its script nodes, strip every on* attribute,
          clear EXACTLY its recorded timers (original clear fns), deregister.
        - Python: rewrite the row's `html` to flattened section markup so any
          future full-document rebuild emits it INERT (no island, no scripts).

        A DOM flatten makes the Python row markup (island) DIVERGE from the
        DOM (flattened) — mark the surface for a compaction rebuild so the two
        re-converge on the next render.
        """
        entry = self._live_sections.pop(live_id, None)
        if entry is None:
            return
        payload = entry["payload"]
        entry["row"]["html"] = _live_flattened_section_html(live_id, payload)
        if dom and self._webview is not None and not self._destroyed:
            self._document_eval_main(
                _live_flatten_js([live_id]), lambda _t: None
            )

    def stream_delta(self, session_key: str, text: str, agent_name: str | None = None) -> None:
        """Buffer a streaming delta — NOTHING renders until end_stream.

        REGISTER (SP3 audit r2, accepted class): pre-flush chunks accumulate
        unbounded until end_stream (P11 register item).

        SPEC-19 §2.4: when the accumulating text is a live fence IN PROGRESS
        (` ```live ` opener present), a plain CODE-BLOCK placeholder is shown —
        the live section is only created at COMPLETION (end_stream). This is
        the streaming rule; end_stream REPLACES the placeholder.
        """
        self._stream_buffers.setdefault(session_key, []).append(text)
        joined = "".join(self._stream_buffers[session_key])
        if self._live_js and self._looks_like_live_stream(joined):
            self._update_stream_placeholder(session_key, joined, agent_name)

    @staticmethod
    def _looks_like_live_stream(text: str) -> bool:
        return text.lstrip().lower().startswith("```live")

    def _update_stream_placeholder(self, session_key: str, joined: str,
                                   agent_name) -> None:
        """Render/refresh the streaming placeholder row (a code block) for a
        live-fence-in-progress. Idempotent per session — one placeholder row.

        Any change to an already-injected placeholder forces a compaction
        rebuild (the incremental path cannot edit an existing row's DOM).
        """
        body = html.escape(joined).replace("\n", "<br>")
        existing = self._stream_placeholders.get(session_key)
        if existing is None:
            row = {
                "role": "agent",
                "html": f'<pre class="stream-placeholder">{body}</pre>',
                "agent": agent_name or "",
                "color": "",
            }
            self._stream_placeholders[session_key] = row
            self._append_row(row)
        else:
            existing["html"] = f'<pre class="stream-placeholder">{body}</pre>'
            self._needs_compact = True
            self._schedule_render()

    def end_stream(self, session_key: str, agent_name: str | None = None) -> None:
        """Flush the buffered stream as ONE atomic message row.

        SPEC-19 §2.4: a completed live fence REPLACES its streaming
        placeholder with the live section (T3 path); anything else keeps the
        existing escaped-row behavior. Removing the placeholder row makes the
        DOM diverge from the deque → force a compaction rebuild so the
        placeholder disappears and the live section appears.
        """
        chunks = self._stream_buffers.pop(session_key, None)
        placeholder = self._stream_placeholders.pop(session_key, None)
        if placeholder is not None:
            try:
                self._rows.remove(placeholder)
            except ValueError:
                pass
            self._needs_compact = True
        if not chunks:
            if self._needs_compact:
                self._schedule_render()
            return
        joined = "".join(chunks)
        payload = live_fence(joined) if self._live_js else None
        if payload is not None:
            self.append_live(payload, agent_name=agent_name)
            return
        self._append_row({
            "role": "agent",
            "html": _cap_row_html(html.escape(joined).replace("\n", "<br>")),
            "agent": agent_name or "",
            "color": "",
        })

    def clear(self) -> None:
        """SPEC-19 SP4 follow-up (the /clear UI plane): empty the transcript
        in place — the surface STAYS mounted and renderable.

        Resets every content state: rows, live sections (timers cleared via
        the flatten path's registry — a dropped section must not tick), the
        two stream buffers, the injected-seq watermark and DOM counter (the
        incremental-injection bookkeeping), then issues a fresh empty
        document load. Deliberately NOT `close_session`-style: no tombstone,
        the tab keeps rendering (the "Cleared…" confirmation appends after).

        Idempotent; safe on a destroyed surface (no webview → state reset
        only, no eval).
        """
        if self._destroyed:
            return
        self._rows.clear()
        self._stream_buffers.clear()
        self._stream_placeholders.clear()
        # Live sections: drop their timers + registry (the DOM is about to be
        # replaced wholesale, but the TIMER REGISTRY is window-global — clear
        # it so no orphaned interval keeps ticking against a dead section id).
        if self._webview is not None and not self._destroyed and self._live_sections:
            self._document_eval_main(
                _live_flatten_js(list(self._live_sections.keys())),
                lambda _t: None,
            )
        self._live_sections.clear()
        self._next_live_id = 1
        self._injected_seq = 0
        self._dom_row_count = 0
        self._pending_scroll = None
        self._dirty = False
        # Fresh empty document (a real load, not an injection — the
        # incremental path can only append; from zero we must rebuild).
        self._issue_load(_document(list(self._rows)))

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
        if self._bridge_poll_source is not None:
            try:
                GLib.source_remove(self._bridge_poll_source)
            except RuntimeError:
                logger.debug("bridge poll source already fired — nothing to cancel")
            self._bridge_poll_source = None
        if self._render_source is not None:
            try:
                GLib.source_remove(self._render_source)
            except RuntimeError:
                logger.debug("render source already fired — nothing to cancel")
            self._render_source = None
        self._render_pending = False
        self._dirty = False
        self._stream_buffers.clear()
        self._stream_placeholders.clear()
        self._rows.clear()
        self._live_sections.clear()
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
            # SPEC-19 SP1 audit BUG#1: detach the LiveGuard BEFORE tearing the
            # webview down. detach() removes the content filter + disconnects
            # the decide-policy handler; a leaked GObject handler against a
            # freed WebView segfaults (becomes a real crash on every chat-tab
            # close once SP2 flips the live-JS default ON). getattr discipline:
            # `_live_guard` may be absent on older/test instances.
            _guard = getattr(self, "_live_guard", None)
            if _guard is not None:
                try:
                    _guard.detach(self._webview)
                except Exception:
                    logger.debug("live_guard detach failed during destroy",
                                 exc_info=True)
                self._live_guard = None
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

    def __init__(self, window_max: int = 500,
                 live_bridge_approver=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._window_max = window_max
        self._rows: deque = deque(maxlen=window_max)
        self._stream_buffers: dict[str, list[str]] = {}
        # SPEC-19 SP4: signature parity with ChatSurface (the live bridge is a
        # no-op here — the fallback has no JS/document).
        self._live_bridge_approver = live_bridge_approver
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

    def clear(self) -> None:
        """SPEC-19 SP4 follow-up (the /clear UI plane): parity with
        ChatSurface.clear — reset rows + stream buffers, keep the surface
        mounted and renderable. The TextView IS the document; clearing the
        buffer empties it (no reload step needed). Idempotent post-destroy
        (same guard contract as ChatSurface.clear)."""
        if self._destroyed:
            return
        self._stream_buffers.clear()
        self._rows.clear()
        self._view.get_buffer().set_text("")

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


def create_chat_surface(window_max: int = 500, live_bridge_approver=None):
    """Call-site factory: resolves the surface class at CALL time (tests
    monkeypatch the module's WebKit binding; the import-time alias above
    covers genuinely WebKit-less boxes). This is the PRODUCTION entry:
    chat_render_handler._finalize mounts a surface through here. It is also
    a supported test monkeypatch seam (tests patch this symbol to inject a
    fake surface).

    SPEC-19 SP4: `live_bridge_approver` is threaded to the surface's
    LiveBridge (window wires it to the exec-approval card).
    """
    if WebKit is None:
        return TextViewFallback(window_max, live_bridge_approver=live_bridge_approver)
    return ChatSurface(window_max, live_bridge_approver=live_bridge_approver)
