"""SPEC-33b item 3: exec_command retention parity."""
from __future__ import annotations

from agent.context_strategy import DefaultContextStrategy
from agent.runtime import AgentRuntime
from agent.tools import set_approval_callback
from models.conversation import Conversation, MessageRole, ToolCall
from tests.test_agent_runtime import _make_cfg

_EXEC_KEY = ("", 0, None)


def _runtime(project: str):
    rt = AgentRuntime(_make_cfg())
    sk = "agent:coder"
    rt.create_conversation("Coder", sk, project, defer_prompt_build=True)
    return rt, sk, rt.get_conversation(sk)


def _tool_bodies(conv):
    return [m.content for m in conv.messages if m.role == MessageRole.TOOL_RESULT]


def _tool_indexes(conv):
    return [
        i for i, m in enumerate(conv.messages)
        if m.role == MessageRole.TOOL_RESULT
    ]


def _exec(conv: Conversation, call_id: str, body: str, command: str = "echo hi") -> None:
    tc = ToolCall(
        call_id=call_id,
        tool_name="exec_command",
        arguments={"command": command},
        result=body,
    )
    conv.add_assistant_message("go", [tc])
    conv.add_tool_result(call_id, body)


def test_tool_result_meta_is_tool_agnostic_for_exec():
    conv = Conversation(agent_name="Coder")
    _exec(conv, "c0", "hello\n")
    name, path, offset, parent = DefaultContextStrategy()._tool_result_meta(
        conv, _tool_indexes(conv)[0],
    )
    assert (name, path, offset) == ("exec_command", "", 0)
    assert parent is conv.messages[0]


def _plant_masked(conv, index: int) -> None:
    conv.messages[index].content = (
        "[masked \u2014 exec_command  offset=0 (6 chars removed)]"
    )


def test_masked_exec_is_dead_and_a_live_one_is_not(tmp_path):
    rt, _sk, conv = _runtime(str(tmp_path))
    _exec(conv, "c0", "first\n")
    assert rt._body_live_in_transcript(conv, "exec_command", _EXEC_KEY) is True
    _plant_masked(conv, _tool_indexes(conv)[0])
    assert rt._body_live_in_transcript(conv, "exec_command", _EXEC_KEY) is False
    _exec(conv, "c1", "second\n")
    assert rt._body_live_in_transcript(conv, "exec_command", _EXEC_KEY) is True


def test_masked_exec_rerun_returns_full_output(tmp_path, monkeypatch):
    rt, sk, conv = _runtime(str(tmp_path))
    monkeypatch.setattr(rt, "_dispatch_approval", lambda *a, **k: True)
    set_approval_callback(lambda *a: True)
    try:
        assert rt._execute_tool_batch(
            sk, None, conv, "run",
            [("c1", "exec_command", {"command": "echo hello-spec33b"})],
            1,
        ) is False
        _plant_masked(conv, _tool_indexes(conv)[0])
        assert rt._body_live_in_transcript(conv, "exec_command", _EXEC_KEY) is False
        assert rt._execute_tool_batch(
            sk, None, conv, "run",
            [("c2", "exec_command", {"command": "echo hello-spec33b"})],
            2,
        ) is False
        body = _tool_bodies(conv)[-1]
        assert "hello-spec33b" in body
        assert not body.startswith("[unchanged")
        assert not body.startswith("[masked")
    finally:
        set_approval_callback(None)


def test_exec_pin_is_dropped_when_stat_fails(tmp_path):
    rt, sk, conv = _runtime(str(tmp_path))
    for _ in range(3):
        rt._note_tool_path(sk, conv, "exec_command", {"command": "echo hi"})
    key = f"<exec:{sk}>"
    assert key in rt._hot_pins[sk]
    protected = rt._protect_paths(sk, conv)
    assert key not in protected
    assert key not in rt._hot_pins.get(sk, {})


def test_panic_stubs_previous_turn_exec(tmp_path):
    rt, sk, conv = _runtime(str(tmp_path))
    conv.add_user_message("old")
    body = "E" * 520
    _exec(conv, "c0", body)
    conv.add_user_message("new")
    _exec(conv, "c1", "kept\n")
    for _ in range(3):
        rt._note_tool_path(sk, conv, "exec_command", {"command": "echo hi"})
    n = DefaultContextStrategy().panic_prune(
        conv, protect_paths=rt._protect_paths(sk, conv),
    )
    bodies = _tool_bodies(conv)
    assert n == 1
    assert bodies[0].startswith("[compacted \u2014 exec_command ")
    assert "chars removed" not in bodies[0]
    assert "re-run to restore" not in bodies[0]
    assert "re-read to restore" not in bodies[0]
    assert bodies[1] == "kept\n"


def test_exec_without_project_or_dict_args_does_not_pin(tmp_path):
    rt, sk, conv = _runtime(str(tmp_path))
    conv.project_path = ""
    rt._note_tool_path(sk, conv, "exec_command", {"command": "echo hi"})
    rt._note_tool_path(sk, conv, "exec_command", "nope")  # type: ignore[arg-type]
    assert rt._read_counts.get(sk, {}) == {}


def test_exec_walk_does_not_treat_a_read_as_the_exec_body(tmp_path):
    rt, _sk, conv = _runtime(str(tmp_path))
    body = "file\n"
    tc = ToolCall(
        call_id="r0",
        tool_name="read_file",
        arguments={"path": "a.py", "offset": 0},
        result=body,
    )
    conv.add_assistant_message("go", [tc])
    conv.add_tool_result("r0", body)
    _exec(conv, "c0", "ran\n")
    _plant_masked(conv, _tool_indexes(conv)[1])
    assert rt._body_live_in_transcript(conv, "exec_command", _EXEC_KEY) is False
    assert rt._key_body_in_transcript(conv, "a.py", 0, None) is True
