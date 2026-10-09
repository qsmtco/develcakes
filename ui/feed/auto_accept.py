"""Auto-accept preferences for the project feed."""
from __future__ import annotations

from typing import Callable

from models.feed_card import FeedCardData

from ui.feed._facade import _mod


class FeedAutoAcceptMixin:
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
        blocking all auto-accept. The migration guard in _mod().feed_store.py
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

