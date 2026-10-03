# tests/test_stop_all.py
# SPEC-09 SP3 — runtime.stop_all + ARH aggregation + review-checkpoint abort.
#
# Coverage (spec Edit 5, tests 5-8):
#   5. test_stop_all_cancels_active_loops   — mid-exec turn cancelled, loop exits
#   6. test_stop_all_denies_pending_approval — _dispatch_approval unblocks DENIED
#   7. TestStopAllAbortsCheckpoint          — pre-commit abort + boundary re-check
#   8. test_stop_all_mid_tool_call_halt     — THE ruling-#6 acceptance: real
#      mid-tool-call halt end-to-end (real Popen sleep, real kill, real
#      cancel machinery, summary card). NOT mocked at the layer under test.
#
# Real-process discipline: exec commands are real `sleep`s scanned via
# /proc (numeric markers — valid sleep durations, see test_process_kill).

import atexit
import os
import pathlib
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.runtime import CANCEL_MESSAGE, AgentRuntime, TurnResult, TurnStatus


def _uniq():
    import uuid
    return f"sa{uuid.uuid4().hex[:8]}"


def _tmpdir():
    """Leak-free variant (audit nit): tracked + auto-removed at module teardown."""
    d = tempfile.mkdtemp(prefix="sp3r2-")
    _TMPDIRS.append(d)
    return d


_TMPDIRS: list[str] = []


def _cleanup_tmpdirs():
    import shutil
    for d in _TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)


atexit.register(_cleanup_tmpdirs)


def _make_cfg():
    from agent.config import AgentConfig, LLMProviderConfig
    return AgentConfig(
        providers={
            "openai": LLMProviderConfig(
                name="openai",
                base_url="https://api.openai.com/v1",
                api_key="test-key",
                default_model="gpt-4o",
            )
        },
        default_provider="openai",
        default_model="openai/gpt-4o",
        max_tool_iterations=5,
        tool_timeout_seconds=30,
        auto_save_conversations=False,
    )


def _marker_procs(marker: str) -> list[int]:
    """PIDs whose /proc cmdline contains marker (numeric — a valid sleep arg)."""
    hits = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if marker in cmd:
            hits.append(int(entry))
    return hits


