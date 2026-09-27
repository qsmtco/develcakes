# tests/test_uirsp3_phase2.py
# UIRESP3 Phase 2 — bound the idle pulse (Edit A) and drop the hidden
# progress bar from traversal (Edit B; the bar itself retired with the old
# status bar in SPEC-07 SP3 — ActivityHandler now renders to the activity
# pill via a status_target mock). Spec §5 Phase-2 test rows.
#
# The state machine runs in-process with the conftest fake_glib fixture
# (armed timers never fire; tests invoke the recorded 250ms callback
# directly, so no real event loop and no sleeps). The 60 s budget row is
# marked slow/manual: it needs the live app plus a real minute — the suite
# stays fast and hermetic; the measurement is executed and reported by hand.
#
# BARE-SAFE since SPEC-07 SP3: no gi import, no widgets — runs without a
# display (env -u DISPLAY).

import os
import sys
import time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ui.handlers.activity_handler import ActivityHandler


@pytest.fixture
def handler(fake_glib):
    """ActivityHandler whose status_target is a Mock (state machine in isolation).

    _active_session() reads main_content.get_current_session_key() — None
    here, so every _is_ui_active() gate passes and transitions land.
    """
    status_target = MagicMock()
    h = ActivityHandler(status_target=status_target, main_content=MagicMock(),
                        GLib_module=fake_glib)
    return h, status_target, fake_glib


def _enter_idle(h, fake_glib):
    """Drive the machine into idle and return the armed 250ms tick callback.

    The initial state is already 'idle', so we transition through 'reasoning'
    first — _set_state early-returns on same-state, and only a real
    transition arms the ticker.
    """
    h._set_state("reasoning", None)
    h._set_state("idle", None)
    assert fake_glib.armed, "idle must arm the status ticker"
    source_id, delay_ms, tick_cb = fake_glib.armed[-1]
    assert delay_ms == 250, "the status ticker is the 250ms source"
    return tick_cb


# ── Row 1: the idle tick terminates within budget ────────────────────────────


def test_idle_tick_terminates(handler):
    h, _status_target, fake_glib = handler  # render asserts deleted (SP2 Edit 5)
    tick_cb = _enter_idle(h, fake_glib)

    seen = []
    for i in range(60):  # far beyond the 20-tick budget
        alive = tick_cb()
        seen.append(bool(alive))

    assert seen[:19] == [True] * 19, "ticks 1..19 keep the source alive"
    assert seen[19] is False, "the 20th tick must return False (source dies)"
    assert not any(seen[20:]), "a dead source must not be re-armed by ticks"
    assert h._idle_ticks >= 20


def test_idle_tick_budget_is_about_five_seconds(handler):
    """20 ticks × 250 ms ≈ 5 s — the documented budget."""
    h, status_target, fake_glib = handler
    tick_cb = _enter_idle(h, fake_glib)
    ticks_alive = 0
    while tick_cb() is True:
        ticks_alive += 1
        assert ticks_alive < 100, "runaway idle ticker — budget not enforced"
    assert ticks_alive == 19, "19 keep-alive ticks then death (≈5s at 250ms)"


# ── Row 2: the counter resets on every state transition ──────────────────────


def test_idle_ticks_reset_on_state_change(handler):
    h, status_target, fake_glib = handler
    tick_cb = _enter_idle(h, fake_glib)

    for _ in range(25):  # exhaust the budget
        tick_cb()
    assert h._idle_ticks >= 20

    # A transition must reset the budget: leave idle and come back.
    h._set_state("reasoning", None)
    assert h._idle_ticks == 0, "_set_state must reset the counter"
    h._set_state("idle", None)
    assert h._idle_ticks == 0

    tick_cb = _enter_idle(h, fake_glib)
    assert tick_cb() is True, "a fresh idle entry gets a full pulse budget"


# ── Row 3: active states are untouched ──────────────────────────────────────


