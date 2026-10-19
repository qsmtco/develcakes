"""SPEC-32: re-read returns the body when the live read_file copy is gone."""
from __future__ import annotations

import os

from agent.context_strategy import DefaultContextStrategy
from agent.runtime import AgentRuntime
from models.conversation import Conversation, MessageRole, ToolCall
from tests.test_agent_runtime import _make_cfg


def _runtime(project: str):
    rt = AgentRuntime(_make_cfg())
    sk = "special:coder"
    rt.create_conversation("Coder", sk, project, defer_prompt_build=True)
    return rt, sk, rt.get_conversation(sk)


def _tool_bodies(conv):
    return [m.content for m in conv.messages if m.role == MessageRole.TOOL_RESULT]


def _tool_indexes(conv):
    return [
        i for i, m in enumerate(conv.messages)
        if m.role == MessageRole.TOOL_RESULT
    ]


def _read(rt, sk, conv, call_id: str, args: dict, iteration: int = 1):
    assert rt._execute_tool_batch(
        sk, None, conv, "reading", [(call_id, "read_file", args)], iteration,
    ) is False


def _notice(path: str, offset, limit) -> str:
    return (
        f"[unchanged \u2014 read_file {path} offset={offset or 0} limit={limit}; "
        "the body is already in this conversation. "
        "Do not read it again and do not print it with a shell command.]"
    )


def _plant_masked(conv, index: int, text: str | None = None) -> None:
    conv.messages[index].content = text or (
        "[masked \u2014 read_file a.txt offset=0 (12 chars removed)]"
    )


def _assert_fingerprint(rt, sk, path: str, offset, limit, file_path: str):
    st = os.stat(file_path)
    key = (path, offset or 0, limit)
    assert rt._read_fingerprints[sk][key] == (st.st_mtime_ns, st.st_size)


def test_limit_reader_keeps_none_and_zero():
    conv = Conversation(agent_name="Coder")
    conv.add_tool_result("orphan", "no parent")
    strategy = DefaultContextStrategy()
    assert strategy._tool_result_limit(conv, 0) is None

    omitted = ToolCall(call_id="c0", tool_name="read_file", arguments={"path": "a.txt"})
    explicit = ToolCall(
        call_id="c1", tool_name="read_file",
        arguments={"path": "a.txt", "limit": None},
    )
    zero = ToolCall(
        call_id="c2", tool_name="read_file",
        arguments={"path": "a.txt", "limit": 0},
    )
    junk = ToolCall(call_id="c3", tool_name="read_file", arguments="not-a-dict")
    conv.add_assistant_message("go", [omitted, explicit, zero, junk])
    conv.add_tool_result("c0", "body")
    conv.add_tool_result("c1", "body")
    conv.add_tool_result("c2", "body")
    conv.add_tool_result("c3", "body")
    indexes = _tool_indexes(conv)
    assert strategy._tool_result_limit(conv, indexes[1]) is None
    assert strategy._tool_result_limit(conv, indexes[2]) is None
    assert strategy._tool_result_limit(conv, indexes[3]) == 0
    assert strategy._tool_result_limit(conv, indexes[4]) is None
    name, path, offset, parent = strategy._tool_result_meta(conv, indexes[1])
    assert (name, path, offset) == ("read_file", "a.txt", 0)
    assert parent is conv.messages[1]


