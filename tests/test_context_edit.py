"""Surgical context edits, and writing the live conversation back to disk."""

import json
from types import SimpleNamespace

from models.conversation import Conversation, Message, MessageRole, ToolCall

from agent.context_edit import (
    outline_messages,
    parse_selectors,
    persist_conversation,
    plan_clear_before_open_turn,
    plan_context_edit,
    snapshot_messages,
)


def _user(text):
    return Message(role=MessageRole.USER, content=text)


def _call(call_id, name="read_file", **arguments):
    return Message(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=[ToolCall(call_id=call_id, tool_name=name, arguments=arguments or {"path": "a.py"})],
    )


def _result(call_id, text):
    return Message(role=MessageRole.TOOL_RESULT, content=text, tool_call_id=call_id)


def _history():
    """A finished tool exchange, then the open turn with an in-flight call."""
    return [
        _user("use sqlite"),
        _call("c1"),
        _result("c1", "x" * 400),
        _user("trim your context"),
        _call("c2", name="agent_context", agent="self"),
    ]


def test_drop_expands_to_the_tool_group_and_spares_the_open_turn():
    messages = _history()
    remove, error = plan_context_edit(messages, drop=[2], protect_open_turn=True)
    assert error is None
    assert remove == {1, 2}
    assert [m.content for m in messages][0] == "use sqlite"


def test_drop_refuses_the_in_flight_tool_call():
    messages = _history()
    remove, error = plan_context_edit(messages, drop=[4], protect_open_turn=True)
    assert remove == set()
    assert error is not None
    assert "in-flight" in error
    assert len(messages) == 5


def test_drop_can_remove_the_current_request():
    messages = _history()
    remove, error = plan_context_edit(messages, drop=[3], protect_open_turn=True)
    assert error is None
    assert remove == {3}


def test_keep_retains_the_named_messages_and_the_in_flight_call():
    messages = _history()
    remove, error = plan_context_edit(messages, keep=[0], protect_open_turn=True)
    assert error is None
    assert remove == {1, 2, 3}


def test_keep_pulls_in_the_rest_of_a_tool_group():
    messages = _history()
    remove, error = plan_context_edit(messages, keep=[2], protect_open_turn=True)
    assert error is None
    assert remove == {0, 3}


def test_index_outside_the_transcript_changes_nothing():
    messages = _history()
    remove, error = plan_context_edit(messages, drop=[9], protect_open_turn=True)
    assert remove == set()
    assert error == "index 9 is outside 0-4"


def test_clear_drops_history_before_the_open_turn():
    messages = _history()
    assert plan_clear_before_open_turn(messages) == {0, 1, 2}


def test_a_finished_tool_group_can_leave_while_the_running_call_stays():
    messages = [
        _call("c1"),
        _user("still working"),
        _result("c1", "partial"),
        _call("c2", name="agent_context", agent="self"),
    ]
    assert plan_clear_before_open_turn(messages) == {0, 2}
    remove, error = plan_context_edit(messages, drop=[3], protect_open_turn=True)
    assert remove == set()
    assert "in-flight" in error


def test_outline_shows_size_group_and_open_turn():
    text = outline_messages(_history(), protect_open_turn=True)
    assert "5 messages" in text
    assert "group 1-2" in text
    assert "[in flight]" in text
    assert "[request]" in text
    assert "In-flight tool call is 4" in text
    assert "read_file" in text


def test_parse_selectors_accepts_ints_and_ranges():
    indexes, error = parse_selectors(["1", 4, "6-8"])
    assert error is None
    assert indexes == [1, 4, 6, 7, 8]
    indexes, error = parse_selectors("0, 2, 4-5")
    assert error is None
    assert indexes == [0, 2, 4, 5]
    _indexes, error = parse_selectors("8-3")
    assert error == "range 8-3 is backwards"
    _indexes, error = parse_selectors(True)
    assert error is not None


def test_snapshot_omits_an_incomplete_tool_call_and_its_partial_results():
    messages = [
        _user("keep"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ToolCall(call_id="c1", tool_name="read_file", arguments={"path": "a.py"}),
                ToolCall(call_id="c2", tool_name="agent_context", arguments={"agent": "self"}),
            ],
        ),
        _result("c1", "file body"),
    ]
    snapshot = snapshot_messages(messages)
    assert [m.content for m in snapshot] == ["keep"]
    assert len(messages) == 3


def test_snapshot_keeps_a_finished_tool_exchange():
    messages = [_user("keep"), _call("c1"), _result("c1", "file body")]
    assert snapshot_messages(messages) is messages


def test_self_edit_writes_the_in_memory_conversation_back(tmp_path, monkeypatch):
    from ui.handlers.agent_runtime_handler import AgentRuntimeHandler

    monkeypatch.setattr("utils.config.get_config_dir", lambda: str(tmp_path))
    conv = Conversation(agent_name="Supervisor", messages=_history())
    live = conv.messages
    rt = SimpleNamespace(get_conversation=lambda sk: conv, is_loop_active=lambda sk: True)
    handler = AgentRuntimeHandler.__new__(AgentRuntimeHandler)
    handler._agents = {
        "agent:supervisor": SimpleNamespace(display_name="Supervisor"),
    }
    handler._runtimes = {"Supervisor": rt}

    result = handler.edit_conversation(
        "agent:supervisor",
        {"drop": [2], "keep": None},
        protect_open_turn=True,
    )

    assert result["ok"] is True
    assert result["persisted"] is True
    assert result["messages_removed"] == 2
    assert conv.messages is live
    assert [m.content for m in conv.messages] == ["use sqlite", "trim your context", ""]
    assert conv.messages[-1].tool_calls[0].tool_name == "agent_context"

    path = tmp_path / "conversations" / "agent:supervisor.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert [m["content"] for m in saved["messages"]] == ["use sqlite", "trim your context"]


def test_persist_restores_the_live_list_when_the_write_fails(tmp_path, monkeypatch):
    conv = Conversation(agent_name="Supervisor", messages=_history())
    live = list(conv.messages)

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("agent.persistence.save_conversation_to_disk", _boom)
    try:
        persist_conversation(conv, "agent:supervisor")
        raised = False
    except OSError:
        raised = True
    assert raised is True
    assert [m.content for m in conv.messages] == [m.content for m in live]
    assert conv.messages[-1].tool_calls
