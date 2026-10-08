# ui/views/feed_tab.py
# Feed tab container - pure view for the Projects notebook's "Feed" sub-tab.
# No business logic, no state mutations.
#
# Public API:
#   class FeedTab(Gtk.Box):
#       get_card_container() -> Gtk.Box
#       append_card(card_widget: Gtk.Widget, card_id: str | None) -> None
#       remove_card(card_id: str) -> None
#       schedule_smart_scroll_to_bottom() -> None
#       show_empty_state() -> None
#       get_vadjustment() -> Gtk.Adjustment | None
#       is_near_bottom(slack: int = 80) -> bool
#       is_above_viewport(widget) -> bool

import logging

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk
from typing import Callable

from utils.gtk_containers import is_in_container

logger = logging.getLogger(__name__)


class FeedTab(Gtk.Box):
    """
    Feed card list for the Projects notebook's "Feed" sub-tab.

    Layout:
      Gtk.Box (vertical)
        └── Gtk.ScrolledWindow
              └── Gtk.Box (vertical, card_container)

    CSS classes:
      .feed-scroll  - the scrolled window
      .feed-card-list - the vertical box holding cards
    """

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_vexpand(True)

        self._card_container: Gtk.Box | None = None
        self._feed_scroll: Gtk.ScrolledWindow | None = None
        self._cards_by_id: dict[str, Gtk.Widget] = {}  # card_id → widget
        self._empty_widget: Gtk.Widget | None = None
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
        # Deferred scroll-to-bottom. The changed handler stays connected until
        # the content height is stable; the timeout fires only if changed never does.
        self._scroll_handler_id: int | None = None
        self._scroll_timeout_id: int | None = None
        self._scroll_changed_seen: bool = False
        self._settle_source: int | None = None
        self._settle_upper: float | None = None
        self._settle_stable_count: int = 0
        # Reader intent across tab hide/show. True until the reader scrolls up.
        self._was_near_bottom: bool = True
        self._saved_value: float = 0.0

        # ── Build scrolled card list ────────────────────────────────────
        scroll = Gtk.ScrolledWindow()
        scroll.add_css_class("feed-scroll")
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.AUTOMATIC)
        scroll.set_halign(Gtk.Align.FILL)
        scroll.set_valign(Gtk.Align.FILL)
        self._feed_scroll = scroll

        card_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card_container.add_css_class("feed-card-list")
        card_container.set_spacing(8)
        card_container.set_valign(Gtk.Align.START)
        card_container.set_vexpand(True)
        self._card_container = card_container

        scroll.set_child(card_container)
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

        # Group 3 — snooze button. Hidden when count == 0; revealed by
        # update_auto_accept_prefs() when the prefs dict carries snoozed ids.
        self._snooze_button = Gtk.MenuButton(label="Snooze 0")
        self._snooze_button.add_css_class("feed-toolbar-snooze")
        self._snooze_button.set_visible(False)  # hidden until snooze count > 0

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
        self._toolbar.append(self._snooze_button)
        self._toolbar.append(self._batch_accept_button)  # existing batch accept button
        self._toolbar.append(self._batch_accept_label)   # existing batch accept label

        self.append(self._toolbar)

        # Track whether the reader is caught up. A hide/show can reset value
        # to 0; the map handler uses this instead of jumping to the bottom.
        vadj = scroll.get_vadjustment()
        if vadj is not None:
            vadj.connect("value-changed", self._on_scroll_value_changed)
        scroll.connect("map", self._on_scroll_mapped)

    # ── Public API ───────────────────────────────────────────────────────

    def get_card_container(self) -> Gtk.Box:
        """Return the vertical box that holds feed cards."""
        return self._card_container

    def show_empty_state(self) -> None:
        """Clear all cards and show the empty state widget."""
        from ui.views.feed_card import build_empty_feed_widget
        if self._card_container is None:
            return

        # Remove all card widgets, clearing CSS state first (Bug B fix)
        for card_id in list(self._cards_by_id.keys()):
            widget = self._cards_by_id[card_id]
            self._clear_widget_state_recursive(widget)
            if is_in_container(widget, self._card_container):
                self._card_container.remove(widget)
        self._cards_by_id.clear()
        # Show the empty state widget
        empty = build_empty_feed_widget()
        self._card_container.append(empty)
        self._empty_widget = empty

    def append_card(self, card_widget: Gtk.Widget, card_id: str | None = None) -> None:
        """
        Append a card widget to the bottom of the feed (chronological, newest last).
        If card_id is provided, the card can be removed via remove_card().
        Removes the empty state widget if present.
        """
        if self._card_container is None:
            return
        # Remove empty state widget if present
        if self._empty_widget is not None and is_in_container(self._empty_widget, self._card_container):
            self._card_container.remove(self._empty_widget)
            self._empty_widget = None
        # Append to bottom (newest card at bottom - social-media order)
        self._card_container.append(card_widget)
        if card_id is not None:
            self._cards_by_id[card_id] = card_widget

    def remove_card(self, card_id: str) -> None:
        """Remove a card widget by card_id.

        Clears CSS state flags (PRELIGHT/ACTIVE/SELECTED) on the card and
        all its children BEFORE unparenting. This prevents GTK4's
        'Broken accounting of active state for widget' warning that fires
        when a widget is removed while the cursor is over it or while a
        child button is in :active state (e.g. user mid-click on Accept).
        """
        if card_id not in self._cards_by_id:
            return
        widget = self._cards_by_id[card_id]
        # Clear CSS state on the card and all descendant widgets.
        # The 'Broken accounting' warning is about GtkStyleContext state
        # (PRELIGHT/ACTIVE/SELECTED), not focus. unset_state_flags is the
        # documented GTK4 API and is safe to call on widgets that never
        # had the flags set.
        self._clear_widget_state_recursive(widget)
        if self._card_container is not None and is_in_container(widget, self._card_container):
            self._card_container.remove(widget)
        del self._cards_by_id[card_id]      

    def prepend_card(self, card_widget: Gtk.Widget, card_id: str | None = None) -> None:
        """
        Prepend a card widget at the top of the feed (above all other cards).

        Used by lazy-load to insert older cards above the existing feed.
        If card_id is provided, the card can be removed via remove_card().
        """
        if self._card_container is None:
            return
        # Remove empty state widget if present
        if self._empty_widget is not None and is_in_container(self._empty_widget, self._card_container):
            self._card_container.remove(self._empty_widget)
            self._empty_widget = None
        # insert_child_after(child, None) prepends in GTK4
        self._card_container.insert_child_after(card_widget, None)
        if card_id is not None:
            self._cards_by_id[card_id] = card_widget

    def replace_card(self, card_id: str, new_widget: Gtk.Widget) -> None:
        """
        Replace an existing card widget with a new one at the same position.

        Used by FeedHandler.update_card() when a tool call card is updated
        with results - the widget is rebuilt and swapped in-place.
        """
        if card_id not in self._cards_by_id:
            return
        old_widget = self._cards_by_id[card_id]
        if self._card_container is None:
            return
        if not is_in_container(old_widget, self._card_container):
            return

        # Find the position of old_widget, then insert new_widget at the same spot
        children = list(self._card_container)
        try:
            idx = children.index(old_widget)
        except ValueError:
            return

        # Remove old widget - now children[0..idx-1] are what come before new_widget
        self._card_container.remove(old_widget)

        # predecessor is children[idx-1] if idx > 0, else None (insert at start)
        predecessor = children[idx - 1] if idx > 0 else None
        self._card_container.insert_child_after(new_widget, predecessor)
        self._cards_by_id[card_id] = new_widget

    def _clear_widget_state_recursive(self, widget: Gtk.Widget) -> None:
        """
        Recursively clear PRELIGHT/ACTIVE/SELECTED state flags on a widget
        and all its children. Called before removing widgets from the
        container to prevent GTK4's 'Broken accounting of active state'
        warning.
        """
        try:
            widget.unset_state_flags(
                Gtk.StateFlags.PRELIGHT | Gtk.StateFlags.ACTIVE | Gtk.StateFlags.SELECTED
            )
        except Exception:
            pass  # unset_state_flags is safe but we guard anyway
        child = widget.get_first_child()
        while child is not None:
            self._clear_widget_state_recursive(child)
            child = child.get_next_sibling()

    _BOTTOM_THRESHOLD = 80.0
    _SETTLE_STABLE_FRAMES = 2

    def _bottom_value(self, adj) -> float:
        """Last visible pixel. GTK clamps value to upper - page_size."""
        lower = adj.get_lower() if hasattr(adj, "get_lower") else 0.0
        return max(lower, adj.get_upper() - adj.get_page_size())

    def _distance_from_bottom(self, adj) -> float:
        return adj.get_upper() - adj.get_page_size() - adj.get_value()

    def _on_scroll_value_changed(self, adj) -> None:
        """Remember whether the reader is caught up.

        A non-scrollable adjustment (upper <= page_size) is a layout
        artifact, not the reader. Do not update the tracker from it.
        """
        if adj.get_upper() <= adj.get_page_size():
            return
        self._saved_value = adj.get_value()
        self._was_near_bottom = (
            self._distance_from_bottom(adj) <= self._BOTTOM_THRESHOLD
        )

    def _cancel_settle(self) -> None:
        if self._settle_source is None:
            return
        from gi.repository import GLib
        try:
            GLib.source_remove(self._settle_source)
        except Exception:  # noqa: BLE001 — source already fired: idempotent cleanup
            logger.debug("settle source already removed (idempotent cancel)")
        self._settle_source = None

    def _disarm_scroll(self, adj) -> None:
        """Drop the pending scroll after it has been consumed."""
        from gi.repository import GLib
        if self._scroll_timeout_id is not None:
            try:
                GLib.source_remove(self._scroll_timeout_id)
            except Exception:
                pass
            self._scroll_timeout_id = None
        if self._scroll_handler_id is not None:
            try:
                adj.disconnect(self._scroll_handler_id)
            except Exception:
                pass
            self._scroll_handler_id = None
        self._settle_stable_count = 0
        self._settle_upper = None

    def _arm_settle(self, adj) -> None:
        """Restart the stable-height count and ensure one idle is queued."""
        from gi.repository import GLib
        self._settle_upper = adj.get_upper()
        self._settle_stable_count = 0
        if self._settle_source is not None:
            return
        self._settle_source = GLib.idle_add(self._settle_scroll)

    def _settle_scroll(self) -> bool:
        """Idle: scroll only after the content height stops changing."""
        from gi.repository import GLib
        self._settle_source = None
        if self._scroll_handler_id is None or self._feed_scroll is None:
            return GLib.SOURCE_REMOVE
        adj = self._feed_scroll.get_vadjustment()
        if adj is None:
            return GLib.SOURCE_REMOVE
        current = adj.get_upper()
        if current != self._settle_upper:
            self._settle_upper = current
            self._settle_stable_count = 0
            self._settle_source = GLib.idle_add(self._settle_scroll)
            return GLib.SOURCE_REMOVE
        if adj.get_upper() <= adj.get_page_size():
            return GLib.SOURCE_REMOVE  # collapsed — leave the request pending
        self._settle_stable_count += 1
        if self._settle_stable_count < self._SETTLE_STABLE_FRAMES:
            self._settle_source = GLib.idle_add(self._settle_scroll)
            return GLib.SOURCE_REMOVE
        adj.set_value(self._bottom_value(adj))
        self._disarm_scroll(adj)
        return GLib.SOURCE_REMOVE

    def _on_adj_changed(self, adj) -> bool:
        """Layout moved. Do not scroll yet — wait until upper is stable."""
        self._scroll_changed_seen = True
        if adj.get_upper() <= adj.get_page_size():
            return False  # nothing to scroll yet; keep the request
        self._arm_settle(adj)
        return False

    def schedule_scroll_to_bottom(self) -> None:
        """
        Scroll to the bottom after the content height settles.

        GTK4 does not recompute vadjustment.upper synchronously after
        append. The first `changed` is often an intermediate height.
        Scrolling there and disconnecting leaves the newest card below
        the fold. This waits for two idle frames at the same upper, then
        sets value to upper - page_size.

        The 150ms timeout scrolls only when `changed` never fires.
        """
        if self._feed_scroll is None:
            return
        vadj = self._feed_scroll.get_vadjustment()
        if vadj is None:
            return

        from gi.repository import GLib

        if self._scroll_handler_id is not None:
            try:
                vadj.disconnect(self._scroll_handler_id)
            except Exception:
                pass
            self._scroll_handler_id = None

        if self._scroll_timeout_id is not None:
            try:
                GLib.source_remove(self._scroll_timeout_id)
            except Exception:
                pass
            self._scroll_timeout_id = None

        self._cancel_settle()
        self._scroll_changed_seen = False
        self._settle_stable_count = 0
        self._settle_upper = None

        self._scroll_handler_id = vadj.connect("changed", self._on_adj_changed)

        def _timeout_fallback():
            self._scroll_timeout_id = None
            # A changed signal owns the settle. Do not scroll and do not
            # disconnect that settle from here.
            if self._scroll_changed_seen:
                return GLib.SOURCE_REMOVE
            if self._scroll_handler_id is not None:
                vadj.set_value(self._bottom_value(vadj))
                try:
                    vadj.disconnect(self._scroll_handler_id)
                except Exception:
                    pass
                self._scroll_handler_id = None
            return GLib.SOURCE_REMOVE

        self._scroll_timeout_id = GLib.timeout_add(150, _timeout_fallback)

    def schedule_smart_scroll_to_bottom(self) -> None:
        """
        Scroll to the bottom if the reader is still following the feed.

        Follow state is the reader's recorded intent (`_was_near_bottom`,
        maintained by `_on_scroll_value_changed` on every value change), NOT a
        live `upper - page_size - value` reading.

        A live reading is wrong here because content can grow after a scroll
        has already completed — a line wrapping, a card body arriving late —
        which raises `upper` without the reader having moved anywhere. The
        reading then reports "the reader scrolled away" and following stops
        permanently: each later card lands below the fold with nothing left to
        catch up. (Reported as: view stuck short of the bottom, each new card
        leaving it further behind.)

        Reading intent instead keeps following until the reader actually
        scrolls away, which is the documented contract for this method.
        """
        if self._feed_scroll is None:
            return
        vadj = self._feed_scroll.get_vadjustment()
        if vadj is None:
            return
        if not self._was_near_bottom:
            return
        self.schedule_scroll_to_bottom()

    def _on_scroll_mapped(self, scroll_window):
        """
        Feed tab became visible.

        GTK may reset value to 0 while the tab is hidden. If the reader
        was caught up, settle on the newest card. If they were reading
        higher up, put the viewport back at the saved offset.
        """
        if self._was_near_bottom:
            self.schedule_scroll_to_bottom()
            return False
        if self._feed_scroll is None:
            return False
        vadj = self._feed_scroll.get_vadjustment()
        if vadj is None:
            return False
        # BUG#2 (audit fix): a settle armed BEFORE the tab was hidden (project-
        # open smart scroll, eviction, or an earlier append) may still be
        # pending. Restoring `_saved_value` alone is not enough — the stale
        # settle later fires and yanks the reader to the bottom. Cancel it and
        # disarm the changed handler BEFORE the restore. (The near-bottom branch
        # is clean: schedule_scroll_to_bottom cancels prior settles internally.)
        self._cancel_settle()
        self._disarm_scroll(vadj)
        lower = vadj.get_lower() if hasattr(vadj, "get_lower") else 0.0
        restored = min(max(self._saved_value, lower), self._bottom_value(vadj))
        vadj.set_value(restored)
        return False

    def get_vadjustment(self):
        """The feed ScrolledWindow's vadjustment, or None when _feed_scroll is absent.

        A constructed tab owns a real ScrolledWindow, so this normally returns
        a real (zeroed, pre-layout) Adjustment; None requires an explicitly
        absent/nulled _feed_scroll. (P2 audit; SPEC-MEMORY-WIDGET-RATCHET
        §2.2 — the earlier "None before first map" wording was wrong.)
        """
        return self._feed_scroll.get_vadjustment() if self._feed_scroll else None

    def is_near_bottom(self, slack: int = 80) -> bool:
        """True when the viewport is within `slack` px of the feed bottom."""
        vadj = self.get_vadjustment()
        if vadj is None:
            return True                       # nothing rendered: eviction is safe
        return (vadj.get_upper() - vadj.get_page_size() - vadj.get_value()) <= slack

    def is_above_viewport(self, widget) -> bool:
        """True when `widget` lies entirely above the visible region.

        FAIL-SAFE. A widget that has never been allocated reports
        `compute_bounds -> (True, rect)` with a ZERO rect (verified on GTK 4.14
        under Xvfb: an unrealized/unmapped Gtk.Label in a Gtk.Box inside a
        Gtk.ScrolledWindow gives ok=True, origin.y=0.0, size.height=0.0). A naive
        `origin.y + height <= value` test therefore returns True — "above the
        viewport" — for every unmeasurable widget and would DESTROY cards that
        are merely not laid out yet (e.g. while the Feed sub-tab is unmapped).
        So a non-positive extent means "unknown", never "above".
        """
        vadj = self.get_vadjustment()
        if vadj is None or self._card_container is None:
            return False
        try:
            ok, rect = widget.compute_bounds(self._card_container)
        except (AttributeError, TypeError):
            return False                  # mock widgets have no compute_bounds
        if not ok or rect.size.height <= 0:
            return False                  # geometry unavailable → never evict
        return (rect.origin.y + rect.size.height) <= vadj.get_value()

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

            # Snooze count
            snoozed = auto.get("snoozed_card_ids", [])
            count = len(snoozed)
            self._snooze_button.set_label(f"Snooze {count}")
            self._snooze_button.set_visible(count > 0)
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
        """Hide specific action buttons on a card widget. (Phase 5)

        Used by FeedHandler._auto_approve_exec_card() in Show mode to hide
        the Approve/Deny buttons on cards that were auto-approved.

        Bug #13 (Phase 7 follow-up): the original implementation looked up
        `_<name>_button` attributes on the card widget, but `build_feed_card`
        never stored those (it constructs local `btn_accept` / `btn_reject`
        variables and appends them inline). The result was a silent no-op:
        buttons stayed visible after auto-approve and the user could click
        Approve/Deny again, triggering a double-action in ARTH.

        Fix: map each button name to its CSS class and hide matching
        buttons in the card widget subtree.

        Args:
            card_id: The card whose buttons should be hidden. If the card
                is not currently in self._cards_by_id (e.g. user navigated
                away from it), the method is a no-op.
            button_names: List of button names to hide. Supported names:
                "approve" | "deny" | "accept" | "reject" | "review".
                Each is mapped to the CSS class set by `build_feed_card()`
                on the corresponding button. Unknown names are skipped.

        Idempotent: calling twice with the same args has the same effect as
        calling once.
        """
        card_widget = self._cards_by_id.get(card_id)
        if card_widget is None:
            return
        # Map hide-card-buttons names to CSS classes used in build_feed_card().
        # Source of truth: ui/views/feed_card.py:431,438,443.
        name_to_css = {
            "approve": "feed-btn-accept",
            "deny": "feed-btn-reject",
            "accept": "feed-btn-accept",
            "reject": "feed-btn-reject",
            "review": "feed-btn-review",
        }
        target_classes = {
            name_to_css[n] for n in button_names if n in name_to_css
        }
        if not target_classes:
            return
        for btn in self._find_buttons_by_css(card_widget, target_classes):
            btn.set_visible(False)

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
