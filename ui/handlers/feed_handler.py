# ui/handlers/feed_handler.py
# Feed state management + card lifecycle (Phase 2).
# Delegates rendering to feed_card.py, git ops to git_ops.py.
from __future__ import annotations
# All GTK via GLib.idle_add(). All git operations run in background threads.
#
# Architecture: one handler per subsystem. Does NOT import other handlers.
# Window wires cross-handler communication via callbacks set in constructor.
# No GTK calls from background threads — always via GLib.idle_add().

import logging
import os
import re
from typing import TYPE_CHECKING, Callable
import copy
import threading
import time


# MED-11: Validate git commit SHA to prevent argument injection
_VALID_SHA_RE = re.compile(r"^(HEAD|[0-9a-fA-F]{4,40})$")
import uuid
from datetime import datetime, timezone

from models.feed_card import AutoAcceptPrefs, FeedCardData
from ui.views.feed_card import build_feed_card, update_card_badge, update_card_in_place
from utils import git_ops
from utils import feed_store
from utils import conversation_store
from utils.config import get_env  # SPEC-11 SP2: renamed env family (D2)

from ui.feed.auto_accept import FeedAutoAcceptMixin
from ui.feed.persist import FeedPersistMixin
from ui.feed.load import FeedLoadMixin
from ui.feed.review import FeedReviewMixin
from ui.feed.snapshots import FeedSnapshotMixin

if TYPE_CHECKING:
    from gi.repository import Gtk

_logger = logging.getLogger(__name__)

# MEMRATCHET §2.1 — read at call time (inside the eviction pass) so tests can
# monkeypatch them. Both bound the LIVE widget window, not the card data.
MAX_LIVE_CARD_WIDGETS = 120   # bound on retained card widgets
KEEP_NEWEST_CARDS = 40        # newest K by seq_num are never evicted