def _wait_for(cond, timeout=5.0, poll=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        val = cond()
        if val:
            return val
        time.sleep(poll)
    return cond()


@pytest.fixture(autouse=True)
def _clean_registry():
    from agent import tools
    with tools._PROCESS_REGISTRY_LOCK:
        tools._PROCESS_REGISTRY.clear()
    yield
    tools.cancel_all_processes(None)
    with tools._PROCESS_REGISTRY_LOCK:
        tools._PROCESS_REGISTRY.clear()


@pytest.fixture(autouse=True)
def _hermetic_audit(monkeypatch):
    """Keep test turns out of the real audit log (test_agent_runtime precedent)."""
    from agent.audit import AuditLog
    monkeypatch.setattr(AuditLog, "flush_audit_log", MagicMock(return_value=None))


def _auto_approve_dispatch(rt, sk):
    """Wire the runtime's approval callback to self-approve (the PM click,
    exercised through the real _dispatch_approval → approve_exec path)."""

    def _approve(session_key, tool_name, args):
        rt.approve_exec(session_key, tool_name, args, True)

    rt._on_tool_call_approval_needed = _approve


class TestStopAllRuntime:
    """Tests 5+6 — runtime.stop_all: turns cancelled, approvals denied."""

    def test_stop_all_cancels_active_loops(self, tmp_path):
        """Test 5: a REAL mid-exec turn (sleep via the real tool chain) —
        stop_all cancels it; the loop thread exits CANCELLED."""
        from agent import tools
        rt = AgentRuntime(_make_cfg(), GLib=None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, str(tmp_path))
        marker = "8.21"
        _auto_approve_dispatch(rt, sk)
        rt._on_error = lambda *a, **k: None  # cancel() UX dispatch — recorded nowhere

        call_resp = {
            "choices": [{"message": {
                "content": "[calling tools]",
                "tool_calls": [{
                    "id": "call_1",
                    "function": {
                        "name": "exec_command",
                        "arguments": f'{{"command": "sleep 300 {marker}", "timeout": 120}}',
                    },
                }],
            }}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

        with patch.object(rt, "_call_llm", return_value=call_resp):
            loop_t = threading.Thread(target=rt._run_loop, args=(sk, "go"), daemon=True)
            loop_t.start()
            # Land stop-all while the exec is REALLY in flight.
            assert _wait_for(
                lambda: tools._PROCESS_REGISTRY.get(sk), timeout=5.0,
            ), "exec never registered — the halt would be vacuous"

            t0 = time.monotonic()
            outcomes = rt.stop_all()
            elapsed = time.monotonic() - t0

            assert sk in outcomes, f"session missing from outcomes: {outcomes}"
            assert outcomes[sk].startswith("cancelled"), outcomes[sk]
            assert "killed:1" in outcomes[sk], outcomes[sk]
            assert elapsed < 3.0, f"stop_all took {elapsed:.2f}s (>3s)"
            loop_t.join(timeout=5.0)
            assert not loop_t.is_alive(), "run_loop still running after stop_all"
            result = rt.get_last_turn_result(sk)
            assert result is not None
            assert result.status == TurnStatus.CANCELLED, (
                f"Expected CANCELLED; got {result.status} ({result.metadata})"
            )
            assert rt._active_loops == set()
            # The process group is really dead.
            assert _wait_for(lambda: not _marker_procs(marker), timeout=3.0), (
                f"process survived stop_all: {_marker_procs(marker)}"
            )
        rt.stop()

    def test_stop_all_denies_pending_approval(self):
        """Test 6 (spec AC): a PM-waiting _dispatch_approval unblocks as
        DENIED when stop_all lands — no 60s hang."""
        rt = AgentRuntime(_make_cfg(), GLib=None)
        rt.start()
        sk = _uniq()
        rt._on_tool_call_approval_needed = lambda *a, **k: None  # PM never answers

        results = []

        def wait_for_pm():
            results.append(rt._dispatch_approval(sk, "exec_command", {"command": "ls"}))

        t = threading.Thread(target=wait_for_pm, daemon=True)
        t.start()
        assert _wait_for(
            lambda: any(k.startswith(sk) for k in rt._pending_approvals),
            timeout=5.0,
        ), "approval never registered — deny-flush would be vacuous"

        t0 = time.monotonic()
        rt._active_loops.add(sk)  # approvals are dispatched from a live loop
        rt.stop_all()
        t.join(timeout=5.0)
        elapsed = time.monotonic() - t0

        assert not t.is_alive(), "approval waiter still blocked after stop_all"
        assert elapsed < 10.0, (
            f"unblock took {elapsed:.1f}s — deny-flush failed (60s timeout path?)"
        )
        assert results == [None], (
            f"expected DENIED (None = denial); got {results!r}"
        )
        rt.stop()

    def test_approval_registered_after_stop_all_denies_immediately(self):
        """SP3 fix round (audit BUG#2 — auditor probe E): tool-call N+1's
        approval registering AFTER stop_all's one-shot deny-flush ran used
        to hang the FULL 60s wait. Registration itself is now
        cancellation-aware (session in _cancelled → immediate None)."""
        rt = AgentRuntime(_make_cfg(), GLib=None)
        rt.start()
        sk = _uniq()
        rt._on_tool_call_approval_needed = lambda *a, **k: None  # PM never answers

        # An in-flight loop (the production registration context) with one
        # approval already pending when stop-all lands.
        rt._active_loops.add(sk)
        results = []

        def wait_for_pm():
            results.append(rt._dispatch_approval(sk, "exec_command", {"command": "ls"}))

        t = threading.Thread(target=wait_for_pm, daemon=True)
        t.start()
        assert _wait_for(
            lambda: any(k.startswith(sk) for k in rt._pending_approvals),
            timeout=5.0,
        ), "approval never registered — the late-registration race would be vacuous"

        rt.stop_all()
        assert sk in rt._cancelled, "stop_all did not mark the session cancelled"
        first = list(results)
        assert first == [None] or not first  # deny-flush resolved the first

        # NOW the race: register AFTER stop_all has fully returned.
        t0 = time.monotonic()
        late = rt._dispatch_approval(sk, "exec_command", {"command": "ls"})
        elapsed = time.monotonic() - t0

        assert late is None, f"late approval must be DENIED, got {late!r}"
        assert elapsed < 10.0, (
            f"late registration took {elapsed:.1f}s — BUG#2 hang not fixed (60s?)"
        )
        rt.stop()

    def test_stop_all_with_nothing_in_flight(self):
        rt = AgentRuntime(_make_cfg(), GLib=None)
        rt.start()
        outcomes = rt.stop_all()
        assert outcomes == {}
        rt.stop()

    def test_late_registration_denied_after_loop_discard(self):
        """Fix round 2, BUG#2(a) — auditor probe dbg_sp3fix_bug2bypass:
        the loop's own cancellation branch DISCARDS sk from _cancelled
        (runtime.py `self._cancelled.discard(session_key)`). If the loop
        exits via that branch (not via _cancel_requested — the GLOBAL
        flag can be consumed by another session's loop), a late approval
        registering afterwards used to bypass the registration guard and
        hang the full 60s. The guard must key on the per-session
        cancellation EPOCH (cleared only by _terminate_turn's terminal
        transition), never on _cancelled bookkeeping."""
        rt = AgentRuntime(_make_cfg(), GLib=None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, str(_tmpdir()))
        rt._on_error = lambda *a, **k: None

        # The exact bypass shape: sk marked cancelled, but the GLOBAL
        # _cancel_requested already consumed (another session's loop) —
        # the loop exits via the _cancelled branch, which discards sk.
        rt._cancelled.add(sk)
        rt._cancel_requested = False

        text_resp = {
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }
        with patch.object(rt, "_call_llm", return_value=text_resp):
            loop_t = threading.Thread(target=rt._run_loop, args=(sk, "go"), daemon=True)
            loop_t.start()
            loop_t.join(timeout=10.0)
        assert not loop_t.is_alive()
        assert sk not in rt._cancelled, (
            "loop did not exit via the _cancelled branch — shape not reproduced"
        )

        rt._on_tool_call_approval_needed = lambda *a, **k: None  # PM never answers
        t0 = time.monotonic()
        denied = rt._dispatch_approval(sk, "exec_command", {"command": "ls"})
        elapsed = time.monotonic() - t0

        assert denied is None, f"late registration must DENY (None), got {denied!r}"
        assert elapsed < 10.0, (
            f"registration guard bypassed — waited {elapsed:.1f}s (60s hang)"
        )
        rt.stop()

    def test_late_registration_pops_denied_entry(self):
        """Fix round 2, BUG#2(b) — auditor probe dbg_sp3fix_regleak: the
        late-registration early-return inserted the entry then returned
        WITHOUT popping it; every denied tool call leaked one
        _pending_approvals entry (5/5 in the probe), inflating stop_all's
        denied count forever. Each refusal must pop its own entry."""
        rt = AgentRuntime(_make_cfg(), GLib=None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, str(_tmpdir()))
        rt._on_tool_call_approval_needed = lambda *a, **k: None  # PM never answers
        # Arm the epoch directly — the state cancel() leaves behind; the
        # guard reads the EPOCH, not _cancelled (round-1's read).
        with rt._lock:
            rt._cancelled_epochs.add(sk)

        for _ in range(5):
            denied = rt._dispatch_approval(sk, "exec_command", {"command": "ls"})
            assert denied is None
        with rt._lock:
            remaining = [k for k in rt._pending_approvals if k.startswith(sk)]
        assert remaining == [], (
            f"denied registrations leaked entries: {remaining} "
            f"(pre-fix: 5/5 leaked per dbg_sp3fix_regleak)"
        )
        rt.stop()

    def test_pm_click_resolves_live_waiter_despite_stale_entry(self):
        """Fix round 2, BUG#2(c) — auditor probe dbg_sp3fix_hijack: a
        leaked stale entry hijacked approve_exec (resolves the FIRST
        prefix match): the PM's click resolved the PHANTOM while the live
        waiter kept waiting out its 60s. With every deny-flush popping,
        post-fix there IS no stale entry; the PM click must resolve the
        live waiter AND leave the dict empty. (The pre-fix hijack cannot
        be staged deterministically without the leak itself; this pins
        the post-fix invariant: live resolution + empty dict.)"""
        rt = AgentRuntime(_make_cfg(), GLib=None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, str(_tmpdir()))
        rt._on_tool_call_approval_needed = lambda *a, **k: None  # PM never answers

        results = []

        def live_waiter():
            results.append(rt._dispatch_approval(sk, "exec_command", {"command": "LIVE"}))

        t = threading.Thread(target=live_waiter, daemon=True)
        t.start()
        assert _wait_for(
            lambda: any(k.startswith(sk) for k in rt._pending_approvals),
            timeout=5.0,
        ), "live approval never registered — hijack pin would be vacuous"

        rt.approve_exec(sk, "exec_command", {"command": "LIVE"}, True)
        t.join(timeout=5.0)

        assert not t.is_alive(), "PM click did not unblock the live waiter"
        assert results == [True], f"PM click must resolve the LIVE waiter: {results!r}"
        with rt._lock:
            remaining = [k for k in rt._pending_approvals if k.startswith(sk)]
        assert remaining == [], f"approve_exec left entries behind: {remaining}"
        rt.stop()

    def test_deny_flush_pops_cancel_and_stop_all(self):
        """Fix round 2, BUG#2(c) — cancel()'s deny-flush AND stop_all's
        final flush must POP every entry they deny (pre-existing no-pop
        was the approve_exec-hijack enabler: a PM click resolved a
        phantom while the live waiter hung).

        Determinism (first RED attempt: this test PASSED with cancel()'s
        pop reverted — the registration guard's own pop raced in, because
        cancel() arms sk's epoch BEFORE its flush, and the waiter's
        post-insert guard check then denied+popped the same entry). Both
        sites here use a waiter whose OWN session key is never armed:
        site A via the production prefix semantics themselves (cancel a
        strict prefix of the waiter's key — the flush's startswith match
        hits the entry, the guard's `sk_waiter in _cancelled_epochs`
        check cannot), site B with no cancel running at all (stop_all's
        final flush sweeps entries whose sessions have no active loop).
        The flush pop is then the ONLY possible remover."""
        rt = AgentRuntime(_make_cfg(), GLib=None)
        rt.start()
        rt._on_error = lambda *a, **k: None

        # Site A: cancel()'s flush. sk_outer is a STRICT PREFIX of
        # sk_waiter; cancel(sk_outer) arms sk_outer's epoch only, so the
        # guard (which reads sk_waiter) can never pop this entry.
        sk_outer = _uniq()
        sk_waiter = sk_outer + "w"
        rt.create_conversation("Coder", sk_waiter, str(_tmpdir()))
        rt._on_tool_call_approval_needed = lambda *a, **k: None
        results_a = []

        def wait_a():
            results_a.append(
                rt._dispatch_approval(sk_waiter, "exec_command", {"command": "ls"})
            )

        t_a = threading.Thread(target=wait_a, daemon=True)
        t_a.start()
        assert _wait_for(
            lambda: any(k.startswith(sk_waiter) for k in rt._pending_approvals),
            timeout=5.0,
        ), "site-A approval never registered"

        rt.cancel(sk_outer)
        t_a.join(timeout=5.0)

        assert results_a == [None], "cancel's prefix flush must deny the waiter"
        assert not t_a.is_alive(), "waiter hung — flush did not set its event"
        with rt._lock:
            assert not [k for k in rt._pending_approvals if k.startswith(sk_waiter)], (
                "cancel()'s deny-flush left the entry behind — hijack enabler"
            )

        # Site B: stop_all's FINAL flush. sk_b has NO active loop and is
        # never cancelled — no epoch is ever armed for it, so the guard
        # cannot pop; only the final sweep can remove the entry.
        sk_b = _uniq()
        rt.create_conversation("Coder", sk_b, str(_tmpdir()))
        results_b = []

        def wait_b():
            results_b.append(rt._dispatch_approval(sk_b, "exec_command", {"command": "ls"}))

        t_b = threading.Thread(target=wait_b, daemon=True)
        t_b.start()
        assert _wait_for(
            lambda: any(k.startswith(sk_b) for k in rt._pending_approvals),
            timeout=5.0,
        ), "site-B approval never registered"

        rt.stop_all()
        t_b.join(timeout=5.0)

        assert results_b == [None], "stop_all's final flush must deny the waiter"
        assert not t_b.is_alive(), "waiter hung — final flush did not set its event"
        with rt._lock:
            assert rt._pending_approvals == {}, (
                f"stop_all's final flush left entries behind: "
                f"{list(rt._pending_approvals)}"
            )
        rt.stop()

    def test_epoch_clears_on_terminal_transition_no_poisoning(self):
        """Fix round 2, BUG#2(iv) — the no-permanent-poisoning control:
        the epoch must clear on _terminate_turn's TERMINAL transition, so
        a session that COMPLETED after a cancelled turn re-registers
        approvals normally. The fix itself must not over-fire."""
        rt = AgentRuntime(_make_cfg(), GLib=None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Coder", sk, str(_tmpdir()))

        # Turn 1: cancel mid-flight (arms the epoch via cancel(); the
        # CANCELLED terminal re-arms — the epoch is the durable trace).
        rt._active_loops.add(sk)
        rt._cancelled.add(sk)
        rt._cancel_requested = True
        # Explicitly reset the GLOBAL consumed flag so the loop's turn-2
        # cancellation branch doesn't self-cancel (see the bypass test).
        rt._cancel_requested = False
        rt._terminate_turn(TurnResult(
            status=TurnStatus.CANCELLED,
            session_key=sk,
            turn_token=None,
            error=CANCEL_MESSAGE,
            metadata={"reason": "turn 1 cancelled"},
        ))
        assert sk in rt._cancelled_epochs, "epoch not armed"

        # The loop consumed the cancellation: _cancelled discarded (the
        # branch's own bookkeeping), _cancel_requested consumed by some
        # session's loop. ONLY the epoch remembers the cancel now — the
        # exact state the epoch exists to cover.
        rt._cancelled.discard(sk)
        rt._cancel_requested = False
        assert sk in rt._cancelled_epochs, "epoch must outlive _cancelled bookkeeping"

        # Turn 2's approval callback: the PM click, through the REAL
        # approve_exec path (turn 1 issued no tool calls, so arming the
        # approver here cannot mask the turn-1 cancellation).
        def _approve(session_key, tool_name, args):
            rt.approve_exec(session_key, tool_name, args, True)

        rt._on_tool_call_approval_needed = _approve

        # Turn 2: sending a new turn clears the epoch at _run_loop start —
        # a COMPLETED turn after a cancelled one must work end to end.
        # RED-strengthened (two masking paths found and closed):
        #   1. A completed status alone proves nothing — a DENIED approval
        #      still completes the turn (the tool call is just refused).
        #   2. The approval callback is NOT a proof either — do_approval's
        #      thread spawns BEFORE the guard check, so the callback fires
        #      even for a denied registration (no-GLib path; production's
        #      idle_add queues behind the main loop instead).
        # The OBSERVABLE that pins the turn-start clear: the tool actually
        # EXECUTED — impossible unless the registration was approved, and
        # approval requires the guard to have let it register.
        marker = pathlib.Path(_tmpdir()) / "sp3r2-poison-control-marker"
        assert not marker.exists(), "pre-existing marker — vacuous control"
        call_resp = {
            "choices": [{"message": {
                "content": "[calling tools]",
                "tool_calls": [{
                    "id": "call_1",
                    "function": {
                        "name": "exec_command",
                        "arguments": f'{{"command": "touch {marker}", "timeout": 15}}',
                    },
                }],
            }}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        text_resp = {
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }
        with patch.object(rt, "_call_llm", side_effect=[call_resp, text_resp]):
            loop_t = threading.Thread(target=rt._run_loop, args=(sk, "again"), daemon=True)
            loop_t.start()
            loop_t.join(timeout=10.0)
        assert not loop_t.is_alive()
        result = rt.get_last_turn_result(sk)
        assert result is not None and result.status == TurnStatus.COMPLETED, (
            f"follow-up turn did not complete normally: "
            f"{result.status if result else None} — fix over-fires"
        )
        assert marker.exists(), (
            "the post-cancel turn's approved tool call never EXECUTED — the "
            "registration was denied, i.e. the turn-start epoch clear is "
            "missing (permanent poisoning by the fix itself)"
        )
        with rt._lock:
            assert sk not in rt._cancelled_epochs
        rt.stop()


class _CapturingFeed:
    def __init__(self):
        self.cards = []

    def add_card(self, card):
        self.cards.append(card)
        return f"card-{len(self.cards)}"


class TestStopAllARH:
    """Test 8 (ARH half) — aggregation, summary card, no-op card."""

    def _make_arh(self):
        from ui.handlers.agent_runtime_handler import AgentRuntimeHandler
        return AgentRuntimeHandler(main_content=None, chat_render_handler=None)

    def test_stop_all_agents_merges_and_cards(self):
        arh = self._make_arh()
        feed = _CapturingFeed()
        arh.set_feed_handler(feed)
        rt = AgentRuntime(_make_cfg(), GLib=None)
        rt.start()
        rt._active_loops.add("special:coder")  # simulate an in-flight loop
        arh._runtimes["Coder"] = rt

        merged = arh.stop_all_agents()

        assert merged.get("special:coder") == "cancelled"
        assert arh.stop_all_in_progress() is False  # cleared after
        assert len(feed.cards) == 1, f"expected ONE summary card; got {len(feed.cards)}"
        card = feed.cards[0]
        assert card.card_type == "system"
        assert "Turns cancelled: 1" in card.body
        assert card.metadata.get("origin") == "stop-all"
        rt.stop()

    def test_stop_all_noop_cards_zero(self):
        """Spec §7: nothing in flight → '0 turns in flight' card, still one."""
        arh = self._make_arh()
        feed = _CapturingFeed()
        arh.set_feed_handler(feed)
        merged = arh.stop_all_agents()
        assert merged == {}
        assert len(feed.cards) == 1
        assert "0 turns in flight" in feed.cards[0].body

    def test_unkillable_group_surfaces_in_card(self):
        """SP3 fix round (audit BUG#3): a group that SURVIVED SIGKILL must
        NOT be counted as killed — the card reports 'N unkillable' so a
        hostile-process situation is visible instead of a silent success."""
        arh = self._make_arh()
        feed = _CapturingFeed()
        arh.set_feed_handler(feed)
        rt = AgentRuntime(_make_cfg(), GLib=None)
        rt.start()
        rt._active_loops.add("special:coder")
        arh._runtimes["Coder"] = rt

        with patch("agent.tools.cancel_all_processes") as fake_cancel:
            # call 1 = the sk-targeted kill, call 2 = the global "*" sweep
            # (runtime.stop_all always sweeps the rest too).
            fake_cancel.side_effect = [(1, 1), (0, 0)]
            merged = arh.stop_all_agents()

        # mock-truthiness guard: pin the call shape the card aggregation reads.
        assert fake_cancel.call_count == 2
        assert merged.get("special:coder") == "cancelled+killed:1+1-unkillable"
        assert "*" not in merged  # global sweep found nothing (0, 0)
        assert arh.stop_all_in_progress() is False
        assert len(feed.cards) == 1
        body = feed.cards[0].body
        assert "Processes killed: 1" in body, f"card body: {body!r}"
        assert "Processes UNKILLABLE: 1" in body, f"card body: {body!r}"
        assert "survived SIGKILL" in body, f"card body: {body!r}"
        rt.stop()

    def test_stop_all_agents_no_feed_handler_no_crash(self):
        arh = self._make_arh()  # no feed handler wired
        assert arh.stop_all_agents() == {}  # must not raise

    def test_note_stop_all_aborted_feeds_summary_card(self):
        """note_stop_all_aborted increments; the summary card consumes+resets."""
        arh = self._make_arh()
        feed = _CapturingFeed()
        arh.set_feed_handler(feed)
        arh.note_stop_all_aborted()
        # No runtimes → outcomes empty, but the aborted count must surface.
        arh.stop_all_agents()
        assert len(feed.cards) == 1
        assert "Checkpoints aborted: 1" in feed.cards[0].body
        assert arh._stop_all_aborted_checkpoints == 0  # consumed
        # And a second stop-all does NOT repeat the stale count.
        feed.cards.clear()
        arh.stop_all_agents()
        assert "Checkpoints aborted" not in feed.cards[0].body


class TestStopAllAbortsCheckpoint:
    """Test 7 — review checkpoint refuses to commit under stop-all."""

    def _make_handlers(self, project_path):
        from ui.handlers.agent_runtime_handler import AgentRuntimeHandler
        from ui.handlers.review_handler import ReviewHandler
        arh = AgentRuntimeHandler(main_content=None, chat_render_handler=None)

        class _MockGLib:
            def idle_add(self, fn, *a, **k):
                fn(*a, **k)
                return 0

        texts = []
        rh = ReviewHandler(
            GLib=_MockGLib(),
            main_content=MagicMock(),
            project_handler=MagicMock(),
            on_review_started=MagicMock(),
            on_review_ended=MagicMock(),
            on_display_card=MagicMock(),
            on_display_text=lambda sk, text: texts.append(text),
            on_feed_card=MagicMock(),
        )
        rh.set_agent_runtime_handler(arh)
        from models.review_state import ReviewState
        rh._states["proj"] = ReviewState(
            project_path=str(project_path),
            review_mode="review",
            checkpoint_sha=None,
            is_dirty=True,
        )
        return arh, rh, texts

    def _git_repo(self, project_path):
        import git as gitpython
        repo = gitpython.Repo.init(str(project_path))
        with repo.config_writer() as cw:
            cw.set_value("user", "name", "t")
            cw.set_value("user", "email", "t@t")
        (project_path / "seed.txt").write_text("seed\n")
        repo.index.add(["seed.txt"])
        repo.index.commit("init")
        return repo

    def _commit_count(self, project_path):
        import git as gitpython
        repo = gitpython.Repo(str(project_path))
        return len(list(repo.iter_commits()))

    def test_checkpoint_aborted_under_stop_flag_no_commit(self, tmp_path):
        """Pre-commit gate: flag set → 'checkpoint aborted', git log UNCHANGED."""
        project = tmp_path / "proj"
        project.mkdir()
        self._git_repo(project)
        before = self._commit_count(project)

        arh, rh, texts = self._make_handlers(project)
        arh._stop_all_in_progress = True  # stop-all in flight
        rh.start_review("proj")
        assert _wait_for(lambda: texts, timeout=5.0), "no text emitted"
        assert "checkpoint aborted: stop-all" in texts[0]
        assert self._commit_count(project) == before, "a commit LANDED under stop-all!"

    def test_checkpoint_runs_when_flag_clear(self, tmp_path):
        """Control: same setup, flag False → the checkpoint DOES commit
        (proves the gate is the flag, not an always-abort)."""
        project = tmp_path / "proj"
        project.mkdir()
        self._git_repo(project)
        before = self._commit_count(project)

        _arh, rh, texts = self._make_handlers(project)  # flag defaults False — the control
        rh.start_review("proj")
        assert _wait_for(
            lambda: any("Review session started" in t for t in texts),
            timeout=10.0,
        ), f"checkpoint never completed: {texts}"
        assert self._commit_count(project) == before + 1

    def test_pre_flight_gate_aborts_and_notes_counter(self, tmp_path):
        """SP3 fix round (audit BUG#4): the PRE-FLIGHT gate (before any git
        work) must abort through the same path as the boundary re-check —
        emit the text AND note_stop_all_aborted, so the summary card counts
        the common case (stop-all already in flight when the review starts)."""
        project = tmp_path / "proj"
        project.mkdir()
        self._git_repo(project)
        before = self._commit_count(project)

        arh, rh, texts = self._make_handlers(project)
        arh._stop_all_in_progress = True  # stop-all already running
        rh.start_review("proj")
        assert _wait_for(lambda: texts, timeout=5.0), "no text emitted"
        assert "checkpoint aborted: stop-all" in texts[0]
        assert self._commit_count(project) == before, "a commit LANDED under stop-all!"
        # The abort was NOTED (BUG#4: the pre-flight path used to emit only).
        assert arh._stop_all_aborted_checkpoints == 1

    def test_boundary_recheck_aborts_when_stop_lands_during_staging(self, tmp_path):
        """The commit-boundary re-check: stop-all landing WHILE staging ran
        aborts before git_ops.commit (no checkpoint faking pre-halt state)."""
        project = tmp_path / "proj"
        project.mkdir()
        self._git_repo(project)
        before = self._commit_count(project)

        arh, rh, texts = self._make_handlers(project)

        import ui.handlers.review_handler as rh_mod

        def _stage_all_sets_flag(project_path):
            # Simulate: stop-all lands while the staging thread is mid-flight.
            arh._stop_all_in_progress = True
            return SimpleNamespace(success=True)

        with patch.object(rh_mod.git_ops, "is_repo", return_value=True), \
             patch.object(rh_mod.git_ops, "stage_all", side_effect=_stage_all_sets_flag):
            rh.start_review("proj")
            assert _wait_for(
                lambda: any("checkpoint aborted" in t for t in texts),
                timeout=5.0,
            ), f"boundary re-check never aborted: {texts}"
        assert self._commit_count(project) == before, "commit slipped past the boundary"
        # The abort was NOTED (feeds the next summary card) — here outside a
        # stop_all_agents window, the count waits for the card that consumes it.
        assert arh._stop_all_aborted_checkpoints == 1
class TestStopAllMidToolCallHalt:
    """Test 8 — THE ruling-#6 acceptance: a REAL mid-tool-call halt through
    the full ARH stack. Real Popen sleep, real registry kill, real cancel
    machinery, real approval path, real summary card. Not mocked at the
    layer under test (only the LLM boundary is a stub)."""

    def test_stop_all_mid_tool_call_halt(self, tmp_path):
        from agent import tools
        from agent import tools as tools_mod

        arh = self._make_arh()
        feed = _CapturingFeed()
        arh.set_feed_handler(feed)

        rt = AgentRuntime(_make_cfg(), GLib=None)
        rt.start()
        arh._runtimes["Coder"] = rt
        sk = _uniq()
        rt.create_conversation("Coder", sk, str(tmp_path))
        _auto_approve_dispatch(rt, sk)
        rt._on_error = lambda *a, **k: None

        marker = "8.31"
        call_resp = {
            "choices": [{"message": {
                "content": "[calling tools]",
                "tool_calls": [{
                    "id": "call_1",
                    "function": {
                        "name": "exec_command",
                        "arguments": f'{{"command": "sleep 300 {marker}", "timeout": 120}}',
                    },
                }],
            }}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

        with patch.object(rt, "_call_llm", return_value=call_resp), \
             patch.object(tools_mod, "MAX_EXEC_OUTPUT", tools.MAX_EXEC_OUTPUT):
            loop_t = threading.Thread(target=rt._run_loop, args=(sk, "go"), daemon=True)
            loop_t.start()

            # 1. The turn is REALLY mid-exec (registered process, live /proc).
            assert _wait_for(
                lambda: tools._PROCESS_REGISTRY.get(sk), timeout=5.0,
            ), "exec never started — acceptance test would be vacuous"
            assert _marker_procs(marker), "sleep not visible in /proc"

            # 2. Stop-all lands MID-EXEC through the ARH aggregation seam.
            t0 = time.monotonic()
            merged = arh.stop_all_agents()
            elapsed = time.monotonic() - t0

            # 3. Process dead ≤3s.
            assert elapsed < 3.0, f"stop_all took {elapsed:.2f}s"
            assert _wait_for(lambda: not _marker_procs(marker), timeout=3.0), (
                f"process still alive {elapsed:.1f}s after stop-all"
            )

            # 4. Turn terminates CANCELLED (join the loop thread).
            loop_t.join(timeout=5.0)
            assert not loop_t.is_alive(), "turn thread outlived stop-all"
            result = rt.get_last_turn_result(sk)
            assert result is not None and result.status == TurnStatus.CANCELLED

            # 5. Approval machinery unblocked (none pending: ours auto-resolved,
            #    but the deny-flush path must have fired for any stragglers).
            registry_after = tools._PROCESS_REGISTRY.get(sk)
            assert not registry_after, f"registry not drained: {registry_after}"

            # 6. Summary card LISTS the halt.
            assert len(feed.cards) == 1
            body = feed.cards[0].body
            assert "Turns cancelled: 1" in body, f"card body: {body!r}"
            assert "Processes killed: 1" in body, f"card body: {body!r}"
            assert merged.get(sk, "").startswith("cancelled")
        rt.stop()

    def _make_arh(self):
        from ui.handlers.agent_runtime_handler import AgentRuntimeHandler
        return AgentRuntimeHandler(main_content=None, chat_render_handler=None)
