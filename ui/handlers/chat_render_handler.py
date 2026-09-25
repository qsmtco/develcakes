# ui/handlers/chat_render_handler.py
# Chat render handler — routes transcript append/stream paths to the
# per-session HTML chat surface (SPEC-06 R2 Phase A, SP4 repoint).
#
# SPEC-06 SP4 (R2A): the Pango bubble pipeline is RETIRED for the transcript
# role. render_async/render_sync/streaming now route through
# render/html.render_document (markdown → HTML → sanitize, ALWAYS in the
# path) into a per-session ChatSurface (ui/views/chat_surface.py). Per
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
#       → destroys that session's surface (SP3 destroy contract).
#   render_event_card / render_task_card
#       → UNCHANGED Pango cards (not transcript sites; R3 catalog untouched).

import html as _html
import logging
from typing import TYPE_CHECKING, Callable

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk

from render.html import render_document
from ui.views.chat_surface import create_chat_surface
from utils.escaping import xml_template
from concurrent.futures import ThreadPoolExecutor

if TYPE_CHECKING:
    from models.feed_card import FeedCardData

_logger = logging.getLogger(__name__)


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
    Routes chat transcript content to per-session HTML chat surfaces.

    SPEC-06 SP4 pipeline (replaces the Pango bubble pipeline):
      text → render/html.render_document()   (markdown → HTML → nh3 sanitize,
                                              fail-closed — ALWAYS in the path)
           → ChatSurface.append_message()    (per-session, windowed deque)

    Feature parity (ruling R2 — dispositions):
      DROPPED for Phase A (documented): forward buttons, copy buttons,
        agent color tint, per-row timestamp header, tight grouping, and the
        render-time crabcard registry (ARH's own extraction path is
        untouched). FORWARD via the toolbar still works; the registry
        retires with chat_bubble in SP5.
      KEPT: reentrancy guard, error fallback (escaped raw text — still
        sanitized), buffered streaming with a final atomic row, Pango
        event/task/diff cards (render_event_card — not transcript sites).

    Args:
        GLib_module: gi.repository.GLib or None — for thread-safe GTK calls
    """

    def __init__(self, GLib_module=None):
        self._GLib = GLib_module
        self._reentrancy = _ReentrancySet()
        # SPEC-06 SP4: per-session chat surfaces (ruling R1 — the surface
        # owns the widget tree). Lazily created; destroy via close_session.
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

    # ── Thread pool for off-main-thread processing ──────────────────
    _pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="crabcakes-render")

    # ── Surface lifecycle (SPEC-06 SP4) ─────────────────────────────

    def set_chat_container_getter(self, getter) -> None:
        """SPEC-06 SP5a (R1): inject the session→chat-box callable.

        RULING R1, option (a): the HANDLER owns mounting — on first surface
        create it asks the getter for the session's chat box and packs the
        surface in. One wiring point (main_content.set_chat_render_handler);
        no window.py edits; SP5c's chat_bubble deletion cannot disturb it."""
        self._container_getter = getter

    def _mount_surface(self, session_key: str, surface, mount_key: str | None = None) -> None:
        """Mount the surface into a chat box — IDEMPOTENT RETRY (FIX 1).

        FIX 1 (SP5a audit BUG #1): the old one-shot skipped mounting forever
        when the box didn't exist yet at surface creation (early render /
        project-routing), leaving the surface permanently unmounted. Now
        every _surface_for call retries until the surface HAS a parent:

        - surface.get_parent() is not None → already mounted, return (the
          retry is a cheap attribute read on the hot path);
        - getter is None or no box for the key → still works unmounted, the
          NEXT render retries;
        - FIX 3 (single-scroll): the mount appends the surface DIRECTLY —
          the surface owns its own ScrolledWindow (chat_surface.py), no
          wrapper is created here (the old wrapper double-scrolled).
        """
        if surface.get_parent() is not None:
            return  # already mounted — nothing to do
        getter = self._container_getter
        if getter is None:
            return
        # FIX 2: mount into the RESOLVED display key's box (project tabs);
        # falls back to the render session key (personal tabs unchanged).
        chat_box = getter(mount_key or session_key)
        if chat_box is None:
            return
        chat_box.append(surface)

    def surface_for_box(self, chat_box):
        """FIX 3 (SP5a audit) — the seam for the single-scroll ruling:
        return the surface mounted in chat_box (identity check on the
        mount parent), or None if the box holds no surface (e.g. a Pango
        welcome bubble only). main_content.scroll_chat_to_bottom uses this
        to drive the SURFACE's own vadjustment instead of a wrapper's."""
        for surface in self._surfaces.values():
            if surface.get_parent() is chat_box:
                return surface
        return None

    def _surface_for(self, session_key: str, mount_key: str | None = None):
        """Lazy per-session surface (created on first use).

        FIX 1: _mount_surface runs on EVERY call — idempotent (parent guard)
        until the box exists, so a None-getter at creation is recovered on
        the next render (the SP5a blank-window case, incl. project routing).
        FIX 2: mount_key — the display key whose box the surface mounts in
        (project-routed replies); the surface CACHE stays keyed by
        session_key (streaming continuity).
        """
        surface = self._surfaces.get(session_key)
        if surface is None:
            surface = create_chat_surface()
            self._surfaces[session_key] = surface
        self._mount_surface(session_key, surface, mount_key)  # SP5a FIX 1: retry
        return surface

    def close_session(self, session_key: str) -> None:
        """Destroy one session's surface (SP3 destroy contract: idempotent,
        cancels pending renders, drops the webview)."""
        surface = self._surfaces.pop(session_key, None)
        if surface is not None:
            surface.destroy()
        self._streaming.discard(session_key)
        self._stream_text.pop(session_key, None)
        self._stream_role.pop(session_key, None)

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
        surface = self._surface_for(session_key or "", mount_key=mount_key)
        surface.append_message(_surface_role(role), html_fragment, agent_name=agent_name)

    # ── Async (thread-safe) ──────────────────────────────────────────────

    def render_async(self, role: str, text: str, session_key: str, on_bubble_ready, on_forward_click=None, on_error=None, agent_name: str = None, agent_color: str = None):
        """
        Compose HTML off-thread, append to the session surface on main.

        RULING R1: on_bubble_ready fires with None — the surface already
        displayed the message. Callers' existing None-guards are verified.
        on_forward_click/agent_color are accepted for signature compat and
        ignored (dropped for Phase A, ruling R2).

        Args:
            role:           "You", "Agent" or "System"
            text:           Raw message text
            session_key:    For reentrancy guarding and surface selection
            on_bubble_ready: callback(None) — called on main thread
            on_error:       optional callback(error_msg) — called on main thread
        """
        if not self._reentrancy.add(session_key):
            return  # render already in flight

        def _compose_off_thread():
            try:
                # Heavy pure-Python work — no GTK calls. sanitize runs here.
                html_fragment = render_document(text)

                def _append_on_main():
                    try:
                        self._surface_for(session_key).append_message(
                            _surface_role(role), html_fragment, agent_name=agent_name
                        )
                    except Exception:
                        _logger.exception("surface append failed — escaped raw text fallback")
                        try:
                            self._surface_for(session_key).append_message(
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

    def set_on_crabcard_extracted(self, cb: "Callable[[list[FeedCardData], str, str], None]") -> None:
        """Set callback for when crabcards are extracted from a message.

        SPEC-06 SP4: no longer invoked by THIS handler's render paths
        (render-time registry dropped for Phase A — ruling R2). Kept for
        signature compat; agent_runtime_handler owns extraction upstream."""
        self._on_crabcard_extracted = cb

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
                       None → mounts/verifies under session_key). Surface
                       CACHE stays session-keyed.

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

    def end_streaming(self, session_key: str, agent_name: str = None, render: bool = True):
        """
        End streaming for session_key: append the final atomic row.

        The buffered text is composed (markdown → sanitized HTML) and
        appended as ONE row. With render=False the buffer is dropped and
        nothing renders (caller renders the final text itself, e.g. via
        render_sync after crabcard cleaning).

        Args:
            session_key: The conversation key whose streaming buffer to finalize.
            agent_name: Optional explicit display name (bypasses the
                agent_mgr.get_name() lookup).
            render: Append the final row (default True).
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
            self._append_to_surface(role, full_text, session_key, agent_name=resolved_name)
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
        from ui.views.chat_bubble import (
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
