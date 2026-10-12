"""SPEC-24 Stage A: compiled wire list after an enforced write."""
from __future__ import annotations

from agent.enforcement import EnforcementCheck
from agent.runtime import AgentRuntime
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