def test_active_states_unaffected(handler):
    h, status_target, fake_glib = handler
    for state in ("reasoning", "streaming", "tool_use"):
        h._set_state(state, None)
        source_id, delay_ms, tick_cb = fake_glib.armed[-1]
        # 20 active ticks: every one re-arms (True) and drives _live_update.
        results = [tick_cb() for _ in range(20)]
        assert all(r is True for r in results), (
            f"{state}: live-update branch must keep ticking"
        )
        # _live_update's skip-gating is the behavior that must NOT change —
        # with an unchanged signature it skips the status_target rebuild.
        # (Mock status_target: rebuild count stays bounded by signature changes.)

    # Pulse is never touched by the active branch.
    status_target.pulse_progress.assert_not_called()


# ── Audit follow-ups: same-state idle, and opacity=0 symmetry ───────────────


def test_same_state_idle_does_not_double_arm_when_ticker_alive(handler):
    """Audit follow-up: repeated same-state idle must NOT install a second
    concurrent 250ms source.

    Found by the supervisor's own post-fix probe: the first version of the
    BUG #1 fix re-armed unconditionally, so a same-state idle arriving while
    the ticker was still live would add a SECOND live source — double-rate
    cost, the exact class this phase exists to bound. The fix guards on
    liveness (_status_ticker_id is None).
    """
    h, status_target, fake_glib = handler
    tick_cb = _enter_idle(h, fake_glib)  # arms the ticker; budget NOT exhausted
    tick_cb()  # consume one tick so the budget is visibly mid-flight
    assert h._idle_ticks == 1, "fixture precondition: one tick consumed"

    # Assert on SOURCE CHURN, not the live count. The live count is 1 in both
    # the correct and the buggy implementation (f3a2cad removed the old id
    # before adding the new one), so a live-count assertion is vacuous — it
    # cannot distinguish them. `armed` is append-only in the conftest fake
    # (source_remove only clears armed_ids), so a growing `armed` is exactly
    # the wasteful remove+add cycle, and a stale budget reset.
    # (Audit BUG #1: the first version of this test was vacuous.)
    armed_before = len(fake_glib.armed)

    h._set_state("idle", None)  # same-state, ticker still alive

    assert len(fake_glib.armed) == armed_before, (
        "a same-state idle with a LIVE ticker must not arm another source "
        f"(churn: armed grew {armed_before} -> {len(fake_glib.armed)})"
    )
    assert h._idle_ticks == 1, (
        "a live ticker's budget must NOT be reset by a same-state idle "
        f"(got {h._idle_ticks}; the pulse is mid-budget, resetting restarts it)"
    )


def test_same_state_idle_restarts_pulse_budget(handler):
    """Audit BUG #1: a same-state idle re-entry must restart the budget.

    Reachable in production via a delayed on_agent_error for a session that
    already finished its round: _set_state('idle') while state is already
    'idle' hits the same-state early return. Pre-fix that left _idle_ticks at
    its exhausted value with a dead ticker, so the pulse never returned.
    """
    h, status_target, fake_glib = handler
    tick_cb = _enter_idle(h, fake_glib)

    # Exhaust the budget — the source dies.
    for _ in range(20):
        last = tick_cb()
    assert last is False, "budget must terminate the ticker"
    assert h._idle_ticks == 20, "fixture precondition: budget exhausted"

    # Same-state idle re-entry (delayed error after the pulse window).
    h._set_state("idle", None)

    assert h._idle_ticks == 0, (
        "a same-state idle re-entry must reset the pulse budget"
    )
    assert fake_glib.armed, "a fresh ticker must be armed"
    _sid, delay, _cb = fake_glib.armed[-1]
    assert delay == 250, "the re-armed source must be the 250ms status ticker"


def test_dead_tick_clears_its_own_source_ids(handler):
    """Audit BUG #1 companion: a dying tick must clear its bookkeeping.

    GLib drops the callback on a False return, so the ids are stale from that
    moment. Leaving them set would make a later same-state re-entry call
    source_remove() on a dead id (GLib critical warning).
    """
    h, status_target, fake_glib = handler
    tick_cb = _enter_idle(h, fake_glib)

    for _ in range(20):
        tick_cb()

    assert h._status_ticker_id is None, "dead tick must clear _status_ticker_id"
    assert h._live_update_timer is None
    assert h._idle_pulse_timer is None


