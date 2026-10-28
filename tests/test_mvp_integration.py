"""SPEC-42: cross-seam integration module — N2 (compiled wire adjacency).

Mock policy: fakes sit ONLY at the provider seam. ``_maybe_compile``,
``_compiled_messages``, ``serialize_messages`` and
``normalize_tool_history`` are all REAL.

Reuse disclosure (copied / imported from the existing suites):
  * ``_make_cfg`` is IMPORTED from ``tests.test_agent_runtime`` (module-level
    helper). The N1 review-wired integration (ARH + ReviewHandler + git) was
    deleted with SPEC-58 §2i — the machinery it exercised no longer exists.
"""

from __future__ import annotations

from unittest.mock import patch

from agent.llm.convert import normalize_tool_history
from agent.runtime import AgentRuntime
from agent.tools import ToolResult
from models.conversation import Conversation, ToolCall, serialize_messages
from tests.test_agent_runtime import _make_cfg

_SK = "agent:coder-2"




def _enforcement_ok():
    from agent.enforcement import EnforcementCheck
    return EnforcementCheck(
        tier="syntax", tool="write_file", file="a.py", passed=True,
        detail="ok", output="", duration_ms=1,
    )


def _write_result() -> ToolResult:
    return ToolResult(success=True, output="OK", enforcement_checks=[
        _enforcement_ok()])


def _compile_write(rt: AgentRuntime, conv: Conversation,
                   call_id: str = "w1") -> dict:
    """Store a compile packet whose write result is already in the transcript
    (production order: add_tool_result runs BEFORE _maybe_compile)."""
    rt._maybe_compile(
        _SK, conv, "write_file",
        {"path": "b.py", "content": "x\n"}, _write_result(),
    )
    return rt._compile[_SK]


def _declared_ids(wire: list[dict]) -> list[str]:
    ids: list[str] = []
    for entry in wire:
        for tc in entry.get("tool_calls") or []:
            if isinstance(tc, dict) and isinstance(tc.get("id"), str):
                ids.append(tc["id"])
    return ids


def _assert_adjacency(wire: list[dict]) -> None:
    """Every declared tool_call id carries a tool row, that row directly
    follows its declaring assistant, and nothing was demoted."""
    declared = _declared_ids(wire)
    tool_ids = [m.get("tool_call_id") for m in wire if m.get("role") == "tool"]
    for call_id in declared:
        assert call_id in tool_ids, (
            f"declared tool_call {call_id!r} has no tool row on the wire")
    for tool_id in tool_ids:
        assert tool_id in declared, (
            f"tool row {tool_id!r} has no declaring assistant (orphan)")
    duplicates = {i for i in tool_ids if tool_ids.count(i) > 1}
    assert not duplicates, f"duplicate tool rows on the wire: {duplicates}"
    for index, entry in enumerate(wire):
        if entry.get("role") != "assistant":
            continue
        ids = [tc["id"] for tc in entry.get("tool_calls") or []
               if isinstance(tc, dict) and isinstance(tc.get("id"), str)]
        if not ids:
            continue
        following: list[str] = []
        j = index + 1
        while j < len(wire) and wire[j].get("role") == "tool":
            following.append(wire[j].get("tool_call_id"))
            j += 1
        assert set(ids) <= set(following), (
            f"tool calls {ids} not immediately followed by their rows "
            f"(got {following}): {wire}")
    for entry in wire:
        content = entry.get("content")
        if entry.get("role") == "user" and isinstance(content, str):
            assert not content.startswith("[tool result "), (
                f"demoted tool result on the wire: {content[:80]!r}")


def _assert_idempotent(wire: list[dict]) -> None:
    once = normalize_tool_history(wire)
    twice = normalize_tool_history(once)
    assert twice == once, "normalize_tool_history is not idempotent on this wire"


