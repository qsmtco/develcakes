"""Context-panel snapshot build and apply."""
from __future__ import annotations

import time
from typing import Callable

from models.feed_card import FeedCardData

from ui.feed._facade import _mod


class FeedSnapshotMixin:
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
        self._snapshot_builder = _mod().threading.Thread(
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
                    _mod()._logger.exception(
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
        if _mod().conversation_store.snapshot_exceeds_size_limit(snapshot):
            _mod()._logger.warning(
                "Snapshot for card %s exceeds %dKB — rendered in-memory but not persisted",
                card.card_id, _mod().conversation_store.MAX_SNAPSHOT_SIZE_KB,
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
                        _mod().conversation_store.snapshot_from_messages(
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
                        _mod().conversation_store.snapshot_from_git_diff(pp, fp),
                )

