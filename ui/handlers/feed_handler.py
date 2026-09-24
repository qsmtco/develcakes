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

if TYPE_CHECKING:
    from gi.repository import Gtk

_logger = logging.getLogger(__name__)

# MEMRATCHET §2.1 — read at call time (inside the eviction pass) so tests can
# monkeypatch them. Both bound the LIVE widget window, not the card data.
MAX_LIVE_CARD_WIDGETS = 120   # bound on retained card widgets
KEEP_NEWEST_CARDS = 40        # newest K by seq_num are never evicted


class FeedHandler:
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

    def set_show_auto_accept_warning(self, callback: Callable | None) -> None:
        """
        Install the callback invoked when the user activates auto-accept
        for any feature (diffs, files, exec). (Phase 5 + v2)

        The callback signature (as of v2) is:
            callback(category: str, agent_name: str,
                     on_confirm: Callable, on_cancel: Callable)
        where category is one of "diffs" | "files" | "exec" and agent_name
        is the human-readable name of the agent the auto-accept applies to
        (resolved by the handler's first_author fallback chain).

        The legacy v1 3-arg signature `(agent_name, on_confirm, on_cancel)`
        is still honored by the legacy wrapper `_on_auto_accept_toggled`
        during the v1→v2 transition; new v2 toggles (_on_diffs_toggled
        etc.) pass all four arguments.

        Pass None to clear. Called by Window after FeedHandler is constructed.
        """
        self._show_auto_accept_warning = callback

    def set_on_auto_accept_level_changed(self, cb: Callable[[str], None] | None) -> None:
        """Register a callback fired after an auto-accept level has COMMITTED.

        cb(level: str) — "off" | "diffs" | "files" | "all". Fired from
        _commit_auto_accept_level() after _refresh_auto_accept_state(), i.e.
        only after the user confirms the warning dialog (Round 3 BUG #4). Safe
        to call with None to unregister.
        """
        self._on_auto_accept_level_changed = cb

    def _resolve_agent_name_for_dialog(self) -> str:
        """
        Return the best human-readable agent name for the warning dialog.
        Fallback chain: _auto_accept_agent → most recent card's author → "the active agent".
        Never returns None or the string "None". (Phase 5)
        """
        if self._auto_accept_agent:
            return self._auto_accept_agent
        # Iterate cards newest-first, find most recent card with an author
        if self._active_project_name:
            card_ids = self._project_cards.get(self._active_project_name, [])
            for cid in card_ids:  # newest first
                card = self._cards.get(cid)
                if card and card.author:
                    return card.author
        return "the active agent"

    def _on_auto_accept_toggled(self, active: bool) -> None:
        """
        Legacy v1 toggle entry point (Phase 5).

        Kept as a thin wrapper around the v2 toggle methods so existing
        tests that bind to the old FeedTab.set_auto_accept_callback
        setter continue to work during the v1→v2 transition.

        ON: show warning dialog (legacy 3-arg signature), then enable all.
        OFF: disable all immediately (no dialog needed).
        """
        if active:
            if self._show_auto_accept_warning is not None:
                # Legacy 3-arg signature (agent_name, on_confirm, on_cancel).
                # Window-supplied callback in current wiring still uses this.
                self._show_auto_accept_warning(
                    self._resolve_agent_name_for_dialog(),
                    on_confirm=self._enable_auto_accept,
                    on_cancel=self._cancel_auto_accept,
                )
            else:
                # No warning callback wired (tests, headless) — enable directly
                self._enable_auto_accept()
        else:
            self._disable_auto_accept()

    def _enable_auto_accept(self) -> None:
        """Legacy v1: enable auto-accept for all file-change types + exec.

        Bug B fix: also call update_auto_accept_state(True) so the toolbar
        toggle's label flips from "Auto-Accept: OFF" to "Auto-Accept: ON".
        Previously only the in-memory flag was set; the label was stuck on
        OFF because Gtk.ToggleButton.set_active(True) does not change
        set_label() text.

        Phase 4 v2 migration: also populates self._prefs so the new
        per-type policy method (_is_card_auto_acceptable) sees a consistent
        state. Without this, a legacy test that sets _auto_accept_enabled
        directly would not trigger v2 auto-accept because _prefs.file_changes
        would still be all-False.
        """
        self._auto_accept_enabled = True
        # Mirror into v2 prefs so per-type policy sees a consistent state.
        for fc in self._prefs.file_changes.values():
            fc.enabled = True
        self._prefs.exec_command.mode = "show"
        self._refresh_auto_accept_state()

    def _cancel_auto_accept(self) -> None:
        """Legacy v1: snap the toggle back to OFF and reset in-memory state.

        Invariant fix: previously this only updated the visible toggle and
        left self._auto_accept_enabled at True, creating a silent-accept
        window where add_card() would auto-accept new cards with no
        user-visible cue. We now reset the in-memory flag so state and UI
        stay in sync.

        Does NOT persist (preserve legacy behavior — the user cancelled, so
        nothing to save).
        """
        self._auto_accept_enabled = False
        # Mirror into v2 prefs so per-type policy sees a consistent state.
        for fc in self._prefs.file_changes.values():
            fc.enabled = False
        self._prefs.exec_command.mode = "off"
        self._refresh_auto_accept_state()

    def _disable_auto_accept(self) -> None:
        """Legacy v1: disable auto-accept and persist state.

        Mirrors the Bug B fix in `_enable_auto_accept`: any code path that
        mutates `_auto_accept_enabled` must also call
        `update_auto_accept_state(...)` so the toolbar label tracks state.
        Without this call, user-click-OFF leaves the label stuck on
        "Auto-Accept: ON" even though the flag and persisted prefs are OFF.
        """
        self._auto_accept_enabled = False
        # Mirror into v2 prefs so per-type policy sees a consistent state.
        for fc in self._prefs.file_changes.values():
            fc.enabled = False
        self._prefs.exec_command.mode = "off"
        self._refresh_auto_accept_state()

    # ── V2 per-toggle methods (Phase 4 / SPEC-AUTO-ACCEPT-GRANULAR-1.md §2.4) ──

    def _on_diffs_toggled(self, active: bool) -> None:
        """Diffs toggle changed. Show warning on first activation."""
        if active:
            if self._show_auto_accept_warning is not None:
                self._show_auto_accept_warning(
                    "diffs",
                    self._resolve_agent_name_for_dialog(),
                    on_confirm=self._enable_diffs,
                    on_cancel=self._cancel_diffs,
                )
            else:
                self._enable_diffs()
        else:
            self._prefs.file_changes["diff"].enabled = False
            self._refresh_auto_accept_state()

    def _enable_diffs(self) -> None:
        self._prefs.file_changes["diff"].enabled = True
        self._refresh_auto_accept_state()

    def _cancel_diffs(self) -> None:
        self._prefs.file_changes["diff"].enabled = False
        self._refresh_auto_accept_state()

    def _on_files_toggled(self, active: bool) -> None:
        """Files toggle changed. Controls file_created/modified/deleted as a group."""
        if active:
            if self._show_auto_accept_warning is not None:
                self._show_auto_accept_warning(
                    "files",
                    self._resolve_agent_name_for_dialog(),
                    on_confirm=self._enable_files,
                    on_cancel=self._cancel_files,
                )
            else:
                self._enable_files()
        else:
            for ct in ("file_created", "file_modified", "file_deleted"):
                self._prefs.file_changes[ct].enabled = False
            self._refresh_auto_accept_state()

    def _enable_files(self) -> None:
        for ct in ("file_created", "file_modified", "file_deleted"):
            self._prefs.file_changes[ct].enabled = True
        self._refresh_auto_accept_state()

    def _cancel_files(self) -> None:
        for ct in ("file_created", "file_modified", "file_deleted"):
            self._prefs.file_changes[ct].enabled = False
        self._refresh_auto_accept_state()

    def _on_exec_toggled(self, mode: str) -> None:
        """Exec toggle changed. mode is 'off', 'show', or 'silent'.

        When the user clicks the Exec toggle to enter 'show' or 'silent', we
        show a stronger warning (since exec has bigger blast radius than file
        changes). The warning callback receives category='exec' and the
        appropriate agent name.
        """
        previous_mode = self._prefs.exec_command.mode
        self._prefs.exec_command.mode = mode
        if mode in ("show", "silent") and previous_mode == "off":
            # First entry into an exec mode — show warning.
            if self._show_auto_accept_warning is not None:
                self._show_auto_accept_warning(
                    "exec",
                    self._resolve_agent_name_for_dialog(),
                    on_confirm=lambda: self._confirm_exec_mode(mode),
                    on_cancel=lambda: self._confirm_exec_mode("off"),
                )
                return
        self._refresh_auto_accept_state()

    def _confirm_exec_mode(self, mode: str) -> None:
        """Confirmed by the user (or auto-confirmed if no dialog wired)."""
        self._prefs.exec_command.mode = mode
        self._refresh_auto_accept_state()

    def _on_agent_scope_changed(self, scope: str) -> None:
        """Agent scope dropdown changed.

        Applies the new scope to ALL auto-accept categories (file changes
        and exec command) since the dropdown is a single global selector.
        Resets _auto_accept_agent to None when switching to 'first_author'
        so lazy lock-in starts fresh.
        """
        for fc in self._prefs.file_changes.values():
            fc.agent_scope = scope
        self._prefs.exec_command.agent_scope = scope
        # Reset lazy lock-in when user explicitly changes scope
        if scope == "first_author":
            self._auto_accept_agent = None
        elif scope == "all_agents":
            self._auto_accept_agent = None
        self._refresh_auto_accept_state()

    def set_agent_options_for_dropdown(self) -> None:
        """Populate the dropdown with registered agent names.

        Called by Window after agents are loaded and FeedTab is wired.
        """
        if self._feed_tab is None:
            return
        try:
            from agent.special_agents import get_special_agents
            names = [a.display_name for a in get_special_agents()]
        except Exception:
            names = []
        if hasattr(self._feed_tab, "set_agent_options"):
            self._feed_tab.set_agent_options(names)

    def _refresh_auto_accept_state(self) -> None:
        """Recompute derived state and push prefs to view + persistence.

        Called after ANY prefs mutation. Ensures the view always reflects
        the handler's canonical state (Bug C invariant).

        Persists via a debounced single-shot idle_add so rapid-fire
        mutations (user clicking toggles + lazy agent lock-in firing in
        the same main-loop iteration) do not produce redundant disk writes
        (BUG #8 in adversarial audit).
        """
        self._auto_accept_enabled = self._prefs.any_enabled()
        if self._feed_tab is not None:
            # V2 path: push full prefs to FeedTab (rebuilt toolbar).
            # Use `elif` (not `if`) so the legacy v1 update_auto_accept_state
            # bridge does NOT also fire on a real FeedTab — calling both
            # would clobber the per-type prefs with the legacy single-toggle
            # reconstruction (Bug #12: Diffs toggle stuck ON after first
            # OFF click). Real FeedTab has both methods; the legacy bridge
            # is only for MockFeedTab and any pre-rebuild FeedTab that lacks
            # update_auto_accept_prefs. See _append_and_schedule_scroll
            # (line ~1057) for the correctly-guarded version.
            if hasattr(self._feed_tab, "update_auto_accept_prefs"):
                self._feed_tab.update_auto_accept_prefs(self._prefs.to_dict())
            elif hasattr(self._feed_tab, "update_auto_accept_state"):
                self._feed_tab.update_auto_accept_state(self._auto_accept_enabled)
        # Cancel any pending save and schedule a new one. The handler is
        # always called from the main thread, so this is safe.
        # Bug #12 (adversarial audit): The previous logic tried
        # source_remove(_pending_save_id) here, but the idle callback
        # (returns False) auto-removes its own source after firing. So
        # _pending_save_id is usually already stale by the time we get
        # back here, and source_remove(stale_id) emits
        # 'Source ID N was not found when attempting to remove it'.
        # Fix: just drop the source_remove call. GLib's idle source is
        # a single-shot — calling idle_add again with a new callback
        # schedules a new save; the old one already ran (or is running)
        # and is harmless to leave alone.
        self._pending_save_id = self._GLib.idle_add(self._save_feed_prefs_idle)

    # ── Auto-accept level (settings bar) ──────────────────────────────────
    # SPEC-PROJECT-SETTINGS-BAR-ENHANCED-FIX-3 §2.3. The four file-change
    # auto-accept states are distinct and round-trippable. exec_command is a
    # SEPARATE axis and is never touched by these methods (file-only scope).

    def get_auto_accept_level(self) -> str:
        """File-change auto-accept level: "off" | "diffs" | "files" | "all".

        Scoped to FILE changes only; exec is a separate axis. Distinct,
        round-trippable mapping:
          - "off":   diff off AND file_created/modified/deleted all off
          - "diffs": diff on  AND file_created/modified/deleted all off
          - "files": diff off AND file_created/modified/deleted all on
          - "all":   all four on
        """
        fc = self._prefs.file_changes
        diff = fc["diff"].enabled
        group = all(fc[ct].enabled for ct in ("file_created", "file_modified", "file_deleted"))
        group_off = not any(fc[ct].enabled for ct in ("file_created", "file_modified", "file_deleted"))
        if not diff and group_off:
            return "off"
        if diff and group_off:
            return "diffs"
        if diff and group:
            return "all"
        return "files"

    def set_auto_accept_level(self, level: str) -> None:
        """Set file-change auto-accept level; enabling routes through the warning gate.

        level in {"off","diffs","files","all"}; invalid -> no-op. Enabling states
        call the warning callback (category + agent + on_confirm/on_cancel) and
        only commit on confirm. All commits call _refresh_auto_accept_state().
        """
        if level not in ("off", "diffs", "files", "all") or self._prefs is None:
            return
        if level == "off":
            for ct in self._prefs.file_changes:
                self._prefs.file_changes[ct].enabled = False
            self._refresh_auto_accept_state()
            self._emit_auto_accept_level_changed(level)
            return
        category = "diffs" if level == "diffs" else "files"
        if self._show_auto_accept_warning is not None:
            self._show_auto_accept_warning(
                category,
                self._resolve_agent_name_for_dialog(),
                on_confirm=lambda lvl=level: self._commit_auto_accept_level(lvl),
                on_cancel=lambda: self._refresh_auto_accept_state(),
            )
        else:
            self._commit_auto_accept_level(level)

    def _emit_auto_accept_level_changed(self, level: str) -> None:
        """Fire the on_auto_accept_level_changed callback (Round 3 BUG #4)."""
        if self._on_auto_accept_level_changed is not None:
            self._on_auto_accept_level_changed(level)

    def _commit_auto_accept_level(self, level: str) -> None:
        """Write the distinct file-change state and sync (internal).

        Round 3 BUG #4: after _refresh_auto_accept_state() this emits
        on_auto_accept_level_changed so the settings bar rebuilds with the
        newly committed level. FeedTab/persistence are updated by the refresh;
        MainWindow is updated by this callback — not by the cycle handler.
        """
        fc = self._prefs.file_changes
        if level == "diffs":
            fc["diff"].enabled = True
            for ct in ("file_created", "file_modified", "file_deleted"):
                fc[ct].enabled = False
        elif level == "files":
            fc["diff"].enabled = False
            for ct in ("file_created", "file_modified", "file_deleted"):
                fc[ct].enabled = True
        elif level == "all":
            for ct in self._prefs.file_changes:
                fc[ct].enabled = True
        self._refresh_auto_accept_state()
        self._emit_auto_accept_level_changed(level)

    # ── V2 policy helpers (Phase 4 / SPEC-AUTO-ACCEPT-GRANULAR-1.md §2.4) ──

    def _is_card_auto_acceptable(self, card: FeedCardData) -> bool:
        """Central auto-accept policy. Returns True if a card should be
        auto-accepted based on current prefs, agent scope, and snooze list.

        Called from add_card() on every new card. Must be O(1).

        Rules:
        1. File-change cards (diff, file_created, file_modified, file_deleted):
           check _prefs.file_changes[card_type].enabled + agent_scope match
           + not in snooze list.
        2. Exec approval cards (agent_action with needs_approval=True):
           check _prefs.exec_command.mode != "off" + agent_scope match
           + not in snooze list.
        3. All other card types: never auto-accepted.

        Legacy-compat fallback: if the legacy _auto_accept_enabled flag is
        True but the v2 _prefs have not been migrated (i.e. _prefs.any_enabled()
        is False), treat the legacy flag as authoritative and accept any
        file-change card matching the legacy _auto_accept_agent scope.
        This preserves the legacy test semantic where _auto_accept_enabled
        is set directly without going through _enable_auto_accept().
        """
        # Fast path: nothing enabled
        if not self._auto_accept_enabled:
            return False

        # Snooze check (per card-id)
        if card.card_id and card.card_id in self._prefs.snoozed_card_ids:
            return False

        # File-change cards
        if card.card_type in ("diff", "file_created", "file_modified", "file_deleted"):
            pref = self._prefs.file_changes.get(card.card_type)
            if pref is None:
                return False
            # Legacy-compat: legacy flag on but v2 prefs not migrated yet.
            # Mirror legacy semantic: accept any file-change card matching
            # the legacy _auto_accept_agent scope.
            if not pref.enabled and not self._prefs.any_enabled():
                if (self._auto_accept_agent is None
                        or card.author == self._auto_accept_agent):
                    return True
                return False
            if not pref.enabled:
                return False
            return self._agent_scope_matches(pref.agent_scope, card.author)

        # Exec approval cards
        if card.card_type == "agent_action" and card.metadata.get("needs_approval"):
            if self._prefs.exec_command.mode == "off":
                return False
            return self._agent_scope_matches(
                self._prefs.exec_command.agent_scope, card.author
            )

        return False

    def _agent_scope_matches(self, scope: str, author: str) -> bool:
        """Check if a card's author matches the configured agent scope.

        - "all_agents": always True
        - "first_author": True if _auto_accept_agent is None (not yet locked)
          or author == _auto_accept_agent
        - "<specific name>": True if author == scope

        Belt-and-suspenders: stale "system" scope (from v1 migration or
        test artifacts) is treated as "all_agents" to avoid silently
        blocking all auto-accept. The migration guard in feed_store.py
        should catch this at load time; this is the runtime safety net.
        See deep-dive report 2026-06-30.

        Side effect: when lazy lock-in fires, _refresh_auto_accept_state() is
        called so the view's agent dropdown updates to reflect the new lock-in
        (BUG #2 in adversarial audit — without this, the dropdown label stays
        at "First author" even though only one agent's cards are accepted).
        Persistence is debounced through _refresh_auto_accept_state.
        """
        if scope == "all_agents" or scope == "system":
            return True
        if scope == "first_author":
            if self._auto_accept_agent is None:
                # Lazy lock-in: first card sets the agent
                if author:
                    self._auto_accept_agent = author
                    self._refresh_auto_accept_state()
                return True
            return author == self._auto_accept_agent
        # Specific agent name (persisted in v2 migration from v1 auto_accept_agent)
        return author == scope

    def snooze_card(self, card_id: str) -> None:
        """Add a card to the snooze list so it is not auto-accepted."""
        if card_id not in self._prefs.snoozed_card_ids:
            self._prefs.snoozed_card_ids.append(card_id)
            self._refresh_auto_accept_state()

    def unsnooze_card(self, card_id: str) -> None:
        """Remove a card from the snooze list."""
        if card_id in self._prefs.snoozed_card_ids:
            self._prefs.snoozed_card_ids.remove(card_id)
            self._refresh_auto_accept_state()

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

        if os.environ.get("CRABCAKES_DEBUG"):
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

    def _ensure_persist_writer(self) -> None:
        """Lazily (re)start the background feed writer thread.

        Clears the stop flag BEFORE the liveness check so an enqueue that
        races a shutdown never strands the entry. When a NEW thread is
        started, deferred retries keep their payloads but reset tries to 0
        (a new writer generation = a fresh retry budget; the reset happens
        per new-thread-start — any prior exit: stop or crash — NOT per
        close/reopen cycle).
        """
        self._persist_stop = False
        w = self._persist_writer
        if w is not None and w.is_alive():
            return
        with self._persist_queue_lock:
            self._persist_deferred = {
                k: (payload, 0) for k, (payload, _t) in self._persist_deferred.items()
            }
        self._persist_writer = threading.Thread(
            target=self._persist_loop, name="crabcakes-feed-writer", daemon=True
        )
        self._persist_writer.start()

    def _enqueue_card_update(self, project_path: str, card_id: str, updates: dict) -> None:
        """Queue a card update for background persistence. Non-blocking.

        Contract: callers MUST pass the FULL current metadata (and body).
        Coalescing merges per top-level key via dict.update — a partial
        second update DROPS omitted keys (correct only for full-state
        payloads like update_card's). metadata is copied one level deep; a
        non-dict metadata value is dropped with a WARNING (never journaled).
        A fresh enqueue DISCARDS any deferred entry for the same key — the
        fresh payload supersedes the old retry entirely (fresh budget).
        """
        if not project_path or not card_id:
            return
        payload = dict(updates)
        if "metadata" in payload:
            if isinstance(payload["metadata"], dict):
                payload["metadata"] = dict(payload["metadata"])
            else:
                _logger.warning(
                    "enqueue: non-dict metadata for card %s dropped", card_id
                )
                payload.pop("metadata")
        with self._persist_queue_lock:
            key = (project_path, card_id)
            self._persist_deferred.pop(key, None)   # fresh wins, structurally
            pending = self._persist_queue.get(key)
            if pending is None:
                self._persist_queue[key] = payload
            else:
                pending.update(payload)
        self._ensure_persist_writer()
        self._persist_wakeup.set()

    def _enqueue_compaction(self, project_path: str) -> None:
        """Queue a feed compaction for the writer thread. Non-blocking.

        EXTERNAL trigger (load-time, §2.3.4): REPLACES any existing entry for
        the same path with a fresh `tries=0` — a load-time enqueue is a new
        attempt and resets the compact budget (accepted, build-time note
        r6#15). This is deliberately DISTINCT from the drain's internal retry
        re-enqueue, which appends only-if-absent with `tries+1` so it can
        never clobber an externally-owned live entry.
        """
        if not project_path:
            return
        with self._persist_queue_lock:
            self._persist_compactions = [
                t for t in self._persist_compactions if t[0] != project_path
            ] + [(project_path, 0)]
        self._ensure_persist_writer()
        self._persist_wakeup.set()

    def _persist_loop(self) -> None:
        """Writer main loop. Stop-check FIRST; drain-before-exit; bounded.

        Shutdown bound: once the stop flag is observed, at most one more
        drain pass runs; failures during that pass DROP with ERROR (no
        deferral), so the writer exits within one pass of the stop signal.
        """
        while True:
            if self._persist_stop:
                with self._persist_queue_lock:
                    drained = (
                        not self._persist_queue
                        and not self._persist_compactions
                        and not self._persist_deferred
                    )
                if drained:
                    return
            self._persist_wakeup.wait(timeout=0.5)
            self._persist_wakeup.clear()
            self._drain_persist_queue()
            # loop: stop-check at top re-examines after the drain

    def _drain_persist_queue(self) -> None:
        """One full drain pass: compactions, then deferred, then queue.

        Bounded pass: compactions are SNAPSHOTTED at pass start (an entry
        enqueued during the pass — internal retry or external trigger — waits
        for the next pass; ≤1 attempt per path per pass regardless of source,
        audit r5 #18). Deferred entries are attempted IN PLACE (never moved to
        the queue — no re-merge, no counter reset, audit r5 #2/#3). A queue
        entry's first failure enters deferred with tries=1 (fresh budget by
        construction — enqueue always pops any deferred entry for the key, so
        deferred and queue entries for one key are mutually exclusive). Never
        raises: the except blocks only log and do dict/list ops under the
        queue lock; `task` is initialized before the try (audit r5 #15).
        """
        # ── compactions: snapshot at pass start ──────────────────────────
        with self._persist_queue_lock:
            compactions = list(self._persist_compactions)
            self._persist_compactions.clear()
        for compact_task in compactions:
            path, tries = compact_task
            try:
                pruned = feed_store.compact_feed(
                    path, window=feed_store.FEED_WINDOW_DEFAULT
                )
                if pruned:
                    self._surface_prune_card(
                        path, pruned, feed_store.FEED_WINDOW_DEFAULT
                    )
            except Exception:  # noqa: BLE001 — writer thread must never die
                _logger.exception("persist: compact task failed (%r)", compact_task)
                if self._persist_stop:
                    _logger.error(
                        "persist: dropping failed compact during shutdown (%r)",
                        compact_task,
                    )
                elif tries + 1 >= 3:
                    _logger.error(
                        "persist: dropping compaction for %s after %d failures",
                        path, tries + 1,
                    )
                else:
                    with self._persist_queue_lock:
                        # only-if-absent: an external _enqueue_compaction that
                        # landed during the pass owns the live entry (fresh
                        # budget); do not overwrite it
                        if not any(
                            p == path for p, _t in self._persist_compactions
                        ):
                            self._persist_compactions.append((path, tries + 1))

        # ── deferred updates: attempted in place ─────────────────────────
        with self._persist_queue_lock:
            deferred_items = list(self._persist_deferred.items())
        for key, (payload, tries) in deferred_items:
            project_path, card_id = key
            try:
                ok = feed_store.update_feed_card(project_path, card_id, payload)
                if ok is False:
                    raise RuntimeError(
                        "update_feed_card returned False (append+legacy failed)"
                    )
                if ok is None:
                    # Legacy path: card gone (pruned between enqueue and
                    # drain). Retrying is futile — log INFO and let the
                    # removal below drop it (symmetric with the queue phase).
                    _logger.info(
                        "persist: card %s no longer exists in %s; "
                        "deferred update dropped (pruned?)",
                        card_id, project_path,
                    )
                # success — remove OUR entry only (a newer entry may exist)
                with self._persist_queue_lock:
                    cur = self._persist_deferred.get(key)
                    if cur is not None and cur[0] is payload:
                        del self._persist_deferred[key]
            except Exception:  # noqa: BLE001
                _logger.exception("persist: deferred update failed (%r)", key)
                with self._persist_queue_lock:
                    cur = self._persist_deferred.get(key)
                    if cur is None:
                        # a fresh enqueue superseded us — drop the stale retry
                        pass
                    elif cur[0] is not payload:
                        # a newer failure already replaced us — leave it
                        pass
                    elif self._persist_stop:
                        _logger.error(
                            "persist: dropping deferred update during shutdown (%r)",
                            key,
                        )
                        del self._persist_deferred[key]
                    elif tries + 1 >= 3:
                        _logger.error(
                            "persist: dropping update for %r after %d failures",
                            key, tries + 1,
                        )
                        del self._persist_deferred[key]
                    else:
                        self._persist_deferred[key] = (payload, tries + 1)

        # ── queue updates ────────────────────────────────────────────────
        while True:
            task = None
            with self._persist_queue_lock:
                if not self._persist_queue:
                    break
                key, updates = self._persist_queue.popitem()
                task = (key, updates)
            project_path, card_id = key
            if not project_path or not card_id:
                _logger.warning(
                    "persist: dropping malformed queue entry for %r", card_id
                )
                continue
            try:
                ok = feed_store.update_feed_card(project_path, card_id, updates)
                if ok is False:
                    raise RuntimeError(
                        "update_feed_card returned False (append+legacy failed)"
                    )
                if ok is None:
                    # Legacy path: card gone (pruned between enqueue and
                    # drain). Retrying is futile — drop with INFO.
                    _logger.info(
                        "persist: card %s no longer exists in %s; "
                        "update dropped (pruned?)", card_id, project_path,
                    )
                    continue
            except Exception:  # noqa: BLE001 — writer thread must never die
                _logger.exception("persist: update task failed (%r)", task)
                if self._persist_stop:
                    _logger.error(
                        "persist: dropping failed update during shutdown (%r)",
                        key,
                    )
                else:
                    # first failure of THIS payload: fresh budget (any prior
                    # deferred entry was popped by the enqueue that queued it)
                    with self._persist_queue_lock:
                        self._persist_deferred[key] = (updates, 1)

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

    def shutdown_persist_writer(self) -> None:
        """Flush and stop the writer. Safe to call multiple times.

        Join timeout scales with the feed's size (a 13.9 MB compact can
        exceed a flat 5 s): min(60.0, 5.0 + size_mb). Exit logging
        distinguishes three conditions (audit r5 #4): undrained entries
        (ERROR), a straggler enqueued during shutdown (WARNING — it will
        be drained by the still-alive writer or the next generation), and
        exit not observed though the queue is empty (WARNING).
        """
        self._persist_stop = True
        self._persist_wakeup.set()
        with self._persist_queue_lock:
            queued_at_stop = (
                len(self._persist_queue)
                + len(self._persist_compactions)
                + len(self._persist_deferred)
            )
        if self._persist_writer is not None:
            timeout = 5.0
            try:
                for path in list(self._project_paths.values()):
                    fp = os.path.join(path, ".crabcakes", "feed.json")
                    if os.path.isfile(fp):
                        timeout = min(60.0, 5.0 + os.path.getsize(fp) / 1_000_000)
                        break
            except OSError:
                pass
            self._persist_writer.join(timeout=timeout)
        with self._persist_queue_lock:
            leftover = (
                len(self._persist_queue)
                + len(self._persist_compactions)
                + len(self._persist_deferred)
            )
        if leftover > queued_at_stop:
            _logger.warning(
                "persist shutdown: %d entries enqueued after shutdown began "
                "(stragglers — drained by the writer or next generation)",
                leftover - queued_at_stop,
            )
        if leftover > 0:
            _logger.error(
                "persist writer stopped with %d undrained entries "
                "(in-flight write exceeded join timeout)", leftover,
            )
        elif self._persist_writer is not None and self._persist_writer.is_alive():
            _logger.warning(
                "persist writer still draining at join timeout; queue is empty "
                "— exit not observed, but entries were drained",
            )
        # Phase 5: the snapshot builder is the second background worker owned
        # by this handler — stop it here so the handler's single shutdown
        # entry point leaves no thread running.
        self.shutdown_snapshot_builder()

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

    def on_project_opened(self, project_name: str, project_path: str) -> None:
        """
        Called when a project opens.

        1. Clear previous project's cards if switching projects
        2. Load cards from .crabcakes/feed.json via feed_store.load_feed()
        3. Render only the last PAGE_SIZE cards (newest)
        4. Store older cards in backlog for "Load More"
        5. Auto-scroll to bottom (newest card visible)
        6. If no cards, show empty state widget
        """
        # Clear previous project if switching — prevents card bleed between projects
        prev = self._active_project_name
        if prev and prev != project_name:
            self.clear_project(prev)
        self._active_project_name = project_name
        # Store project_path for persistence on new card adds
        self._project_paths[project_name] = project_path
        # Clear backlog from previous project before starting load thread.
        # Under _lock: the load thread and the main thread's eviction pass both
        # write _backlog (round-2 BUG #3).
        with self._lock:
            self._backlog = []
        self._load_more_widget = None

        def _load_and_render():
            # Mark loading mode — add_card skips persistence for already-saved cards
            self._loading = True

            # Load persisted cards from .crabcakes/feed.json
            cards = feed_store.load_feed(project_path)

            # §2.3.4 one-time large-feed compaction: a legacy feed bigger than
            # the window's soft bound gets compacted once, on open. Accepted
            # cost (audit r1 #6): the first tool results after a legacy-feed
            # open may wait behind the compaction. `load_feed` itself never
            # compacts — this is the only open-time trigger.
            if len(cards) > feed_store.FEED_WINDOW_DEFAULT * 1.25:
                _logger.info(
                    "on_project_opened: feed for %s has %d cards (> %s) — "
                    "requesting a one-time compaction",
                    project_name, len(cards),
                    int(feed_store.FEED_WINDOW_DEFAULT * 1.25),
                )
                self._enqueue_compaction(project_path)

            # Phase 5 + v2: load auto-accept prefs (separate file from feed.json).
            # Phase 2 of utils/feed_store guarantees load_feed_prefs returns
            # a v2-shaped dict (v1 files are migrated in-memory).
            prefs_raw = feed_store.load_feed_prefs(project_path)
            self._prefs = AutoAcceptPrefs.from_dict(prefs_raw)
            self._auto_accept_enabled = self._prefs.any_enabled()
            self._auto_accept_agent = None  # Reset runtime lock-in on project open

            if not cards:
                self._GLib.idle_add(lambda: self._feed_tab.show_empty_state() if self._feed_tab else None)
                self._loading = False
                return

            # Seed state: set metadata
            for card in cards:
                if not card.metadata:
                    card.metadata = {}
                card.metadata["project_path"] = project_path

            # Migrate old cards: assign seq_nums to cards with seq_num=None,
            # in order of creation timestamp. This ensures every project gets
            # a clean sequence from #1 on first load after the seq_num field
            # is added. Without this migration, old projects would show a mix
            # of cards with seq badges and cards without, which is confusing.
            cards_sorted_by_timestamp = sorted(cards, key=lambda c: c.timestamp)
            next_seq = 1
            for card in cards_sorted_by_timestamp:
                if card.seq_num is None:
                    card.seq_num = next_seq
                next_seq = max(next_seq, card.seq_num + 1)

            # Rebuild sequence counter from loaded cards (now all have seq_num).
            # max() against the LIVE counter, not a bare assignment: a card that
            # arrived during this load's parse window has already advanced
            # _project_seq past the snapshot's high-water mark, and clobbering
            # that back down would let the next arrival reuse its seq_num
            # (duplicate badge + a non-unique ordering key for eviction).
            max_seq = max((card.seq_num for card in cards if card.seq_num), default=0)
            # Under _lock: add_card increments the same counter (P5 audit
            # BUG #1), so the read-modify-write must not interleave with it.
            with self._lock:
                self._project_seq[project_name] = max(
                    max_seq, self._project_seq.get(project_name, 0)
                )

            # Split into recent (render now) and backlog (lazy load)
            # cards is chronological: oldest first, newest last
            if len(cards) > self.PAGE_SIZE:
                backlog = cards[:-self.PAGE_SIZE]  # older cards
                recent = cards[-self.PAGE_SIZE:]   # newest PAGE_SIZE cards
            else:
                backlog = []
                recent = cards

            # Store backlog for "Load More" (thread-safe under lock).
            # MERGE, not rebind (round-4 BUG #3 / round-5 BUG #2): the loader
            # runs on a background thread and can be pre-empted for hundreds of
            # ms on a big feed, so a card that eviction pushed in the meantime
            # has ALREADY had its widget unparented — replacing the list would
            # leave it neither rendered nor drainable until the project is
            # reopened. A plain rebind inside the lock serializes the two
            # operations without merging them.
            new_backlog = list(reversed(backlog))          # newest-first; every entry older than PAGE_SIZE
            seen = {c.card_id for c in new_backlog}
            with self._lock:
                pushed = [c for c in self._backlog if c.card_id not in seen]   # eviction's inserts
                self._backlog = pushed + new_backlog
                # The label must count the MERGED list, not the snapshot slice:
                # `pushed` holds survivors eviction released during this load, so
                # `len(backlog)` would under-report exactly the cards the merge
                # just rescued (and the count is a shared-list read → same lock).
                merged_backlog_count = len(self._backlog)

            # Build widgets for recent cards only, OUTSIDE the lock (round-3
            # BUG #8). The loader's lock region is the dict writes ONLY: up to
            # PAGE_SIZE GTK widget constructions inside it would block the main
            # thread's eviction pass behind them (its locked gate reads
            # len(_card_widgets) under the same lock).
            widgets = {}
            for card in recent:
                widget = build_feed_card(
                    card,
                    on_review=self._make_review_cb(card.card_id),
                    on_accept=self._make_accept_cb(card.card_id),
                    on_reject=self._make_reject_cb(card.card_id),
                    on_copy=self._make_copy_cb(card),
                )
                widgets[card.card_id] = widget

            with self._lock:
                # Index ALL cards (including backlog) so _project_cards is complete.
                # Order by seq_num, newest-first — do NOT infer provenance from set
                # membership. `prev` can hold ids from two different sources:
                #   (1) live arrivals during the parse window  → NEWER than the snapshot
                #   (2) ids the feed pruned at compaction      → OLDER than the snapshot
                # (feed_store.py:55 `FEED_WINDOW_DEFAULT`, pruned oldest-first at
                # :673-678; `_project_cards` is never pruned, so those ids persist here).
                # Concatenating leftovers in front promotes (2) to "newest" and makes
                # Accept All act on a card no longer on disk — the same hazard as
                # acting on a stale card. seq_num is the only correct key, and it is
                # the same key eviction uses (round-6 BUG #2, round-7 BUG #1).
                # Dedupe is implicit via dict.fromkeys; ids no longer in `_cards` drop.
                new_ids = [c.card_id for c in cards if c.card_id]
                prev = self._project_cards.get(project_name, [])
                for card in cards:
                    self._cards[card.card_id] = card
                candidates = [cid for cid in dict.fromkeys(prev + new_ids)
                              if cid in self._cards]
                self._project_cards[project_name] = sorted(
                    candidates,
                    key=lambda cid: self._cards[cid].seq_num or 0,
                    reverse=True,
                )

                # Store the widgets built above — the MAP WRITE stays inside the
                # lock (spec "One rule for _card_widgets"); only the expensive
                # construction left it.
                for card in recent:
                    self._card_widgets[card.card_id] = widgets[card.card_id]

            # Build "Load More" widget if the MERGED backlog is non-empty
            load_more_widget = None
            if merged_backlog_count > 0:
                load_more_widget = self._build_load_more_widget(merged_backlog_count)
                self._load_more_widget = load_more_widget

            # Add cards + load-more on main thread.
            # Use schedule_scroll_to_bottom() to defer the scroll until
            # AFTER GTK updates the vadjustment upper (layout pass).
            # Two GLib.idle_add callbacks run in the same idle batch and
            # do NOT yield to layout between them, so the second callback
            # reads a stale upper. The 'changed' signal on the vadjustment
            # fires after GTK allocates the new content height. (Bug A fix)
            def _append_and_schedule_scroll():
                if self._feed_tab is None:
                    return False

                # Drop any stale sentinel FIRST and unconditionally (round-6
                # BUG #3): on_project_opened only nulls the attribute, so the
                # previous project's bar — or this project's own bar from an
                # earlier open — stays parented otherwise. Hoisted out of the
                # `if` because the switch-to-a-project-with-no-backlog case
                # builds no new sentinel and would leak the old one.
                self._feed_tab.remove_card("__load_more__")   # drop any stale sentinel
                if load_more_widget is not None:
                    self._feed_tab.prepend_card(load_more_widget, card_id="__load_more__")
                else:
                    self._load_more_widget = None             # no sentinel for this project

                # Append recent cards (chronological: oldest first, newest last)
                for card in recent:
                    widget = widgets.get(card.card_id)
                    if widget:
                        # Same-project reopen: append_card only overwrites the
                        # map entry, leaving the previous widget parented and
                        # unreachable by every map/removal path (round-7 BUG #2).
                        self._feed_tab.remove_card(card.card_id)
                        self._feed_tab.append_card(widget, card.card_id)

                # Bound the live widget window once this batch is parented.
                self._evict_surplus_card_widgets()

                # Smart scroll: respects reading position if user scrolled up.
                # On project open the user has no prior position in this
                # project's feed, so the proximity check trivially passes.
                self._schedule_smart_scroll()

                # Phase 5 + v2: push persisted prefs to the FeedTab.
                # V2 FeedTab has update_auto_accept_prefs(); legacy/mock has
                # update_auto_accept_state(bool). Use whichever exists.
                if hasattr(self._feed_tab, "update_auto_accept_prefs"):
                    self._feed_tab.update_auto_accept_prefs(self._prefs.to_dict())
                elif hasattr(self._feed_tab, "update_auto_accept_state"):
                    self._feed_tab.update_auto_accept_state(self._auto_accept_enabled)

                return False  # one-shot

            self._GLib.idle_add(_append_and_schedule_scroll)
            self._loading = False

        t = threading.Thread(target=_load_and_render, daemon=True)
        t.start()

    def on_project_closed(self, project_name: str) -> None:
        """Called when project closes. Clear cards for this project."""
        if self._active_project_name == project_name:
            self._active_project_name = None
        self.clear_project(project_name)
        # Under _lock: the load thread and the main thread's eviction pass both
        # write _backlog (round-2 BUG #3).
        with self._lock:
            self._backlog = []
        self._load_more_widget = None

        def _clear():
            if self._feed_tab is not None:
                self._feed_tab.show_empty_state()
        self._GLib.idle_add(_clear)

    # ─────────────────────────────────────────────────────────────────
    # Lazy load: "Load More" button
    # ─────────────────────────────────────────────────────────────────

    def _build_load_more_widget(self, remaining: int) -> Gtk.Widget:
        """Build the 'Load More' card that sits at the top of the feed.

        Shows count of older cards and a button to load the next page.
        Uses feed-card CSS for visual consistency.
        """
        from gi.repository import Gtk

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("feed-card")
        card.add_css_class("feed-card-load-more")

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        body.add_css_class("feed-card-body")
        body.set_spacing(8)

        label = Gtk.Label(label=f"📂 {remaining} older card{'s' if remaining != 1 else ''}")
        label.set_halign(Gtk.Align.START)
        label.set_hexpand(True)

        btn = Gtk.Button(label="Load More")
        btn.add_css_class("feed-btn-load-more")
        btn.connect("clicked", lambda *_: self._load_more())

        body.append(label)
        body.append(btn)
        card.append(body)
        return card

    def _load_more(self) -> None:
        """Load the next PAGE_SIZE cards from backlog and prepend them."""
        # Empty-check, page slice and remaining count are ONE critical section
        # (round-2 BUG #3): eviction inserts into _backlog and the loader merges
        # into it from other threads, so an unlocked read-then-reassign can
        # drop a concurrently pushed card or mis-count the label.
        with self._lock:
            if not self._backlog:
                return

            # Take next page from backlog (newest-first)
            page = self._backlog[:self.PAGE_SIZE]
            self._backlog = self._backlog[self.PAGE_SIZE:]
            remaining = len(self._backlog)

        # Build widgets for this page (construction OUTSIDE the lock)
        widgets = []
        for card in page:
            # Render from the authoritative map, not the backlog entry: a card
            # evicted then updated exists as a fresh instance in _cards while
            # the backlog copy is stale (round-4 BUG #1). The map is written by
            # update_card before its guards, so resolving through it is enough
            # — the spec explicitly does NOT require rewriting _backlog.
            data = self._cards.get(card.card_id, card)
            widget = build_feed_card(
                data,
                on_review=self._make_review_cb(data.card_id),
                on_accept=self._make_accept_cb(data.card_id),
                on_reject=self._make_reject_cb(data.card_id),
                on_copy=self._make_copy_cb(data),
            )
            widgets.append((data.card_id, widget))

        # Map write under the lock (F8 — this was the one existing writer that
        # violated the class contract at :83); only the construction left it.
        with self._lock:
            for card_id, widget in widgets:
                self._card_widgets[card_id] = widget

        # Ids just built — the eviction exclusion set. Mandatory: this page is
        # the OLDEST content in the feed, i.e. exactly the victim set, so
        # without it every click would build PAGE_SIZE widgets, evict them and
        # push them straight back — a permanent no-op (round-3 BUG #1).
        ids_just_loaded = {card_id for card_id, _w in widgets}

        def _render():
            if self._feed_tab is None:
                return

            # Remove old load-more widget
            if self._load_more_widget is not None:
                self._feed_tab.remove_card("__load_more__")

            # Prepend new cards (oldest of page first → they go above existing cards)
            for card_id, widget in widgets:
                self._feed_tab.prepend_card(widget, card_id)

            # Re-add load-more if backlog still has cards
            if remaining > 0:
                self._load_more_widget = self._build_load_more_widget(remaining)
                self._feed_tab.prepend_card(self._load_more_widget, card_id="__load_more__")
            else:
                self._load_more_widget = None

            # FINAL statement — NOT after the page loop above. The sentinel
            # rebuild above bakes the PRE-push `remaining`; eviction running
            # mid-_render would rebuild the sentinel from the post-push backlog
            # and this tail would then build a SECOND bar from the stale
            # `remaining`, repointing _cards_by_id at it (round-6 BUG #4).
            self._evict_surplus_card_widgets(exclude=frozenset(ids_just_loaded))

        self._GLib.idle_add(_render)

    def _effective_live_window(self) -> int:
        """Resolve the eviction cap for THIS pass (SPEC-03 SP2 rulings).

        R1: effective cap = min(MAX_LIVE_CARD_WIDGETS, get_live_window())
        — config can only LOWER the widget cap, never raise it. The 300
        retention default is a card-retention figure (SP3's harness), not a
        widget raise: the post-mortem budget is 0.5 MB/min and the measured
        clean slope was already 0.82, so the default must not add widgets.

        R3: read at eviction-call time (MEMRATCHET §2.1) — never cached in
        __init__, so a config edit lands on the next pass.

        R4: get_live_window is a lock-free read-only prefs read (file
        open + json parse, µs–ms) — safe on the main thread; the eviction
        pass must never take the prefs flock.

        R5: any failure — no active project path, I/O error, parse error,
        or a garbage (non-int) value from the accessor that the clamp
        itself cannot compare — logs and falls back to
        MAX_LIVE_CARD_WIDGETS (pre-SP2 behavior): config is non-critical
        and must never break the append/evict path.
        """
        try:
            project_path = self._project_paths.get(self._active_project_name or "")
            if not project_path:
                return MAX_LIVE_CARD_WIDGETS  # R3/R5: no active project
            configured = feed_store.get_live_window(project_path)
            return min(MAX_LIVE_CARD_WIDGETS, configured)  # R1: inside try —
            # a garbage return raises TypeError at the clamp (077bdd64 latent
            # bug, caught by SPEC-04's gate); guarded here like any other
            # accessor failure.
        except Exception as e:  # noqa: BLE001 — R5: config is non-critical
            _logger.warning(
                "eviction cap: live-window read failed; using %d: %s",
                MAX_LIVE_CARD_WIDGETS, e,
            )
            return MAX_LIVE_CARD_WIDGETS

    def _evict_surplus_card_widgets(self, exclude: frozenset[str] = frozenset()) -> None:
        """Release card widgets for the oldest cards beyond the live window.

        Card DATA is never dropped: the evicted FeedCardData is pushed back onto
        _backlog (newest-first), so Load More re-renders it (F4). If the backlog
        was fully drained, the Load More widget is rebuilt so the pushed cards
        are reachable without reopening the project (round-3 BUG #2).

        Victims are chosen by seq_num — NOT list position — and across ALL
        projects, because _card_widgets is one flat dict (feed_handler.py:73)
        while widget creation is keyed on the CARD's project, which need not be
        the active one (round-3 BUG #5).

        `exclude` are the ids the caller just rendered (Load More passes its
        page): without it, a Load More click re-evicts the page it just built
        and the button becomes a permanent no-op (round-3 BUG #1).

        Threading: dict mutation under self._lock (class contract at :83);
        FeedTab calls are main-thread only and are never made while holding it.
        """
        if self._feed_tab is None:
            return
        # SP2 (SPEC-03): the cap is CONFIG — resolved per pass (R3 call-time
        # read) as min(constant, get_live_window) (R1: config can only lower;
        # R5: failure → 120 inside the helper). Read OUTSIDE the lock: the
        # helper does file I/O and must never hold self._lock (R4).
        cap = self._effective_live_window()
        with self._lock:
            if len(self._card_widgets) <= cap:
                return
            candidates = {
                cid for ids in self._project_cards.values() for cid in ids
            } | set(self._card_widgets)
            live = [c for c in candidates
                    if c in self._card_widgets and c in self._cards and c not in exclude]
            if len(live) <= KEEP_NEWEST_CARDS:
                return
            live.sort(key=lambda cid: self._cards[cid].seq_num or 0)  # oldest first
            victims = live[: len(live) - KEEP_NEWEST_CARDS]
            target = len(self._card_widgets) - cap

        was_near_bottom = self._feed_tab.is_near_bottom()
        vadj = self._feed_tab.get_vadjustment()
        spacing = self._card_container_spacing()
        removed_height = 0
        released = 0
        backlog_count = 0
        for card_id in victims:
            if released >= target:
                break
            with self._lock:
                widget = self._card_widgets.get(card_id)
                data = self._cards.get(card_id)
            if widget is None:
                continue
            # Never destroy a widget that is on screen or below the viewport (F10).
            if not self._feed_tab.is_above_viewport(widget):
                break
            height = widget.get_height() or 0
            with self._lock:
                if self._card_widgets.pop(card_id, None) is None:
                    continue
                if data is not None:
                    # Newest-first backlog (pop(0)) → Load More re-renders it. Under
                    # the lock: _load_and_render MERGES _backlog on a background
                    # thread (survivors first; :1661-1665) (round-2 BUG #3).
                    self._backlog.insert(0, data)
                backlog_count = len(self._backlog)   # P7a: label read, same lock
            self._feed_tab.remove_card(card_id)     # clears state, unparents, drops map entry
            # Gtk.Box spacing (feed_tab.py:86) applies BETWEEN children, so each
            # removed child shortens the content by height + spacing (round-2 BUG #2).
            removed_height += height + spacing
            released += 1

        if released:
            # Load More must reflect the cards eviction just pushed back: the label
            # bakes `remaining` in at build time (:1736) and is otherwise only
            # recomputed inside _load_more (:1771, :1786-1788). Before this change
            # _backlog only ever shrank, so eviction is the first thing that can
            # make the widget under-report (round-4 BUG #4).
            # The remove_card call (early-returns on an unknown id) is for the
            # rebuild, NOT for duplicate prevention — the duplicate is created on
            # the load path, which eviction never runs on (round-5 BUG #1; see the
            # load-path bullet below).
            self._feed_tab.remove_card("__load_more__")
            self._load_more_widget = None
            # backlog_count was read inside the locked insert region above —
            # reading len(self._backlog) here would be an unlocked read against
            # a list the loader can merge into on another thread (P5 audit).
            if backlog_count:
                self._load_more_widget = self._build_load_more_widget(backlog_count)
                self._feed_tab.prepend_card(self._load_more_widget, card_id="__load_more__")
            if was_near_bottom:
                self._feed_tab.schedule_scroll_to_bottom()      # keep the bottom pinned
            elif vadj is not None and removed_height:
                vadj.set_value(max(0.0, vadj.get_value() - removed_height))

    def _card_container_spacing(self) -> int:
        """Gtk.Box spacing between cards, for scroll compensation. 0 if unknown."""
        try:
            return self._feed_tab.get_card_container().get_spacing()
        except (AttributeError, TypeError):
            return 0

    # ─────────────────────────────────────────────────────────────────
    # Button action handlers
    # ─────────────────────────────────────────────────────────────────

    def handle_review(self, card_id: str, card_widget=None) -> None:
        """Review button clicked — toggle context panel visibility."""
        card = self._cards.get(card_id)
        if card is None:
            return

        card.reviewed = True

        if card_widget is not None and hasattr(card_widget, '_context_panel'):
            panel = card_widget._context_panel
            panel.set_visible(not panel.get_visible())

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

    def handle_accept(self, card_id: str) -> None:
        """
        Accept button clicked.

        For git-backed cards (diff, file_created, file_deleted):
          1. Stage + commit in background thread
          2. Mark card.accepted = True on main thread
          3. Visual feedback via CSS class

        For other card types:
          1. Mark card.accepted = True
        """
        card = self._cards.get(card_id)
        if card is None:
            return

        if card.card_type in ("diff", "file_created", "file_modified", "file_deleted"):
            project_path = card.metadata.get("project_path", "") or self._project_paths.get(card.project_name, "")
            if not project_path:
                return

            def _git_accept():
                result_stage = git_ops.stage_all(project_path)
                if not result_stage.success:
                    _logger.warning("handle_accept: git stage failed for %s", project_path)
                    return

                # Generate the commit message from the ACTUAL staged files,
                # not from card.title. card.title is user-facing text (not a
                # file path) and may not match the real diff. Same fix as
                # T2-RL2 in review_handler.
                #
                # Only catch ImportError (gitpython not installed). Other
                # exceptions are logged as warnings — the user clicked Accept
                # on a card and the card remains visible, so we don't need
                # to surface a chat message like T2-RL2 does.
                try:
                    import git as gitpython
                except ImportError:
                    staged = []
                else:
                    try:
                        repo = gitpython.Repo(project_path)
                        staged = repo.index.diff("HEAD")
                    except Exception as e:
                        _logger.warning(
                            "handle_accept: failed to read diff for %s: %s: %s",
                            project_path, type(e).__name__, e,
                        )
                        return

                if not staged:
                    # Working tree is clean — nothing to commit. Silent no-op:
                    # the user clicked Accept on a card but the underlying
                    # changes have already been accepted (or never existed).
                    # Log a warning for observability, but don't create an
                    # empty commit and don't mark the card as accepted.
                    _logger.info(
                        "handle_accept: nothing to commit for card %s (working tree clean)",
                        card_id,
                    )
                    return

                # Build a descriptive message from the actual files
                file_list = sorted({d.a_path or d.b_path for d in staged if d.a_path or d.b_path})
                if len(file_list) == 1:
                    commit_msg = f"Accept: {file_list[0]}"
                elif len(file_list) <= 3:
                    commit_msg = f"Accept: {len(file_list)} files ({', '.join(file_list)})"
                else:
                    commit_msg = f"Accept: {len(file_list)} files ({', '.join(file_list[:3])}...)"

                result_commit = git_ops.commit(project_path, commit_msg)
                if result_commit.success:
                    card.accepted = True
                    card.metadata["project_path"] = project_path
                    # Persist to feed.json (Phase 1: via the background writer)
                    self._enqueue_card_update(project_path, card_id, {"accepted": True})
                    # Update visual on main thread
                    def _mark():
                        self._update_card_visual(card_id, accepted=True)
                    self._GLib.idle_add(_mark)
                    self._GLib.idle_add(lambda: self._add_git_card(card, result_commit))

            # Record path for echo suppression BEFORE starting git thread.
            # CrabWatch will fire events when git modifies the filesystem;
            # on_filesystem_event() checks _recent_git_paths to suppress echoes.
            if card.file_path:
                self._recent_git_paths[card.file_path] = time.monotonic()

            t = threading.Thread(target=_git_accept, daemon=True)
            t.start()
        else:
            # REVIEW-PERSIST-1: a non-git decision is durable state too — the
            # git path above enqueues {"accepted": True}; this branch used to
            # stop at the in-memory mutation, so the decision was lost on
            # reload. update_card records the decision, refreshes the widget
            # (in-place seam or rebuild) and enqueues the persist — the same
            # approve_exec mechanism the approval cards use.
            project_path = card.metadata.get("project_path", "") or self._project_paths.get(card.project_name, "")
            if not project_path:
                _logger.warning(
                    "handle_accept: non-git card %s accepted with no project "
                    "path — decision cannot be persisted to feed.json",
                    card_id,
                )
            card.accepted = True
            self.update_card(card_id, card)
            # Refresh batch accept bar (Phase 5)
            self._update_batch_bar_for_active_project()

    def handle_reject(self, card_id: str) -> None:
        """
        Reject button clicked.

        For git-backed cards:
          1. Revert changes in background thread
          2. Mark card.accepted = False
          3. Notify agent via on_send_to_agent

        For other cards:
          1. Mark card.accepted = False
        """
        card = self._cards.get(card_id)
        if card is None:
            return

        if card.card_type in ("diff", "file_created", "file_modified", "file_deleted"):
            project_path = card.metadata.get("project_path", "") or self._project_paths.get(card.project_name, "")
            if not project_path:
                return

            def _git_reject():
                fp = card.file_path
                sha = card.commit_sha or "HEAD"

                # MED-11: Validate commit_sha before git call
                if not _VALID_SHA_RE.match(sha):
                    _logger.warning(
                        "MED-11: Invalid commit SHA %r for card %s — skipping reject",
                        sha, card_id,
                    )
                    return

                result_reject = git_ops.checkout_paths(project_path, sha, [fp]) if fp else None

                def _mark():
                    card.accepted = False
                    card.metadata["project_path"] = project_path
                    self._update_card_visual(card_id, accepted=False)
                    # Persist to feed.json (Phase 1: via the background writer)
                    self._enqueue_card_update(project_path, card_id, {"accepted": False})
                    # Notify agents — FIX 6.2 (SP2 audit): the former
                    # f"project:{name}" key was a guaranteed no-op receiver.
                    # Per-member routing mirrors ReviewHandler's rejection
                    # fan-out; special:supervisor is the fallback when the
                    # project has no registered members (honest minimal
                    # option — the FeedHandler callback shape is a single
                    # (session_key, text) pair, so member iteration happens
                    # here rather than in the callback).
                    msg = f"[PM] Rejected change: {card.title}"
                    members = []
                    if self._project_handler is not None:
                        try:
                            members = list(
                                self._project_handler.get_project_members(
                                    card.project_name
                                )
                            )
                        except Exception as exc:
                            _logger.warning(
                                "feed: member lookup for %s failed: %s",
                                card.project_name, exc,
                            )
                    targets = members or ["special:supervisor"]
                    for target in targets:
                        self._on_send_to_agent(target, msg)
                    # Add git card
                    self._add_git_card(card, result_reject)
                self._GLib.idle_add(_mark)

            # Record path for echo suppression BEFORE starting git thread.
            if card.file_path:
                self._recent_git_paths[card.file_path] = time.monotonic()

            t = threading.Thread(target=_git_reject, daemon=True)
            t.start()
        else:
            # REVIEW-PERSIST-1: same persist parity as handle_accept's non-git
            # branch — record the durable decision via update_card.
            project_path = card.metadata.get("project_path", "") or self._project_paths.get(card.project_name, "")
            if not project_path:
                _logger.warning(
                    "handle_reject: non-git card %s rejected with no project "
                    "path — decision cannot be persisted to feed.json",
                    card_id,
                )
            card.accepted = False
            self.update_card(card_id, card)
            # Refresh batch accept bar (Phase 5)
            self._update_batch_bar_for_active_project()

    def handle_batch_accept(self, card_ids: list[str]) -> None:
        """
        Accept a batch of consecutive file-change cards in one click.
        Iterates in order (top-to-bottom in the feed); each accept creates a
        git_commit card via _add_git_card() with the same flow as the singular
        handle_accept(). (Phase 5)

        Used by the batch accept bar when ≥2 file-change cards are pending.
        """
        for card_id in card_ids:
            self.handle_accept(card_id)

    def _on_batch_accept_clicked(self) -> None:
        """
        Called when user clicks the batch accept bar's "Accept All" button.
        Computes the list of consecutive pending file-change cards at the
        bottom of the feed and accepts them all. (Phase 5)
        """
        if self._feed_tab is None:
            return
        project_name = self._active_project_name
        if project_name is None:
            return
        all_cards = self.get_cards_for_project(project_name)
        if not all_cards:
            return
        actionable_types = ("diff", "file_created", "file_modified", "file_deleted")
        batch_ids: list[str] = []
        for card in all_cards:  # newest first
            if (card.card_type in actionable_types
                    and card.accepted is None
                    and card.card_id is not None):
                batch_ids.append(card.card_id)
            else:
                break
        # batch_ids is newest-first; reverse to top-to-bottom for handle_accept order
        batch_ids.reverse()
        self.handle_batch_accept(batch_ids)
        # Refresh the batch bar (count may now be 0 or 1)
        self._update_batch_bar_for_active_project()

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

    def _finalize_snapshot(self, card_id: str) -> bool:
        """Deferred snapshot creation — runs via GLib.idle_add after bubble is in chat box.

        Phase 5 split (spec §2.5 Edit B): the GTK-bound chat-box walk stays on
        this (main) thread inside _maybe_create_snapshot; the pure snapshot
        construction runs on the snapshot builder thread and dispatches only
        the widget update back via GLib.idle_add.
        """
        card = self._cards.get(card_id)
        if card is not None:
            self._maybe_create_snapshot(card)
        return False  # Don't repeat

    def _ensure_snapshot_builder(self) -> None:
        """Lazily (re)start the background snapshot builder thread.

        Mirrors _ensure_persist_writer: the stop flag is cleared before the
        liveness check so a request racing a shutdown is never stranded.
        """
        self._snapshot_stop = False
        thread = self._snapshot_builder
        if thread is not None and thread.is_alive():
            return
        self._snapshot_builder = threading.Thread(
            target=self._snapshot_build_loop,
            name="crabcakes-snapshot-builder",
            daemon=True,
        )
        self._snapshot_builder.start()

    def _snapshot_build_loop(self) -> None:
        """Background thread: build queued snapshots (Phase 5 §2.5 Edit B)."""
        while not self._snapshot_stop:
            self._snapshot_wakeup.wait()
            self._snapshot_wakeup.clear()
            while True:
                with self._snapshot_queue_lock:
                    if not self._snapshot_jobs:
                        break
                    key = next(iter(self._snapshot_jobs))
                    builder, card_ids = self._snapshot_jobs.pop(key)
                    # Cards that register for this key while the build runs
                    # append to this same list, so a duplicate event never
                    # triggers a second build (Edit C).
                    self._snapshot_in_flight[key] = card_ids
                try:
                    snapshot = builder()
                except Exception:  # noqa: BLE001 — a failed build must not kill the thread
                    _logger.exception(
                        "snapshot builder failed for key %r; card(s) keep no snapshot",
                        key,
                    )
                    snapshot = None
                with self._snapshot_queue_lock:
                    self._snapshot_in_flight.pop(key, None)
                    self._snapshot_cache[key] = (snapshot, time.monotonic())
                    self._prune_snapshot_cache_locked()
                    targets = list(card_ids)
                for target in targets:
                    self._GLib.idle_add(self._apply_snapshot, target, snapshot)

    def _prune_snapshot_cache_locked(self) -> None:
        """Drop reuse-cache entries older than the reuse window.

        Caller must hold `_snapshot_queue_lock`. Keeps the cache bounded — it
        only ever exists to collapse duplicate events on the same tick.
        """
        cutoff = time.monotonic() - self.SNAPSHOT_REUSE_SECONDS
        for key in [
            k for k, (_snapshot, ts) in self._snapshot_cache.items() if ts < cutoff
        ]:
            del self._snapshot_cache[key]

    def _request_snapshot_build(self, key, card_id: str, builder: Callable) -> None:
        """Queue a pure snapshot build for `key`, coalesced across cards.

        Args:
            key:        Coalescing key — ("git_diff", project_path, file_path)
                        for system/crabwatch cards, ("messages", card_id) for
                        agent cards.
            card_id:    The card that needs the snapshot. Every card that
                        registers for `key` receives the built snapshot.
            builder:    Zero-arg callable performing the PURE construction
                        (no GTK) and returning a ConversationSnapshot | None.

        Returns immediately. The build (if needed) happens on the snapshot
        builder thread; only the widget update is dispatched back.
        """
        with self._snapshot_queue_lock:
            cached = self._snapshot_cache.get(key)
            if cached is not None and (time.monotonic() - cached[1]) < self.SNAPSHOT_REUSE_SECONDS:
                # Same-tick duplicate: reuse the just-built result (last
                # write wins per (project_path, file_path)) — no second build.
                reuse = cached[0]
            elif key in self._snapshot_in_flight:
                self._snapshot_in_flight[key].append(card_id)
                return
            else:
                job = self._snapshot_jobs.get(key)
                if job is None:
                    self._snapshot_jobs[key] = (builder, [card_id])
                    self._ensure_snapshot_builder()
                    self._snapshot_wakeup.set()
                else:
                    job[1].append(card_id)
                return
        self._GLib.idle_add(self._apply_snapshot, card_id, reuse)

    def _apply_snapshot(self, card_id: str, snapshot) -> bool:
        """Attach a built snapshot to its card and refresh the panel.

        Main thread (dispatched via GLib.idle_add by the builder thread, or
        called directly on a cache hit). Returns False for the GLib contract.
        """
        card = self._cards.get(card_id)
        if card is None or snapshot is None:
            return False
        card.conversation_snapshot = snapshot
        # Check size limit — skip persistence if too large
        if conversation_store.snapshot_exceeds_size_limit(snapshot):
            _logger.warning(
                "Snapshot for card %s exceeds %dKB — rendered in-memory but not persisted",
                card.card_id, conversation_store.MAX_SNAPSHOT_SIZE_KB,
            )
            # Remove from metadata so to_dict() won't persist it,
            # but keep on card_data for in-memory rendering.
            # We achieve this by NOT setting metadata["snapshot"] —
            # to_dict() serializes from conversation_snapshot field.
            # Instead, we'll handle this in _persist by stripping snapshot.
            card.metadata["_snapshot_oversized"] = True

        widget = self._card_widgets.get(card_id)
        if widget is not None and hasattr(widget, "_context_panel"):
            # Panel already built without snapshot — update it
            panel = widget._context_panel
            # Clear old content
            child = panel.get_first_child()
            while child is not None:
                next_child = child.get_next_sibling()
                panel.remove(child)
                child = next_child
            # Rebuild panel content with snapshot data
            self._rebuild_context_panel(panel, snapshot)
        return False

    def _rebuild_context_panel(self, panel, snapshot):
        """Rebuild the contents of a context panel from a snapshot."""
        from ui.views.feed_card import build_context_panel
        # We can't replace the panel in-place easily, so we rebuild its children.
        # build_context_panel returns a new box — copy its children into the existing panel.
        new_panel = build_context_panel(snapshot)
        child = new_panel.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            new_panel.remove(child)
            panel.append(child)
            child = next_child

    def _maybe_create_snapshot(self, card_data: FeedCardData) -> None:
        """
        Schedule a conversation snapshot build for the card if applicable.

        - Agent cards: extract conversation from the chat box (GTK, this
          thread) and build the snapshot on the worker thread.
        - System/crabwatch cards: build the git diff on the worker thread.

        Phase 5 (spec §2.5): this is now the SINGLE build site for
        system/crabwatch cards — on_filesystem_event() no longer builds a diff
        inline (Edit A) — hence the defensive early return below.
        """
        if card_data.conversation_snapshot is not None:
            # Defensive: never rebuild a snapshot that is already attached.
            return

        if card_data.source == "agent" and self._get_chat_box_for_session:
            # Use tab_key (the chat box key, e.g. "project:crabwatch") not session_key
            # (the agent's gateway key, e.g. "agent:qaster:...") for the lookup.
            # Falls back to session_key for non-project chats where both are the same.
            lookup_key = card_data.metadata.get("tab_key", "") or card_data.metadata.get("session_key", "")
            chat_box = self._get_chat_box_for_session(lookup_key)
            if chat_box is not None:
                messages_raw = self._extract_messages_from_chat_box(chat_box)
                # Per-card key: a conversation snapshot belongs to one card.
                key = ("messages", card_data.card_id)
                self._request_snapshot_build(
                    key,
                    card_data.card_id,
                    lambda messages=messages_raw, lk=lookup_key:
                        conversation_store.snapshot_from_messages(
                            messages, lk, total_available=len(messages)
                        ),
                )

        elif card_data.source in ("system", "crabwatch"):
            project_path = card_data.metadata.get("project_path", "") or self._project_paths.get(card_data.project_name, "")
            if project_path and card_data.file_path:
                # Coalescing key: same project + same path → one build, so
                # rapid duplicate events for one path build the diff once
                # (last-write-wins per (project_path, file_path)).
                key = ("git_diff", project_path, card_data.file_path)
                self._request_snapshot_build(
                    key,
                    card_data.card_id,
                    lambda pp=project_path, fp=card_data.file_path:
                        conversation_store.snapshot_from_git_diff(pp, fp),
                )

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
