"""SPEC-24 Stage A: compiled wire list after an enforced write."""
from __future__ import annotations

from unittest.mock import patch

from agent.enforcement import EnforcementCheck
from agent.runtime import REPO_MAP_HEADER, AgentRuntime
from agent.tools import ToolResult
from models.conversation import Conversation, ToolCall
from tests.test_agent_runtime import _make_cfg


def _check(detail="syntax failed", output="boom"):
    return EnforcementCheck(
        tier="syntax",
        tool="edit_file",
        file="a.py",
        passed=False,
        detail=detail,
        output=output,
        duration_ms=1,
    )


def _conv() -> Conversation:
    conv = Conversation(agent_name="Coder", system_prompt="SYS", project_path="/tmp/proj")
    conv.add_user_message("fix the parser")
    tc = ToolCall(call_id="r1", tool_name="read_file", arguments={"path": "a.py"})
    conv.add_assistant_message("reading", [tc])
    conv.add_tool_result("r1", "READBODY-UNIQUE")
    return conv


def test_compile_omits_the_read_and_keeps_the_transcript():
    rt = AgentRuntime(_make_cfg())
    conv = _conv()
    sk = "special:compile"
    rt._conversations[sk] = conv
    before = len(conv.messages)
    result = ToolResult(success=True, output="OK — edited a.py", enforcement_checks=[_check()])
    rt._maybe_compile(
        sk, conv, "edit_file",
        {"path": "a.py", "old_text": "OLD", "new_text": "NEW"},
        result,
    )
    # The write's tool result is what moves `after`. The unit path above
    # did not append it; append it the way the batch does, then set after.
    conv.add_tool_result("w1", "OK — edited a.py")
    rt._compile[sk]["after"] = len(conv.messages)
    wired = rt._messages_for_model(sk, conv, 128_000)
    assert len(conv.messages) == before + 1
    user = next(m for m in wired if m["role"] == "user")
    assert "fix the parser" in user["content"]
    assert "--- old_text" in user["content"]
    assert "OLD" in user["content"]
    assert "syntax failed" in user["content"]
    assert "READBODY-UNIQUE" not in user["content"]
    blob = "\n".join(m.get("content") or "" for m in wired)
    assert "READBODY-UNIQUE" not in blob
    assert conv.messages[2].content == "READBODY-UNIQUE"

    conv.add_assistant_message("applied the fix")
    wired2 = rt._messages_for_model(sk, conv, 128_000)
    blob2 = "\n".join(m.get("content") or "" for m in wired2)
    assert "applied the fix" in blob2
    assert "READBODY-UNIQUE" not in blob2


def test_hunk_is_capped_at_4000_chars():
    rt = AgentRuntime(_make_cfg())
    conv = _conv()
    sk = "special:hunk"
    rt._conversations[sk] = conv
    old = "O" * 5000
    new = "N" * 5000
    rt._maybe_compile(
        sk, conv, "edit_file",
        {"path": "a.py", "old_text": old, "new_text": new},
        ToolResult(success=True, enforcement_checks=[_check()]),
    )
    hunk = rt._compile[sk]["hunk"]
    assert old[:4000] in hunk
    assert "O" * 4001 not in hunk
    assert new[:4000] in hunk
    assert "N" * 4001 not in hunk


def test_write_file_hunk_is_a_byte_count_not_the_content():
    rt = AgentRuntime(_make_cfg())
    conv = _conv()
    sk = "special:write"
    rt._conversations[sk] = conv
    content = "h" * 100
    rt._maybe_compile(
        sk, conv, "write_file",
        {"path": "a.py", "content": content},
        ToolResult(success=True, enforcement_checks=[_check(detail="wrote")]),
    )
    hunk = rt._compile[sk]["hunk"]
    assert hunk == "write_file replaced a.py (100 bytes)"
    assert content not in hunk


def test_compiled_call_skips_transcript_budget_when_packet_fits(monkeypatch):
    monkeypatch.setattr("models.conversation._tiktoken_encoding_for", lambda model: None)
    rt = AgentRuntime(_make_cfg())
    conv = Conversation(agent_name="Coder", system_prompt="SYS", model="")
    conv.add_user_message("goal")
    conv.add_tool_result("r", "Z" * 40_000)
    sk = "special:budget"
    rt._conversations[sk] = conv
    model_max = 8_000
    # Transcript is over the ceiling. Without a packet this raises.
    try:
        rt._messages_for_model(sk, conv, model_max)
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "after compaction" in str(exc)
    assert raised
    rt._maybe_compile(
        sk, conv, "edit_file",
        {"path": "a.py", "old_text": "a", "new_text": "b"},
        ToolResult(success=True, enforcement_checks=[_check()]),
    )
    conv.add_tool_result("w", "OK")
    rt._compile[sk]["after"] = len(conv.messages)
    wired = rt._messages_for_model(sk, conv, model_max)
    assert wired[1]["role"] == "user"
    assert "Z" * 100 not in wired[1]["content"]


