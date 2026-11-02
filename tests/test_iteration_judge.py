"""SPEC-60 §2.5: iteration judge — six cases.

Modeled on tests/test_agent_runtime.py:630-665 (_make_cfg, mock _call_llm,
direct synchronous _run_loop, rt.get_last_turn_result for terminal state).

post_decision is patched at the SOURCE module (agent.llm.jev_client): the
runtime's lazy `from agent.llm.jev_client import post_decision` re-reads the
module attribute at call time, so source-module patching is the seam.

Case 2 leaves post_decision unpatched per spec §2.5 — this assumes the test
environment has no DecisionModel card (spec DISCOVERY verified this). On a
machine WITH a configured card, case 2 would make one real 2s POST.
"""
from __future__ import annotations

import unittest.mock
import uuid

import pytest

from agent.runtime import AgentRuntime, TurnStatus
from tests.test_agent_runtime import _make_cfg, _resp

_GRANT = 25        # spec §2.2 _ITERATION_GRANT
_MAX_GRANTS = 2    # spec §2.2 _ITERATION_MAX_GRANTS


@pytest.fixture(scope="module", autouse=True)
def _hermetic_audit_flush():
    """Same guard as tests/test_agent_runtime.py: tool-loop turns record real
    audit entries; intercept the flush so the suite stays hermetic."""
    from agent.audit import AuditLog
    original = AuditLog.flush_audit_log
    AuditLog.flush_audit_log = unittest.mock.MagicMock(return_value=None)
    yield
    AuditLog.flush_audit_log = original


def _uniq():
    return f"ij{uuid.uuid4().hex[:8]}"


def _cfg(soft: int, hard: int):
    """Legacy-shaped config with the SPEC-60 knobs set.

    tool_iteration_hard_cap is assigned as an attribute so case 1 runs green
    against a tree that predates Coder's config field (dataclass instances
    take new attributes); post-merge it assigns the same value as the field.
    """
    cfg = _make_cfg()
    cfg.max_tool_iterations = soft
    cfg.tool_iteration_hard_cap = hard
    return cfg


def _runtime(tmp_path, soft: int, hard: int):
    rt = AgentRuntime(_cfg(soft, hard))
    rt.start()
    sk = _uniq()
    rt.create_conversation("Coder", sk, str(tmp_path))
    return rt, sk


def _tool_caller(counter):
    """Every LLM call answers with one more tool call — drives the loop to
    the soft line exactly the way the legacy max-iterations test does."""
    def caller(sk, msgs, tools, **kwargs):
        counter[0] += 1
        return _resp(tool_calls=[{
            "id": f"c{counter[0]}",
            "function": {"name": "list_files", "arguments": '{"path": "."}'},
        }])
    return caller


def _judge_verdicts(verdicts):
    """post_decision double. Pops scripted verdicts ("continue"/"stop"),
    then defaults to stop — a buggy implementation that re-consults after
    the grant budget fails assertions instead of hanging the loop.
    Continue answers carry confidence 0.9 (decisive, >= 0.6)."""
    calls = []

    def fake(state, questions):
        calls.append(state)
        verdict = verdicts.pop(0) if verdicts else "stop"
        answer = {
            "type": "choice",
            "choice": verdict,
            "confidence": 0.9,
            "probabilities": {},
        }
        return {"decision": answer}, ""

    fake.calls = calls
    return fake


def test_case1_legacy_hard_cap_at_or_below_soft(tmp_path, monkeypatch):
    """Case 1: tool_iteration_hard_cap=0 (<= soft) — judge disarmed.

    Exact legacy path: turn FAILED, reason == "max_iterations", the
    "[max tool iterations reached]" marker lands in the conversation, and
    the judge is never consulted (patched to explode if it is).
    """
    rt, sk = _runtime(tmp_path, soft=3, hard=0)
    counter = [0]

    def boom(state, questions):
        raise AssertionError("judge must not be consulted when hard_cap <= soft")

    monkeypatch.setattr("agent.llm.jev_client.post_decision", boom)
    try:
        with unittest.mock.patch.object(rt, "_call_llm", _tool_caller(counter)):
            rt._run_loop(sk, "keep calling tools")
        result = rt.get_last_turn_result(sk)
        assert result is not None
        assert result.status == TurnStatus.FAILED
        assert result.metadata.get("reason") == "max_iterations"
        conv = rt.get_conversation(sk)
        assert any(
            m.content == "[max tool iterations reached]" for m in conv.messages
        )
        assert counter[0] == 3
    finally:
        rt.stop()


def test_case2_no_card_stops_at_soft_completes(tmp_path, monkeypatch):
    """Case 2: hard_cap armed (100), card loader unpatched — no DecisionModel
    card in the test env means the judge returns stop. Turn closes
    COMPLETED with reason "iteration_judge" and exactly `soft` LLM calls."""
    rt, sk = _runtime(tmp_path, soft=3, hard=100)
    counter = [0]
    try:
        with unittest.mock.patch.object(rt, "_call_llm", _tool_caller(counter)):
            rt._run_loop(sk, "keep calling tools")
        result = rt.get_last_turn_result(sk)
        assert result is not None
        assert result.status == TurnStatus.COMPLETED
        assert result.metadata.get("reason") == "iteration_judge"
        assert result.metadata.get("grants") == 0
        assert counter[0] == 3
        conv = rt.get_conversation(sk)
        assert any(
            m.content.startswith("[tool loop closed") for m in conv.messages
        )
    finally:
        rt.stop()


