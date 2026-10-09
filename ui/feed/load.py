"""Project open/close, load-more, and live-widget eviction."""
from __future__ import annotations


from models.feed_card import AutoAcceptPrefs
from ui.views.feed_card import build_feed_card

from ui.feed._facade import _mod


class FeedLoadMixin:
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
            cards = _mod().feed_store.load_feed(project_path)

            # §2.3.4 one-time large-feed compaction: a legacy feed bigger than
            # the window's soft bound gets compacted once, on open. Accepted
            # cost (audit r1 #6): the first tool results after a legacy-feed
            # open may wait behind the compaction. `load_feed` itself never
            # compacts — this is the only open-time trigger.
            if len(cards) > _mod().feed_store.FEED_WINDOW_DEFAULT * 1.25:
                _mod()._logger.info(
                    "on_project_opened: feed for %s has %d cards (> %s) — "
                    "requesting a one-time compaction",
                    project_name, len(cards),
                    int(_mod().feed_store.FEED_WINDOW_DEFAULT * 1.25),
                )
                self._enqueue_compaction(project_path)

            # Phase 5 + v2: load auto-accept prefs (separate file from feed.json).
            # Phase 2 of utils/feed_store guarantees load_feed_prefs returns
            # a v2-shaped dict (v1 files are migrated in-memory).
            prefs_raw = _mod().feed_store.load_feed_prefs(project_path)
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

        t = _mod().threading.Thread(target=_load_and_render, daemon=True)
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

        R1: effective cap = min(_mod().MAX_LIVE_CARD_WIDGETS, get_live_window())
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
        _mod().MAX_LIVE_CARD_WIDGETS (pre-SP2 behavior): config is non-critical
        and must never break the append/evict path.
        """
        try:
            project_path = self._project_paths.get(self._active_project_name or "")
            if not project_path:
                return _mod().MAX_LIVE_CARD_WIDGETS  # R3/R5: no active project
            configured = _mod().feed_store.get_live_window(project_path)
            return min(_mod().MAX_LIVE_CARD_WIDGETS, configured)  # R1: inside try —
            # a garbage return raises TypeError at the clamp (077bdd64 latent
            # bug, caught by SPEC-04's gate); guarded here like any other
            # accessor failure.
        except Exception as e:  # noqa: BLE001 — R5: config is non-critical
            _mod()._logger.warning(
                "eviction cap: live-window read failed; using %d: %s",
                _mod().MAX_LIVE_CARD_WIDGETS, e,
            )
            return _mod().MAX_LIVE_CARD_WIDGETS

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
            if len(live) <= _mod().KEEP_NEWEST_CARDS:
                return
            live.sort(key=lambda cid: self._cards[cid].seq_num or 0)  # oldest first
            victims = live[: len(live) - _mod().KEEP_NEWEST_CARDS]
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

