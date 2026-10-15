"""SPEC-23: one tool-message copy, same-turn read dedup, task card."""
from __future__ import annotations

import json
import os
from unittest.mock import patch

from agent.llm.convert import convert_messages_for_anthropic
from agent.runtime import AgentRuntime
from models.conversation import Conversation, MessageRole
from tests.test_agent_runtime import _make_cfg, _resp


def _runtime(project: str):
    rt = AgentRuntime(_make_cfg())
    sk = "special:coder"
    rt.create_conversation("Coder", sk, project, defer_prompt_build=True)
    return rt, sk, rt.get_conversation(sk)


def _tool_bodies(conv):
    return [m.content for m in conv.messages if m.role == MessageRole.TOOL_RESULT]


def test_second_identical_read_is_unchanged_notice(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello world\n")
    rt, sk, conv = _runtime(str(tmp_path))
    first = [("c1", "read_file", {"path": "a.txt"})]
    second = [("c2", "read_file", {"path": "a.txt"})]
    assert rt._execute_tool_batch(sk, None, conv, "reading", first, 1) is False
    assert rt._execute_tool_batch(sk, None, conv, "reading", second, 2) is False
    bodies = _tool_bodies(conv)
    assert bodies[0] == "hello world\n"
    assert bodies[1] == (
        "[unchanged \u2014 read_file a.txt offset=0 limit=None; "
        "same mtime and size as the earlier read this turn]"
    )
    for msg in conv.messages:
        for tc in msg.tool_calls:
            assert tc.result is None
            assert tc.status.value == "completed"


def test_changed_mtime_is_not_deduped(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello world\n")
    rt, sk, conv = _runtime(str(tmp_path))
    rt._execute_tool_batch(sk, None, conv, "reading", [("c1", "read_file", {"path": "a.txt"})], 1)
    st = target.stat()
    os.utime(target, (st.st_atime, st.st_mtime + 10))
    rt._execute_tool_batch(sk, None, conv, "reading", [("c2", "read_file", {"path": "a.txt"})], 2)
    bodies = _tool_bodies(conv)
    assert bodies[1] == "hello world\n"
    assert "unchanged" not in bodies[1]


def test_different_limit_is_not_deduped(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello world\n")
    rt, sk, conv = _runtime(str(tmp_path))
    rt._execute_tool_batch(sk, None, conv, "reading", [("c1", "read_file", {"path": "a.txt"})], 1)
    rt._execute_tool_batch(
        sk, None, conv, "reading",
        [("c2", "read_file", {"path": "a.txt", "offset": 0, "limit": 5})],
        2,
    )
    bodies = _tool_bodies(conv)
    assert bodies[1] == "hello"
    assert "unchanged" not in bodies[1]


def test_cleanup_drops_fingerprint_so_next_turn_rereads(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello world\n")
    rt, sk, conv = _runtime(str(tmp_path))
    rt._execute_tool_batch(sk, None, conv, "reading", [("c1", "read_file", {"path": "a.txt"})], 1)
    rt._cleanup_tool_history(sk)
    rt._execute_tool_batch(sk, None, conv, "reading", [("c2", "read_file", {"path": "a.txt"})], 2)
    bodies = _tool_bodies(conv)
    assert bodies[1] == "hello world\n"


def test_drop_read_cache_missing_key_is_noop():
    rt = AgentRuntime(_make_cfg())
    rt.drop_read_cache("special:missing")


def test_failed_read_is_not_fingerprinted(tmp_path):
    rt, sk, conv = _runtime(str(tmp_path))
    args = {"path": "missing.txt"}
    rt._execute_tool_batch(sk, None, conv, "reading", [("c1", "read_file", args)], 1)
    rt._execute_tool_batch(sk, None, conv, "reading", [("c2", "read_file", args)], 2)
    bodies = _tool_bodies(conv)
    assert "unchanged" not in bodies[0]
    assert "unchanged" not in bodies[1]
    assert "Not a file" in bodies[0]


def test_denied_exec_clears_tool_call_result(tmp_path):
    rt, sk, conv = _runtime(str(tmp_path))
    rt._execute_tool_batch(
        sk, None, conv, "run",
        [("c1", "exec_command", {"command": "echo hi"})],
        1,
    )
    assistant = next(m for m in conv.messages if m.tool_calls)
    assert assistant.tool_calls[0].result is None
    assert "approval" in _tool_bodies(conv)[0]


def test_empty_project_path_skips_dedup(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello world\n")
    rt, sk, conv = _runtime(str(tmp_path))
    # The tool batch refuses an empty project before dedup (workspace
    # validation). The dedup helper itself must still skip.
    conv.project_path = ""
    text = rt._dedup_read_output(sk, conv, {"path": "a.txt"}, "hello world\n")
    assert text == "hello world\n"
    assert sk not in rt._read_fingerprints


def test_stat_oserror_skips_dedup(tmp_path):
    rt, sk, conv = _runtime(str(tmp_path))
    text = rt._dedup_read_output(
        sk, conv, {"path": "no-such-file.txt"}, "body",
    )
    assert text == "body"
    assert rt._read_fingerprints.get(sk, {}) == {}


def test_task_card_round_trip_and_wire_prefix(tmp_path, monkeypatch):
    from agent.persistence import load_conversation_from_disk, save_conversation_to_disk

    conv = Conversation(agent_name="Coder", system_prompt="SYS", model="")
    conv.add_user_message("goal")
    conv.task_card = "read a.py offset=0"
    monkeypatch.setattr("models.conversation._tiktoken_encoding_for", lambda model: None)
    before = conv.get_token_estimate()
    prompt = conv.system_prompt
    conv.task_card = "read a.py offset=0\nedit_file b.py"
    after = conv.get_token_estimate()
    assert after != before
    assert conv.system_prompt == prompt

    no_card = Conversation(agent_name="Coder", system_prompt="SYS")
    no_card.add_user_message("goal")
    wired = conv.to_api_messages()
    assert len(wired) == len(no_card.to_api_messages())
    assert wired[-1]["content"].startswith("read a.py offset=0\nedit_file b.py\n\n")
    assert wired[-1]["content"].endswith("goal")
    anth = convert_messages_for_anthropic(wired)
    plain = convert_messages_for_anthropic(no_card.to_api_messages())
    assert len(anth) == len(plain) == 2
    assert anth[0]["content"] == "SYS"
    assert anth[1]["content"].startswith("read a.py offset=0")

    with patch("agent.persistence.conversations_dir", return_value=str(tmp_path)), \
         patch("agent.persistence._get_store"), \
         patch("agent.persistence._append_conversation_delta"):
        save_conversation_to_disk(conv, "special:card")
        loaded, _meta = load_conversation_from_disk("special:card")
        assert loaded.task_card == conv.task_card
        raw = json.loads((tmp_path / "special:card.json").read_text())
        del raw["task_card"]
        (tmp_path / "special:card.json").write_text(json.dumps(raw))
        old, _meta = load_conversation_from_disk("special:card")
        assert old.task_card == ""
        raw["task_card"] = 12
        (tmp_path / "special:card.json").write_text(json.dumps(raw))
        bad, _meta = load_conversation_from_disk("special:card")
        assert bad.task_card == ""


def test_task_card_waits_until_a_user_message_exists():
    conv = Conversation(agent_name="Coder", system_prompt="SYS", task_card="pending")
    msgs = conv.to_api_messages()
    assert msgs == [{"role": "system", "content": "SYS"}]


def test_note_task_trims_to_last_2000_on_a_newline(tmp_path):
    rt, sk, conv = _runtime(str(tmp_path))
    rt._note_task(sk, "a" * 1500)
    rt._note_task(sk, "b" * 1500)
    assert len(conv.task_card) <= 2000
    assert conv.task_card.startswith("b")
    assert "\n" not in conv.task_card[:1]


def test_next_turn_rereads_instead_of_reporting_unchanged(tmp_path):
    (tmp_path / "a.txt").write_text("hello world\n")
    rt = AgentRuntime(_make_cfg())
    sk = "special:turns"
    rt.create_conversation("Coder", sk, str(tmp_path), defer_prompt_build=True)
    rt._running = True
    calls = {"n": 0}

    def llm(session_key, messages, tools, **kwargs):
        calls["n"] += 1
        if calls["n"] in (1, 3):
            return _resp(tool_calls=[{
                "id": f"c{calls['n']}",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "a.txt"})},
            }])
        return _resp("done")

    with patch.object(rt, "_call_llm", llm):
        rt._run_loop(sk, "first")
        rt._run_loop(sk, "second")
    bodies = _tool_bodies(rt.get_conversation(sk))
    assert bodies == ["hello world\n", "hello world\n"]


def test_task_card_prefixes_only_the_last_user_message():
    conv = Conversation(agent_name="Coder", system_prompt="SYS", task_card="CARD")
    conv.add_user_message("first")
    conv.add_assistant_message("ok")
    conv.add_user_message("second")
    wired = conv.to_api_messages()
    users = [m for m in wired if m["role"] == "user"]
    assert users[0]["content"] == "first"
    assert users[1]["content"] == "CARD\n\nsecond"
    assert len(wired) == 4


def test_successful_read_appends_task_line(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    rt, sk, conv = _runtime(str(tmp_path))
    rt._execute_tool_batch(
        sk, None, conv, "reading",
        [("c1", "read_file", {"path": "a.txt", "offset": 2})],
        1,
    )
    # SPEC-24 Stage B owns project facts. The per-session card is no longer
    # appended from the tool batch.
    assert conv.task_card == ""
    rt._execute_tool_batch(
        sk, None, conv, "reading",
        [("c2", "read_file", {"path": "missing.txt"})],
        2,
    )
    assert conv.task_card == ""