def test_masked_reread_returns_full_body(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello world\n")
    rt, sk, conv = _runtime(str(tmp_path))
    _read(rt, sk, conv, "c1", {"path": "a.txt"})
    _plant_masked(conv, _tool_indexes(conv)[0])
    assert _tool_bodies(conv)[0].startswith("[masked \u2014")
    _read(rt, sk, conv, "c2", {"path": "a.txt"}, 2)
    assert _tool_bodies(conv)[1] == "hello world\n"
    _assert_fingerprint(rt, sk, "a.txt", None, None, str(target))


def test_compacted_reread_returns_full_body(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello world\n")
    rt, sk, conv = _runtime(str(tmp_path))
    _read(rt, sk, conv, "c1", {"path": "a.txt"})
    conv.messages[_tool_indexes(conv)[0]].content = (
        "[compacted \u2014 read_file output, 12 chars removed]"
    )
    _read(rt, sk, conv, "c2", {"path": "a.txt"}, 2)
    assert _tool_bodies(conv)[1] == "hello world\n"


def test_summarize_stub_returns_full_body_not_tail(tmp_path):
    body = "x" * 600
    target = tmp_path / "a.txt"
    target.write_text(body)
    rt, sk, conv = _runtime(str(tmp_path))
    _read(rt, sk, conv, "c1", {"path": "a.txt"})
    idx = _tool_indexes(conv)[0]
    original_body = conv.messages[idx].content
    _plant_masked(
        conv, idx,
        "[masked \u2014 read_file a.txt offset=0 (600 chars removed)]\n" + original_body[:200],
    )
    stub = conv.messages[idx].content
    assert stub.startswith("[masked \u2014")
    assert stub.split("\n", 1)[1] == body[:200]
    assert stub != body
    _read(rt, sk, conv, "c2", {"path": "a.txt"}, 2)
    assert _tool_bodies(conv)[1] == body


def test_live_body_second_read_is_unchanged_notice(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello world\n")
    rt, sk, conv = _runtime(str(tmp_path))
    _read(rt, sk, conv, "c1", {"path": "a.txt"})
    _read(rt, sk, conv, "c2", {"path": "a.txt"}, 2)
    assert _tool_bodies(conv)[0] == "hello world\n"
    assert _tool_bodies(conv)[1] == _notice("a.txt", None, None)


def test_other_window_does_not_decide(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello world\n")
    rt, sk, conv = _runtime(str(tmp_path))
    _read(rt, sk, conv, "c1", {"path": "a.txt"})
    _read(rt, sk, conv, "c2", {"path": "a.txt", "limit": 5}, 2)
    _plant_masked(conv, _tool_indexes(conv)[0])
    _read(rt, sk, conv, "c3", {"path": "a.txt"}, 3)
    assert _tool_bodies(conv)[2] == "hello world\n"

    rt2, sk2, conv2 = _runtime(str(tmp_path))
    _read(rt2, sk2, conv2, "c1", {"path": "a.txt"})
    _read(rt2, sk2, conv2, "c2", {"path": "a.txt", "offset": 6}, 2)
    _plant_masked(conv2, _tool_indexes(conv2)[1])
    _read(rt2, sk2, conv2, "c3", {"path": "a.txt"}, 3)
    assert _tool_bodies(conv2)[2] == _notice("a.txt", None, None)


def test_notice_does_not_shadow_masked_body(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello world\n")
    rt, sk, conv = _runtime(str(tmp_path))
    _read(rt, sk, conv, "c1", {"path": "a.txt"})
    _read(rt, sk, conv, "c2", {"path": "a.txt"}, 2)
    assert _tool_bodies(conv)[1] == _notice("a.txt", None, None)
    _plant_masked(conv, _tool_indexes(conv)[0])
    _read(rt, sk, conv, "c3", {"path": "a.txt"}, 3)
    assert _tool_bodies(conv)[2] == "hello world\n"


def test_recovery_then_unchanged_then_remask_keeps_fingerprint(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello world\n")
    rt, sk, conv = _runtime(str(tmp_path))
    _read(rt, sk, conv, "c1", {"path": "a.txt"})
    _plant_masked(conv, _tool_indexes(conv)[0])
    _read(rt, sk, conv, "c2", {"path": "a.txt"}, 2)
    assert _tool_bodies(conv)[1] == "hello world\n"
    _assert_fingerprint(rt, sk, "a.txt", None, None, str(target))
    _read(rt, sk, conv, "c3", {"path": "a.txt"}, 3)
    assert _tool_bodies(conv)[2] == _notice("a.txt", None, None)
    _plant_masked(conv, _tool_indexes(conv)[1])
    _read(rt, sk, conv, "c4", {"path": "a.txt"}, 4)
    assert _tool_bodies(conv)[3] == "hello world\n"
    _assert_fingerprint(rt, sk, "a.txt", None, None, str(target))


def test_cleared_transcript_reread_returns_body(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello world\n")
    rt, sk, conv = _runtime(str(tmp_path))
    _read(rt, sk, conv, "c1", {"path": "a.txt"})
    conv.messages.clear()
    text = rt._dedup_read_output(sk, conv, {"path": "a.txt"}, "hello world\n")
    assert text == "hello world\n"
    _assert_fingerprint(rt, sk, "a.txt", None, None, str(target))


def test_walk_log_failure_still_returns_body(tmp_path, monkeypatch):
    """A walk exception is swallowed; the body is served, not a notice.

    SPEC-40 moved the read_file walk off `_tool_result_limit` (which is
    consulted only for the exec_command key) onto `_read_key_for_result`,
    so this fault is injected there.
    """
    target = tmp_path / "a.txt"
    target.write_text("hello world\n")
    rt, sk, conv = _runtime(str(tmp_path))
    _read(rt, sk, conv, "c1", {"path": "a.txt"})

    def _boom(*args, **kwargs):
        raise RuntimeError("logger broke")

    def _key_boom(conv_arg, idx):
        raise RuntimeError("walk broke")

    monkeypatch.setattr("agent.runtime.logger.debug", _boom)
    rt._read_key_for_result = _key_boom
    text = rt._dedup_read_output(sk, conv, {"path": "a.txt"}, "hello world\n")
    assert text == "hello world\n"


def test_walk_failure_returns_body(tmp_path):
    """Same contract without the logger fault: the walk failing is 'unknown',
    which must resolve to serving the body."""
    target = tmp_path / "a.txt"
    target.write_text("hello world\n")
    rt, sk, conv = _runtime(str(tmp_path))
    _read(rt, sk, conv, "c1", {"path": "a.txt"})

    def _boom(conv_arg, idx):
        raise RuntimeError("walk broke")

    rt._read_key_for_result = _boom
    text = rt._dedup_read_output(sk, conv, {"path": "a.txt"}, "hello world\n")
    assert text == "hello world\n"


def test_write_file_result_does_not_hide_masked_read(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello world\n")
    rt, sk, conv = _runtime(str(tmp_path))
    _read(rt, sk, conv, "c1", {"path": "a.txt"})
    _plant_masked(conv, _tool_indexes(conv)[0])
    tc = ToolCall(
        call_id="w1",
        tool_name="write_file",
        arguments={"path": "a.txt", "content": "hello world\n"},
    )
    conv.add_assistant_message("write", [tc])
    conv.add_tool_result("w1", "wrote a.txt")
    text = rt._dedup_read_output(sk, conv, {"path": "a.txt"}, "hello world\n")
    assert text == "hello world\n"


def test_newer_write_does_not_replace_a_live_read_body(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello world\n")
    rt, sk, conv = _runtime(str(tmp_path))
    _read(rt, sk, conv, "c1", {"path": "a.txt"})
    tc = ToolCall(
        call_id="w1",
        tool_name="list_files",
        arguments={"path": "a.txt"},
    )
    conv.add_assistant_message("list", [tc])
    conv.add_tool_result("w1", "a.txt\n")
    text = rt._dedup_read_output(sk, conv, {"path": "a.txt"}, "hello world\n")
    assert text == _notice("a.txt", None, None)
