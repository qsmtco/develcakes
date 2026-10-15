"""SPEC-30 Stage 3: heavy compact at the provider threshold."""
from __future__ import annotations

from agent.context_strategy import DefaultContextStrategy
from agent.runtime import AgentRuntime
from models.conversation import Conversation, MessageRole, ToolCall
from tests.test_agent_runtime import _make_cfg


def _body() -> str:
    return "B" * 520


def _tool(conv: Conversation, call_id: str, path: str) -> None:
    body = _body()
    tc = ToolCall(
        call_id=call_id,
        tool_name="read_file",
        arguments={"path": path, "offset": 0},
        result=body,
    )
    conv.add_assistant_message("go", [tc])
    conv.add_tool_result(call_id, body)


def _two_turns() -> Conversation:
    conv = Conversation(agent_name="Coder", model="gpt-4o", system_prompt="")
    conv.add_user_message("first")
    _tool(conv, "c0", "old.py")
    conv.add_user_message("second")
    _tool(conv, "c1", "new.py")
    return conv


def test_panic_stubs_only_before_the_last_user_message():
    conv = _two_turns()
    n = DefaultContextStrategy().panic_prune(conv, protect_paths=())
    assert n == 1
    tools = [m.content for m in conv.messages if m.role == MessageRole.TOOL_RESULT]
    assert tools[0].startswith("[compacted \u2014 read_file output, ")
    assert tools[1] == _body()


def test_no_user_message_stubs_nothing():
    conv = Conversation(agent_name="Coder", model="gpt-4o")
    _tool(conv, "c0", "old.py")
    assert DefaultContextStrategy().panic_prune(conv) == 0
    assert conv.messages[-1].content == _body()


def test_protected_path_survives_panic_and_compact():
    conv = _two_turns()
    conv.add_assistant_message("note")
    strategy = DefaultContextStrategy()
    strategy.panic_prune(conv, protect_paths=("old.py",))
    tools = [m.content for m in conv.messages if m.role == MessageRole.TOOL_RESULT]
    assert tools[0] == _body()
    strategy.compact(
        conv,
        token_budget=1,
        protect_paths=("old.py",),
        preserve_current_turn=True,
    )
    assert any(m.content == "second" for m in conv.messages if m.role == MessageRole.USER)
    assert any(
        m.content == _body() and m.tool_call_id == "c1"
        for m in conv.messages
        if m.role == MessageRole.TOOL_RESULT
    )
    assert any(
        m.content == _body() and m.tool_call_id == "c0"
        for m in conv.messages
        if m.role == MessageRole.TOOL_RESULT
    )


def test_panic_twice_frees_nothing_more():
    conv = _two_turns()
    strategy = DefaultContextStrategy()
    assert strategy.panic_prune(conv) == 1
    assert strategy.panic_prune(conv) == 0


def test_under_threshold_does_not_panic(tmp_path, monkeypatch):
    rt = AgentRuntime(_make_cfg())
    sk = "special:panic"
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


def test_over_threshold_lands_on_the_goal(tmp_path, monkeypatch):
    rt = AgentRuntime(_make_cfg())
    sk = "special:panic"
    rt.create_conversation("Coder", sk, str(tmp_path), defer_prompt_build=True)
    conv = rt.get_conversation(sk)
    conv.get_token_estimate = lambda: 10_000_000  # type: ignore[method-assign]
    called = {}

    def panic(_conv, protect_paths=()):
        called["protect"] = protect_paths
        return 0

    def compact(_conv, budget, **kwargs):
        called["budget"] = budget
        called["preserve"] = kwargs.get("preserve_current_turn")
        called["paths"] = kwargs.get("protect_paths")

    monkeypatch.setattr(rt._context_strategy, "panic_prune", panic)
    monkeypatch.setattr(rt._context_strategy, "compact", compact)
    soft, hard = rt._compute_compaction_threshold(conv)
    rt._compact_before_llm(sk, conv, soft, hard)
    assert called["budget"] == rt._compute_landing_goal(hard)
    assert called["budget"] != soft
    assert called["preserve"] is True


def test_assistant_text_in_the_current_turn_is_not_trimmed():
    conv = Conversation(agent_name="Coder", model="gpt-4o", system_prompt="S" * 4000)
    conv.add_user_message("old request")
    conv.add_assistant_message("old answer " + ("A" * 800))
    conv.add_user_message("current request")
    conv.add_assistant_message("current answer " + ("C" * 800))
    DefaultContextStrategy().compact(
        conv,
        token_budget=1,
        preserve_current_turn=True,
    )
    texts = [m.content for m in conv.messages]
    assert any(t.startswith("current answer") for t in texts)
    assert any(t == "current request" for t in texts)


def test_over_ceiling_stubs_previous_turn_and_keeps_the_current_turn(tmp_path):
    hot = tmp_path / "hot.py"
    hot.write_text("pinned\n")
    rt = AgentRuntime(_make_cfg())
    sk = "special:panic"
    rt.create_conversation("Coder", sk, str(tmp_path), defer_prompt_build=True)
    conv = rt.get_conversation(sk)
    rt._note_tool_path(sk, conv, "read_file", {"path": "hot.py"})
    conv.add_user_message("old request")
    huge = "U" * 800_000
    pinned = "P" * 800
    current = "C" * 800
    for call_id, path, body in (("old", "old.py", huge), ("hot", "hot.py", pinned)):
        tc = ToolCall(
            call_id=call_id,
            tool_name="read_file",
            arguments={"path": path, "offset": 0},
            result=body,
        )
        conv.add_assistant_message("go", [tc])
        conv.add_tool_result(call_id, body)
    conv.add_user_message("current request")
    tc = ToolCall(
        call_id="now",
        tool_name="read_file",
        arguments={"path": "now.py", "offset": 0},
        result=current,
    )
    conv.add_assistant_message("go", [tc])
    conv.add_tool_result("now", current)
    soft, _hard = rt._compute_compaction_threshold(conv)
    assert conv.get_token_estimate() > soft
    rt._compact_before_llm(sk, conv, soft, _hard)
    bodies = {
        m.tool_call_id: m.content
        for m in conv.messages
        if m.role == MessageRole.TOOL_RESULT
    }
    assert bodies["now"] == current
    assert bodies["hot"] == pinned
    assert bodies["old"].startswith("[compacted \u2014")
    assert any(
        m.content == "current request"
        for m in conv.messages
        if m.role == MessageRole.USER
    )
