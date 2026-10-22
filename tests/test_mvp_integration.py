"""SPEC-42: cross-seam integration module — N1 (writer turn end-to-end) and
N2 (compiled wire adjacency).

Mock policy: fakes sit ONLY at the provider seam. Worktree, checkpoint,
queue, persistence, ``_maybe_compile``, ``_compiled_messages``,
``serialize_messages`` and ``normalize_tool_history`` are all REAL.

Reuse disclosure (copied / imported from the existing suites):
  * ``_sp2_make_handler``, ``_sp2_git_repo``, ``_sp2_writer_def`` are
    IMPORTED from ``tests.test_agent_runtime`` (module-level helpers).
  * The ReviewHandler double (``_ImmediateGLib``), the review-state wiring and
    the ``_finish`` helper are COPIED from
    ``TestSpec10AgentCheckpoints`` (``_spec10_rh`` / ``_wired`` / ``_finish``,
    tests/test_agent_runtime.py:6472+) because N1 needs the same real
    ARH + ReviewHandler + git wiring, with the suffixed writer session
    registered before the turn.
"""

from __future__ import annotations

import json
import os
import time
import unittest.mock
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.llm.convert import normalize_tool_history
from agent.runtime import AgentRuntime, TurnResult, TurnStatus
from agent.tools import ToolResult
from models.conversation import Conversation, ToolCall, serialize_messages
from tests.test_agent_runtime import (
    _make_cfg,
    _sp2_git_repo,
    _sp2_make_handler,
    _sp2_writer_def,
)
from tests.test_stop_all import _CapturingFeed, _marker_procs

_SK = "special:coder-2"
_PROJECT_NAME = "proj"