def test_sending_and_done_ticks_clear_their_own_source_ids(handler):
    """Audit BUG #2: the fallthrough (state=sending/done) must clear the
    source-id bookkeeping too.

    Neither the active branch nor the idle branch matches for sending/done, so
    the tick falls through and the source dies. Leaving the ids non-None makes
    the next _stop_live_update/_stop_idle_pulse call source_remove() a dead id
    — a real-GLib warning (confirmed: 'Source ID N was not found when
    attempting to remove it').
    """
    for state in ("sending", "done"):
        h, status_target, fake_glib = handler
        h._set_state(state, None)
        assert fake_glib.armed, f"{state} must arm the ticker"
        tick_cb = fake_glib.armed[-1][2]

        assert tick_cb() is False, f"a {state} tick must stop the source"
        assert h._status_ticker_id is None, (
            f"the {state} fallthrough must clear _status_ticker_id"
        )
        assert h._live_update_timer is None
        assert h._idle_pulse_timer is None


def test_live_update_elapsed_bucket_is_one_second(handler, monkeypatch):
    """F12: with state/phase/hop constant, two ticks inside the same 1 s bucket
    rebuild at most one markup; a tick crossing the bucket boundary re-renders.

    Pre-P9 the divisor was 0.5, so the 0.75 s tick below landed in bucket 1 and
    rebuilt — this test is RED on that code.
    """
    h, status_target, fake_glib = handler
    h._mc.get_current_session_key.return_value = "sk-1"   # deterministic sk
    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])

    h._set_state("streaming", "sk-1")
    h._agent_start_time["sk-1"] = 1000.0      # elapsed 0.0s
    tick = fake_glib.armed[0][2]
    status_target.set_status_text.reset_mock()

    tick()                                    # elapsed 0.0 → bucket 0 → renders
    assert status_target.set_status_text.call_count == 1

    clock["t"] = 1000.75                      # same bucket under 1.0 (was bucket 1 at 0.5)
    tick()
    assert status_target.set_status_text.call_count == 1, (
        "two ticks inside the same 1.0s bucket must rebuild at most once"
    )

    clock["t"] = 1001.0                       # crosses into bucket 1 → re-render
    tick()
    assert status_target.set_status_text.call_count == 2, (
        "crossing the 1.0s bucket boundary must re-render"
    )

    clock["t"] = 1001.9                       # still bucket 1 → skipped again
    tick()
    assert status_target.set_status_text.call_count == 2, (
        "the new bucket must itself dedupe until it advances"
    )


def test_live_update_bucket_holds_hop_change_still_renders(handler, monkeypatch):
    """Control: the bucket widening must not suppress a real progress change —
    a hop-count change rebuilds inside the same bucket."""
    h, status_target, fake_glib = handler
    h._mc.get_current_session_key.return_value = "sk-1"
    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])

    h._set_state("reasoning", "sk-1")
    h._agent_start_time["sk-1"] = 1000.0
    tick = fake_glib.armed[0][2]
    status_target.set_status_text.reset_mock()

    tick()
    assert status_target.set_status_text.call_count == 1

    clock["t"] = 1000.4                       # same bucket
    h._event_hop_count["sk-1"] = 7            # progress moved
    tick()
    assert status_target.set_status_text.call_count == 2, (
        "a hop change must rebuild the markup even inside the same bucket"
    )


# ── Row 5: the §7 main-thread budget (slow/manual — live app + real minute) ──

@pytest.mark.slow
@pytest.mark.manual
def test_main_thread_idle_budget():
    """§7: main-thread CPU < 10% sustained over 60 s with the app idle.

    SLOW/MANUAL — stated reason: requires the LIVE app process (a real PID
    via --pid) and a genuine 60 s sampling window; it cannot run headless in
    CI against a fixture. Executed by hand with the committed probe:
        python3 scripts/crab_perf_probe.py --pid <pid> --duration 60
    Results are pasted in the unit report. This row SKIPS (with this reason)
    in automated runs so the suite stays green; it is discoverable via
    `pytest -m slow -m manual -rs`.
    """
    pytest.skip(
        "manual measurement row — run scripts/crab_perf_probe.py against the "
        "live app and paste the output (see module docstring)"
    )
