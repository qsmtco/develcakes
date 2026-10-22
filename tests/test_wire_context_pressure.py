"""SPEC-WIRE-CONTEXT-PRESSURE §2.7: wire-pressure + single-flight queue tests.

No GTK. Uses AgentRuntime the same way tests/test_panic_prune.py does
(_make_cfg, create_conversation, monkeypatched compact / panic_prune).
"""
from __future__ import annotations

from agent.runtime import AgentRuntime
from models.conversation import Conversation, MessageRole, ToolCall
from tests.test_agent_runtime import _make_cfg


def _packet(conv, wire_from: int, goal: str = "small goal") -> dict:
    return {
        "goal": goal,
        "path": "a.py",
        "hunk": "hunk",
        "checks": [],
        "after": len(conv.messages),
        "wire_from": wire_from,  # index after the goal user message
        "hot": (),
    }


def test_compiled_wire_under_ceiling_does_not_panic(tmp_path, monkeypatch):
    rt = AgentRuntime(_make_cfg())
    sk = "agent:wire"
    rt.create_conversation("Coder", sk, str(tmp_path), defer_prompt_build=True)
    conv = rt.get_conversation(sk)
    conv.add_user_message("old request")
    conv.add_assistant_message("old answer " + ("A" * 600))
    conv.add_user_message("goal")  # the write turn's user message
    wire_from = len(conv.messages)
    tc = ToolCall(call_id="c0", tool_name="read_file", arguments={"path": "a.py"})
    conv.add_assistant_message("[calling tools]", [tc])
    conv.add_tool_result("c0", "B" * 400)
    rt._compile[sk] = _packet(conv, wire_from)
    # The transcript is huge; the compiled wire is small.
    conv.get_token_estimate = lambda: 10_000_000  # type: ignore[method-assign]

    called = {"panic": 0, "budgets": [], "preserve": []}

    def panic(*_a, **_k):
        called["panic"] += 1
        return 0

    def compact(_conv, budget, **kwargs):
        called["budgets"].append(budget)
        called["preserve"].append(kwargs.get("preserve_current_turn", False))

    monkeypatch.setattr(rt._context_strategy, "panic_prune", panic)
    monkeypatch.setattr(rt._context_strategy, "compact", compact)

    soft, hard = rt._compute_compaction_threshold(conv)
    rt._compact_before_llm(sk, conv, soft, hard)

    assert called["panic"] == 0
    assert called["budgets"] == [10_000_000]
    assert called["preserve"] == [False]
    bodies = [m.content for m in conv.messages if m.role == MessageRole.USER]
    assert "old request" in bodies


def test_compiled_wire_over_ceiling_still_panics(tmp_path, monkeypatch):
    rt = AgentRuntime(_make_cfg())
    sk = "agent:wire"
    rt.create_conversation("Coder", sk, str(tmp_path), defer_prompt_build=True)
    conv = rt.get_conversation(sk)
    conv.add_user_message("goal")
    wire_from = len(conv.messages)
    conv.add_tool_result("c0", "B" * 400)
    # A huge goal makes the compiled wire count exceed soft_ceiling.
    rt._compile[sk] = _packet(conv, wire_from, goal="G" * 1_000_000)

    called = {}

    def panic(_conv, protect_paths=()):
        called["protect"] = protect_paths
        return 0

    def compact(_conv, budget, **kwargs):
        called["budget"] = budget
        called["preserve"] = kwargs.get("preserve_current_turn")

    monkeypatch.setattr(rt._context_strategy, "panic_prune", panic)
    monkeypatch.setattr(rt._context_strategy, "compact", compact)

    soft, hard = rt._compute_compaction_threshold(conv)
    rt._compact_before_llm(sk, conv, soft, hard)

    assert called["budget"] == rt._compute_landing_goal(hard)
    assert called["budget"] != soft
    assert called["preserve"] is True
    assert "protect" in called


def test_plain_under_ceiling_is_unchanged(tmp_path, monkeypatch):
    rt = AgentRuntime(_make_cfg())
    sk = "agent:wire"
    rt.create_conversation("Coder", sk, str(tmp_path), defer_prompt_build=True)
    conv = rt.get_conversation(sk)
    conv.get_token_estimate = lambda: 1_000  # type: ignore[method-assign]

    called = {"panic": 0, "budget": None, "preserve": None}

    def panic(*_a, **_k):
        called["panic"] += 1
        return 0

    def compact(_conv, budget, **kwargs):
        called["budget"] = budget
        called["preserve"] = kwargs.get("preserve_current_turn", False)

    monkeypatch.setattr(rt._context_strategy, "panic_prune", panic)
    monkeypatch.setattr(rt._context_strategy, "compact", compact)

    soft, hard = rt._compute_compaction_threshold(conv)
    rt._compact_before_llm(sk, conv, soft, hard)

    assert called["panic"] == 0
    assert called["budget"] == soft
    assert called["preserve"] is False


def test_begin_send_while_active_queues():
    rt = AgentRuntime(_make_cfg())
    sk = "agent:wire"
    assert rt.begin_send(sk, "first") is True
    assert rt.is_loop_active(sk)
    assert rt.begin_send(sk, "second") is False
    assert rt._inbox[sk] == ["second"]
    assert rt.is_loop_active(sk)
    rt._finish_loop(sk)
    assert not rt.is_loop_active(sk)
    assert rt.take_stranded(sk) == ["second"]
    assert rt.take_stranded(sk) == []


def test_drain_appends_in_order():
    rt = AgentRuntime(_make_cfg())
    sk = "agent:wire"
    rt.begin_send(sk, "first")
    rt._inbox[sk] = ["a", "b"]
    conv = Conversation(agent_name="Coder", model="openai/gpt-4o")
    n = rt._drain_inbox(sk, conv)
    assert n == 2
    texts = [m.content for m in conv.messages]
    assert texts == ["a", "b"]
    assert rt._drain_inbox(sk, conv) == 0


def test_generation_drops_a_stale_store():
    conv = Conversation(
        agent_name="Coder",
        model="openai/gpt-4o",
        system_prompt="S" * 400,
    )
    conv.add_user_message("first")
    conv.add_tool_result("c0", "T" * 10_000)
    before = conv.get_token_estimate()
    # Replace the tool-result body WITHOUT invalidating — the cache key
    # (len, hash(system_prompt), hash(task_card)) is unchanged, so the
    # count sticks (that is the bug this generation fixes).
    conv.messages[-1].content = "stub"
    stale = conv.get_token_estimate()
    assert stale == before
    # Proper invalidation makes the next count see the stub.
    conv.invalidate_token_estimate()
    fresh = conv.get_token_estimate()
    expected = Conversation(
        agent_name="Coder",
        model="openai/gpt-4o",
        system_prompt="S" * 400,
    )
    expected.add_user_message("first")
    expected.add_tool_result("c0", "stub")
    expected_total = expected.get_token_estimate()
    assert abs(fresh - expected_total) <= 2
    assert fresh < before