def test_case3_grant_extends_then_stop(tmp_path, monkeypatch):
    """Case 3: judge says continue @0.9 once, then stop. Exactly one grant:
    call_count lands in (soft, soft+25] and metadata.grants == 1."""
    rt, sk = _runtime(tmp_path, soft=3, hard=30)
    counter = [0]
    fake = _judge_verdicts(["continue", "stop"])
    monkeypatch.setattr("agent.llm.jev_client.post_decision", fake)
    try:
        with unittest.mock.patch.object(rt, "_call_llm", _tool_caller(counter)):
            rt._run_loop(sk, "keep calling tools")
        result = rt.get_last_turn_result(sk)
        assert result is not None
        assert result.status == TurnStatus.COMPLETED
        assert result.metadata.get("reason") == "iteration_judge"
        assert result.metadata.get("grants") == 1
        assert 3 < counter[0] <= 3 + _GRANT
        assert fake.calls, "the judge must have been consulted"
    finally:
        rt.stop()


def test_case4_hard_cap_clamps_a_continuing_judge(tmp_path, monkeypatch):
    """Case 4: judge always says continue. The hard cap still ends the turn:
    call_count == hard_cap, COMPLETED, reason == "hard_cap", grants == 2.

    soft=3/hard=30: grant 1 -> min(3+25, 30) = 28; grant 2 -> min(28+25, 30)
    = 30 (clamped, hit_hard_cap=True). The grant budget needs both grants to
    be consumed before the cap, so hard must satisfy soft+25 < hard <= soft+50.
    """
    rt, sk = _runtime(tmp_path, soft=3, hard=30)
    counter = [0]
    fake = _judge_verdicts(["continue", "continue"])
    monkeypatch.setattr("agent.llm.jev_client.post_decision", fake)
    try:
        with unittest.mock.patch.object(rt, "_call_llm", _tool_caller(counter)):
            rt._run_loop(sk, "keep calling tools")
        result = rt.get_last_turn_result(sk)
        assert result is not None
        assert result.status == TurnStatus.COMPLETED
        assert result.metadata.get("reason") == "hard_cap"
        assert result.metadata.get("grants") == _MAX_GRANTS
        assert counter[0] == 30
    finally:
        rt.stop()


def test_case5_nudge_at_soft_minus_margin_only_when_armed(tmp_path, monkeypatch):
    """Case 5: at soft-5 the judge-armed loop queues exactly one
    "[iteration notice]" in _pending_stuck_messages. (The consumer that pops
    the queue lives in the real _call_llm, which these tests patch, so the
    notice survives the turn and is directly assertable.)

    Stuck-detection may add its own intervention lines to the same queue —
    only entries starting with "[iteration notice]" are counted.

    Negative half: with the judge disarmed (hard_cap <= soft) no notice is
    queued (AC: nudge fires only when the judge is armed).
    """
    rt, sk = _runtime(tmp_path, soft=10, hard=100)
    counter = [0]
    try:
        with unittest.mock.patch.object(rt, "_call_llm", _tool_caller(counter)):
            rt._run_loop(sk, "keep calling tools")
        pending = rt._pending_stuck_messages.get(sk, [])
        notices = [m for m in pending if m.startswith("[iteration notice]")]
        assert len(notices) == 1
    finally:
        rt.stop()

    rt2, sk2 = _runtime(tmp_path, soft=10, hard=0)
    counter2 = [0]
    try:
        with unittest.mock.patch.object(rt2, "_call_llm", _tool_caller(counter2)):
            rt2._run_loop(sk2, "keep calling tools")
        pending2 = rt2._pending_stuck_messages.get(sk2, [])
        assert not any(
            m.startswith("[iteration notice]") for m in pending2
        )
    finally:
        rt2.stop()


def test_case6_post_decision_failure_fails_safe(tmp_path, monkeypatch):
    """Case 6: post_decision returns (None, "decision model unavailable")
    — the judge fails SAFE (stop), the turn closes COMPLETED, and no
    exception escapes the loop."""
    rt, sk = _runtime(tmp_path, soft=3, hard=100)
    counter = [0]

    def broken(state, questions):
        return None, "decision model unavailable"

    monkeypatch.setattr("agent.llm.jev_client.post_decision", broken)
    try:
        with unittest.mock.patch.object(rt, "_call_llm", _tool_caller(counter)):
            rt._run_loop(sk, "keep calling tools")  # must not raise
        result = rt.get_last_turn_result(sk)
        assert result is not None
        assert result.status == TurnStatus.COMPLETED
        assert result.error is None
        assert result.metadata.get("reason") == "iteration_judge"
        assert counter[0] == 3
    finally:
        rt.stop()
