"""SPEC-33: turn-immune observation mask, sweep fail-keep, recovery stub."""
from __future__ import annotations

import logging
from unittest.mock import patch

from agent.context_strategy import DefaultContextStrategy
from agent.runtime import AgentRuntime
from models.conversation import Conversation, MessageRole, ToolCall
from tests.test_agent_runtime import _make_cfg

_HINT = "\u2014 re-read to restore]"
_MASK_PREFIX = "[masked \u2014"


def _big(conv: Conversation, call_id: str, path: str) -> str:
    body = f"{call_id}-" + ("B" * 520)
    tc = ToolCall(
        call_id=call_id,
        tool_name="read_file",
        arguments={"path": path, "offset": 0},
        result=body,
    )
    conv.add_assistant_message("go", [tc])
    conv.add_tool_result(call_id, body)
    return body


def _bodies(conv: Conversation) -> list[str]:
    return [m.content for m in conv.messages if m.role == MessageRole.TOOL_RESULT]


def _sweep_runtime(tmp_path):
    rt = AgentRuntime(_make_cfg())
    sk = "special:mask33"
    rt.create_conversation("Coder", sk, str(tmp_path), defer_prompt_build=True)
    conv = rt.get_conversation(sk)
    conv.get_token_estimate = lambda: 50_000  # type: ignore[method-assign]
    return rt, sk, conv


def test_sweep_never_masks_current_turn_results(tmp_path):
    rt, sk, conv = _sweep_runtime(tmp_path)
    conv.add_user_message("current")
    for i in range(7):
        _big(conv, f"c{i}", f"f{i}.py")
    before = _bodies(conv)
    with patch("agent.llm.jev_client.classify_mask_candidates", return_value={}):
        rt._maybe_mask_observations(sk, conv)
    assert _bodies(conv) == before


def test_sweep_masks_previous_turn_after_new_user_message(tmp_path):
    rt, sk, conv = _sweep_runtime(tmp_path)
    conv.add_user_message("old")
    _big(conv, "prior", "old.py")
    conv.add_user_message("new")
    current = [_big(conv, f"c{i}", f"new{i}.py") for i in range(4)]
    with patch("agent.llm.jev_client.classify_mask_candidates", return_value={}):
        rt._maybe_mask_observations(sk, conv)
    bodies = _bodies(conv)
    assert bodies[0].startswith(_MASK_PREFIX)
    assert bodies[1:] == current


def test_jev_failure_keeps_all_in_sweep(tmp_path, caplog):
    rt, sk, conv = _sweep_runtime(tmp_path)
    conv.add_user_message("old")
    for i in range(4):
        _big(conv, f"p{i}", f"old{i}.py")
    conv.add_user_message("new")
    before = _bodies(conv)
    with caplog.at_level(logging.WARNING, logger="agent.runtime"):
        with patch("agent.llm.jev_client.classify_mask_candidates", return_value=None):
            rt._maybe_mask_observations(sk, conv)
    assert _bodies(conv) == before
    assert any(
        "JEV batch failed" in rec.message and "keeping all 1 candidates" in rec.message
        for rec in caplog.records
    )


def test_panic_prune_still_masks_all_on_none():
    conv = Conversation(agent_name="Coder", model="gpt-4o")
    conv.add_user_message("old")
    _big(conv, "p0", "old0.py")
    _big(conv, "p1", "old1.py")
    conv.add_user_message("new")
    current = _big(conv, "c0", "new.py")
    n = DefaultContextStrategy().panic_prune(conv)
    bodies = _bodies(conv)
    assert n == 2
    assert all(body.startswith(_MASK_PREFIX) for body in bodies[:2])
    assert bodies[2] == current


def test_stub_carries_recovery_hint():
    conv = Conversation(agent_name="Coder", model="gpt-4o")
    _big(conv, "c0", "a.py")
    n = DefaultContextStrategy().mask_old_observations(conv, keep_last=0)
    assert n == 1
    body = _bodies(conv)[0]
    assert body.startswith(_MASK_PREFIX)
    assert body.endswith(_HINT)


def test_no_user_message_sweep_is_noop(tmp_path):
    rt, sk, conv = _sweep_runtime(tmp_path)
    for i in range(4):
        _big(conv, f"c{i}", f"f{i}.py")
    before = _bodies(conv)
    with patch("agent.llm.jev_client.classify_mask_candidates", return_value={}):
        rt._maybe_mask_observations(sk, conv)
    assert _bodies(conv) == before
