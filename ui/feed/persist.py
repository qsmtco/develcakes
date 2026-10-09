"""Background persist writer for the project feed."""
from __future__ import annotations

import os

from ui.feed._facade import _mod


class FeedPersistMixin:
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
        self._persist_writer = _mod().threading.Thread(
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
                _mod()._logger.warning(
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
                pruned = _mod().feed_store.compact_feed(
                    path, window=_mod().feed_store.FEED_WINDOW_DEFAULT
                )
                if pruned:
                    self._surface_prune_card(
                        path, pruned, _mod().feed_store.FEED_WINDOW_DEFAULT
                    )
            except Exception:  # noqa: BLE001 — writer thread must never die
                _mod()._logger.exception("persist: compact task failed (%r)", compact_task)
                if self._persist_stop:
                    _mod()._logger.error(
                        "persist: dropping failed compact during shutdown (%r)",
                        compact_task,
                    )
                elif tries + 1 >= 3:
                    _mod()._logger.error(
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
                ok = _mod().feed_store.update_feed_card(project_path, card_id, payload)
                if ok is False:
                    raise RuntimeError(
                        "update_feed_card returned False (append+legacy failed)"
                    )
                if ok is None:
                    # Legacy path: card gone (pruned between enqueue and
                    # drain). Retrying is futile — log INFO and let the
                    # removal below drop it (symmetric with the queue phase).
                    _mod()._logger.info(
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
                _mod()._logger.exception("persist: deferred update failed (%r)", key)
                with self._persist_queue_lock:
                    cur = self._persist_deferred.get(key)
                    if cur is None:
                        # a fresh enqueue superseded us — drop the stale retry
                        pass
                    elif cur[0] is not payload:
                        # a newer failure already replaced us — leave it
                        pass
                    elif self._persist_stop:
                        _mod()._logger.error(
                            "persist: dropping deferred update during shutdown (%r)",
                            key,
                        )
                        del self._persist_deferred[key]
                    elif tries + 1 >= 3:
                        _mod()._logger.error(
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
                _mod()._logger.warning(
                    "persist: dropping malformed queue entry for %r", card_id
                )
                continue
            try:
                ok = _mod().feed_store.update_feed_card(project_path, card_id, updates)
                if ok is False:
                    raise RuntimeError(
                        "update_feed_card returned False (append+legacy failed)"
                    )
                if ok is None:
                    # Legacy path: card gone (pruned between enqueue and
                    # drain). Retrying is futile — drop with INFO.
                    _mod()._logger.info(
                        "persist: card %s no longer exists in %s; "
                        "update dropped (pruned?)", card_id, project_path,
                    )
                    continue
            except Exception:  # noqa: BLE001 — writer thread must never die
                _mod()._logger.exception("persist: update task failed (%r)", task)
                if self._persist_stop:
                    _mod()._logger.error(
                        "persist: dropping failed update during shutdown (%r)",
                        key,
                    )
                else:
                    # first failure of THIS payload: fresh budget (any prior
                    # deferred entry was popped by the enqueue that queued it)
                    with self._persist_queue_lock:
                        self._persist_deferred[key] = (updates, 1)

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
            _mod()._logger.warning(
                "persist shutdown: %d entries enqueued after shutdown began "
                "(stragglers — drained by the writer or next generation)",
                leftover - queued_at_stop,
            )
        if leftover > 0:
            _mod()._logger.error(
                "persist writer stopped with %d undrained entries "
                "(in-flight write exceeded join timeout)", leftover,
            )
        elif self._persist_writer is not None and self._persist_writer.is_alive():
            _mod()._logger.warning(
                "persist writer still draining at join timeout; queue is empty "
                "— exit not observed, but entries were drained",
            )
        # Phase 5: the snapshot builder is the second background worker owned
        # by this handler — stop it here so the handler's single shutdown
        # entry point leaves no thread running.
        self.shutdown_snapshot_builder()

