# ui/views/feed_tab.py
# Feed tab container - pure view for the Projects notebook's "Feed" sub-tab.
# No business logic, no state mutations.
#
# HTML-only (feed GTK-removal plan, chunk E1): the card list is a WebKit
# document (ui/views/feed_surface.py). The GTK card list is gone; a feed
# without WebKit is a hard error at wiring time, not a silent fallback.
#
# Public API:
#   class FeedTab(Gtk.Box):
#       attach_html_surface(on_action) -> FeedSurface
#       upsert_card(card) / prepend_html_card(card) / remove_card_html(card_id)
#       set_html_cards(cards) / set_html_load_more(remaining)
#       show_empty_state() -> None
#       remove_card(card_id) -> None          # compat no-op (E2/E3 re-route callers)
#       schedule_smart_scroll_to_bottom() -> None
#       get_vadjustment() -> Gtk.Adjustment | None
#       is_near_bottom(slack: int = 80) -> bool

import logging
from collections.abc import Callable

import gi

gi.require_version('Gtk', '4.0')
from gi.repository import Gtk

logger = logging.getLogger(__name__)  # kept: file-wide convention, used by future handlers


class FeedTab(Gtk.Box):
    """
    Feed document host for the Projects notebook's "Feed" sub-tab.

    Layout:
      Gtk.Box (vertical)
        ├── Gtk.ScrolledWindow (.feed-scroll)
        │     └── WebKit webview (set by attach_html_surface)
        └── bottom toolbar (auto-accept / batch accept)
    """

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_vexpand(True)

        self._feed_scroll: Gtk.ScrolledWindow | None = None
        self._html_surface = None
        self._html_load_more: Gtk.Box | None = None
        # Phase 5 — Auto-accept toggle callback (set by FeedHandler via set_auto_accept_callback).
        # Stores the caller's function; invoked from _on_auto_accept_toggled when the user clicks
        # the toggle button. None means no callback is wired yet (toggling still updates the
        # visual but does nothing externally). Legacy/Phase-5 compat: FeedHandler's Phase 4
        # code still wires this for the legacy single-toggle path.
        self._auto_accept_callback: Callable[[bool], None] | None = None
        # V2 — Per-type toggle callbacks (set by FeedHandler via set_*_toggle_callback).
        self._diffs_toggle_callback: Callable[[bool], None] | None = None
        self._files_toggle_callback: Callable[[bool], None] | None = None
        self._exec_toggle_callback: Callable[[str], None] | None = None
        # V2 — Agent scope callback (wired by FeedHandler in Phase 6).
        self._agent_scope_callback: Callable[[str], None] | None = None
        # V2 — Exec mode (3-state cycle: off → show → silent → off).
        self._exec_mode: str = "off"
        # Bug F regression guard: programmatic set_active() in
        # update_auto_accept_prefs() emits the 'toggled' signal on GTK 4.14
        # (the inline comments claimed it does NOT, but my repro under Xvfb
        # confirmed the signal fires whenever the value changes False→True
        # or True→False). Without this guard, loading v1 prefs with
        # auto_accept_enabled=true and then opening a project would
        # trigger the warning dialog even though the user never clicked the
        # toggle. See .crabcakes/coder-bugs.md Bug #11 (auto-accept dialog
        # cascade on project open).
        self._syncing_toolbar: bool = False
        # Phase 5 — Batch button label (read by tests for assertions; mirrors the actual
        # _batch_accept_button.get_label()).
        self._batch_button_label: str = ""
        # ── Build the scroll host ───────────────────────────────────────
        # The scroll's child is the WebKit document set by
        # attach_html_surface() (set_child swaps the child; it does not add a
        # second scroller). There is no GTK card-list fallback: a feed
        # without WebKit is a hard error at wiring time (PM directive — SPEC
        # fallback deleted entirely).
        scroll = Gtk.ScrolledWindow()
        scroll.add_css_class("feed-scroll")
        scroll.set_vexpand(True)
        # Vertical scrolling belongs to the WebKit document attached below
        # (FeedSurface's webview). NEVER prevents a second, dead scrollbar
        # around the webview; attach_html_surface() re-asserts this once the
        # child exists.
        scroll.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.NEVER)
        scroll.set_halign(Gtk.Align.FILL)
        scroll.set_valign(Gtk.Align.FILL)
        self._feed_scroll = scroll

        self.append(scroll)

        # ── Persistent bottom toolbar (Phase 5 — auto-accept + batch accept) ──────
        # Always visible regardless of pending count. The batch button inside this
        # toolbar is shown only when count >= 2; the auto-accept toggle is always
        # shown. Built eagerly in __init__ so the toggle exists before
        # update_auto_accept_state() is ever called.
        self._toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._toolbar.add_css_class("feed-toolbar")

        # Group 1 — per-type toggles (v2 granular controls; replaces legacy
        # _auto_accept_toggle per SPEC-AUTO-ACCEPT-GRANULAR-1.md §2.3).
        self._diffs_toggle = Gtk.ToggleButton(label="Diffs: OFF")
        self._diffs_toggle.add_css_class("feed-toolbar-toggle")
        self._diffs_toggle.add_css_class("feed-toolbar-toggle-per-type")
        self._diffs_toggle.connect("toggled", self._on_diffs_toggled)

        # Files is a GROUP toggle covering file_created/modified/deleted.
        # Uses Gtk.ToggleButton for consistency with the Diffs toggle. The
        # three underlying prefs are always toggled as a group, so there is
        # no inconsistent-state ambiguity in normal usage.
        self._files_toggle = Gtk.ToggleButton(label="Files: OFF")
        self._files_toggle.add_css_class("feed-toolbar-toggle")
        self._files_toggle.add_css_class("feed-toolbar-toggle-per-type")
        self._files_toggle.connect("toggled", self._on_files_toggled)

        # Exec toggle is a 3-state cycle (off → show → silent → off), NOT a
        # 2-state toggle. GTK has no native 3-state cycle button, so we use
        # a plain Button with a "clicked" handler that computes the next
        # state from the current self._exec_mode.
        self._exec_toggle = Gtk.ToggleButton(label="Exec: OFF")
        self._exec_toggle.add_css_class("feed-toolbar-toggle")
        self._exec_toggle.add_css_class("feed-toolbar-toggle-per-type")
        self._exec_toggle.connect("clicked", self._on_exec_clicked)

        # Group 2 — agent scope dropdown.
        # Lets the user pick which agents' cards get auto-accepted:
        #   "All agents"  → agent_scope = "all_agents"
        #   "First author" → agent_scope = "first_author" (lazy lock-in)
        #   "<agent name>" → agent_scope = that specific name
        # Additional entries are appended dynamically via set_agent_options().
        self._agent_scope_model = Gtk.StringList()
        self._agent_scope_model.append("All agents")
        self._agent_scope_model.append("First author")
        self._agent_dropdown = Gtk.DropDown(model=self._agent_scope_model)
        self._agent_dropdown.add_css_class("feed-toolbar-agent-dropdown")
        # Map: dropdown index → scope string. Index 0 = "all_agents", 1 = "first_author",
        # 2+ = specific agent names (dynamic).
        self._agent_scope_keys: list[str] = ["all_agents", "first_author"]
        self._agent_dropdown.connect("notify::selected", self._on_agent_dropdown_changed)
        self._syncing_agent_dropdown = False

        self._batch_accept_button = Gtk.Button(label="Accept All")
        self._batch_accept_button.add_css_class("feed-btn-batch-accept")
        self._batch_accept_button.set_visible(False)  # hidden until count >= 2
        self._batch_accept_button.connect("clicked", self._on_batch_button_clicked)

        self._batch_accept_label = Gtk.Label(label="")
        self._batch_accept_label.add_css_class("feed-batch-bar-info")

        # Group separators previously rendered as Gtk.Separator widgets
        # (with CSS class feed-toolbar-divider) took up ~26px of horizontal
        # room each and didn't help legibility. Removed in Phase 7 cleanup
        # to reclaim toolbar space; the tool CSS class is also removed.
        self._toolbar.append(self._diffs_toggle)
        self._toolbar.append(self._files_toggle)
        self._toolbar.append(self._exec_toggle)
        self._toolbar.append(self._agent_dropdown)
        self._toolbar.append(self._batch_accept_button)  # existing batch accept button
        self._toolbar.append(self._batch_accept_label)   # existing batch accept label

        self.append(self._toolbar)

    # ── Public API ───────────────────────────────────────────────────────

    def show_empty_state(self) -> None:
        """Clear the document, or show the NO_WEBKIT notice.

        There is no widget-list empty state any more: the feed is a WebKit
        document or it is nothing (NO_WEBKIT ruling).
        """
        if self._html_surface is None:
            self._show_no_webkit()
            return
        self._html_surface.set_cards([])
        self.set_html_load_more(0)

    def _show_no_webkit(self) -> None:
        """Single GTK label standing in for a feed that cannot exist.

        The feed requires WebKit exactly like the chat surface does. When
        `create_feed_surface` returns None there is no card list to fall back
        to, so the tab says so and stops.
        """
        if self._feed_scroll is None:
            return
        label = Gtk.Label(label="Feed requires WebKit (unset NO_WEBKIT)")
        label.add_css_class("feed-no-webkit")
        label.set_wrap(True)
        label.set_halign(Gtk.Align.CENTER)
        label.set_valign(Gtk.Align.CENTER)
        self._feed_scroll.set_child(label)

    def attach_html_surface(self, on_action):
        """Swap the card list for a WebKit document. None keeps the GTK list.

        Idempotent. Called from FeedHandler.set_feed_tab. Does not load a
        page; the first card or empty-state call does.
        """
        if self._html_surface is not None:
            return self._html_surface
        if self._feed_scroll is None:
            return None
        from ui.views.feed_surface import create_feed_surface

        # The document owns scrolling now: FeedSurface reads the page's own
        # scroll_state reports, so the legacy GTK probes are not wired. The
        # hooks stay in the signature for call compatibility.
        surface = create_feed_surface(
            on_action,
            is_near_bottom=None,
            on_load_finished=None,
        )
        if surface is None:
            return None
        self._html_surface = surface
        self._feed_scroll.set_child(surface.webview)
        # Vertical scrolling belongs to the document (the webview fills the
        # viewport; its own scrollbars move the page). A vertical AUTOMATIC
        # here would add a second, dead scrollbar around the webview.
        self._feed_scroll.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.NEVER)
        surface.set_cards([])
        return surface

    def upsert_card(self, card) -> None:
        if self._html_surface is not None:
            self._html_surface.upsert(card)

    def prepend_html_card(self, card) -> None:
        if self._html_surface is not None:
            self._html_surface.upsert(card, prepend=True)

    def remove_card_html(self, card_id: str) -> None:
        if self._html_surface is not None:
            self._html_surface.remove(card_id)

    # ── Compatibility API (WU 10 purge survivors, SPEC-41 §2 Phase 1(a)) ──
    # The GTK card list was removed in WU 10. These five methods keep the
    # pre-purge signatures so callers that still speak the old API do not
    # raise AttributeError. Only `remove_card` does work: it forwards to the
    # document row so a project switch/close actually clears the surface.

    def remove_card(self, card_id: str) -> None:
        """Compat no-op: the feed is a document. Removes the row if attached.

        The GTK card list was removed in WU 10; callers that still speak the
        old API are re-pointed here so project switch/close cannot raise
        AttributeError.
        """
        self.remove_card_html(card_id)

    def append_card(self, widget, card_id: str) -> None:
        """Compat no-op: widgets are not part of the document feed."""

    def prepend_card(self, widget, card_id: str) -> None:
        """Compat no-op: widgets are not part of the document feed."""

    def replace_card(self, card_id: str, widget) -> None:
        """Compat no-op: widgets are not part of the document feed."""

    def get_card_container(self) -> Gtk.Box:
        """Compat no-op: the document has no GTK container. Returns None."""

    def set_html_cards(self, cards: list) -> None:
        if self._html_surface is not None:
            self._html_surface.set_cards(cards)

    def set_html_load_more(self, remaining: int) -> None:
        """GTK control above the document. Hidden when there is nothing older."""
        if remaining <= 0:
            if self._html_load_more is not None:
                self._html_load_more.set_visible(False)
            return
        if self._html_load_more is None:
            bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            bar.add_css_class("feed-card-load-more")
            label = Gtk.Label()
            label.set_hexpand(True)
            label.set_halign(Gtk.Align.START)
            button = Gtk.Button(label="Load More")
            button.add_css_class("feed-btn-load-more")
            button.connect("clicked", lambda *_a: self._on_html_load_more())
            bar.append(label)
            bar.append(button)
            self._html_load_more = bar
            self._html_load_more_label = label
            self.prepend(bar)
        noun = "card" if remaining == 1 else "cards"
        self._html_load_more_label.set_text(f"📂 {remaining} older {noun}")
        self._html_load_more.set_visible(True)

    def set_html_load_more_callback(self, callback) -> None:
        self._html_load_more_callback = callback

    def _on_html_load_more(self) -> None:
        callback = getattr(self, "_html_load_more_callback", None)
        if callback is not None:
            callback()

    def schedule_smart_scroll_to_bottom(self) -> None:
        """No-op. Callers exist across the handler/mixins; the follow
        decision lives in FeedSurface._flush (the document owns scrolling).
        """
        return

    def get_vadjustment(self):
        """The feed ScrolledWindow's vadjustment, or None when _feed_scroll is absent.

        A constructed tab owns a real ScrolledWindow, so this normally returns
        a real (zeroed, pre-layout) Adjustment; None requires an explicitly
        absent/nulled _feed_scroll. (P2 audit; SPEC-MEMORY-WIDGET-RATCHET
        §2.2 — the earlier "None before first map" wording was wrong.)
        """
        return self._feed_scroll.get_vadjustment() if self._feed_scroll else None

    def is_near_bottom(self, slack: int = 80) -> bool:
        """True when the feed is within `slack` px of its bottom.

        With an HTML surface attached the document scrolls itself; its own
        `scroll_state` reports carry the follow intent. No surface attached
        means nothing is rendered, so eviction is trivially safe.
        """
        if self._html_surface is not None:
            return self._html_surface._doc_near_bottom
        return True

    def is_above_viewport(self, widget) -> bool:
        """Always False: card eviction is a GTK-list concept (WU 10 purge).

        The only caller was the GTK eviction branch, which is dead on the
        HTML surface: the document owns its own rows and there is no GTK
        container geometry to measure against. False is the fail-safe answer
        — "not above the viewport" means "never evict".
        """
        return False

    def update_batch_bar(self, pending_count: int) -> None:
        """
        Update the batch accept button inside the persistent bottom toolbar.
        pending_count is the number of consecutive pending file-change cards
        stacked at the bottom of the feed. Button is hidden when count < 2,
        shown with label "Accept All (N)" when count >= 2. (Phase 5)
        """
        if pending_count >= 2:
            self._batch_accept_button.set_label(f"Accept All ({pending_count})")
            self._batch_accept_button.set_visible(True)
        else:
            self._batch_accept_button.set_label("Accept All")
            self._batch_accept_button.set_visible(False)
        self._batch_button_label = self._batch_accept_button.get_label()
        self._batch_accept_label.set_text("")

    # ── Phase 5: Auto-accept toggle API ───────────────────────────────────────

    def set_auto_accept_callback(self, callback: Callable[[bool], None] | None) -> None:
        """
        Install the callback invoked when the user clicks the auto-accept toggle.
        The callback receives the new active state (True = ON, False = OFF).
        Pass None to clear. Called by FeedHandler after set_feed_tab(). (Phase 5)

        Legacy compatibility: kept on the rebuilt toolbar as a no-op-style
        hook for any code that still passes a legacy callback. The wired
        legacy callback still updates _auto_accept_active state in MockFeedTab
        (used by ~10 existing tests); on the real FeedTab, the callback is
        invoked and the new per-type toggles set the toggle visuals via
        update_auto_accept_prefs(). See set_diffs_toggle_callback etc. for
        the v2 wiring.
        """
        self._auto_accept_callback = callback

    def set_diffs_toggle_callback(self, callback: Callable[[bool], None] | None) -> None:
        """Install callback for the Diffs toggle. Receives new active state.

        Triggered by: _on_diffs_toggled() (the widget's 'toggled' signal).
        The callback is invoked synchronously from the GTK main loop when
        the user clicks the Diffs toggle.
        """
        self._diffs_toggle_callback = callback

    def set_files_toggle_callback(self, callback: Callable[[bool], None] | None) -> None:
        """Install callback for the Files toggle. Receives new active state.

        Triggered by: _on_files_toggled() (the widget's 'toggled' signal).
        The callback is invoked synchronously from the GTK main loop when
        the user clicks the Files toggle.
        """
        self._files_toggle_callback = callback

    def set_exec_toggle_callback(self, callback: Callable[[str], None] | None) -> None:
        """Install callback for the Exec toggle. Receives new mode string.

        Triggered by: _on_exec_clicked() (the widget's 'clicked' signal).
        The callback receives the new mode ("off" | "show" | "silent") after
        the 3-state cycle computes it. Note: this is NOT a 'toggled' signal
        because the Exec toggle is a 3-state cycle button, not a binary toggle.
        """
        self._exec_toggle_callback = callback

    def set_agent_scope_callback(self, callback: Callable[[str], None] | None) -> None:
        """Install callback for the agent dropdown. Receives new scope string.

        Called when the user changes the dropdown selection. The callback
        receives the scope key ("all_agents", "first_author", or an agent name).
        """
        self._agent_scope_callback = callback

    def set_agent_options(self, agent_names: list[str]) -> None:
        """Populate the dropdown with additional agent name entries.

        Keeps the first two entries ("All agents", "First author") fixed;
        replaces entries at index 2+ with the supplied agent names.
        Called by FeedHandler when agents are registered.
        """
        # Remove dynamic entries (index 2 onward)
        while self._agent_scope_model.get_n_items() > 2:
            self._agent_scope_model.remove(2)
        self._agent_scope_keys = ["all_agents", "first_author"]
        for name in agent_names:
            self._agent_scope_model.append(name)
            self._agent_scope_keys.append(name)

    def _on_agent_dropdown_changed(self, dropdown, _param) -> None:
        """Signal handler for notify::selected on the agent scope dropdown.

        Suppresses callbacks during programmatic sync (update_auto_accept_prefs)
        so only real user selections fire the callback chain.
        """
        if self._syncing_agent_dropdown:
            return
        idx = dropdown.get_selected()
        if 0 <= idx < len(self._agent_scope_keys):
            scope = self._agent_scope_keys[idx]
            if self._agent_scope_callback is not None:
                self._agent_scope_callback(scope)

    def update_auto_accept_prefs(self, prefs_dict: dict) -> None:
        """Reconcile all toolbar visuals from the v2 prefs dict.

        Called by FeedHandler whenever prefs change. The view never owns state;
        it only reflects what the handler tells it.

        Args:
            prefs_dict: A v2 prefs dict (as produced by AutoAcceptPrefs.to_dict()
                or load_feed_prefs()). Must have version == 2.

        Bug F regression guard: GTK 4.14's Gtk.ToggleButton.set_active()
        emits the 'toggled' signal whenever the value actually changes
        (False→True or True→False), contrary to the GTK 3 documentation
        and the inline comments in this file that claimed it does NOT.
        Without the _syncing_toolbar flag, loading v1 prefs with
        auto_accept_enabled=true and then opening a project would trigger
        two warning dialogs (one for Diffs, one for Files) even though the
        user never clicked the toolbar. The flag short-circuits
        _on_diffs_toggled / _on_files_toggled for the duration of the
        programmatic sync so only real user clicks reach the FeedHandler
        and its warning-dialog callback chain. See
        .crabcakes/coder-bugs.md Bug #11.
        """
        self._syncing_toolbar = True
        try:
            auto = prefs_dict.get("auto_accept", {})
            fc = auto.get("file_changes", {})

            # Diffs toggle (covers "diff" type)
            diff_enabled = fc.get("diff", {}).get("enabled", False)
            self._diffs_toggle.set_active(diff_enabled)
            self._diffs_toggle.set_label(f"Diffs: {'ON' if diff_enabled else 'OFF'}")

            # Files toggle (covers file_created, file_modified, file_deleted)
            files_enabled = any(
                fc.get(ct, {}).get("enabled", False)
                for ct in ("file_created", "file_modified", "file_deleted")
            )
            self._files_toggle.set_active(files_enabled)
            self._files_toggle.set_label(f"Files: {'ON' if files_enabled else 'OFF'}")

            # Exec toggle (3-state). set_label does NOT emit 'clicked', so
            # no signal-block guard is needed here (only set_active on the
            # Diffs/Files toggles triggers the cascade).
            exec_mode = auto.get("exec_command", {}).get("mode", "off")
            self._exec_mode = exec_mode
            self._exec_toggle.set_label(f"Exec: {exec_mode.upper()}")

            # Agent scope dropdown — sync selection from prefs.
            # Uses exec_command.agent_scope as the representative scope
            # (all categories share the same scope in practice; the handler
            # applies scope changes to all categories uniformly).
            scope = auto.get("exec_command", {}).get("agent_scope", "first_author")
            # Guard: normalize stale "system" to "all_agents"
            if scope == "system":
                scope = "all_agents"
            self._syncing_agent_dropdown = True
            try:
                if scope in self._agent_scope_keys:
                    self._agent_dropdown.set_selected(
                        self._agent_scope_keys.index(scope)
                    )
                else:
                    # Unknown scope name — default to "First author" (index 1)
                    self._agent_dropdown.set_selected(1)
            finally:
                self._syncing_agent_dropdown = False

            # Snooze state stays on the prefs dataclass + store round-trip
            # (test_feed_retention pins the field); the toolbar button is gone.
        finally:
            self._syncing_toolbar = False

    def update_auto_accept_state(self, active: bool) -> None:
        """Legacy bridge — constructs a prefs dict from the single-toggle state.

        Deprecated: use update_auto_accept_prefs() directly. Preserves v1
        semantics: ALL four file-change types enabled together with
        agent_scope='all_agents'. New code should set per-type prefs via
        update_auto_accept_prefs instead. (Phase 5 → Phase 6)
        """
        prefs = {
            "version": 2,
            "auto_accept": {
                "file_changes": {
                    ct: {"enabled": active, "agent_scope": "all_agents"}
                    for ct in ("diff", "file_created", "file_modified", "file_deleted")
                },
                "exec_command": {"mode": "off", "agent_scope": "all_agents"},
                "snoozed_card_ids": [],
            },
        }
        self.update_auto_accept_prefs(prefs)

    def _on_auto_accept_toggled(self, button: Gtk.ToggleButton) -> None:
        """
        Handler for the legacy toggle's 'toggled' signal. (Phase 5)

        Kept for legacy compat: the legacy _auto_accept_toggle widget was
        removed in Phase 5, but this handler is preserved because:
        (a) MockFeedTab mirrors the same signature,
        (b) FeedHandler.set_auto_accept_callback() still wires to it,
        (c) It is not called on real FeedTab (no widget emits 'toggled' for
            it anymore), so it is effectively dead code on real FeedTab.
        Fires when the user clicks the legacy toggle. (programmatic
        set_active() does NOT emit 'toggled'). Forwards the new state to
        the legacy callback installed via set_auto_accept_callback(), if any.
        """
        if self._auto_accept_callback is not None:
            self._auto_accept_callback(button.get_active())

    def _on_diffs_toggled(self, button: Gtk.ToggleButton) -> None:
        """
        Handler for the Diffs toggle's 'toggled' signal. Fires when the user
        clicks the Diffs toggle OR when update_auto_accept_prefs() does a
        programmatic set_active() with a value change (Bug F regression —
        GTK 4.14 emits 'toggled' on every state change, contrary to the
        GTK 3 documented behavior). The _syncing_toolbar guard short-
        circuits the second case so the warning dialog is only shown on
        real user clicks. Forwards the new active state to the callback
        installed via set_diffs_toggle_callback(), if any. (Phase 5)
        """
        if self._syncing_toolbar:
            return
        if self._diffs_toggle_callback is not None:
            self._diffs_toggle_callback(button.get_active())

    def _on_files_toggled(self, button: Gtk.ToggleButton) -> None:
        """
        Handler for the Files toggle's 'toggled' signal. Fires when the user
        clicks the Files toggle OR when update_auto_accept_prefs() does a
        programmatic set_active() with a value change (Bug F regression —
        see _on_diffs_toggled for the full diagnosis). The _syncing_toolbar
        guard short-circuits the second case. Forwards the new active state
        to the callback installed via set_files_toggle_callback(), if any.
        (Phase 5)
        """
        if self._syncing_toolbar:
            return
        if self._files_toggle_callback is not None:
            self._files_toggle_callback(button.get_active())

    def _on_exec_clicked(self, button: Gtk.ToggleButton) -> None:
        """Handler for the Exec toggle's 'clicked' signal. (Phase 5)

        3-state cycle: OFF → SHOW → SILENT → OFF. Updates self._exec_mode
        to the new state, mirrors the new state into the widget label (so
        the user sees the cycle), and forwards the new mode string to the
        callback installed via set_exec_toggle_callback(), if any.

        Implementation note: this is a 'clicked' handler (not 'toggled').
        The Exec widget IS a `Gtk.ToggleButton` (see __init__ at line 120),
        but we keep its state in self._exec_mode and cycle manually because
        Gtk.ToggleButton is binary — it cannot represent the third
        'silent' state natively. We deliberately bind 'clicked' rather than
        'toggled' so the handler fires exactly once per user click and
        never from programmatic state changes. Programmatic label changes
        in update_auto_accept_prefs() do NOT emit 'clicked', so this
        handler only fires on real user clicks.
        """
        if self._exec_mode == "off":
            new_mode = "show"
        elif self._exec_mode == "show":
            new_mode = "silent"
        else:
            new_mode = "off"
        self._exec_mode = new_mode
        self._exec_toggle.set_label(f"Exec: {new_mode.upper()}")
        if self._exec_toggle_callback is not None:
            self._exec_toggle_callback(new_mode)

    def _on_batch_button_clicked(self, button: Gtk.Button) -> None:
        """
        Handler for the toolbar batch button's 'clicked' signal. Fires when the
        user clicks the batch accept button. Forwards to the callback installed
        via set_batch_accept_callback(), if any. (Phase 5)
        """
        if self._batch_accept_callback is not None:
            self._batch_accept_callback()

    def _on_batch_accept_clicked(self) -> None:
        """
        Placeholder - overridden by FeedHandler when it wires the batch accept flow.
        The handler calls set_batch_accept_callback() to install the real handler. (Phase 5)
        """
        pass

    def set_batch_accept_callback(self, callback: Callable[[], None]) -> None:
        """
        Install the real batch accept callback. Called by FeedHandler after construction. (Phase 5)

        Stores the callback both on self._batch_accept_callback (used by the new
        _on_batch_button_clicked toolbar handler) AND on self._on_batch_accept_clicked
        (legacy placeholder; kept for backward compatibility with any external code
        that still references the old attribute).
        """
        self._batch_accept_callback = callback
        self._on_batch_accept_clicked = callback

    def hide_card_buttons(self, card_id: str, button_names: list[str]) -> None:
        """Compat no-op: the document has no per-card widget to mutate.

        Used by FeedHandler._auto_approve_exec_card() in Show mode to stop a
        double action. On the HTML surface the card re-renders through
        `update_card` / `upsert_card`, which draws its own approved/rejected
        state — there is no button widget to hide and no `_cards_by_id` map.
        Kept so the legacy caller (and MockFeedTab) does not raise.
        """
        del card_id, button_names

    def _find_buttons_by_css(
        self, widget: Gtk.Widget, css_classes: set[str]
    ) -> list[Gtk.Widget]:
        """Walk the widget subtree and return every Gtk.Button whose CSS
        classes intersect `css_classes`. Recursive via get_first_child +
        get_next_sibling (GTK 4 widget tree API; GTK 3's get_children is gone).
        Returns a flat list; duplicates are possible if a button somehow
        carries multiple target classes (callers handle idempotently).
        """
        matches: list[Gtk.Widget] = []
        if isinstance(widget, Gtk.Button):
            for cls in css_classes:
                if widget.has_css_class(cls):
                    matches.append(widget)
                    break  # only add once per button
        child = widget.get_first_child()
        while child is not None:
            matches.extend(self._find_buttons_by_css(child, css_classes))
            child = child.get_next_sibling()
        return matches