class TestN2CompiledWireAdjacency:
    """Every packet shape we emit must serialize to a wire where every
    declared tool_call id carries a tool row (no MiniMax-2013 shape)."""

    def _base_conv(self) -> Conversation:
        conv = Conversation(agent_name="Coder", project_path="")
        conv.add_user_message("fix the parser")
        return conv

    def test_wire_from_splits_assistant_from_its_own_results(self):
        """The audit BUG#2 shape: [user, assistant(call_write+call_read),
        tool(call_write), user-nudge, tool(call_read)]. ONE serializer pass
        over turn+tail must keep both tool rows under their assistant."""
        rt = AgentRuntime(_make_cfg())
        conv = self._base_conv()
        conv.add_assistant_message("working", [
            ToolCall(call_id="w1", tool_name="write_file",
                     arguments={"path": "b.py", "content": "x\n"}),
            ToolCall(call_id="r1", tool_name="read_file",
                     arguments={"path": "a.py"}),
        ])
        conv.add_tool_result("w1", "OK — wrote b.py")   # only the write replied
        packet = _compile_write(rt, conv)
        assert packet["wire_from"] == 1
        assert packet["after"] == 3
        # The nudge and the late read result arrive AFTER the cut.
        conv.add_user_message("nudge from the PM")
        conv.add_tool_result("r1", "READ-BODY")

        wire = rt._compiled_messages(conv, packet, "")
        _assert_adjacency(wire)
        assert set(_declared_ids(wire)) == {"w1", "r1"}
        blob = "\n".join(m.get("content") or "" for m in wire)
        assert "READ-BODY" in blob, "the late read result fell off the wire"
        _assert_idempotent(wire)

    def test_parallel_calls_with_interleaved_nudge(self):
        """SPEC-41 reproduction: three parallel calls, a nudge between the
        first and the remaining results."""
        rt = AgentRuntime(_make_cfg())
        conv = self._base_conv()
        conv.add_assistant_message("sweeping", [
            ToolCall(call_id="w1", tool_name="write_file",
                     arguments={"path": "b.py", "content": "x\n"}),
            ToolCall(call_id="r1", tool_name="read_file",
                     arguments={"path": "a.py"}),
            ToolCall(call_id="w2", tool_name="edit_file",
                     arguments={"path": "c.py", "old_text": "a", "new_text": "b"}),
        ])
        conv.add_tool_result("w1", "OK")
        packet = _compile_write(rt, conv)
        conv.add_user_message("keep going")
        conv.add_tool_result("r1", "READ-BODY")
        conv.add_tool_result("w2", "OK")

        wire = rt._compiled_messages(conv, packet, "")
        _assert_adjacency(wire)
        assert set(_declared_ids(wire)) == {"w1", "r1", "w2"}
        _assert_idempotent(wire)

    def test_hot_windows_byte_and_line_mode(self):
        """hot windows replay as `line=42` (line mode) and `offset=8` (byte
        mode) — never `offset=0` for a line capture — and the wire with the
        replay stays adjacency-clean and idempotent."""
        rt = AgentRuntime(_make_cfg())
        conv = self._base_conv()
        byte_body = "b" * 600
        line_body = "l" * 600
        rt._capture_hot_window(_SK, "a.py", 8, 40, byte_body)
        rt._capture_hot_window(_SK, "l.py", None, 20, line_body, line=42)
        conv.add_assistant_message("working", [
            ToolCall(call_id="w1", tool_name="write_file",
                     arguments={"path": "b.py", "content": "x\n"}),
            ToolCall(call_id="r1", tool_name="read_file",
                     arguments={"path": "a.py", "line": 42}),
        ])
        conv.add_tool_result("w1", "OK")
        packet = _compile_write(rt, conv)
        assert len(packet["hot"]) == 2
        conv.add_tool_result("r1", "READ-BODY")

        wire = rt._compiled_messages(conv, packet, "")
        user = next(m for m in wire if m.get("role") == "user")["content"]
        assert "### a.py offset=8 limit=40" in user
        assert "### l.py line=42 limit=20" in user, (
            f"line-mode window did not replay as line=42: {user[-800:]!r}")
        assert "### l.py offset=0" not in user
        assert line_body in user and byte_body in user
        _assert_adjacency(wire)
        _assert_idempotent(wire)

    def test_over_budget_retry_drops_hot_and_stays_adjacency_clean(
            self, monkeypatch):
        """The over-budget retry rebuilds the wire with hot=False; that wire is
        still adjacency-clean."""
        monkeypatch.setattr("models.conversation._tiktoken_encoding_for",
                            lambda model: None)
        rt = AgentRuntime(_make_cfg())
        conv = self._base_conv()
        rt._capture_hot_window(_SK, "a.py", 8, 40, "h" * 600)
        conv.add_assistant_message("working", [
            ToolCall(call_id="w1", tool_name="write_file",
                     arguments={"path": "b.py", "content": "x\n"}),
            ToolCall(call_id="r1", tool_name="read_file",
                     arguments={"path": "a.py"}),
        ])
        conv.add_tool_result("w1", "OK")
        packet = _compile_write(rt, conv)
        conv.add_user_message("nudge")
        conv.add_tool_result("r1", "READ-BODY")

        without = rt._compile_token_count(
            conv, rt._compiled_messages(conv, packet, "", hot=False), None)
        with patch("agent.repo_map.build_repo_map", return_value=""):
            wired = rt._messages_for_model(
                _SK, conv, without + rt._RESPONSE_RESERVE_TOKENS + 1)

        user = next(m for m in wired if m.get("role") == "user")["content"]
        assert "## Hot files" not in user, "over-budget retry kept the hot replay"
        assert "## Write" in user
        _assert_adjacency(wired)
        _assert_idempotent(wired)

    def test_legacy_packet_without_wire_from_is_tail_only(self):
        """A packet with no `wire_from` (older shape) still slices the tail
        only — byte-identical to serialize_messages over the tail."""
        rt = AgentRuntime(_make_cfg())
        conv = self._base_conv()
        conv.add_assistant_message("working", [
            ToolCall(call_id="w1", tool_name="write_file",
                     arguments={"path": "b.py", "content": "x\n"}),
        ])
        conv.add_tool_result("w1", "OK")
        packet = _compile_write(rt, conv)
        packet.pop("wire_from")
        conv.add_user_message("follow-up")
        conv.add_assistant_message("reading", [
            ToolCall(call_id="r2", tool_name="read_file",
                     arguments={"path": "d.py"}),
        ])
        conv.add_tool_result("r2", "TAIL-ONLY-BODY")

        wire = rt._compiled_messages(conv, packet, "")
        expected_tail = serialize_messages(
            conv.messages[packet["after"]:], index_base=1)
        assert wire[1:] == expected_tail, (
            "legacy (no wire_from) wire is not the tail-only slice")
        blob = "\n".join(m.get("content") or "" for m in wire)
        assert "TAIL-ONLY-BODY" in blob
        assert wire[0]["role"] == "user"
        assert "## Write" in wire[0]["content"]
        _assert_adjacency(wire)
        _assert_idempotent(wire)

    def test_every_shape_survives_double_normalization(self):
        """Idempotency lock across the two layers: for a split shape and a
        parallel shape, normalize(normalize(x)) == normalize(x) AND the
        normalized wire keeps the adjacency contract."""
        rt = AgentRuntime(_make_cfg())
        shapes: list[list[dict]] = []
        for parallel in (False, True):
            conv = self._base_conv()
            calls = [ToolCall(call_id="w1", tool_name="write_file",
                              arguments={"path": "b.py", "content": "x\n"}),
                     ToolCall(call_id="r1", tool_name="read_file",
                              arguments={"path": "a.py"})]
            if parallel:
                calls.append(ToolCall(call_id="w2", tool_name="edit_file",
                                      arguments={"path": "c.py"}))
            conv.add_assistant_message("working", calls)
            conv.add_tool_result("w1", "OK")
            packet = _compile_write(rt, conv)
            conv.add_user_message("nudge")
            conv.add_tool_result("r1", "READ-BODY")
            if parallel:
                conv.add_tool_result("w2", "OK")
            wire = rt._compiled_messages(conv, packet, "")
            _assert_adjacency(wire)
            _assert_idempotent(wire)
            shapes.append(normalize_tool_history(wire))
        assert len(shapes) == 2
        for normalised in shapes:
            _assert_adjacency(normalised)