def _wait(cond, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


# ── N1: writer turn end-to-end ───────────────────────────────────────────────


class _ImmediateGLib:
    """idle_add runs inline (copied from TestSpec10AgentCheckpoints)."""

    def idle_add(self, fn, *a, **k):
        fn(*a, **k)
        return 0


def _review_handler():
    from ui.handlers.review_handler import ReviewHandler
    return ReviewHandler(
        GLib=_ImmediateGLib(),
        main_content=unittest.mock.MagicMock(),
        project_handler=unittest.mock.MagicMock(),
        on_review_started=unittest.mock.MagicMock(),
        on_review_ended=unittest.mock.MagicMock(),
        on_display_card=unittest.mock.MagicMock(),
        on_display_text=unittest.mock.MagicMock(),
        on_feed_card=unittest.mock.MagicMock(),
    )


class TestN1WriterTurnEndToEnd:
    """ONE writer turn must name itself identically at every stage: worktree,
    enforced write, checkpoint commit, queue attribution, persistence file —
    and accept must not create a duplicate commit."""

    def _wired(self, tmp_path):
        """ARH + real ReviewHandler + real git repo + active review session.

        Same shape as TestSpec10AgentCheckpoints._wired, except the suffixed
        writer session is registered here so N1 can resolve its worktree."""
        from models.review_state import ReviewState

        handler = _sp2_make_handler()
        rh = _review_handler()
        handler.set_review_handler(rh)
        project = tmp_path / "proj"
        project.mkdir()
        _sp2_git_repo(project)
        pstr = str(project)

        rh._states[_PROJECT_NAME] = ReviewState(
            project_path=pstr, review_mode="review", checkpoint_sha="c" * 40)
        handler._agents[_SK] = _sp2_writer_def()
        token = object()
        handler._turn_tokens[_SK] = token
        handler._turn_attr[_SK] = (_PROJECT_NAME, pstr, pstr)
        return handler, rh, pstr, token

    def test_writer_turn_end_to_end(self, tmp_path, monkeypatch):
        import git as gitpython

        from agent.persistence import (
            conversations_dir,
            load_conversation_from_disk,
            save_conversation_to_disk,
        )

        handler, rh, pstr, token = self._wired(tmp_path)

        # ── Step 1: the suffixed writer resolves its worktree ───────────────
        # No lease exists in SPEC-49: session-key suffix alone drives worktree
        # entry.
        wt = handler._worktree_for_turn(_SK, _sp2_writer_def(), pstr)
        assert wt is not None, "no worktree for a suffixed writer"
        assert wt == os.path.realpath(wt)
        assert wt != os.path.realpath(pstr), "worktree resolved to project root"
        assert os.path.realpath(os.path.dirname(wt)) == os.path.realpath(
            os.path.join(pstr, ".worktrees"))
        listing = gitpython.Repo(pstr).git.worktree("list")
        assert wt in [os.path.realpath(line.split()[0]) for line in
                      listing.splitlines() if line.strip()], (
            f"worktree not registered with the project: {listing!r}")

        # ── Step 2: the turn's write lands INSIDE the worktree ──────────────
        written = Path(wt) / "agent_work.txt"
        written.write_text("ENFORCED-WRITE-MARKER\n", encoding="utf-8")
        assert written.is_file()
        project_copy = Path(pstr) / "agent_work.txt"
        assert not project_copy.exists(), (
            "write landed in the project root instead of the worktree")

        # ── Step 3: completion → checkpoint in the WORKTREE + queue entry ───
        handler._turn_attr[_SK] = (_PROJECT_NAME, pstr, wt)
        project_head_before = gitpython.Repo(pstr).head.commit.hexsha
        handler._do_response_complete(_SK, "done", token)

        assert _wait(lambda: rh.pending_count(_PROJECT_NAME, _SK) == 1), (
            "no queue entry after a completed writer turn")
        entry = rh._queues[_PROJECT_NAME][_SK][0]
        assert entry.agent_key == _SK
        assert entry.path_used == wt, "queue path_used != worktree path"
        wt_repo = gitpython.Repo(wt)
        assert entry.sha == wt_repo.head.commit.hexsha, (
            "queue sha is not the worktree checkpoint commit")
        body = wt_repo.git.log("--format=%B", "-1")
        assert "Agent: special:coder-2" in body.splitlines(), (
            f"worktree checkpoint missing the Agent: trailer: {body!r}")
        assert gitpython.Repo(pstr).head.commit.hexsha == project_head_before, (
            "checkpoint committed in the project root, not the worktree")

        # ── Step 4: per-session persistence round-trip ──────────────────────
        monkeypatch.setattr("utils.config.get_config_dir",
                            lambda: str(tmp_path / "cfg"))
        conv = Conversation(agent_name="Coder", project_path=wt)
        conv.add_user_message("write agent_work.txt")
        call_id = "w-n1"
        conv.add_assistant_message("writing", [ToolCall(
            call_id=call_id, tool_name="write_file",
            arguments={"path": "agent_work.txt", "content": "ENFORCED-WRITE-MARKER\n"},
        )])
        conv.add_tool_result(call_id, "OK — wrote agent_work.txt")
        saved = save_conversation_to_disk(conv, _SK)
        assert os.path.isfile(saved)
        assert os.path.dirname(os.path.realpath(saved)) == os.path.realpath(
            conversations_dir())
        assert os.path.basename(saved) == f"{_SK}.json", (
            "persistence file does not belong to the session")
        loaded = load_conversation_from_disk(_SK)
        assert loaded is not None
        loaded_conv, _raw = loaded
        assert loaded_conv.agent_name == "Coder"
        assert loaded_conv.messages[2].tool_call_id == call_id
        assert loaded_conv.messages[1].tool_calls[0].arguments["path"] == \
            "agent_work.txt"
        assert loaded_conv.messages[2].content == "OK — wrote agent_work.txt", (
            "the enforced write is missing from the persisted transcript")

        # ── Step 5: accept → reviewed, NO duplicate commit ──────────────────
        wt_commits_before = len(list(wt_repo.iter_commits()))
        proj_commits_before = len(list(gitpython.Repo(pstr).iter_commits()))
        wt_head_before = wt_repo.head.commit.hexsha
        rh.accept_agent_queue(_SK, _PROJECT_NAME)
        assert _wait(lambda: rh.pending_count(_PROJECT_NAME, _SK) == 0), (
            "worktree queue entry was not dequeued by accept")
        assert gitpython.Repo(wt).head.commit.hexsha == wt_head_before, (
            "accept committed on the worktree (mark-reviewed must be bookkeeping)")
        assert len(list(gitpython.Repo(wt).iter_commits())) == wt_commits_before
        assert len(list(gitpython.Repo(pstr).iter_commits())) == proj_commits_before, (
            "accept fabricated a duplicate commit in the project root")


# ── N2: compiled wire ↔ normalization ↔ provider adjacency ──────────────────


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
