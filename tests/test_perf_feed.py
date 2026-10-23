"""R-7 perf row — feed card update / load time bounds (SPEC-47 §2.6).

Bounds (from the 2026-09-11 P0.1 measurements — the 0.62 s main-thread
stall class being the regression this row exists to catch):

  * ``feed_handler.update_card()`` with a large feed      < 50 ms   (was ~620 ms)
  * ``feed_store.load_feed()`` at FEED_WINDOW_DEFAULT    < 100 ms  (was 0.24 s)
  * ``append_card_update()`` cost vs. an empty feed      within 3× (O(1) invariant)
  * main-thread stall while a card update is enqueued    < 5 ms

The test is marked ``slow`` (pytest.ini) so it is excluded from fast suites.

Import note: FeedHandler is imported dynamically via importlib rather than at
module top. tests/conftest.py's GTK guard now detects dynamic ui imports too,
so this module opts out with the top-level ``GTK_HEADLESS_SAFE = True``
marker: the benchmarks build no widgets (feed_handler is driven with a stub
GLib), so the suite runs headless — and R-7 runs under the plain runner,
where a skip is a failure.
"""

from __future__ import annotations

GTK_HEADLESS_SAFE = True

import importlib
import os
import statistics
import time
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from models.feed_card import FeedCardData
from utils import feed_store

pytestmark = pytest.mark.slow

FEED_SCALE = feed_store.FEED_WINDOW_DEFAULT  # 2000
_UPDATE_CARD_BOUND_MS = 50.0
_LOAD_FEED_BOUND_MS = 100.0
_ENQUEUE_STALL_BOUND_MS = 5.0
_APPEND_RATIO_BOUND = 3.0


class _StubGLib:
    """Minimal GLib stand-in: update_card's idle_add path is never hit with
    no feed tab attached, but the handler's constructor requires a GLib."""

    def idle_add(self, fn, *args, **kwargs):  # pragma: no cover - stub
        return 0


def _feed_handler_cls():
    return importlib.import_module("ui.handlers.feed_handler").FeedHandler


def _make_card(i: int) -> FeedCardData:
    return FeedCardData(
        card_type="diff",
        source="agent",
        title=f"perf card {i}",
        body="b" * 96,
        author="perf",
        timestamp=datetime.now(UTC),
        project_name="perfproj",
    )


def _median_ms(fn, n: int = 7) -> float:
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return statistics.median(samples)


def _handler_with_large_feed(project_path: str):
    fh = _feed_handler_cls()(GLib=_StubGLib(), on_send_to_agent=lambda *a: None)
    fh._project_paths["perfproj"] = project_path
    proj_cards = fh._project_cards.setdefault("perfproj", [])
    for i in range(FEED_SCALE):
        card = _make_card(i)
        card.card_id = f"card-{i}"
        fh._cards[card.card_id] = card
        proj_cards.append(card.card_id)
    return fh


def test_update_card_large_feed_below_50ms(tmp_path):
    """feed_handler.update_card() at FEED_WINDOW_DEFAULT scale stays < 50 ms.

    Measures the main-thread portion (in-memory replace + enqueue). The
    R-7 mutation delays exactly this path, so the bound must blow under it.
    """
    fh = _handler_with_large_feed(str(tmp_path))
    target = fh._cards["card-0"]
    updated = replace(target, body="updated body")

    fh.update_card(target.card_id, updated)  # warm-up: start the writer
    time.sleep(0.05)

    best = _median_ms(lambda: fh.update_card(target.card_id, updated), n=7)
    print(f"[perf] update_card large feed: median {best:.3f} ms")
    assert best < _UPDATE_CARD_BOUND_MS, (
        f"update_card took {best:.1f} ms (bound {_UPDATE_CARD_BOUND_MS} ms)"
    )


def test_load_feed_at_window_default_below_100ms(tmp_path):
    """feed_store.load_feed() at FEED_WINDOW_DEFAULT (2000) stays < 100 ms."""
    project_path = str(tmp_path)
    cards = [_make_card(i) for i in range(FEED_SCALE)]
    feed_store.save_feed(project_path, cards)  # setup — untimed

    feed_store.load_feed(project_path)  # warm read

    best = _median_ms(lambda: feed_store.load_feed(project_path), n=7)
    print(f"[perf] load_feed({FEED_SCALE}): median {best:.3f} ms")
    assert best < _LOAD_FEED_BOUND_MS, (
        f"load_feed took {best:.1f} ms (bound {_LOAD_FEED_BOUND_MS} ms)"
    )


def test_append_card_update_within_3x_of_empty_feed(tmp_path):
    """append_card_update() is O(1): a full journal is no slower than an
    empty one beyond a bounded 3× factor."""
    empty_proj = str(tmp_path / "empty")
    large_proj = str(tmp_path / "large")
    os.makedirs(empty_proj)
    os.makedirs(large_proj)

    # Pre-fill the "large" journal (untimed).
    for i in range(500):
        feed_store.append_card_update(large_proj, f"card-{i}", {"body": "x"})

    updates = {"body": "x"}
    t_empty = _median_ms(
        lambda: feed_store.append_card_update(empty_proj, "card-new", updates), n=9
    )
    t_large = _median_ms(
        lambda: feed_store.append_card_update(large_proj, "card-new", updates), n=9
    )
    print(f"[perf] append_card_update: empty {t_empty:.4f} ms, large {t_large:.4f} ms")
    assert t_large <= _APPEND_RATIO_BOUND * t_empty, (
        f"append on a full journal took {t_large:.4f} ms vs empty "
        f"{t_empty:.4f} ms (bound {_APPEND_RATIO_BOUND}×)"
    )


def test_enqueue_stall_below_5ms(tmp_path):
    """Main-thread stall while a card update is enqueued stays < 5 ms.

    This is the user-visible metric: the enqueue runs on the GTK main
    thread, and the old synchronous 0.62 s RMW used to block it.
    """
    fh = _feed_handler_cls()(GLib=_StubGLib(), on_send_to_agent=lambda *a: None)
    project_path = str(tmp_path)
    os.makedirs(project_path, exist_ok=True)
    fh._project_paths["perfproj"] = project_path

    fh._enqueue_card_update(project_path, "card-warm", {"body": "x"})  # warm-up
    time.sleep(0.05)

    def _enqueue():
        fh._enqueue_card_update(project_path, "card-x", {"body": "x"})

    best = _median_ms(_enqueue, n=9)
    print(f"[perf] enqueue stall: median {best:.4f} ms")
    assert best < _ENQUEUE_STALL_BOUND_MS, (
        f"enqueue stall {best:.2f} ms (bound {_ENQUEUE_STALL_BOUND_MS} ms)"
    )