def test_compiled_packet_carries_the_repo_map_label():
    """SPEC-24 packet labels its map block with the same constant as the
    normal path, in the same shape. Both attachment points are pinned here
    and in test_repo_map.py, so the two cannot drift apart."""
    rt = AgentRuntime(_make_cfg())
    conv = _conv()
    sk = "special:compiledmap"
    rt._conversations[sk] = conv
    rt._maybe_compile(
        sk, conv, "edit_file",
        {"path": "a.py", "old_text": "OLD", "new_text": "NEW"},
        ToolResult(success=True, enforcement_checks=[_check()]),
    )
    conv.add_tool_result("w1", "OK — edited a.py")
    rt._compile[sk]["after"] = len(conv.messages)
    with patch("agent.repo_map.build_repo_map", return_value="MAPBLOCK") as built:
        wired = rt._messages_for_model(sk, conv, 128_000)
    assert built.call_count == 1
    user = next(m for m in wired if m["role"] == "user")["content"]

    # Goal first, then the labelled block, then the packet sections. The
    # label is the runtime constant, byte for byte — not a copy in this file.
    prefix = "fix the parser\n\n" + REPO_MAP_HEADER + "\n\nMAPBLOCK\n\n## Write\n"
    assert user.startswith(prefix)
    assert user.index(REPO_MAP_HEADER) < user.index("MAPBLOCK") < user.index("## Write")

    # Same label on the non-compiled attachment point, for the same block.
    plain = _conv()
    rt._conversations["special:plainmap"] = plain
    with patch("agent.repo_map.build_repo_map", return_value="MAPBLOCK"):
        wired_plain = rt._messages_for_model("special:plainmap", plain, 128_000)
    plain_user = next(m for m in wired_plain if m["role"] == "user")["content"]
    assert plain_user == REPO_MAP_HEADER + "\n\nMAPBLOCK\n\nfix the parser"
    assert user[len("fix the parser\n\n"):].startswith(plain_user[: plain_user.rindex("\n\n") + 2])
    assert REPO_MAP_HEADER in plain_user and REPO_MAP_HEADER in user

    # Negative control: no map block, no label on the packet.
    conv2 = _conv()
    sk2 = "special:compilednomap"
    rt._conversations[sk2] = conv2
    rt._maybe_compile(
        sk2, conv2, "edit_file",
        {"path": "a.py", "old_text": "OLD", "new_text": "NEW"},
        ToolResult(success=True, enforcement_checks=[_check()]),
    )
    conv2.add_tool_result("w1", "OK — edited a.py")
    rt._compile[sk2]["after"] = len(conv2.messages)
    with patch("agent.repo_map.build_repo_map", return_value=""):
        wired2 = rt._messages_for_model(sk2, conv2, 128_000)
    user2 = next(m for m in wired2 if m["role"] == "user")["content"]
    assert REPO_MAP_HEADER not in user2
    assert "MAPBLOCK" not in user2


def test_markdown_write_and_failed_edit_do_not_start_compile(tmp_path):
    rt = AgentRuntime(_make_cfg())
    sk = "special:skip"
    rt.create_conversation("Coder", sk, str(tmp_path), defer_prompt_build=True)
    rt._running = True
    conv = rt.get_conversation(sk)
    conv.add_user_message("note")
    rt._execute_tool_batch(
        sk, None, conv, "writing",
        [("c1", "write_file", {"path": "notes.md", "content": "# hi\n"})],
        1,
    )
    assert sk not in rt._compile
    rt._execute_tool_batch(
        sk, None, conv, "editing",
        [("c2", "edit_file", {"path": "notes.md", "old_text": "nope", "new_text": "x"})],
        2,
    )
    assert sk not in rt._compile


def test_no_user_message_does_not_compile():
    rt = AgentRuntime(_make_cfg())
    conv = Conversation(agent_name="Coder", system_prompt="SYS")
    sk = "special:nouser"
    rt._maybe_compile(
        sk, conv, "edit_file",
        {"path": "a.py", "old_text": "a", "new_text": "b"},
        ToolResult(success=True, enforcement_checks=[_check()]),
    )
    assert sk not in rt._compile


def test_clear_drops_compile_and_the_next_send_uses_the_transcript():
    rt = AgentRuntime(_make_cfg())
    conv = _conv()
    sk = "special:clear"
    rt._conversations[sk] = conv
    rt._compile[sk] = {
        "goal": "fix the parser",
        "path": "a.py",
        "hunk": "--- old_text\nx\n--- new_text\ny",
        "checks": [{"tier": "syntax", "passed": True, "detail": "ok", "output": ""}],
        "after": len(conv.messages),
    }
    rt.drop_compile(sk)
    wired = rt._messages_for_model(sk, conv, 128_000)
    blob = "\n".join(m.get("content") or "" for m in wired)
    assert "READBODY-UNIQUE" in blob
    assert "## Write" not in blob
