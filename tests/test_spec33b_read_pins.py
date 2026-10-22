"""SPEC-33b item 5: pin a file on the first successful read."""
from __future__ import annotations

import os

from agent.context_strategy import DefaultContextStrategy
from agent.runtime import AgentRuntime
from models.conversation import Conversation, MessageRole, ToolCall
from tests.test_agent_runtime import _make_cfg


def _runtime(project: str):
    rt = AgentRuntime(_make_cfg())
    sk = "agent:pin"
    rt.create_conversation("Coder", sk, project, defer_prompt_build=True)
    return rt, sk, rt.get_conversation(sk)


def test_single_read_pins(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("hello\n")
    rt, sk, conv = _runtime(str(tmp_path))
    rt._note_tool_path(sk, conv, "read_file", {"path": "a.py"})
    assert "a.py" in rt._protect_paths(sk, conv)
    st = os.stat(target)
    assert rt._hot_pins[sk]["a.py"] == (st.st_mtime_ns, st.st_size)
    assert rt._read_counts[sk]["a.py"] == 1


def test_change_between_reads_repins_at_count_1(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("v1\n")
    rt, sk, conv = _runtime(str(tmp_path))
    rt._note_tool_path(sk, conv, "read_file", {"path": "a.py"})
    target.write_text("v2-changed\n")
    rt._note_tool_path(sk, conv, "read_file", {"path": "a.py"})
    assert rt._read_counts[sk]["a.py"] == 1
    st = os.stat(target)
    assert rt._hot_pins[sk]["a.py"] == (st.st_mtime_ns, st.st_size)
    assert "a.py" in rt._protect_paths(sk, conv)


def test_empty_path_missing_project_and_stat_failure_skip(tmp_path):
    rt, sk, conv = _runtime(str(tmp_path))
    rt._note_tool_path(sk, conv, "read_file", {"path": "missing.py"})
    rt._note_tool_path(sk, conv, "read_file", {"path": ""})
    rt._note_tool_path(sk, conv, "read_file", "nope")  # type: ignore[arg-type]
    assert rt._read_counts.get(sk, {}) == {}
    assert rt._hot_pins.get(sk, {}) == {}

    conv.project_path = ""
    rt._note_tool_path(sk, conv, "read_file", {"path": "a.py"})
    assert rt._read_counts.get(sk, {}) == {}


def test_first_read_is_protected_from_panic(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("pinned\n")
    rt, sk, conv = _runtime(str(tmp_path))
    rt._note_tool_path(sk, conv, "read_file", {"path": "a.py"})
    conv.add_user_message("old")
    body = "P" * 520
    other = "Q" * 520
    for call_id, path, text in (("c0", "a.py", body), ("c1", "b.py", other)):
        tc = ToolCall(
            call_id=call_id,
            tool_name="read_file",
            arguments={"path": path, "offset": 0},
            result=text,
        )
        conv.add_assistant_message("go", [tc])
        conv.add_tool_result(call_id, text)
    conv.add_user_message("new")
    protect = rt._protect_paths(sk, conv)
    DefaultContextStrategy().panic_prune(conv, protect_paths=protect)
    bodies = [m.content for m in conv.messages if m.role == MessageRole.TOOL_RESULT]
    assert bodies[0] == body
    assert bodies[1].startswith("[compacted \u2014")


def test_unchanged_reread_keeps_the_same_pin(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("hello\n")
    rt, sk, conv = _runtime(str(tmp_path))
    rt._note_tool_path(sk, conv, "read_file", {"path": "a.py"})
    stored = rt._hot_pins[sk]["a.py"]
    rt._note_tool_path(sk, conv, "read_file", {"path": "a.py"})
    assert rt._hot_pins[sk]["a.py"] == stored
    assert rt._read_counts[sk]["a.py"] == 1