class FeedHandler(
    FeedAutoAcceptMixin,
    FeedPersistMixin,
    FeedLoadMixin,
    FeedReviewMixin,
    FeedSnapshotMixin,
):
    """
    Manages project feed state and coordinates card lifecycle.

    Card lifecycle: add_card() → build widget → prepend to FeedTab
    Button actions: handle_review/accept/reject → GLib.idle_add for GTK
    Git operations: run in background threads, callback to main thread on completion

    Args:
        GLib:                    gi.repository.GLib module — for idle_add dispatch
        feed_tab:                FeedTab instance — for card prepend/remove
        on_populate_input:       Callable[[str], None] — fill input box for Review
        on_send_to_agent:        Callable[[str, str], None] — send message to agent
        on_tab_switch:           Callable[[], None] — switch to feed tab
        on_card_added:           Callable[[str], None] | None — card_id after add
        project_handler:         ProjectHandler | None — git-reject member
                                 fan-out lookup (get_project_members). FIX 8
                                 (SPEC-05 SP2 audit): was previously NEVER
                                 assigned — the git-reject path died with
                                 AttributeError before notifying members or
                                 adding the git card. Passed as a ctor arg
                                 (not a setter) because window.py builds
                                 ProjectHandler (:387) before FeedHandler
                                 (:441) — no construction-order hazard.
    """

    def __init__(
        self,
        *,
        GLib,                        # gi.repository.GLib
        on_send_to_agent,             # callback(session_key, text) — send to agent
        on_card_added=None,           # callback(card_id) | None
        on_approve_exec=None,         # callback(approval_id, approved: bool) | None — Phase E
        get_chat_box_for_session=None,  # callback(session_key) -> Gtk.Box | None
        project_handler=None,         # ProjectHandler | None — git-reject fan-out
    ):
        self._GLib = GLib
        self._feed_tab = None         # set via set_feed_tab() after FeedTab is created
        self._on_send_to_agent = on_send_to_agent
        self._on_card_added = on_card_added
        self._on_approve_exec = on_approve_exec  # Phase E
        self._get_chat_box_for_session = get_chat_box_for_session
        self._project_handler = project_handler

        # Card storage: card_id → FeedCardData
        self._cards: dict[str, FeedCardData] = {}
        # Widget storage: card_id → Gtk.Widget
        self._card_widgets: dict[str, Gtk.Widget] = {}
        # Project → [card_ids] index (newest first)
        self._project_cards: dict[str, list[str]] = {}
        # Project name → project path lookup (for persistence)
        self._project_paths: dict[str, str] = {}
        # Per-project sequence counter for display numbers (Phase 3)
        self._project_seq: dict[str, int] = {}
        # True when loading persisted cards (skips redundant feed.json writes)
        self._active_project_name: str | None = None
        self._loading = False
        # Protects all shared dicts from concurrent access across threads
        self._lock = threading.Lock()

        # ── Background feed persistence (SPEC-UI-RESPONSIVENESS-2 Phase 1) ──
        # One writer thread per handler. Producers enqueue (project_path,
        # card_id, updates); the dict coalesces per (project, card). Failed
        # writes move to _persist_deferred (retry cap); a FRESH enqueue for
        # the same key discards the deferred entry (new payload, fresh
        # budget).
        self._persist_queue: dict[tuple[str, str], dict] = {}
        self._persist_deferred: dict[tuple[str, str], tuple[dict, int]] = {}
        # Compactions ride a SEPARATE list: a sentinel key in the update dict
        # would be unpacked as a (project_path, card_id) pair. Entries are
        # (project_path, tries). External triggers replace (fresh budget);
        # the drain's internal retry appends only-if-absent with tries+1.
        self._persist_compactions: list[tuple[str, int]] = []
        self._persist_queue_lock = threading.Lock()
        self._persist_wakeup = threading.Event()
        self._persist_writer: threading.Thread | None = None
        self._persist_stop = False

        # ── Background snapshot builds (SPEC-UI-RESPONSIVENESS-2 Phase 5) ──
        # Pure snapshot construction (conversation_store.snapshot_from_*) runs
        # on this daemon thread; the GTK-bound chat-box walk stays on the main
        # thread and only the resulting widget update is dispatched back via
        # GLib.idle_add. Builds are coalesced per key so rapid duplicate
        # filesystem events for one path build the diff once (Edit C).
        self._snapshot_jobs: dict[tuple, tuple[Callable, list[str]]] = {}
        self._snapshot_in_flight: dict[tuple, list[str]] = {}
        self._snapshot_cache: dict[tuple, tuple[object, float]] = {}
        self._snapshot_queue_lock = threading.Lock()
        self._snapshot_wakeup = threading.Event()
        self._snapshot_builder: threading.Thread | None = None
        self._snapshot_stop = False
        # A build result is reused for the same key within this window
        # (last-write-wins per (project_path, file_path)). Matches the
        # crabwatch 200 ms debounce that feeds this path.
        self.SNAPSHOT_REUSE_SECONDS = 0.2

        # Lazy-load backlog: cards not currently rendered as live widgets.
        # NEWEST-FIRST: every reader pops from the front.
        #   • populated by on_project_opened() — the loader MERGES (survivors
        #     first, then the snapshot's older slice) when total > PAGE_SIZE;
        #   • drained by _load_more() — one PAGE_SIZE page per click;
        #   • re-populated by _evict_surplus_card_widgets() — evicted cards are
        #     pushed back newest-first (insert(0, …)) so nothing becomes
        #     unreachable; Load More re-renders them.
        # Written from the loader thread, the main thread and _load_more, so
        # every access is under self._lock.
        self._backlog: list[FeedCardData] = []
        self._load_more_widget: Gtk.Widget | None = None
        self.PAGE_SIZE = 15

        # Echo suppression: git accept/reject triggers filesystem changes that
        # CrabWatch detects as new events. Track recently operated file paths
        # to suppress these echoes. dict[file_path] → timestamp (time.monotonic()).
        self._recent_git_paths: dict[str, float] = {}
        self._echo_suppress_seconds = 3.0

        # Phase 5: auto-accept toggle state
        # _auto_accept_enabled: master toggle persisted in feed-prefs.json
        # _auto_accept_agent: once set, only cards from this author are auto-accepted.
        #   None = agent not yet locked-in (first matching card will lock it in).
        # _show_auto_accept_warning: callback injected by Window; receives
        #   (agent_name, on_confirm, on_cancel) and is expected to show a dialog.
        # V2 auto-accept preferences (replaces _auto_accept_enabled + _auto_accept_agent
        # as the canonical state; the legacy fields below remain as derived
        # bookkeeping for the transition period and for legacy direct-set tests).
        self._prefs: AutoAcceptPrefs = AutoAcceptPrefs()
        self._auto_accept_enabled: bool = False  # derived: equals _prefs.any_enabled()
        self._auto_accept_agent: str | None = None  # runtime lock-in (not persisted)
        # Pending save-id for debounced _save_feed_prefs_idle() (set by
        # _refresh_auto_accept_state; cleared after the idle callback runs).
        self._pending_save_id = None
        self._show_auto_accept_warning: Callable | None = None  # callback injected by Window
        # Round 3 BUG #4 (SPEC-PROJECT-SETTINGS-BAR-ENHANCED-FIX-3): fired after
        # an auto-accept level COMMITS (on_auto_accept_level_changed), so the
        # settings bar can rebuild with the newly confirmed level after the async
        # warning dialog. See _emit_auto_accept_level_changed.
        self._on_auto_accept_level_changed: Callable[[str], None] | None = None

    def set_card_added_callback(self, callback) -> None:
        """Late-bind the card-added observer (SPEC-15 SP3b — the Telegram
        bridge filters pending approvals from this seam). Overrides the ctor
        arg when called; None clears."""
        self._on_card_added = callback

    def set_feed_tab(self, feed_tab) -> None:
        """
        Set the FeedTab view instance.

        Called by window after FeedTab is created and before any project is opened.
        Once set, FeedHandler can add/remove cards from the FeedTab.
        """
        self._feed_tab = feed_tab
        # Phase 5: wire batch accept callback
        if self._feed_tab is not None:
            self._feed_tab.set_batch_accept_callback(
                lambda: self._on_batch_accept_clicked()
            )
            # V2: wire per-toggle callbacks (Phase 3 rebuild of the toolbar).
            # These setters only exist on the rebuilt FeedTab; legacy tests
            # using MockFeedTab don't define them, so guard with hasattr.
            if hasattr(self._feed_tab, "set_diffs_toggle_callback"):
                self._feed_tab.set_diffs_toggle_callback(self._on_diffs_toggled)
            if hasattr(self._feed_tab, "set_files_toggle_callback"):
                self._feed_tab.set_files_toggle_callback(self._on_files_toggled)
            if hasattr(self._feed_tab, "set_exec_toggle_callback"):
                self._feed_tab.set_exec_toggle_callback(self._on_exec_toggled)
            # V2: wire agent scope dropdown callback.
            if hasattr(self._feed_tab, "set_agent_scope_callback"):
                self._feed_tab.set_agent_scope_callback(self._on_agent_scope_changed)
            # Keep legacy callback for backward compat during the v1→v2
            # transition — legacy tests still call _on_auto_accept_toggled.
            self._feed_tab.set_auto_accept_callback(self._on_auto_accept_toggled)








    # ── V2 per-toggle methods (Phase 4 / SPEC-AUTO-ACCEPT-GRANULAR-1.md §2.4) ──












    # ── Auto-accept level (settings bar) ──────────────────────────────────
    # SPEC-PROJECT-SETTINGS-BAR-ENHANCED-FIX-3 §2.3. The four file-change
    # auto-accept states are distinct and round-trippable. exec_command is a
    # SEPARATE axis and is never touched by these methods (file-only scope).





    # ── V2 policy helpers (Phase 4 / SPEC-AUTO-ACCEPT-GRANULAR-1.md §2.4) ──





    def _save_feed_prefs_idle(self) -> None:
        """
        Persist v2 auto-accept prefs to .crabcakes/feed-prefs.json.

        Called via GLib.idle_add so it runs on the main thread. The save is
        debounced by _refresh_auto_accept_state() — rapid-fire mutations
        coalesce into one disk write.

        Bug #12 (adversarial audit): No longer touches _pending_save_id.
        The previous attempt to clear it here raced with _refresh_auto_accept_state's
        `self._pending_save_id = idle_add(...)` assignment (which
        immediately overwrote the clear with a stale id). The fix that
        actually works is in _refresh_auto_accept_state: drop the
        source_remove call so GLib's auto-cleanup of single-shot idle
        sources doesn't trigger 'Source ID N was not found' warnings.
        """
        project_path = self._project_paths.get(self._active_project_name or "")
        if not project_path:
            return
        feed_store.save_feed_prefs(project_path, self._prefs.to_dict())

    # ─────────────────────────────────────────────────────────────────
    # V2 exec auto-accept getter (Phase 6 / §2.5)
    # ─────────────────────────────────────────────────────────────────

    def get_exec_auto_accept_mode(self) -> str | None:
        """Public API: return the current exec auto-accept mode. (Phase 6 / v2)

        Used by AgentRuntimeHandler via the installed callback to decide
        whether to bypass card creation in Silent mode. See
        set_check_exec_auto_accept_callback_for_handler() for the wiring
        pattern; window.py calls that method to install this getter as
        ARTH's `_on_check_exec_auto_accept` callback.

        Returns:
            - "off" | "show" | "silent" — the current exec mode stored in
              _prefs.exec_command.mode (see models/feed_card.py:210-215
              ExecCommandPref definition).
            - None if _prefs is not yet initialized. ARTH treats None as
              "no bypass" and falls through to normal card creation. This
              guards against the constructor-ordering race: window.py wires
              the callback before FeedHandler.__init__ has set _prefs in
              some test fixtures.

        Cheap: single attribute read. Called once per approval request from
        AgentRuntimeHandler._do_approval_needed (Phase 6 / §2.5 Silent bypass).
        """
        if self._prefs is None:
            return None
        return self._prefs.exec_command.mode

    def set_check_exec_auto_accept_callback_for_handler(
        self, handler_setter: Callable[[Callable[[], str | None] | None], None]
    ) -> None:
        """Wire the exec auto-accept callback to AgentRuntimeHandler. (Phase 6 / §2.5)

        Per §8.6 R2 (no handler-to-handler imports), this indirection lets
        AgentRuntimeHandler query the exec mode without importing FeedHandler.
        Window.py calls this after both handlers are constructed.

        Args:
            handler_setter: AgentRuntimeHandler.set_check_exec_auto_accept_callback.
                The argument is wrapped (NOT called directly) so the ARTH
                setter receives our bound method `self.get_exec_auto_accept_mode`.
                When ARTH later invokes the callback, the lookup is
                `fh.get_exec_auto_accept_mode()` which returns
                `self._prefs.exec_command.mode`.

        Wiring pattern (called once at startup by window.py):
            self._feed_handler.set_check_exec_auto_accept_callback_for_handler(
                self._agent_runtime_handler.set_check_exec_auto_accept_callback
            )
            # Equivalent to:
            self._agent_runtime_handler.set_check_exec_auto_accept_callback(
                self._feed_handler.get_exec_auto_accept_mode
            )
        """
        handler_setter(self.get_exec_auto_accept_mode)

    # ─────────────────────────────────────────────────────────────────
    # Card lifecycle
    # ─────────────────────────────────────────────────────────────────

    def add_card(self, card_data: FeedCardData, persist: bool = True) -> str:
        """
        Add a card to the project feed.

        1. Assign unique card_id (uuid4)
        2. Store in self._cards[card_id]
        3. Index under project_name
        4. Build widget via build_feed_card()
        5. Prepend to feed_tab
        6. Persist to feed.json via feed_store.append_feed_card()
        7. Return card_id

        `persist=False` skips step 6 entirely — used by `_surface_prune_card`,
        which persists a main-thread COPY instead (§2.3.5) and must not depend
        on the `_loading` gate. All existing callers are positional on
        `card_data` and keep the default.

        Thread-safe: GTK operations via GLib.idle_add().
        """
        card_id = str(uuid.uuid4())
        card_data.card_id = card_id

        # Assign sequence number (Phase 3). Under _lock: the loader also
        # read-modify-writes this counter (P5 audit BUG #1) — an unlocked
        # increment racing that rebuild could be lost, handing two cards the
        # same seq_num (duplicate badge + a non-unique eviction key).
        proj = card_data.project_name
        with self._lock:
            if proj not in self._project_seq:
                self._project_seq[proj] = 0
            self._project_seq[proj] += 1
            card_data.seq_num = self._project_seq[proj]

        # ── Create conversation snapshot (deferred) ───────────────────────
        # Must run AFTER the bubble is appended to the chat box, because
        # _maybe_create_snapshot reads messages from the chat box widgets.
        # At this point render_sync() just returned the bubble but it hasn't
        # been appended yet (that happens in _handle_final_response after
        # render_sync returns). idle_add defers to the next main-loop cycle.
        _card_id = card_id
        self._GLib.idle_add(lambda: self._finalize_snapshot(_card_id))

        # Store data under lock (protects against concurrent _load_and_render)
        with self._lock:
            self._cards[card_id] = card_data

            # Index under project
            proj = card_data.project_name
            if proj not in self._project_cards:
                self._project_cards[proj] = []
            self._project_cards[proj].insert(0, card_id)  # newest first

        # Build widget (pure Python, no shared state) — done outside lock
        # Phase E: approval cards use approve/deny callbacks instead of accept/reject
        if card_data.metadata.get("needs_approval"):
            on_approve, on_deny = self._make_approve_exec_cb(card_id)
            widget = build_feed_card(
                card_data,
                on_review=self._make_review_cb(card_id),
                on_accept=on_approve,
                on_reject=on_deny,
                on_copy=self._make_copy_cb(card_data),
            )
        else:
            widget = build_feed_card(
                card_data,
                on_review=self._make_review_cb(card_id),
                on_accept=self._make_accept_cb(card_id),
                on_reject=self._make_reject_cb(card_id),
                on_copy=self._make_copy_cb(card_data),
            )

        with self._lock:
            self._card_widgets[card_id] = widget

        # Get project path from card metadata (set by on_project_opened load path),
        # or fall back to _project_paths if the card came from the parser (metadata empty).
        project_path = card_data.metadata.get("project_path", "") or self._project_paths.get(card_data.project_name, "")

        # Append to feed tab on main thread (newest at bottom), then persist
        def _append():
            if self._feed_tab is not None:
                self._feed_tab.append_card(widget, card_id)
                self._schedule_smart_scroll()  # one funnel for all append paths
                # MEMRATCHET §2.1: bound the live widget window. Only releases
                # widgets for cards already scrolled above the viewport.
                self._evict_surplus_card_widgets()
                # Phase 5 + v2: auto-accept check (runs on main thread via idle_add)
                # Must run AFTER append_card so the widget exists in the tree
                # before handle_accept starts git ops. The lazy agent lock-in
                # is now handled inside _agent_scope_matches().
                if card_data.accepted is None and self._is_card_auto_acceptable(card_data):
                    # Exec approval cards in Show mode: auto-approve (not git accept)
                    # and hide the Approve/Deny buttons so the user can't double-act.
                    # Silent mode never reaches here (bypassed in AgentRuntimeHandler).
                    if (card_data.card_type == "agent_action"
                            and card_data.metadata.get("needs_approval")):
                        self._GLib.idle_add(
                            lambda cid=card_data.card_id: self._auto_approve_exec_card(cid)
                        )
                    else:
                        self._GLib.idle_add(lambda cid=card_data.card_id: self.handle_accept(cid))
                if self._on_card_added:
                    self._on_card_added(card_id)

        # Refresh batch accept bar (Phase 5)
        self._update_batch_bar_for_active_project(card_data.project_name)

        def _persist():
            if project_path and hasattr(card_data, 'to_dict') and not self._loading:
                # Skip snapshot persistence if oversized
                if card_data.metadata.get("_snapshot_oversized"):
                    # Temporarily remove snapshot for persistence
                    saved = card_data.conversation_snapshot
                    card_data.conversation_snapshot = None
                    feed_store.append_feed_card(project_path, card_data)
                    card_data.conversation_snapshot = saved
                else:
                    feed_store.append_feed_card(project_path, card_data)

        self._GLib.idle_add(_append)
        # Persist in background to avoid blocking UI.
        # Skip persistence when _loading=True (cards already on disk from load)
        # or when the caller explicitly owns persistence (persist=False).
        if project_path and not self._loading and persist:
            t = threading.Thread(target=_persist, daemon=True)
            t.start()

        return card_id

    def add_cards_batch(self, cards: list[FeedCardData]) -> list[str]:
        """Add multiple cards in a single main-thread pass.

        Each card still goes through the same pipeline as add_card() (id,
        sequence number, widget build, store, index, persist). The only
        difference: all widgets are appended in ONE GLib.idle_add callback
        and the smart scroll fires ONCE at the end.

        Why this matters: a single LLM response can contain N crabcards.
        Without batching, add_card() called N times enqueues N idle
        callbacks, each connecting its own one-shot vadjustment 'changed'
        handler and 150ms timeout. If cards arrive faster than GTK can
        lay them out, the proximity check reads stale values and the
        vadjustment 'changed' signal may already have fired before later
        handlers attach — leaving the feed scrolled mid-batch instead of
        at the newest card.

        Returns: list of card_ids in input order.
        """
        if not cards:
            return []

        card_ids: list[str] = []
        widget_by_id: dict[str, Gtk.Widget] = {}
        persist_data: list[tuple[FeedCardData, str]] = []  # (card, project_path)

        # Phase 1: assign ids, sequence numbers, build widgets (cheap, pure)
        with self._lock:
            for card_data in cards:
                card_id = str(uuid.uuid4())
                card_data.card_id = card_id

                proj = card_data.project_name
                if proj not in self._project_seq:
                    self._project_seq[proj] = 0

                # Sequence numbers stay monotonic per project even when
                # batching across projects — same as add_card() per-card.
                self._project_seq[proj] += 1
                card_data.seq_num = self._project_seq[proj]

                self._cards[card_id] = card_data
                if proj not in self._project_cards:
                    self._project_cards[proj] = []
                self._project_cards[proj].insert(0, card_id)

                card_ids.append(card_id)

        # Phase 2: build widgets outside the lock (pure Python, no shared state)
        for card_data, card_id in zip(cards, card_ids):
            if card_data.metadata.get("needs_approval"):
                on_approve, on_deny = self._make_approve_exec_cb(card_id)
                widget = build_feed_card(
                    card_data,
                    on_review=self._make_review_cb(card_id),
                    on_accept=on_approve,
                    on_reject=on_deny,
                    on_copy=self._make_copy_cb(card_data),
                )
            else:
                widget = build_feed_card(
                    card_data,
                    on_review=self._make_review_cb(card_id),
                    on_accept=self._make_accept_cb(card_id),
                    on_reject=self._make_reject_cb(card_id),
                    on_copy=self._make_copy_cb(card_data),
                )
            widget_by_id[card_id] = widget

            # Cache project_path for the persistence phase
            project_path = (
                card_data.metadata.get("project_path", "")
                or self._project_paths.get(card_data.project_name, "")
            )
            persist_data.append((card_data, project_path))

            # Deferred snapshot per-card (same trick add_card uses)
            _cid = card_id
            self._GLib.idle_add(lambda: self._finalize_snapshot(_cid))

        with self._lock:
            for card_id, widget in widget_by_id.items():
                self._card_widgets[card_id] = widget

        # Phase 3: ONE main-thread pass to append all cards + ONE smart scroll
        def _append_all():
            if self._feed_tab is None:
                return
            for card_id in card_ids:
                widget = widget_by_id.get(card_id)
                if widget is not None:
                    self._feed_tab.append_card(widget, card_id)
            # Single scroll decision for the whole batch
            self._schedule_smart_scroll()
            # MEMRATCHET §2.1: one eviction pass for the whole batch — the
            # batch path is the other unbounded widget writer.
            self._evict_surplus_card_widgets()
            if self._on_card_added:
                for card_id in card_ids:
                    self._on_card_added(card_id)

        self._GLib.idle_add(_append_all)

        # Phase 4: refresh batch bar once (cheap)
        # Use the first card's project; in practice all batched cards
        # come from the same agent response so they share project_name.
        if cards:
            self._update_batch_bar_for_active_project(cards[0].project_name)

        # Phase 5: persist all cards in one background thread
        if not self._loading:
            def _persist_all():
                for card_data, project_path in persist_data:
                    if not (project_path and hasattr(card_data, 'to_dict')):
                        continue
                    if card_data.metadata.get("_snapshot_oversized"):
                        saved = card_data.conversation_snapshot
                        card_data.conversation_snapshot = None
                        feed_store.append_feed_card(project_path, card_data)
                        card_data.conversation_snapshot = saved
                    else:
                        feed_store.append_feed_card(project_path, card_data)

            t = threading.Thread(target=_persist_all, daemon=True)
            t.start()

        return card_ids

    def remove_card(self, card_id: str) -> None:
        """Remove a card from the feed."""
        with self._lock:
            if card_id not in self._cards:
                return
            proj = self._cards[card_id].project_name
            if proj in self._project_cards and card_id in self._project_cards[proj]:
                self._project_cards[proj].remove(card_id)
            self._cards.pop(card_id, None)
            widget = self._card_widgets.pop(card_id, None)

        def _remove():
            if self._feed_tab is not None:
                self._feed_tab.remove_card(card_id)

        self._GLib.idle_add(_remove)

    def get_card(self, card_id: str) -> FeedCardData | None:
        """Get card data by ID."""
        return self._cards.get(card_id)

    def update_card(self, card_id: str, card_data: FeedCardData) -> None:
        """
        Update an existing card's data and re-render its widget.

        Used by AgentRuntimeHandler Phase D to update tool call cards with results.

        Steps:
        1. Update in-memory FeedCardData in self._cards
        2. Refresh the widget. Phase 4 Part A: when the existing widget
           exposes the child seams added in build_feed_card (`_body_label`),
           mutate those children by reference (update_card_in_place);
           otherwise rebuild the card and swap it into FeedTab (the
           pre-Phase-4 path, unchanged).
        3. Persist to feed_store (Phase 1: background writer, coalesced)

        Thread-safe: GTK operations via GLib.idle_add().
        """
        if card_id not in self._cards:
            _logger.warning("update_card: card %s not found", card_id)
            return

        # Update in-memory data
        with self._lock:
            self._cards[card_id] = card_data

        old_widget = self._card_widgets.get(card_id)

        # Phase 1: persist off the main thread (was a synchronous 13.9 MB
        # read-modify-write on the GTK main thread — measured 0.62 s per tool
        # result). Coalesced per (project, card); last-write-wins.
        #
        # F1 amendment (spec §2.3.2): `accepted` is included ONLY when a
        # decision exists. It is the durable record the pin rule reads for
        # resolved approval cards (`approve_exec` sets it in memory), and the
        # pre-fix payload dropped it, so the decision never reached disk.
        # `None` is omitted rather than written: a later update_card for the
        # same card (tool-result body refresh) must never clobber a recorded
        # decision back to pending.
        project_path = self._project_paths.get(card_data.project_name, "")
        if project_path:
            updates = {
                "body": card_data.body,
                "metadata": card_data.metadata,
            }
            if card_data.accepted is not None:
                updates["accepted"] = card_data.accepted
            self._enqueue_card_update(project_path, card_id, updates)
        else:
            # Loud, not silent (audit: mutate-before-persist / silent-no-op).
            # update_card has already replaced the in-memory card, so without
            # this warning a caller would believe the decision/flag reached
            # disk when it never will. The card knowingly stays ahead of disk
            # only where the project has no registered path (never opened).
            _logger.warning(
                "update_card: no registered project path for %r — card %s "
                "changed in memory but NOT persisted (decision/flag will be "
                "lost on reload)",
                card_data.project_name, card_id,
            )

        if old_widget is None:
            # Evicted (§2.1). Data was updated and persisted above; the widget is
            # rebuilt from data if the card is rendered again. Rebuilding here
            # would re-append an old card at the bottom of the feed.
            return

        # ── Phase 4 Part A: in-place fast path ──────────────────────────
        # build_feed_card exposes the body label (`_body_label`) — when the
        # live widget has it, refreshing by reference avoids constructing a
        # whole new card and doing a FeedTab remove/insert on the main
        # thread. The mutation is dispatched via idle_add, exactly like the
        # fallback swap below.
        if old_widget is not None and getattr(old_widget, "_body_label", None) is not None:
            _card_data = card_data

            def _mutate_in_place():
                try:
                    if update_card_in_place(old_widget, _card_data):
                        return
                except Exception:
                    # A partially mutating refresh must not leave a broken
                    # card: fall through to the rebuild path.
                    _logger.exception(
                        "update_card: in-place refresh failed for %s; rebuilding card",
                        card_id,
                    )
                self._rebuild_and_replace_card(card_id, _card_data, old_widget)

            self._GLib.idle_add(_mutate_in_place)
            return

        # ── Fallback: rebuild the widget and swap it into FeedTab ────────
        self._rebuild_and_replace_card(card_id, card_data, old_widget)

    def _rebuild_and_replace_card(
        self, card_id: str, card_data: FeedCardData, old_widget
    ) -> None:
        """Rebuild a card widget and swap it into FeedTab (Phase 4 fallback).

        Extracted from the pre-Phase-4 update_card body so both the
        no-seam path and the in-place-refresh-failed path share one
        implementation. The widget is constructed here (pure Python, no
        shared state) and the FeedTab swap is dispatched via idle_add.

        `old_widget` is always a live widget: both callers pass a non-None
        value, because update_card returns early for an evicted card whose
        widget is gone (§2.1 evicted-card guard). The previous
        `old_widget is None` → `append_card` branch was therefore unreachable
        and has been deleted (MEMRATCHET P7b) — re-appending an old card at the
        bottom of the feed is exactly the behaviour the guard exists to stop.
        """
        # Rebuild widget (same construction as add_card)
        # Phase E: approval cards use approve/deny callbacks instead of accept/reject
        if card_data.metadata.get("needs_approval"):
            on_approve, on_deny = self._make_approve_exec_cb(card_id)
            new_widget = build_feed_card(
                card_data,
                on_review=self._make_review_cb(card_id),
                on_accept=on_approve,
                on_reject=on_deny,
                on_copy=self._make_copy_cb(card_data),
            )
        else:
            new_widget = build_feed_card(
                card_data,
                on_review=self._make_review_cb(card_id),
                on_accept=self._make_accept_cb(card_id),
                on_reject=self._make_reject_cb(card_id),
                on_copy=self._make_copy_cb(card_data),
            )

        # Update widget storage
        with self._lock:
            self._card_widgets[card_id] = new_widget

        if get_env("DEBUG"):
            _logger.info("feed card rebuilt (no seam): %s", card_id)

        # Replace widget in FeedTab on main thread
        _card_id = card_id
        _new_widget = new_widget

        def _replace():
            if self._feed_tab is None:
                return
            # old_widget is always live (see docstring) — the previous
            # `is None` → append_card branch was unreachable and is deleted.
            self._feed_tab.replace_card(_card_id, _new_widget)

        self._GLib.idle_add(_replace)

    # ─────────────────────────────────────────────────────────────────
    # Background feed persistence (SPEC-UI-RESPONSIVENESS-2 Phase 1)
    # ─────────────────────────────────────────────────────────────────




            # loop: stop-check at top re-examines after the drain


    def _surface_prune_card(self, project_path: str, pruned: int, window: int) -> None:
        """Surface a compaction as a UI system card. Called on the writer.

        UI work (add_card: seq assignment, widget build, indexing) happens on
        the MAIN thread via idle_add. Persistence writes a point-in-time COPY
        of the card state (deep-copied on the main thread inside _ui) from a
        tiny persist thread — bypassing add_card's _loading-gated persist
        (audit r1 #9) and immune to post-add in-memory mutation (audit r3 #7).
        System cards never carry snapshots (no file_path ⇒
        _maybe_create_snapshot no-ops), so the copy is always complete.
        """
        card = FeedCardData(
            card_type="system",
            source="system",
            title="Feed compacted",
            body=f"{pruned} oldest cards pruned (window {window})",
            author="system",
            timestamp=datetime.now(timezone.utc),
            project_name="",   # assigned inside _ui by reverse lookup (spec §2.3.5)
        )

        def _ui():
            # Main thread: resolve the compacted project's NAME here — never
            # on the writer. Deriving it from _active_project_name on the
            # writer made the guard below self-satisfying (a project switch
            # mid-compaction misfiled the card into the new project's view
            # while persisting it into the old project's feed.json — Coder
            # Phase-3 finding; spec §2.3.5 amended likewise).
            name = next(
                (n for n, p in self._project_paths.items() if p == project_path),
                "",
            )
            if not name or self._active_project_name != name:
                return
            card.project_name = name
            self.add_card(card, persist=False)   # seq, widgets, indexing — main only
            snapshot = copy.deepcopy(card)       # point-in-time copy, main thread

            def _persist():
                feed_store.append_feed_card(project_path, snapshot)

            threading.Thread(target=_persist, daemon=True).start()

        self._GLib.idle_add(_ui)


    def shutdown_snapshot_builder(self) -> None:
        """Stop the background snapshot builder. Safe to call multiple times."""
        self._snapshot_stop = True
        self._snapshot_wakeup.set()
        thread = self._snapshot_builder
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._snapshot_builder = None
        with self._snapshot_queue_lock:
            self._snapshot_jobs.clear()
            self._snapshot_in_flight.clear()
            self._snapshot_cache.clear()

    def get_cards_for_project(self, project_name: str) -> list[FeedCardData]:
        """Get all cards for a project, newest first."""
        card_ids = self._project_cards.get(project_name, [])
        return [self._cards[cid] for cid in card_ids if cid in self._cards]

    def _schedule_smart_scroll(self) -> None:
        """One funnel for all append paths. Only scrolls if the user is
        already near the bottom (within 80px), so users reading older
        cards are not yanked away.

        Safe to call when _feed_tab is None (no-op) or when no cards
        were actually appended (FeedTab handles that internally).
        """
        if self._feed_tab is None:
            return
        self._feed_tab.schedule_smart_scroll_to_bottom()

    def clear_project(self, project_name: str) -> None:
        """Remove all cards for a project (on project close)."""
        with self._lock:
            card_ids = self._project_cards.get(project_name, [])
            for cid in list(card_ids):
                self._card_widgets.pop(cid, None)
                self._cards.pop(cid, None)
            self._project_cards.pop(project_name, None)
            self._project_seq.pop(project_name, None)
        for cid in card_ids:
            if self._feed_tab:
                self._feed_tab.remove_card(cid)

    # ─────────────────────────────────────────────────────────────────
    # Project lifecycle hooks
    # ─────────────────────────────────────────────────────────────────



    # ─────────────────────────────────────────────────────────────────
    # Lazy load: "Load More" button
    # ─────────────────────────────────────────────────────────────────






    # ─────────────────────────────────────────────────────────────────
    # Button action handlers
    # ─────────────────────────────────────────────────────────────────


    def _add_git_card(self, original_card: FeedCardData, result) -> None:
        """Create a git_commit feed card after accept or reject."""
        if result is None or not hasattr(result, 'success') or not result.success:
            return
        accepted = original_card.accepted is True
        action = "Accepted" if accepted else "Rejected"
        git_card = FeedCardData(
            card_type="git_commit",
            source="git",
            title=f"{action}: {original_card.title}",
            body=result.stdout.strip() if result.stdout else "",
            author="PM",
            timestamp=datetime.now(timezone.utc),
            project_name=original_card.project_name,
            commit_sha=result.sha if hasattr(result, 'sha') and result.sha else None,
            file_path=original_card.file_path,
            accepted=accepted,  # NEW — propagate decision so badge renders (Phase 2)
        )
        self.add_card(git_card)





    def _update_batch_bar_for_active_project(self, project_name: str | None = None) -> None:
        """
        Recompute the pending count for the active project and update the bar.
        (Phase 5)

        Args:
            project_name: Project to count pending cards for. If None, uses
                _active_project_name (for on_project_opened context). For
                add_card() calls, pass card_data.project_name directly.
        """
        target = project_name or self._active_project_name
        if self._feed_tab is None or target is None:
            return
        all_cards = self.get_cards_for_project(target)
        actionable_types = ("diff", "file_created", "file_modified", "file_deleted")
        count = 0
        for card in all_cards:  # newest first
            if card.card_type in actionable_types and card.accepted is None:
                count += 1
            else:
                break
        self._feed_tab.update_batch_bar(count)

    def handle_copy(self, text: str) -> None:
        """Copy card body text to clipboard."""
        def _copy():
            import gi
            gi.require_version('Gtk', '4.0')
            from gi.repository import Gdk
            display = Gdk.Display.get_default()
            if display is None:
                return
            clipboard = display.get_clipboard()
            clipboard.set(text)
        self._GLib.idle_add(_copy)

    # ─────────────────────────────────────────────────────────────────
    # CrabWatch integration (Phase 5 stub — no-op now)
    # ─────────────────────────────────────────────────────────────────

    def on_filesystem_event(self, card_data: FeedCardData) -> None:
        """
        Entry point for CrabWatch file change events.
        Same as add_card() but source is always 'system' or 'crabwatch'.

        Includes echo suppression: if this file_path was involved in a recent
        git accept/reject (within _echo_suppress_seconds), the event is dropped
        to avoid duplicate cards for changes the PM just approved/rejected.
        """
        # Echo suppression — check if this path was recently part of a git op
        fp = card_data.file_path
        if fp and fp in self._recent_git_paths:
            elapsed = time.monotonic() - self._recent_git_paths[fp]
            if elapsed < self._echo_suppress_seconds:
                _logger.debug(
                    "on_filesystem_event: suppressed echo for %s (%.1fs after git op)",
                    fp, elapsed,
                )
                return
            # Expired — clean up
            del self._recent_git_paths[fp]

        card_data.source = "system"
        # ── Diff snapshot: deferred (Phase 5 §2.5 Edit A) ──────────────
        # The inline `snapshot_from_git_diff` call (a git subprocess) ran on
        # the GTK main thread for every filesystem event. The card is created
        # with no snapshot; add_card() schedules the deferred
        # `_finalize_snapshot` path, which now builds the diff on the
        # background snapshot builder and dispatches only the panel update
        # back to the main thread (Edit B).
        card_data.conversation_snapshot = None
        self.add_card(card_data)









    # ─────────────────────────────────────────────────────────────────
    # Private helpers
    # ─────────────────────────────────────────────────────────────────

    def _make_review_cb(self, card_id: str):
        def cb(cid=card_id, widget=None):
            self.handle_review(cid, widget)
        return cb

    def _extract_messages_from_chat_box(self, chat_box) -> list[tuple[str, str]]:
        """
        Extract (role, text) pairs from a Gtk.Box containing chat bubbles.

        Walks child widgets and reads _crabcakes_role / _crabcakes_text
        attributes set by build_role_bubble(). Returns oldest-first.

        This is the ONLY place GTK widget methods are called for snapshot
        extraction — keeping utils/conversation_store.py GTK-free.
        """
        all_children = []
        child = chat_box.get_first_child()
        while child is not None:
            all_children.append(child)
            child = child.get_next_sibling()

        messages = []
        for widget in all_children:
            role = getattr(widget, "_crabcakes_role", None)
            text = getattr(widget, "_crabcakes_text", None)
            if role is not None and text is not None:
                messages.append((role, text))
        return messages

    def _make_accept_cb(self, card_id: str):
        def cb(cid=card_id):
            self.handle_accept(cid)
        return cb

    def _make_reject_cb(self, card_id: str):
        def cb(cid=card_id):
            self.handle_reject(cid)
        return cb

    def handle_approve_exec(self, card_id: str, approved: bool) -> None:
        """
        Phase E: Handle Approve/Deny click on a pending exec approval card.

        For cards with needs_approval=True, this is called instead of
        handle_accept/handle_reject. Delegates to on_approve_exec callback
        (AgentRuntimeHandler.approve_exec) to resolve the pending approval.
        """
        card = self._cards.get(card_id)
        if card is None:
            return
        if card.metadata.get("needs_approval") != True:
            # Not an approval card — fall through to handle_accept/handle_reject
            return

        if self._on_approve_exec is not None:
            self._on_approve_exec(card_id, approved)
        else:
            _logger.warning("handle_approve_exec: no on_approve_exec callback registered")

    def _auto_approve_exec_card(self, card_id: str) -> None:
        """Auto-approve an exec card in Show mode.

        Called from add_card() via GLib.idle_add when _is_card_auto_acceptable
        returns True for an exec approval card. Does three things:
        1. Calls handle_approve_exec(card_id, True) to approve the command
           via AgentRuntimeHandler.approve_exec().
        2. Hides the Approve/Deny buttons on the card widget via
           feed_tab.hide_card_buttons() so the user can't double-act.
        3. Updates the card visual to show "approved" state.

        Silent mode never reaches here — AgentRuntimeHandler._do_approval_needed
        bypasses card creation entirely when mode == "silent".

        Args:
            card_id: The card to auto-approve.
        """
        # 1. Approve the command via the registered callback
        self.handle_approve_exec(card_id, True)

        # 2. Hide Approve/Deny buttons on the card widget
        if self._feed_tab is not None:
            try:
                self._feed_tab.hide_card_buttons(card_id, ["approve", "deny"])
            except AttributeError:
                # MockFeedTab or legacy FeedTab without hide_card_buttons
                pass

        # 3. Update visual to show approved state, and persist the decision.
        # REVIEW-PERSIST-1: step 1's handle_approve_exec reaches
        # agent_runtime_handler.approve_exec, which sets card.accepted = True
        # BEFORE calling update_card — so that first enqueue already carries
        # accepted=True (F1 satisfied) whenever the approval callback is
        # registered and the card passes its needs_approval gate. This site is
        # therefore a defensive SECOND enqueue (same payload, coalesced) that
        # additionally refreshes the widget, and the ONLY durable record when
        # the callback is absent (test/headless paths) or the gate does not
        # match. (Audit BUG #2: earlier comment claimed step 1's payload
        # omitted accepted, which is false in production.)
        card = self._cards.get(card_id)
        if card is not None:
            project_path = card.metadata.get("project_path", "") or self._project_paths.get(card.project_name, "")
            if not project_path:
                _logger.warning(
                    "_auto_approve_exec_card: card %s approved with no project "
                    "path — decision cannot be persisted to feed.json",
                    card_id,
                )
            card.accepted = True
            self.update_card(card_id, card)

    def _make_approve_exec_cb(self, card_id: str) -> tuple[Callable, Callable]:
        """
        Phase E: Return (approve_cb, deny_cb) for an approval card.

        Called when building an approval card to wire Accept/Deny buttons
        to handle_approve_exec with approved=True/False.
        """
        def on_approve(cid=card_id):
            self.handle_approve_exec(cid, True)
        def on_deny(cid=card_id):
            self.handle_approve_exec(cid, False)
        return on_approve, on_deny

    def _make_copy_cb(self, card_data: FeedCardData):
        body = card_data.body or card_data.title
        def cb(text=body):
            self.handle_copy(text)
        return cb

    def add_audit_report_card(
        self,
        report: dict,
        project_name: str | None = None,
    ) -> str | None:
        """
        Construct and add a feed card for a structured audit report (SPEC-3).

        Args:
            report: dict with keys: severity, file_path, task, bug_description,
                    pattern, reviewer, target_role, project_path.
            project_name: Override project name. If None, uses report["project_path"]
                          to derive the name. If no project can be determined, returns None.

        Returns:
            card_id string on success, None if no project context available.

        Thread-safe: dispatches to main thread via GLib.idle_add() if needed.
        """
        from pathlib import Path
        from models.feed_card import FeedCardData

        severity = report.get("severity", "issue")
        icons = {"bug": "🔴", "issue": "🟡", "suggestion": "🔵"}
        icon = icons.get(severity, "⚪")

        file_path = report.get("file_path", "?")
        pattern = report.get("pattern")
        reviewer = report.get("reviewer", "unknown")
        target = report.get("target_role", "unknown")
        desc = report.get("bug_description", "")

        pattern_suffix = f" ({pattern})" if pattern else ""
        title = f"{icon} {severity.upper()}: {file_path}{pattern_suffix}"
        body = f"**{reviewer}** reviewed **{target}**: {desc}"

        resolved_project = project_name
        if not resolved_project:
            project_path = report.get("project_path")
            if project_path:
                resolved_project = Path(project_path).name

        if not resolved_project:
            _logger.warning(
                "Cannot add audit report card: no project context"
            )
            return None

        card = FeedCardData(
            card_type="audit_report",
            source="agent",
            title=title,
            body=body,
            author=reviewer,
            timestamp=datetime.now(timezone.utc),
            project_name=resolved_project,
            file_path=file_path,
            metadata={
                "severity": severity,
                "pattern": pattern,
                "target_role": target,
            },
        )
        return self.add_card(card)

    def _update_card_visual(self, card_id: str, accepted: bool) -> None:
        """Apply accepted/rejected CSS class + badge to card widget."""
        widget = self._card_widgets.get(card_id)
        if widget is None:
            return
        # accepted=True → add feed-card-accepted, remove feed-card-rejected
        # accepted=False → add feed-card-rejected, remove feed-card-accepted
        cls_add = "feed-card-accepted" if accepted else "feed-card-rejected"
        cls_rem = "feed-card-rejected" if accepted else "feed-card-accepted"
        widget.add_css_class(cls_add)
        widget.remove_css_class(cls_rem)

        # Update badge in footer (ACCEPTED/REJECTED label)
        update_card_badge(widget, accepted)

        # Update card data
        card = self._cards.get(card_id)
        if card:
            card.accepted = accepted
