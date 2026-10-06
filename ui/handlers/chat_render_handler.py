# ui/handlers/chat_render_handler.py
# Chat render handler — routes transcript append/stream paths to the
# display-keyed (one per project box) HTML chat surface (SPEC-06 R2 Phase A,
# SP4 repoint; key domain migrated to display keys by SPEC-12 SP2).
#
# SPEC-06 SP4 (R2A): the Pango bubble pipeline is RETIRED for the transcript
# role. render_async/render_sync/streaming now route through
# render/html.render_document (markdown → HTML → sanitize, ALWAYS in the
# path) into a display-keyed ChatSurface (ui/views/chat_surface.py) — the
# cache key is `mount_key or session_key`, so every agent rendering into one
# project tab shares ONE surface. Per
# ruling R1 the surface owns the widget tree: on_bubble_ready fires with
# None (all callers already tolerate None — `if bubble is not None` guards
# verified at chat_handler :228/:532 and agent_runtime_handler :2101/:2116/
# :2280/:2316/:2438).
#
# Security: No secrets, no file I/O, no network calls.
#
# Thread safety: all GTK calls dispatched via GLib.idle_add when GLib is set.
# If GLib is None (tests), GTK calls are made directly — only safe when
# the caller is already on the main thread.
#
# Reentrancy guard: _ReentrancySet prevents concurrent renders for the same
# session_key. If a render is already in-flight for a key, subsequent calls
# are skipped silently.
#
# Public API (unchanged signatures — out-of-scope callers, e.g.
# agent_runtime_handler's streaming extraction, keep working):
#   render_async / render(role, text, session_key, on_bubble_ready, ...)
#       → surface.append_message; on_bubble_ready(None) on the main thread.
#   render_sync(role, text, session_key=None, ...) -> None
#       → surface.append_message; returns None (R1 contract).
#   start_streaming / update_streaming / end_streaming / is_streaming /
#   get_streaming_text / set_streaming_text
#       → buffered streaming with a REPLACEABLE pending buffer (see
#         _stream_text below — why surface.stream_delta is not used).
#   close_session(session_key)
#       → destroys that session's surface (SP3 destroy contract) AND any
#         surface MOUNTED in that key's box (SP5a FIX 10: project close
#         kills the agent-keyed surfaces mounted in the project box) and
#         tombstones both keys — late renders are dropped, not resurrected.
#   render_event_card / render_task_card
#       → UNCHANGED Pango cards (not transcript sites; R3 catalog untouched).

import html as _html
import logging

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk

from render.html import render_document
from render.sanitize import sanitize_html
from ui.views.chat_surface import create_chat_surface
from utils.escaping import xml_template
from concurrent.futures import ThreadPoolExecutor

_logger = logging.getLogger(__name__)

# SPEC-06 SP5c-1 — the welcome content, carried through the SAME fail-closed
# pipeline as agent text (render_document at emission; constraint 2). The
# Pango bubble's logo is NOT rebuilt: survey verdict (2026-09-25) —
# render/html emits no <img> at all (markdown images → "[alt]" text, register
# ruling), and the sanitizer's src filter admits http(s) only, so a logo
# (file path / data: URI) cannot pass without WEAKENING the policy, which
# constraint 1 forbids. Text-only + CSS-class hook instead: the class rides
# the sanitizer's existing class-token allowlist (added there), so SP-later
# styling/themeing can target .welcome-row without another policy change.
# SP5c-1-audit BUG #4 (round 3 ruling, option a): the markdown is a SINGLE
# INLINE RUN and the welcome-row class rides the emitter's <p> DIRECTLY
# (`class` is admitted for `p` in render/sanitize._ATTRIBUTES — additive).
# No wrapper element exists anymore: the emitter hardcodes <p> and has NO
# inline mode, so any wrapper (span/div) would nest blocks-invalidly.
# "Project Development Environment" stays — the SP5c-1 identity pin asserts
# it.
_WELCOME_CLASS = "welcome-row"
_WELCOME_MARKDOWN = (
    "**DevelCakes** — Project Development Environment. "
    "Development happens here, as a group chat."
)


class _ReentrancySet:
    """
    Tracks which session keys are currently being rendered.

    Prevents concurrent renders for the same session — if a render is
    already in-flight for a key, subsequent calls for that key are skipped.
    """

    def __init__(self):
        self._keys: set[str] = set()

    def add(self, key: str) -> bool:
        """Add a key. Returns True if not already present (not in flight)."""
        if key in self._keys:
            return False
        self._keys.add(key)
        return True

    def remove(self, key: str):
        """Remove a key when rendering is complete."""
        self._keys.discard(key)

    def __contains__(self, key: str) -> bool:
        return key in self._keys


def _surface_role(role: str) -> str:
    """Map handler roles ("You"/"Agent"/"System") to surface roles
    ("user"/"agent"/"system") — unknown values fall back to system."""
    return {"You": "user", "Agent": "agent"}.get(role, "system")


