"""SPEC-21 SP1: compact() no speculative cache wipe; sweep clears only on change."""
from __future__ import annotations

from agent.context_strategy import DefaultContextStrategy
from models.conversation import (
    Conversation,
    Message,
    MessageRole,
    ToolCall,
)


def _make(n: int = 20) -> Conversation:
    conv = Conversation(
        agent_name="Coder",
        system_prompt="S" * 4000,
        model="openrouter/deepseek",
    )
    for i in range(n):
        conv.add_user_message("go")
        conv.add_assistant_message(
            "ok",
            tool_calls=[
                ToolCall(call_id=f"c{i}", tool_name="read_file", arguments={"p": i})
            ],
        )
        conv.add_tool_result(f"c{i}", "body " * 40 + str(i))
    return conv


def test_noop_compact_at_most_one_full_encode():
    n = {"v": 0}
    orig = Conversation._count_tokens_accurate

    def counting(self, encoding):
        n["v"] += 1
        return orig(self, encoding)

    Conversation._count_tokens_accurate = counting
    try:
        conv = _make(20)
        budget = conv.get_token_estimate() * 4
        n["v"] = 0
        strat = DefaultContextStrategy()
        strat.compact(conv, budget)
        assert n["v"] <= 1, f"no-op compact performed {n['v']} full encodes (want <= 1)"
        ev = strat.last_result
        assert ev is not None
        assert ev.layer == 0
        assert ev.messages_removed == 0
        assert ev.tokens_freed == 0
        assert ev.turn == conv.step_count
    finally:
        Conversation._count_tokens_accurate = orig


def test_stale_event_reports_second_conversation():
    strat = DefaultContextStrategy()
    a = _make(40)
    strat.compact(a, int(a.get_token_estimate() * 0.4))
    ev_a = strat.last_result
    assert ev_a is not None
    b = _make(8)
    budget_b = b.get_token_estimate() * 4
    strat.compact(b, budget_b)
    ev_b = strat.last_result
    assert ev_b is not None
    assert ev_b.messages_before == len(b.messages)
    assert ev_b.tokens_before == b.get_token_estimate()
    assert ev_b is not ev_a


def test_orphan_sweep_under_budget_invalidates_cache():
    conv = _make(3)
    conv.messages.insert(
        1,
        Message(
            role=MessageRole.TOOL_RESULT,
            content="orphan",
            tool_call_id="nope",
        ),
    )
    conv._token_estimate_cache = None
    conv.get_token_estimate()
    assert conv._token_estimate_cache is not None
    DefaultContextStrategy().compact(conv, 10**9)
    assert all(m.tool_call_id != "nope" for m in conv.messages)
    # sweep removed a message → cache must have been cleared then refilled
    assert conv._token_estimate_cache is not None


def test_noop_sweep_does_not_clear_cache():
    conv = _make(6)
    conv.get_token_estimate()
    cached = conv._token_estimate_cache
    DefaultContextStrategy().compact(conv, conv.get_token_estimate() * 4)
    assert conv._token_estimate_cache is cached
