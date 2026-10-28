# tests/test_feed_snapshot_off_thread.py
# SPEC-UI-RESPONSIVENESS-2 §2.5 Phase 5 — git/snapshot work off the main thread.
#
# Edits under test:
#   A. on_filesystem_event() no longer calls snapshot_from_git_diff() inline;
#      the card is created with conversation_snapshot=None.
#   B. the pure snapshot construction runs on the "crabcakes-snapshot-builder"
#      thread; only the widget update is dispatched back via GLib.idle_add.
#      (The GTK-bound chat-box walk stays on the main thread.)
#   C. multiple filesystem events for the same (project_path, file_path) on
#      one tick build the diff ONCE.
#
# GLib double: dispatches are RECORDED and run only when the test flushes them
# (the handler's own contract says idle_add defers to the next main-loop cycle,
# so running them inline would fire _finalize_snapshot before add_card has
# stored the card). _pump() alternates flushing + polling so a dispatch
# produced by the background worker is picked up too.
#
# Hermeticity: every project path is a tmp_path; the pure git call is patched
# (no real git subprocess) and the real conversation store is never touched.

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

from models.conversation_snapshot import ConversationSnapshot
from models.feed_card import FeedCardData

BUILDER_THREAD_NAME = "crabcakes-snapshot-builder"


class _QueueingGLib:
    """GLib double: records main-thread dispatches; runs them on flush()."""

    def __init__(self):
        self.idle_calls = []   # (fn, args, thread_name)

    def idle_add(self, fn, *args, **kwargs):
        self.idle_calls.append((fn, args, threading.current_thread().name))
        return 1

    def flush_idle(self) -> int:
        """Run every recorded idle callback in FIFO order. Returns the count."""
        calls, self.idle_calls = self.idle_calls, []
        for fn, args, _thread in calls:
            fn(*args)
        return len(calls)

    def names(self) -> list[str]:
        return [getattr(fn, "__name__", repr(fn)) for fn, _a, _t in self.idle_calls]