class ChatRenderHandler:
    """
    Routes chat transcript content to the display-keyed HTML chat surface
    (one surface per project box; SPEC-12).

    SPEC-06 SP4 pipeline (replaces the Pango bubble pipeline):
      text → render/html.render_document()   (markdown → HTML → nh3 sanitize,
                                              fail-closed — ALWAYS in the path)
           → ChatSurface.append_message()    (display-keyed, windowed deque)

    Feature parity (ruling R2 — dispositions):
      DROPPED for Phase A (documented): forward buttons, copy buttons,
        agent color tint, per-row timestamp header, tight grouping, and the
        render-time crabcard registry (ARH's own extraction path is
        untouched). FORWARD via the toolbar still works; the registry
        retires with the bubble pipeline in SP5.
      KEPT: reentrancy guard, error fallback (escaped raw text — still
        sanitized), buffered streaming with a final atomic row, Pango
        event/task/diff cards (render_event_card — not transcript sites).

    Args:
        GLib_module: gi.repository.GLib or None — for thread-safe GTK calls
    """

    def __init__(self, GLib_module=None):
        self._GLib = GLib_module
        self._reentrancy = _ReentrancySet()
        # SPEC-06 SP4: display-keyed chat surfaces (SPEC-12: one per project
        # box; ruling R1 — the surface owns the widget tree). Lazily
        # created; destroy via close_session.
        self._surfaces: dict = {}
        self._on_forward_message = None   # set via set_on_forward_message()
        self._main_content = None
        # SPEC-06 SP4: streaming pending buffers. Handler-side REPLACEMENT
        # buffer (not surface.stream_delta) because set_streaming_text — a
        # live agent_runtime_handler dependency, out of scope this round —
        # must be able to OVERWRITE the pending text (crabcard cleaning),
        # and the surface's stream buffer is append-only by contract.
        self._streaming: set[str] = set()
        self._stream_text: dict[str, str] = {}
        # FIX 6 (SP4 audit): streaming role carried to the final row
        # (start_streaming(role=...) → end_streaming renders with it).
        self._stream_role: dict[str, str] = {}
        # SPEC-06 SP5a (R1): session→chat-box callable, injected by
        # main_content.set_chat_render_handler. On first surface create the
        # handler mounts the surface into the session's chat box.
        self._container_getter = None
        # SPEC-06 SP5c-1: welcome already shown for these keys. Once per
        # MOUNT: close_session removes the key (fresh tab = fresh welcome);
        # SP5a's tombstone-pop path (pop_tombstones_for_box) also clears it
        # so a project reopen re-welcomes exactly like a fresh mount. Bounded
        # by the surface lifecycle — entries live only while their surface
        # does.
        self._welcome_shown: set[str] = set()
        # SP5a FIX 10/#4 (round 2): mount-relationship lifecycle.
        #   _closed_sessions — tombstones: a closed key's LATE renders are
        #     dropped instead of resurrecting an unmounted orphan surface.
        #     Cleared when the key's box is LIVE again (tab/project reopen =
        #     legit new render → fresh surface). Keyed dict (bounded by
        #     sessions-ever-closed) to keep insertion order debuggable.
        #   _mount_misses — consecutive None-getter misses per display key.
        #     FIX 11: at _MOUNT_MISS_LIMIT the unmountable surface is evicted
        #     (recreated lazily) so a dead getter can't accumulate surfaces.
        #   _surfaces_by_parent — id(chat_box) → surface index for the O(1)
        #     surface_for_box scan (FIX 11). Identity re-checked on read:
        #     id() reuse after a box dies must not return a stale surface.
        self._closed_sessions: dict[str, bool] = {}
        # r3 FIX 3: sk → the BOX key it died mounted in (fan-out tombstones
        # are cleared per-box at reopen by pop_tombstones_for_box).
        self._mounted_box_keys: dict[str, str] = {}
        self._mount_misses: dict[str, int] = {}
        self._surfaces_by_parent: dict[int, object] = {}

    # ── Thread pool for off-main-thread processing ──────────────────
    _pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="crabcakes-render")

    # ── Surface lifecycle (SPEC-06 SP4) ─────────────────────────────

    # FIX 11 (round 2): consecutive None-getter mount misses before the
    # unmountable surface is evicted (recreated lazily on a later render).
    _MOUNT_MISS_LIMIT = 3

    def set_chat_container_getter(self, getter) -> None:
        """SPEC-06 SP5a (R1): inject the session→chat-box callable.

        RULING R1, option (a): the HANDLER owns mounting — on first surface
        create it asks the getter for the session's chat box and packs the
        surface in. One wiring point (main_content.set_chat_render_handler);
        no window.py edits; SP5c's bubble-pipeline deletion cannot disturb it.
        (SP5c-2 Phase A note: the bubble pipeline's builders now live in
        ui/views/event_cards.py; this handler imports from there.)

        FIX 10 (round 2): a NEW getter is the wiring signal of a reopened
        tab/project — r3 FIX 3 REMOVES the global tombstone clear() that
        lived here (the latent trap: ANY setter call, incl. test wiring,
        resurrected every closed session). Tombstones now clear ONLY per-key
        via pop_tombstones_for_box, driven by create_chat_tab."""
        self._container_getter = getter

    def _mount_surface(self, key: str, surface, mount_key: str | None = None) -> bool:
        """Mount the surface into a chat box — IDEMPOTENT RETRY (FIX 1).

        SPEC-12: the first arg is the surface's DISPLAY key (the cache key it
        lives under); it only feeds the getter fallback.

        FIX 1 (SP5a audit BUG #1): the old one-shot skipped mounting forever
        when the box didn't exist yet at surface creation (early render /
        project-routing), leaving the surface permanently unmounted. Now
        every _surface_for call retries until the surface HAS a parent:

        - surface.get_parent() is not None → already mounted, return True
          (the retry is a cheap attribute read on the hot path);
        - getter is None or no box for the key → still works unmounted, the
          NEXT render retries (returns False);
        - FIX 3 (single-scroll): the mount appends the surface DIRECTLY —
          the surface owns its own ScrolledWindow (chat_surface.py), no
          wrapper is created here (the old wrapper double-scrolled).

        Returns:
            bool — True if the surface is mounted after this call, False on
            every early return. SP5a r3 FIX 1: this contract is what makes
            the _surface_for miss-counter RESET live; without it a transient
            miss after a successful mount evicted a LIVE surface.
        """
        if surface.get_parent() is not None:
            return True  # already mounted — nothing to do
        getter = self._container_getter
        if getter is None:
            return False
        # FIX 2: mount into the RESOLVED display key's box (project tabs);
        # falls back to the surface's display key (personal tabs unchanged).
        chat_box = getter(mount_key or key)
        if chat_box is None:
            return False
        chat_box.append(surface)
        # FIX 11 (round 2): maintain the id(box) → surface index alongside
        # the mount so surface_for_box is O(1). Identity re-checked on read
        # (id reuse after a box dies → identity check False → miss, no stale
        # surface returned).
        self._surfaces_by_parent[id(chat_box)] = surface
        return True  # FIX 1 (r3): bool contract — the miss reset reads this

    def surface_for_box(self, chat_box):
        """FIX 3 (SP5a audit) — the seam for the single-scroll ruling:
        return the surface mounted in chat_box, or None if the box holds no
        surface (e.g. a Pango welcome bubble only). main_content.
        scroll_chat_to_bottom uses this to drive the SURFACE's own
        vadjustment instead of a wrapper's.

        FIX 11 (round 2): O(1) via the id(box) index kept by _mount_surface
        (round 1 scanned every surface's parent — O(n)). Identity is
        RE-CHECKED against the live parent: id() reuse after a box dies
        must not return a stale surface. Index miss → authoritative scan
        (and index repair) keeps correctness primary."""
        if chat_box is None:
            return None
        indexed = self._surfaces_by_parent.get(id(chat_box))
        if indexed is not None and indexed.get_parent() is chat_box:
            return indexed
        for surface in self._surfaces.values():
            if surface.get_parent() is chat_box:
                self._surfaces_by_parent[id(chat_box)] = surface
                return surface
        return None

    def surface_for_key(self, session_key: str):
        """SPEC-07 SP1: READ-ONLY surface lookup by DISPLAY key (or None).

        SPEC-12: the arg is a display key (`mount_key or session_key` — for
        a project tab, `project:<name>`) — the surface CACHE is display-
        keyed. A legacy agent key (`agent:coder`) is no longer a cache key
        and returns None.

        Deliberately NOT _surface_for() — that method creates and mounts on
        a miss; status resolution runs on a 250ms tick and must be
        side-effect free. A miss here simply means "no surface yet" →
        caller renders nothing.
        """
        return self._surfaces.get(session_key)

    def _surface_for(self, session_key: str, mount_key: str | None = None):
        """Lazy per-PROJECT surface (SPEC-12: display-keyed).

        SPEC-12 BUG#1 fix: the cache is keyed by the DISPLAY key
        (`mount_key or session_key`), NOT the raw session key — one surface
        per project box. Mount retries (FIX 1) and eviction (FIX 11) are
        tracked on the same display key. Streaming stays session-keyed
        (see _stream_text/_streaming) — do not touch those here.

        FIX 1: _mount_surface runs on EVERY call — idempotent (parent guard)
        until the box exists, so a None-getter at creation is recovered on
        the next render (the SP5a blank-window case, incl. project routing).
        FIX 11 (round 2): a surface still unmounted after _MOUNT_MISS_LIMIT
        consecutive getter misses is EVICTED (destroyed; recreated lazily if
        a render comes later) — a dead getter can't accumulate surfaces.
        """
        display_key = mount_key or session_key
        surface = self._surfaces.get(display_key)
        if surface is None:
            surface = create_chat_surface()
            self._surfaces[display_key] = surface
        # SP5a FIX 1: retry — idempotent on every call (parent guard).
        mounted = self._mount_surface(display_key, surface, display_key)
        # FIX 11: track consecutive mount misses — ONLY when a getter is
        # actually wired (a None-getter-at-all is the pre-wiring state; the
        # round-1 FIX 1 retry semantics apply there, and unit surfaces that
        # work unmounted must not be evicted for it). A wired getter that
        # keeps returning None is the dead-wiring case the cap bounds.
        if mounted:
            self._mount_misses.pop(display_key, None)
        elif surface.get_parent() is None and self._container_getter is not None:
            self._mount_misses[display_key] = self._mount_misses.get(display_key, 0) + 1
        if (not mounted
                and surface.get_parent() is None
                and self._container_getter is not None
                and self._mount_misses.get(display_key, 0) >= self._MOUNT_MISS_LIMIT):
            surface.destroy()
            self._surfaces.pop(display_key, None)          # BUG#1b: no KeyError
            self._mount_misses.pop(display_key, None)
            # SP5c-1-audit BUG #3: the eviction destroys the surface — the
            # welcome flag must die with it (a recreated surface is a fresh
            # mount and must re-welcome). Without the discard, the stale
            # flag suppresses the recreated surface's welcome (same
            # lockstep as close_session / pop_tombstones_for_box).
            self._welcome_shown.discard(display_key)
            # FIX 7 (r3): this is a MESSAGE DROP, not silent cleanup — the
            # render that triggered eviction is lost. REGISTER: the
            # lost-message window is now exactly "dead getter at the 4th
            # consecutive render" (FIX 1's live reset shrank it from
            # any-transient-miss to this shape); it closes entirely when the
            # getter re-wires (tab reopen) or a later render mounts.
            _logger.warning(
                "chat surface evicted after %d consecutive mount misses — "
                "dropped render for display_key=%r (dead getter / closed tab)",
                self._MOUNT_MISS_LIMIT, display_key)
            # Brief contract: recreate LAZILY — this render drops (the SP3
            # destroyed-surface contract already swallows late appends), so
            # no live-but-orphaned surface lingers while wiring is dead.
            return None
        return surface

    def close_session(self, session_key: str, box=None) -> None:
        """Destroy one session's surface (SP3 destroy contract: idempotent,
        cancels pending renders, drops the webview).

        FIX 10/#4 (round 2): mount-relationship lifecycle — closes (a) the
        sk-keyed surface AND (b) every surface MOUNTED in a box belonging to
        this key (project close must kill agent-keyed surfaces mounted in
        the project box — their mount parent is dying). Both keys are
        TOMBSTONED so a late in-flight render after close is DROPPED instead
        of resurrecting an unmounted orphan surface.

        SP5a r3: (b) is a FAN-OUT — every surface with `get_parent() is
        box` dies (r2's surface_for_box+break killed only the first of N in
        multi-agent projects). Tombstones record each dying surface's BOX
        key so the reopen path (create_chat_tab → pop_tombstones_for_box)
        can clear exactly the right set per key (the r2 global clear() in
        set_chat_container_getter is REMOVED — that was the latent trap).

        SP5a-audit FIX (BUG #2, pre-existing since 61de23cb0): `box` — the
        production caller (_close_tab) passes the dying tab's chat box
        object directly, captured BEFORE it is popped from the tracking
        dicts. The getter fallback (resolving session_key → box by
        iterating _tab_sessions) returns None on that path — the key is
        already popped when close_session runs — which made the entire
        fan-out dead in production (probe: sp5c1-fanout-probe.py). The box
        argument wins when present; the getter remains the fallback for
        direct close_session(key) calls (tests, non-tab callers).

        SPEC-12 key domain: `session_key` here is the closing TAB's key,
        which IS its display key (`project:<name>` for a project tab) — the
        tombstone and the pop below hit the DISPLAY-keyed structures
        unchanged. The fan-out loop stays DEFENSIVE ("plus any surface
        mounted in the passed box"): each victim's key `sk` is now a
        display key (the cache is display-keyed), tombstoned on itself;
        one box holds one surface, so N≤1 in the normal case. Streaming
        structures stay SESSION-keyed — untouched here."""
        self._closed_sessions[session_key] = True
        surface = self._surfaces.pop(session_key, None)
        if surface is not None:
            # FIX 10 hygiene (round 2): unparent BEFORE destroy — GTK destroy
            # does not reliably detach the child, and the dead surface must
            # not linger visibly in the box (pinned by the lifecycle tests).
            if surface.get_parent() is not None:
                surface.unparent()
            surface.destroy()
        # (b) kill ALL surfaces MOUNTED to this key's box. BUG #2 FIX: the
        # box comes from the caller when the dying tab passed it (production
        # _close_tab); the getter is the fallback (resolves by iterating
        # _tab_sessions — None for an already-popped key). Unmounted
        # surfaces (no box) simply aren't mounted to it and are left for
        # their own key. FIX 2 (r3): iterate+destroy ALL matches (was:
        # first-only via surface_for_box + break). FIX 6 (r3): the box's
        # index entries are popped here — dead ids must not linger in
        # _surfaces_by_parent.
        resolved_box = box
        if resolved_box is None:
            getter = self._container_getter
            resolved_box = getter(session_key) if getter is not None else None
        if resolved_box is not None:
            box_id = id(resolved_box)
            for sk, s in list(self._surfaces.items()):
                if s.get_parent() is resolved_box:
                    self._closed_sessions[sk] = True
                    self._mounted_box_keys[sk] = session_key
                    del self._surfaces[sk]
                    # SP5c-1: fan-out victims lose their welcome flags too
                    # (the project box died — reopen = fresh mount).
                    self._welcome_shown.discard(sk)
                    if s.get_parent() is not None:
                        s.unparent()
                    s.destroy()
            self._surfaces_by_parent.pop(box_id, None)
        self._streaming.discard(session_key)
        self._stream_text.pop(session_key, None)
        self._stream_role.pop(session_key, None)
        # SP5c-1: a closed key's surface is gone — its welcome flag goes
        # with it (a fresh surface is a fresh mount = fresh welcome).
        self._welcome_shown.discard(session_key)

    def pop_tombstones_for_box(self, box_key: str) -> None:
        """SP5a r3 FIX 3 — the reopen signal AT THE REAL ENTRY POINT: called
        from create_chat_tab. Clears the reopened key's tombstone AND the
        tombstones of any surfaces that died MOUNTED in that key's box
        (project reopen: the agent surfaces killed by the close are wanted
        again — per-key fan-pop, NOT the removed global clear())."""
        self._closed_sessions.pop(box_key, None)
        # SP5c-1 re-welcome pin: the reopened BOX key re-welcomes too —
        # project-tab reopen must not stay suppressed (the box's welcome
        # rode the box key).
        self._welcome_shown.discard(box_key)
        for sk in [k for k, v in self._mounted_box_keys.items() if v == box_key]:
            self._closed_sessions.pop(sk, None)
            self._mounted_box_keys.pop(sk, None)
            # SP5c-1: reopen resets the welcome (fresh mount semantics) —
            # kept in lockstep with the tombstone fan-pop above.
            self._welcome_shown.discard(sk)

    def pop_tombstone(self, session_key: str) -> None:
        """SP5a FIX 10: clear one tombstone — the reopen signal. The next
        render for this key legitimately creates a fresh surface (a session
        re-created after close is NOT late-render noise)."""
        self._closed_sessions.pop(session_key, None)

    def _append_to_surface(self, role: str, text: str, session_key: str | None, agent_name=None,
                           mount_key: str | None = None):
        """Compose (markdown → sanitized HTML) and append to the session
        surface. Sanitize is ALWAYS in the path here — SP6's guard pins
        this call site. Raw-HTML fallback only if composition itself
        raises, and even that goes through html.escape (never raw).

        FIX 2: mount_key threads through so a project-routed reply mounts
        its surface in the project tab's box (see _surface_for)."""
        try:
            html_fragment = render_document(text)
        except Exception:
            _logger.exception("render_document failed — appending escaped raw text")
            html_fragment = _html.escape(text) + "<!-- fallback: escaped raw -->"
        # FIX 10 (round 2): tombstone check BEFORE _surface_for — a closed
        # session's late render is DROPPED (an unmounted orphan surface must
        # not resurrect after close). NOT cleared here: closure is only
        # reversed by re-wiring (reopen path), not by the late render itself.
        # SPEC-12 BUG#1a: tombstones are DISPLAY-keyed — a late render for a
        # session routed into a closed project tab dies on the TAB's key.
        display_key = mount_key or session_key or ""      # SPEC-12 BUG#1a
        if self._closed_sessions.get(display_key):
            return
        surface = self._surface_for(session_key or "", mount_key=mount_key)
        # FIX 11: _surface_for returns None when the unmountable surface was
        # JUST evicted (lazy recreation) — this render drops, exactly like
        # the tombstone path above.
        if surface is None:
            _logger.debug("render dropped: surface evicted after mount misses display_key=%r", display_key)
            return
        surface.append_message(_surface_role(role), html_fragment, agent_name=agent_name)
        # FIX 1 (r3): the round-2 parent re-read reset that lived here is
        # REMOVED — the _mount_surface bool contract (read inside
        # _surface_for) is now the SOLE miss-reset mechanism, per the audit's
        # either/or. Two reset paths made the return contract untestable.

    def render_welcome(self, session_key: str) -> None:
        """SP5c-1 — emit the HTML-native welcome row for this session's
        surface, ONCE per surface mount.

        Brief constraint 3: the HANDLER owns emission (SP5a ruling (a) —
        the handler owns mounting); main_content stays dumb (it just calls
        render_welcome at tab creation, same line count as the retired
        Pango call). No render_sync role-magic: the welcome is NOT a
        transcript row, so it bypasses the reentrancy guard (it composes
        synchronously on the main thread — no off-thread pool involved).

        Sanitizer guard (constraint 3/gate 3): the content is composed with
        render_document and PASSES THE RESULT THROUGH sanitize_html AGAIN —
        belt-and-braces so this site independently witnesses the fail-closed
        path. If the pipeline ever returns "" (fail-closed), nothing is
        emitted (no empty row). Brief fallback: text-only content (the
        logo cannot pass the http(s)-only src policy — survey verdict, see
        the module-top comment on _WELCOME_MARKDOWN).

        Semantics (constraint 4): once per surface mount — a per-key flag
        (self._welcome_shown) suppresses re-emission on tab reopen (the
        surface and its document state survive reopen; the flag mirrors the
        surface's lifetime exactly — cleared at close/fan-out/tombstone-pop).

        SPEC-12: the arg is the TAB key, which IS its display key (project
        tab → `project:<name>`); every gate here runs on the display key.
        """
        display_key = session_key or ""
        if display_key in self._welcome_shown:
            return
        # Tombstone drop (mirrors _append_to_surface's guard order): a
        # closed key's late welcome is DROPPED — _surface_for does NOT know
        # about tombstones (the check lives in the append path), so it must
        # be explicit here or the welcome leaks onto the orphan surface.
        if self._closed_sessions.get(display_key):
            return
        # Guard order (mirrors _append_to_surface): surface FIRST — if the
        # unmountable surface was just evicted (FIX 11, lazy recreation),
        # this emission drops with the render; the flag is NOT consumed so
        # the recreated surface still gets its welcome.
        surface = self._surface_for(display_key)
        if surface is None:
            return
        if display_key in self._welcome_shown:
            return
        # Composition — the SAME pipeline as agent content, then re-sanitize
        # (constraint 2: never raw HTML into the surface). The stable CSS
        # hook (constraint 1) cannot ride the markdown TEXT (render/html is
        # escape-first — it would be escaped); it is stamped onto the emitted
        # BLOCK node and must survive the sanitizer (welcome-row token is in
        # the class-token allowlist; `class` is admitted for `p` — additive
        # entries, no policy weakening).
        # SP5c-1-audit BUG #4 (round 3 ruling, option a): the class rides the
        # <p> DIRECTLY — no wrapper element at all. History: the span wrapper
        # produced invalid nesting (span around the emitter's block <p> — the
        # emitter hardcodes <p> and has NO inline mode, so the round-2
        # single-run restructure could not remove it). A wrapper is not used
        # because span is inline (invalid around a block) and div is not in
        # the sanitizer's tag allowlist — not because div-around-p nesting
        # would be invalid. The re-sanitize below is the correctness gate: if
        # the class does not survive (no block tag / policy regression) the
        # welcome is emitted UNSTYLED; the re-sanitize guarantees it is never
        # RAW. (No class-survival fallback branch exists — it was removed
        # with the round-2 machinery.)
        try:
            inner = render_document(_WELCOME_MARKDOWN)
            if not inner:
                # Fail-closed composition returned "" — no empty row.
                return
            html_fragment = sanitize_html(inner.replace("<p>", f'<p class="{_WELCOME_CLASS}">', 1))
        except Exception:  # fail-closed: drop the welcome, never raw HTML
            _logger.exception("welcome compose failed — welcome dropped")
            return
        if not html_fragment:
            return  # fail-closed returned "" — no empty welcome row
        if display_key in self._welcome_shown:
            return
        surface.append_message("system", html_fragment)
        self._welcome_shown.add(display_key)

    # ── Async (thread-safe) ──────────────────────────────────────────────

    def render_async(self, role: str, text: str, session_key: str, on_bubble_ready, on_forward_click=None, on_error=None, agent_name: str = None, agent_color: str = None, mount_key: str | None = None):
        """
        Compose HTML off-thread, append to the session surface on main.

        RULING R1: on_bubble_ready fires with None — the surface already
        displayed the message. Callers' existing None-guards are verified.
        on_forward_click/agent_color are accepted for signature compat and
        ignored (dropped for Phase A, ruling R2).

        SPEC-12 BUG#4+#10: pass mount_key for any non-project caller; the
        surface cache is display-keyed (mount_key or session_key). SP4c
        threads the mount_key at the chat_handler call sites.

        Args:
            role:           "You", "Agent" or "System"
            text:           Raw message text
            session_key:    For reentrancy guarding and surface selection
            on_bubble_ready: callback(None) — called on main thread
            on_error:       optional callback(error_msg) — called on main thread
            mount_key:      display key the surface is cached/mounted under
                            (project-routed renders); None → session_key
        """
        if not self._reentrancy.add(session_key):
            return  # render already in flight

        def _compose_off_thread():
            try:
                # Heavy pure-Python work — no GTK calls. sanitize runs here.
                html_fragment = render_document(text)

                def _append_on_main():
                    try:
                        # FIX 10 (round 2): tombstone check in the async path
                        # too — close_session during an in-flight compose
                        # drops the late render (no resurrection).
                        # SPEC-12 BUG#1a: the tombstone set is DISPLAY-keyed.
                        display_key = mount_key or session_key
                        if self._closed_sessions.get(display_key):
                            return
                        surface = self._surface_for(session_key, mount_key=mount_key)
                        # FIX 11: evicted-just-now surface → drop (lazy
                        # recreation contract, same as _append_to_surface).
                        if surface is None:
                            return
                        surface.append_message(
                            _surface_role(role), html_fragment, agent_name=agent_name
                        )
                    except Exception:
                        _logger.exception("surface append failed — escaped raw text fallback")
                        try:
                            self._surface_for(session_key, mount_key=mount_key).append_message(
                                _surface_role(role),
                                _html.escape(text) + "<!-- fallback: escaped raw -->",
                                agent_name=agent_name,
                            )
                        except Exception:
                            _logger.exception("surface fallback append failed")
                    finally:
                        self._reentrancy.remove(session_key)
                        # R1: the surface owns the widget tree — no bubble.
                        on_bubble_ready(None)

                self._dispatch(_append_on_main)
            except Exception as exc:
                self._reentrancy.remove(session_key)
                if on_error:
                    self._dispatch(lambda err=exc: on_error(str(err)))

        self._pool.submit(_compose_off_thread)

    def render(self, role: str, text: str, session_key: str, on_bubble_ready, on_forward_click=None, on_error=None):
        """Legacy async entry — same surface path as render_async (R1:
        on_bubble_ready fires with None)."""
        self.render_async(
            role, text, session_key,
            on_bubble_ready=on_bubble_ready,
            on_forward_click=on_forward_click,
            on_error=on_error,
        )

    # ── Sync (main thread only) ──────────────────────────────────────────

    def set_on_forward_message(self, cb):
        """Set callback for forward button: cb(text, anchor_widget).

        Kept: the FORWARD toolbar button still works in Phase A (ruling R2
        disposition (b)) even though per-row forward buttons are dropped."""
        self._on_forward_message = cb

    # set_on_crabcard_extracted deleted (SP5c-3): storage-only setter, zero
    # callers — lineage: extraction wiring died with SP5c-2 B.2's window
    # callback removal; agent_runtime_handler owns extraction upstream.

    def set_project_name(self, name: str) -> None:
        """Set the active project name (kept for caller compat)."""
        self._project_name = name

    def set_main_content(self, main_content) -> None:
        """Set MainContent reference for scroll operations and agent name lookup."""
        self._main_content = main_content

    def _resolve_agent_color(self, agent_name: str) -> str | None:
        """Resolve hex color for an agent name (3-tier fallback).

        SPEC-06 SP4: color tint is dropped for Phase A (ruling R2) — kept
        only because set-signature callers may still probe it; no longer
        used by the render paths."""
        if not agent_name:
            return None
        # Tier 1: live agent
        if self._main_content is not None:
            agent_mgr = getattr(self._main_content, '_agent_mgr', None)
            if agent_mgr is not None:
                color = agent_mgr.get_color(agent_name)
                if color:
                    return color
        # Tier 2: special agent role
        from agent.special_agents import get_special_agents
        from models.colors import color_for_special_agent
        for agent_def in get_special_agents():
            if agent_def.display_name == agent_name:
                return color_for_special_agent(agent_def.role)
        # Tier 3: deterministic default
        return "#6366f1"

    def render_sync(self, role: str, text: str, session_key: str = None, on_forward_click=None, forwarded_from: str = None, agent_name: str = None, tab_key: str = None, mount_key: str | None = None):
        """
        Append to the session surface synchronously. Returns None (R1).

        WARNING: Only call this when already on the GTK main thread.

        Args:
            role:  "You" or "Agent"
            text:  Raw message text
            session_key: Session key for surface selection.
            agent_name: Optional agent display name. If None and role is "Agent",
                        looked up from _main_content._agent_mgr using session_key.
            mount_key: FIX 2 — key of the box the surface mounts in
                       (project-routed replies pass the resolved box key;
                       None → mounts/verifies under session_key). SPEC-12:
                       the surface CACHE is DISPLAY-keyed
                       (mount_key or session_key), not session-keyed.

        Returns:
            None — ALWAYS (ruling R1: the surface owns the widget tree;
            callers' `if bubble is not None` guards skip the append).
        """
        _ = (on_forward_click, forwarded_from, tab_key)  # compat; dropped for Phase A
        if agent_name is None and role == "Agent" and session_key and self._main_content is not None:
            agent_mgr = getattr(self._main_content, '_agent_mgr', None)
            if agent_mgr is not None:
                agent_name = agent_mgr.get_name(session_key)
        self._append_to_surface(role, text, session_key, agent_name=agent_name,
                                mount_key=mount_key)

    # ── Streaming (SPEC-06 SP4) ────────────────────────────────────────

    def start_streaming(self, session_key: str, container=None, role: str = "Agent"):
        """
        Begin a streaming session: buffer deltas, render at end_streaming.

        SPEC-06 SP4: no widget is created here — the surface shows nothing
        until the final atomic row (SP3 stream contract: buffer now, one
        row at end). Re-starting an active session finalizes it first.
        """
        if session_key in self._streaming:
            self.end_streaming(session_key)
        self._streaming.add(session_key)
        self._stream_text[session_key] = ""
        self._stream_role[session_key] = role

    def is_streaming(self, session_key: str) -> bool:
        """Return True if a streaming session is active for session_key."""
        return session_key in self._streaming

    def get_streaming_text(self, session_key: str) -> str | None:
        """
        Get the current accumulated plain text for a streaming session.

        Used by AgentRuntimeHandler to extract crabcards from the accumulated
        streaming text before end_streaming() finalizes the row.
        Returns None if no streaming session exists for this session.
        """
        if session_key not in self._streaming:
            return None
        return self._stream_text.get(session_key)

    def set_streaming_text(self, session_key: str, text: str) -> bool:
        """
        Overwrite the accumulated streaming text for a session.

        Used by AgentRuntimeHandler after extracting crabcards — sets the
        cleaned text so end_streaming() renders the row without crabcard
        blocks. Returns True if successful, False if no streaming session.
        """
        if session_key not in self._streaming:
            return False
        self._stream_text[session_key] = text
        return True

    def update_streaming(self, session_key: str, delta_text: str):
        """
        Buffer the streaming text for session_key (nothing renders yet).

        The gateway sends FULL cumulative text in each delta — the buffer is
        REPLACED, not appended (do not double-accumulate). Renders at
        end_streaming as ONE atomic sanitized row (SP3 stream contract).

        Safe to call from the GTK main thread (no GTK work is done here).
        """
        if session_key not in self._streaming:
            _logger.debug(
                "update_streaming: SKIP sk=%r not in _streaming",
                session_key,
            )
            return
        self._stream_text[session_key] = delta_text

    def end_streaming(self, session_key: str, agent_name: str = None, render: bool = True,
                      mount_key: str | None = None):
        """
        End streaming for session_key: append the final atomic row.

        FIX 7 (round 2): mount_key threads through _finalize into
        _append_to_surface — production ALWAYS streams, so without this the
        round-1 mount_key fixes (wired only into render_sync) never fired on
        the real path; project-routed sessions kept blank windows.

        The buffered text is composed (markdown → sanitized HTML) and
        appended as ONE row. With render=False the buffer is dropped and
        nothing renders (caller renders the final text itself, e.g. via
        render_sync after crabcard cleaning).

        Args:
            session_key: The conversation key whose streaming buffer to finalize.
            agent_name: Optional explicit display name (bypasses the
                agent_mgr.get_name() lookup).
            render: Append the final row (default True).
            mount_key: FIX 7 — key of the box the surface mounts in
                (project-routed replies pass the resolved key; None →
                mounts/verifies under session_key). SPEC-12: the surface
                CACHE is DISPLAY-keyed (mount_key or session_key), not
                session-keyed.
        """
        if session_key not in self._streaming:
            return

        self._streaming.discard(session_key)
        full_text = self._stream_text.pop(session_key, "")
        role = self._stream_role.pop(session_key, "Agent")  # FIX 6: carried role

        if not render:
            return

        def _finalize():
            # Fallback name resolution on the main thread (v1 semantics):
            # explicit arg > agent_mgr.get_name(session_key) > None.
            resolved_name = agent_name
            if resolved_name is None and role == "Agent" and self._main_content is not None:
                agent_mgr = getattr(self._main_content, '_agent_mgr', None)
                if agent_mgr is not None:
                    resolved_name = agent_mgr.get_name(session_key)
            # FIX 7: the mount_key threads into the shared append path so
            # the FINAL row's surface mounts in the project box exactly like
            # the render_sync path already did (round-1 FIX 2).
            self._append_to_surface(role, full_text, session_key, agent_name=resolved_name,
                                    mount_key=mount_key)
            if self._main_content is not None:
                self._main_content.scroll_chat_to_bottom()

        self._dispatch(_finalize)

    def render_event_card(self, event_type: str, container: Gtk.Box, session_key: str = None, **kwargs):
        """
        Render a special event card into container.

        SPEC-06 SP4: UNCHANGED — Pango event cards are not transcript sites
        (ruling R2/R3; architecture keeps cards Pango in Phase A).

        Args:
            event_type: "file_read" | "edit_proposal" | "tool_call" | "error" | "thinking"
            container: Parent box to append the card widget to.
            session_key: Optional session key for agent name lookup (thinking events).
            kwargs: Per-event-type fields:
                file_read:   file_path, snippet="", line_range=""
                edit_proposal: file_path, diff=""
                tool_call:   tool_name, detail=""
                error:       error_msg
                thinking:    thought_text
        """
        from ui.views.event_cards import (
            build_role_bubble,
            create_file_card,
            create_edit_card,
            create_tool_card,
            create_error_bubble,
        )

        if event_type == "file_read":
            card = create_file_card(kwargs.get("file_path", ""),
                                   kwargs.get("snippet", ""),
                                   kwargs.get("line_range", ""))
        elif event_type == "edit_proposal":
            card = create_edit_card(kwargs.get("file_path", ""),
                                    kwargs.get("diff", ""))
        elif event_type == "tool_call":
            card = create_tool_card(kwargs.get("tool_name", ""),
                                    kwargs.get("detail", ""))
        elif event_type == "error":
            card = create_error_bubble(kwargs.get("error_msg", ""))
        elif event_type == "thinking":
            # Fall back to plain text bubble for thoughts
            text = kwargs.get("thought_text", "")
            # Look up agent name for header
            agent_name = None
            if session_key and self._main_content is not None:
                agent_mgr = getattr(self._main_content, '_agent_mgr', None)
                if agent_mgr is not None:
                    agent_name = agent_mgr.get_name(session_key)
            card = build_role_bubble("Agent", text, agent_name=agent_name)
        elif event_type == "task":
            card = self.render_task_card(
                action=kwargs.get("action", ""),
                task_id=kwargs.get("id", ""),
                title=kwargs.get("title", ""),
                status=kwargs.get("status", ""),
                priority=kwargs.get("priority", ""),
                assigned_to=kwargs.get("assigned_to", ""),
            )
        elif event_type == "diff_summary":
            from ui.views.diff_card import build_diff_summary_card
            parsed_diff = kwargs.get("parsed_diff")
            on_accept_all = kwargs.get("on_accept_all")
            on_reject_all = kwargs.get("on_reject_all")
            card = build_diff_summary_card(
                parsed_diff=parsed_diff,
                on_accept_all=on_accept_all,
                on_reject_all=on_reject_all,
            )
        elif event_type == "diff_file":
            from ui.views.diff_card import build_file_diff_card
            file_diff = kwargs.get("file_diff")
            on_accept_file = kwargs.get("on_accept_file")
            on_reject_file = kwargs.get("on_reject_file")
            card = build_file_diff_card(
                file_diff=file_diff,
                on_accept_file=on_accept_file,
                on_reject_file=on_reject_file,
            )
        elif event_type == "widget":
            # Pass-through for pre-built widgets
            card = kwargs.get("widget")
        else:
            # Unknown event type — ignore silently
            return

        def _append():
            container.append(card)
            if self._main_content is not None:
                self._main_content.scroll_chat_to_bottom()

        self._dispatch(_append)


    def render_task_card(
        self,
        action: str,
        task_id: str,
        title: str,
        status: str,
        priority: str,
        assigned_to: str,
    ) -> Gtk.Widget | None:
        """Render a task card bubble (created/updated)."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(4)
        box.set_margin_bottom(4)

        # Title label
        title_label = Gtk.Label()
        # MED-9: xml_template escapes interpolated values to prevent Pango markup injection
        title_label.set_markup(xml_template(
            "<b>Task {action}:</b> {task_id}",
            action=action.capitalize(),
            task_id=task_id,
        ))
        title_label.set_xalign(0)
        box.append(title_label)

        # Task title
        if title:
            desc_label = Gtk.Label(label=title)
            desc_label.set_xalign(0)
            desc_label.set_selectable(True)
            box.append(desc_label)

        # Status + priority row
        meta_label = Gtk.Label()
        parts = [s for s in [status, priority] if s]
        # MED-9: xml_template escapes interpolated values to prevent Pango markup injection
        meta_label.set_markup(xml_template("{parts}", parts=" | ".join(parts)))
        meta_label.set_xalign(0)
        box.append(meta_label)

        # Assigned-to
        if assigned_to:
            at_label = Gtk.Label()
            # MED-9: xml_template escapes interpolated values to prevent Pango markup injection
            at_label.set_markup(xml_template("→ {assigned_to}", assigned_to=assigned_to))
            at_label.set_xalign(0)
            box.append(at_label)

        return box

    def _dispatch(self, fn):
        """Call fn on the GTK main thread.

        Uses GLib.idle_add to dispatch to the GTK main thread when
        GLib is available. Wraps the callback in try/except so that
        exceptions are logged rather than silently swallowed by GLib's
        main loop exception handler.

        KeyboardInterrupt and SystemExit are intentionally re-raised
        (not caught by the generic except Exception).
        """
        if self._GLib is not None:
            def _wrap():
                try:
                    fn()
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception:
                    _logger.exception("Unhandled exception in _dispatch callback")
                return False
            self._GLib.idle_add(_wrap)
        else:
            fn()