def _pump(glib, predicate, timeout=3.0) -> bool:
    """Flush pending dispatches while polling `predicate` (worker-aware)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        glib.flush_idle()
        if predicate():
            return True
        time.sleep(0.01)
    return False


class _StubFeedTab:
    """Minimal FeedTab double.

    The real FeedTab exposes many setter/wiring methods; unknown attributes
    resolve to no-ops so the handler's wiring calls are harmless.
    """

    def __init__(self):
        self.cards = []
        self._vadjustment = None
        # MEMRATCHET P3 hygiene (audit follow-up): explicit eviction-pass
        # surface instead of relying on __getattr__ no-ops, so eviction is
        # testable here rather than silently skipped.
        self._near_bottom = True
        self._above_viewport = True
        self._card_spacing = 8

    def is_near_bottom(self, slack: int = 80) -> bool:
        return self._near_bottom

    def is_above_viewport(self, widget) -> bool:
        return self._above_viewport

    def get_vadjustment(self):
        return self._vadjustment

    def get_card_container(self):
        return self

    def get_spacing(self) -> int:
        return self._card_spacing

    def append_card(self, widget, card_id=None):
        self.cards.append((widget, card_id))

    def prepend_card(self, widget, card_id=None):
        self.cards.insert(0, (widget, card_id))

    def remove_card(self, card_id):
        return None

    def __getattr__(self, name):
        def _noop(*_args, **_kwargs):
            return None
        return _noop


def _make_handler(glib, project_path):
    from ui.handlers.feed_handler import FeedHandler
    handler = FeedHandler(GLib=glib, on_send_to_agent=lambda *a, **k: None)
    handler.set_feed_tab(_StubFeedTab())
    handler._project_paths["proj"] = str(project_path)
    return handler


def _file_card(file_path="src/foo.py", project_name="proj"):
    return FeedCardData(
        card_type="file_modified",
        source="crabwatch",
        title=f"Modified {file_path}",
        body=f"✏️ {file_path}",
        author="system",
        timestamp=datetime.now(UTC),
        project_name=project_name,
        file_path=file_path,
    )


def _diff_snapshot(text="--- a\n+++ b\n+x\n"):
    return ConversationSnapshot(snapshot_type="diff", diff_text=text)


def _patch_diff(monkeypatch, fn):
    """Patch the module attribute the handler resolves at call time."""
    monkeypatch.setattr("utils.conversation_store.snapshot_from_git_diff", fn)


class TestDiffBuildIsDeferred:
    def test_filesystem_event_does_not_build_the_diff_inline(self, monkeypatch, tmp_path):
        """Edit A: the card is created with no snapshot; nothing is built yet."""
        glib = _QueueingGLib()
        handler = _make_handler(glib, tmp_path)
        calls = []
        _patch_diff(monkeypatch, lambda pp, fp: calls.append((pp, fp)) or _diff_snapshot())

        seen = []
        real_add = handler.add_card

        def spy_add(card_data, persist=True):
            seen.append(card_data.conversation_snapshot)
            return real_add(card_data, persist=persist)

        handler.add_card = spy_add
        handler.on_filesystem_event(_file_card())

        assert seen == [None], (
            "on_filesystem_event must create the card WITHOUT an inline git "
            f"diff snapshot (got {seen!r})"
        )
        assert calls == [], (
            "no git diff may be built on the caller thread during "
            f"on_filesystem_event; got {calls!r}"
        )
        handler.shutdown_persist_writer()

    def test_main_thread_never_waits_for_the_diff_build(self, monkeypatch, tmp_path):
        """Edit B: the pure build leaves the main thread; the card fills later."""
        glib = _QueueingGLib()
        handler = _make_handler(glib, tmp_path)
        started = threading.Event()
        release = threading.Event()
        built = {}

        def slow_diff(project_path, file_path):
            built["thread"] = threading.current_thread().name
            built["args"] = (project_path, file_path)
            started.set()
            release.wait(timeout=5)
            return _diff_snapshot()

        _patch_diff(monkeypatch, slow_diff)

        card = _file_card()
        t0 = time.perf_counter()
        handler.on_filesystem_event(card)
        glib.flush_idle()      # main-thread work only: the deferred finalize
        elapsed = time.perf_counter() - t0

        assert elapsed < 0.5, (
            f"the main thread spent {elapsed:.2f}s on a filesystem event — the "
            "git diff must not be built there"
        )
        assert started.wait(timeout=2), "the snapshot builder should have picked up the job"
        assert built["thread"] == BUILDER_THREAD_NAME, (
            f"build ran on {built['thread']!r}, expected {BUILDER_THREAD_NAME!r}"
        )
        assert built["args"] == (str(tmp_path), "src/foo.py")
        assert card.conversation_snapshot is None, "still building — nothing applied"

        release.set()
        assert _pump(glib, lambda: card.conversation_snapshot is not None), (
            "the built snapshot must be applied to the card"
        )
        assert card.conversation_snapshot.diff_text.endswith("+x\n")
        handler.shutdown_persist_writer()

    def test_panel_update_is_dispatched_to_the_main_thread(self, monkeypatch, tmp_path):
        """Edit B: `_apply_snapshot` arrives as a main-thread idle callback."""
        glib = _QueueingGLib()
        handler = _make_handler(glib, tmp_path)
        built = {}

        def fake_diff(project_path, file_path):
            built["thread"] = threading.current_thread().name
            return _diff_snapshot("+only-line\n")

        _patch_diff(monkeypatch, fake_diff)

        card = _file_card()
        handler.on_filesystem_event(card)
        glib.flush_idle()   # _finalize_snapshot / _append — main thread

        # Wait for the WORKER to publish the build, and for its idle_add
        # dispatch to be recorded — without flushing, so the recorded dispatch
        # can be inspected (flushing would consume it).
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not handler._snapshot_cache:
            time.sleep(0.01)
        assert handler._snapshot_cache, "worker never built the diff"
        assert built["thread"] == BUILDER_THREAD_NAME

        def _apply_dispatches():
            return [
                (fn, thread) for fn, _a, thread in glib.idle_calls
                if getattr(fn, "__name__", "") == "_apply_snapshot"
            ]

        while time.monotonic() < deadline and not _apply_dispatches():
            time.sleep(0.01)

        apply_dispatches = _apply_dispatches()
        assert apply_dispatches, (
            f"the widget update must be dispatched via idle_add; got {glib.names()}"
        )
        assert all(thread == BUILDER_THREAD_NAME for _fn, thread in apply_dispatches), (
            "the widget update must be dispatched FROM the builder thread"
        )

        glib.flush_idle()   # runs _apply_snapshot on the test's (main) thread

        assert card.conversation_snapshot is not None
        # There is no per-card GTK widget any more (WU feed-surface chunk 3b):
        # `_apply_snapshot` attaches the snapshot to the CARD DATA and the
        # document re-renders from it. The equivalent assertion is that the
        # card carries the diff and the document surfaces carry it, i.e. the
        # handler did not route the update through a deleted widget path.
        assert "only-line" in card.conversation_snapshot.diff_text
        assert not hasattr(handler, "_card_widgets"), (
            "the HTML feed path must not carry a per-card GTK widget map"
        )
        handler.shutdown_persist_writer()

    def test_existing_snapshot_is_never_rebuilt(self, monkeypatch, tmp_path):
        """Edit A defensive guard: a card that already carries a snapshot is left alone."""
        glib = _QueueingGLib()
        handler = _make_handler(glib, tmp_path)
        calls = []
        _patch_diff(monkeypatch, lambda pp, fp: calls.append((pp, fp)) or _diff_snapshot())

        card = _file_card()
        preset = _diff_snapshot("+preset\n")
        card.conversation_snapshot = preset

        handler._maybe_create_snapshot(card)

        assert calls == [], "a card that already carries a snapshot must not rebuild it"
        assert card.conversation_snapshot is preset
        handler.shutdown_persist_writer()


class TestSameTickCoalescing:
    def test_two_events_for_one_path_build_the_diff_once(self, monkeypatch, tmp_path):
        """Edit C: two same-tick events for one path → ONE build, both cards filled."""
        glib = _QueueingGLib()
        handler = _make_handler(glib, tmp_path)
        calls = []
        _patch_diff(monkeypatch, lambda pp, fp: calls.append((pp, fp)) or _diff_snapshot())

        first = _file_card()
        second = _file_card()
        handler.on_filesystem_event(first)
        handler.on_filesystem_event(second)

        assert _pump(glib, lambda: first.conversation_snapshot is not None
                     and second.conversation_snapshot is not None), (
            "both cards must receive the (single) built snapshot"
        )
        assert len(calls) == 1, f"expected ONE git diff build, got {len(calls)}: {calls}"
        handler.shutdown_persist_writer()

    def test_different_paths_still_build_separately(self, monkeypatch, tmp_path):
        """Coalescing is per (project_path, file_path) — not global."""
        glib = _QueueingGLib()
        handler = _make_handler(glib, tmp_path)
        calls = []
        _patch_diff(monkeypatch, lambda pp, fp: calls.append(fp) or _diff_snapshot())

        a = _file_card("src/a.py")
        b = _file_card("src/b.py")
        handler.on_filesystem_event(a)
        handler.on_filesystem_event(b)

        assert _pump(glib, lambda: a.conversation_snapshot is not None
                     and b.conversation_snapshot is not None)
        assert sorted(calls) == ["src/a.py", "src/b.py"]
        handler.shutdown_persist_writer()


class TestBuilderLifecycle:
    def test_failing_build_is_logged_and_thread_survives(self, monkeypatch, tmp_path, caplog):
        """Rule 7: a raising builder must not kill the worker thread."""
        import logging
        glib = _QueueingGLib()
        handler = _make_handler(glib, tmp_path)
        attempts = []

        def flaky_diff(project_path, file_path):
            attempts.append(file_path)
            if file_path == "src/a.py":
                raise RuntimeError("git exploded")
            return _diff_snapshot()

        _patch_diff(monkeypatch, flaky_diff)

        first = _file_card("src/a.py")
        with caplog.at_level(logging.ERROR, logger="ui.handlers.feed_handler"):
            handler.on_filesystem_event(first)
            assert _pump(glib, lambda: bool(attempts))

        assert any("snapshot builder failed" in r.getMessage() for r in caplog.records), (
            [r.getMessage() for r in caplog.records]
        )
        assert first.conversation_snapshot is None, "a failed build must attach nothing"

        # The thread is still alive and the next build succeeds.
        thread = handler._snapshot_builder
        assert thread is not None and thread.is_alive()
        second = _file_card("src/b.py")
        handler.on_filesystem_event(second)
        assert _pump(glib, lambda: second.conversation_snapshot is not None)
        handler.shutdown_persist_writer()

    def test_shutdown_stops_the_snapshot_builder(self, monkeypatch, tmp_path):
        """shutdown_persist_writer() owns both background workers."""
        glib = _QueueingGLib()
        handler = _make_handler(glib, tmp_path)
        _patch_diff(monkeypatch, lambda pp, fp: _diff_snapshot())

        handler.on_filesystem_event(_file_card())
        assert _pump(glib, lambda: handler._snapshot_builder is not None
                     and handler._snapshot_builder.is_alive())

        handler.shutdown_persist_writer()

        assert handler._snapshot_builder is None
        assert handler._snapshot_stop is True
