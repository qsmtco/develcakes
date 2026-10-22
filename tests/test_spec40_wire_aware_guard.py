"""SPEC-40 Part A: wire-aware read guard (compile-packet cutoff)."""
from __future__ import annotations

from unittest.mock import patch

from agent.runtime import AgentRuntime
from models.conversation import Conversation, MessageRole, ToolCall
from tests.test_agent_runtime import _make_cfg

_SK = "agent:coder"
_BODY = "line one\nline two\nline three\n"


def _conv(tmp_path) -> Conversation:
    conv = Conversation(
        agent_name="Coder", system_prompt="SYS", project_path=str(tmp_path),
    )
    conv.add_user_message("fix the parser")
    return conv


def _plant_read(
    conv: Conversation,
    call_id: str,
    body: str,
    path: str = "a.py",
    offset: int | None = None,
    limit: int | None = None,
) -> int:
    """Append assistant(read_file) + tool_result and return the result index."""
    args: dict = {"path": path}
    if offset is not None:
        args["offset"] = offset
    if limit is not None:
        args["limit"] = limit
    tc = ToolCall(call_id=call_id, tool_name="read_file", arguments=args)
    conv.add_assistant_message("reading", [tc])
    conv.add_tool_result(call_id, body)
    return len(conv.messages) - 1


def _write_read_file(tmp_path, text: str = _BODY) -> None:
    (tmp_path / "a.py").write_text(text)


def _runtime() -> AgentRuntime:
    return AgentRuntime(_make_cfg())


# --- (b) _last_transcript_body_index -------------------------------------


def test_body_index_is_newest_first_and_skips_unchanged_notices(tmp_path):
    rt = _runtime()
    conv = _conv(tmp_path)
    first = _plant_read(conv, "r1", _BODY)
    second = _plant_read(conv, "r2", _BODY)
    _plant_read(conv, "r3", "[unchanged \u2014 read_file a.py offset=0 limit=None]")

    idx = rt._last_transcript_body_index(conv, "read_file", ("a.py", 0, None))

    assert idx == second
    assert idx > first
    assert conv.messages[idx].role == MessageRole.TOOL_RESULT


def test_body_index_is_none_for_masked_newest_and_for_missing_body(tmp_path):
    rt = _runtime()
    conv = _conv(tmp_path)
    idx = _plant_read(conv, "r1", _BODY)
    conv.messages[idx].content = "[masked \u2014 read_file a.py (400 chars removed)]"

    assert rt._last_transcript_body_index(conv, "read_file", ("a.py", 0, None)) is None
    assert rt._last_transcript_body_index(conv, "read_file", ("b.py", 0, None)) is None


def test_body_index_is_none_when_the_walk_raises(tmp_path, monkeypatch):
    rt = _runtime()
    conv = _conv(tmp_path)
    _plant_read(conv, "r1", _BODY)

    def boom(*_args, **_kwargs):
        raise RuntimeError("meta exploded")

    monkeypatch.setattr(rt._context_strategy, "_tool_result_meta", boom)
    assert rt._last_transcript_body_index(conv, "read_file", ("a.py", 0, None)) is None


# --- (a) _wire_cutoff ----------------------------------------------------


def test_wire_cutoff_is_none_without_a_packet(tmp_path):
    rt = _runtime()
    conv = _conv(tmp_path)
    assert rt._wire_cutoff(_SK, conv) is None


def test_wire_cutoff_returns_packet_after(tmp_path):
    rt = _runtime()
    conv = _conv(tmp_path)
    rt._compile[_SK] = {"after": 7, "goal": "g", "path": "a.py", "hunk": "", "checks": []}
    assert rt._wire_cutoff(_SK, conv) == 7


# --- (c) guard insert ----------------------------------------------------


def test_off_wire_body_serves_after_an_enforced_write(tmp_path):
    """Body index below packet['after'] => serve fresh, reset notice counter."""
    _write_read_file(tmp_path)
    rt = _runtime()
    conv = _conv(tmp_path)
    body_idx = _plant_read(conv, "r1", _BODY)

    # First call pins the fingerprint and serves the body (miss path).
    assert rt._dedup_read_output(_SK, conv, {"path": "a.py"}, _BODY) == _BODY
    # An enforced write compiles a packet whose 'after' sits above the only
    # live body: that body is off the wire now.
    rt._compile[_SK] = {
        "after": body_idx + 1, "goal": "g", "path": "a.py", "hunk": "", "checks": [],
    }
    rt._reread_notices.setdefault(_SK, {})[("a.py", 0, None)] = 1

    served = rt._dedup_read_output(_SK, conv, {"path": "a.py"}, _BODY)

    assert served == _BODY
    assert not served.startswith("[unchanged")
    assert not served.startswith("REFUSED:")
    assert ("a.py", 0, None) not in rt._reread_notices.get(_SK, {})


def test_on_wire_notice_ladder_is_unchanged(tmp_path):
    """Newest body at or above the cut keeps [unchanged -> REFUSED exactly."""
    _write_read_file(tmp_path)
    rt = _runtime()
    conv = _conv(tmp_path)
    body_idx = _plant_read(conv, "r1", _BODY)
    # Ballot: the body sits AT the cut, so it is on the wire.
    rt._compile[_SK] = {
        "after": body_idx, "goal": "g", "path": "a.py", "hunk": "", "checks": [],
    }

    assert rt._dedup_read_output(_SK, conv, {"path": "a.py"}, _BODY) == _BODY
    second = rt._dedup_read_output(_SK, conv, {"path": "a.py"}, _BODY)
    third = rt._dedup_read_output(_SK, conv, {"path": "a.py"}, _BODY)

    assert second.startswith("[unchanged")
    assert third.startswith("REFUSED:")
    assert _BODY not in third


# --- (d) _block_file_reprint path-set construction -----------------------


def _pin_and_cut(rt: AgentRuntime, conv: Conversation, after: int) -> int:
    """Pin a.py's fingerprint via the miss path, then compile a cut above it."""
    assert rt._dedup_read_output(_SK, conv, {"path": "a.py"}, _BODY) == _BODY
    body_idx = rt._last_transcript_body_index(conv, "read_file", ("a.py", 0, None))
    assert body_idx is not None
    rt._compile[_SK] = {
        "after": after, "goal": "g", "path": "a.py", "hunk": "", "checks": [],
    }
    return body_idx


def test_off_wire_only_path_sed_is_allowed(tmp_path):
    """Every body for a.py is below the cut => the shell dump is not refused."""
    _write_read_file(tmp_path)
    rt = _runtime()
    conv = _conv(tmp_path)
    body_idx = _plant_read(conv, "r1", _BODY)
    _pin_and_cut(rt, conv, body_idx + 1)

    blocked = rt._block_file_reprint(_SK, conv, {"command": "sed -n 1,5p a.py"})

    assert blocked is None


def test_on_wire_path_sed_is_still_refused(tmp_path):
    """A live body at or above the cut keeps the refusal."""
    _write_read_file(tmp_path)
    rt = _runtime()
    conv = _conv(tmp_path)
    body_idx = _plant_read(conv, "r1", _BODY)
    _pin_and_cut(rt, conv, body_idx)

    blocked = rt._block_file_reprint(_SK, conv, {"command": "sed -n 1,5p a.py"})

    assert blocked is not None
    assert blocked.startswith("REFUSED:")
    assert "a.py" in blocked


def test_path_is_refused_when_any_fingerprint_key_is_on_wire(tmp_path):
    """Path-set rule: SOME key on the wire is enough to refuse the path."""
    _write_read_file(tmp_path, _BODY + "line four\n")
    rt = _runtime()
    conv = _conv(tmp_path)
    win = _BODY + "line four\n"
    old_idx = _plant_read(conv, "r1", win, offset=0, limit=None)
    new_idx = _plant_read(conv, "r2", win, offset=0, limit=10)
    assert new_idx > old_idx
    assert rt._dedup_read_output(_SK, conv, {"path": "a.py", "limit": 10}, win) == win
    assert rt._dedup_read_output(_SK, conv, {"path": "a.py"}, win) == win
    # Cut sits between the two keys: (a.py, 0, None) is off-wire,
    # (a.py, 0, 10) is on-wire. The path must still be refused.
    rt._compile[_SK] = {
        "after": old_idx + 1, "goal": "g", "path": "a.py", "hunk": "", "checks": [],
    }

    blocked = rt._block_file_reprint(_SK, conv, {"command": "sed -n 1,5p a.py"})

    assert blocked is not None
    assert blocked.startswith("REFUSED:")


def test_allowed_off_wire_path_stays_allowed_until_a_new_read_lands(tmp_path):
    """Documented relaxation: shell output is not a read_file body, so the
    allowance persists until a fresh read_file of that path lands on-wire."""
    _write_read_file(tmp_path)
    rt = _runtime()
    conv = _conv(tmp_path)
    body_idx = _plant_read(conv, "r1", _BODY)
    _pin_and_cut(rt, conv, body_idx + 1)
    cmd = {"command": "cat a.py"}

    assert rt._block_file_reprint(_SK, conv, cmd) is None
    # Second attempt in the same turn: still allowed (no re-arming).
    assert rt._block_file_reprint(_SK, conv, cmd) is None

    # A new read_file of a.py lands at or above the cut: the guard is re-armed.
    new_idx = _plant_read(conv, "r2", _BODY)
    assert new_idx >= body_idx + 1

    blocked = rt._block_file_reprint(_SK, conv, cmd)

    assert blocked is not None
    assert blocked.startswith("REFUSED:")


def test_spiral_double_gets_the_body_on_every_post_write_reread(tmp_path):
    """Regression for the 150-iteration spiral: the first post-write re-read
    (and every later one) serves the BODY, never `[unchanged` / REFUSED."""
    _write_read_file(tmp_path)
    rt = _runtime()
    conv = _conv(tmp_path)
    body_idx = _plant_read(conv, "r1", _BODY)
    assert rt._dedup_read_output(_SK, conv, {"path": "a.py"}, _BODY) == _BODY
    rt._compile[_SK] = {
        "after": body_idx + 1, "goal": "g", "path": "a.py", "hunk": "", "checks": [],
    }

    for _ in range(3):
        served = rt._dedup_read_output(_SK, conv, {"path": "a.py"}, _BODY)
        assert served == _BODY


# --- Part B: hot-window carry (§2.2) -------------------------------------

_BIG = "h" * 600  # above the 512-char probe floor, below the 16KiB cap
_HOT_HEADER = "## Hot files"


def _enforcement_ok():
    from agent.enforcement import EnforcementCheck
    return EnforcementCheck(
        tier="syntax", tool="edit_file", file="a.py", passed=True,
        detail="ok", output="", duration_ms=1,
    )


def _compile_packet(rt: AgentRuntime, conv: Conversation, path: str = "b.py") -> dict:
    from agent.tools import ToolResult
    result = ToolResult(
        success=True, output="OK", enforcement_checks=[_enforcement_ok()],
    )
    rt._maybe_compile(
        _SK, conv, "edit_file",
        {"path": path, "old_text": "a", "new_text": "b"}, result,
    )
    conv.add_tool_result("w1", "OK")
    rt._compile[_SK]["after"] = len(conv.messages)
    return rt._compile[_SK]


def _hot_keys(rt: AgentRuntime) -> list[tuple]:
    return list(rt._hot_windows.get(_SK, {}).keys())


def _hot_body(rt: AgentRuntime, path: str = "a.py") -> str | None:
    for key, win in rt._hot_windows.get(_SK, {}).items():
        if key[0] == path:
            return win["body"]
    return None


def test_capture_stores_the_window_and_caps_the_body(tmp_path):
    rt = _runtime()
    rt._capture_hot_window(_SK, "a.py", 4, 200, "x" * (20 * 1024))

    win = rt._hot_windows[_SK][("a.py", 4, 200)]
    assert win["path"] == "a.py"
    assert win["offset"] == 4
    assert win["limit"] == 200
    assert len(win["body"]) == 16 * 1024


def test_capture_skips_a_probe_below_the_floor(tmp_path):
    rt = _runtime()
    rt._capture_hot_window(_SK, "a.py", 0, None, "p" * 180)
    assert _hot_keys(rt) == []


def test_reread_moves_the_key_to_the_end(tmp_path):
    """pop-then-set: the next new file evicts the OLDEST, never the re-read."""
    rt = _runtime()
    rt._capture_hot_window(_SK, "a.py", 0, None, "a.py-" + "b" * 600)
    rt._capture_hot_window(_SK, "b.py", 0, None, "b.py-" + "b" * 600)
    rt._capture_hot_window(_SK, "c.py", 0, None, "c.py-" + "b" * 600)
    # a.py is already gone (oldest of three). Re-read b.py to move it behind c.py.
    rt._capture_hot_window(_SK, "b.py", 0, None, "b.py-again-" + "b" * 600)

    assert _hot_keys(rt) == [("c.py", 0, None), ("b.py", 0, None)]
    assert _hot_body(rt, "b.py").startswith("b.py-again-")


def test_miss_path_captures_the_served_window(tmp_path):
    _write_read_file(tmp_path)
    rt = _runtime()
    conv = _conv(tmp_path)

    assert rt._dedup_read_output(_SK, conv, {"path": "a.py"}, _BIG) == _BIG
    assert _hot_body(rt) == _BIG


def test_spec32_recovery_path_captures_the_served_window(tmp_path):
    _write_read_file(tmp_path)
    rt = _runtime()
    conv = _conv(tmp_path)
    idx = _plant_read(conv, "r1", _BIG)
    assert rt._dedup_read_output(_SK, conv, {"path": "a.py"}, _BIG) == _BIG
    rt._hot_windows.pop(_SK)
    conv.messages[idx].content = "[masked \u2014 read_file a.py (600 chars removed)]"

    assert rt._dedup_read_output(_SK, conv, {"path": "a.py"}, _BIG) == _BIG
    assert _hot_body(rt) == _BIG


def test_off_wire_serve_path_captures_the_served_window(tmp_path):
    _write_read_file(tmp_path)
    rt = _runtime()
    conv = _conv(tmp_path)
    idx = _plant_read(conv, "r1", _BIG)
    assert rt._dedup_read_output(_SK, conv, {"path": "a.py"}, _BIG) == _BIG
    rt._hot_windows.pop(_SK)
    rt._compile[_SK] = {
        "after": idx + 1, "goal": "g", "path": "a.py", "hunk": "", "checks": [],
    }

    assert rt._dedup_read_output(_SK, conv, {"path": "a.py"}, _BIG) == _BIG
    assert _hot_body(rt) == _BIG


def test_a_write_evicts_its_own_hot_window(tmp_path):
    rt = _runtime()
    conv = _conv(tmp_path)
    rt._capture_hot_window(_SK, "a.py", 0, None, "a.py-" + "b" * 600)
    rt._capture_hot_window(_SK, "b.py", 0, None, "b.py-" + "b" * 600)

    rt._note_tool_path(_SK, conv, "edit_file", {"path": "a.py"})

    assert _hot_keys(rt) == [("b.py", 0, None)]


def test_packet_snapshot_is_a_point_in_time_copy(tmp_path):
    rt = _runtime()
    conv = _conv(tmp_path)
    rt._capture_hot_window(_SK, "a.py", 0, None, _BIG)

    packet = _compile_packet(rt, conv)

    assert isinstance(packet["hot"], tuple)
    assert len(packet["hot"]) == 1
    assert packet["hot"][0]["body"] == _BIG
    assert packet["hot"][0]["limit"] is None

    # A later serve must not rewrite what the packet replays.
    rt._capture_hot_window(_SK, "a.py", 0, None, "re-read-" + "z" * 600)
    assert packet["hot"][0]["body"] == _BIG


def test_packet_carries_at_most_two_windows(tmp_path):
    rt = _runtime()
    conv = _conv(tmp_path)
    for path in ("a.py", "b.py", "c.py"):
        rt._capture_hot_window(_SK, path, 0, None, path + "-" + "b" * 600)

    packet = _compile_packet(rt, conv, path="d.py")

    assert [w["path"] for w in packet["hot"]] == ["b.py", "c.py"]


def test_compiled_message_replays_the_hot_section(tmp_path):
    rt = _runtime()
    conv = _conv(tmp_path)
    rt._capture_hot_window(_SK, "a.py", 8, 40, _BIG)
    packet = _compile_packet(rt, conv)

    user = next(
        m for m in rt._compiled_messages(conv, packet, "") if m["role"] == "user"
    )["content"]
    assert _HOT_HEADER in user
    assert "### a.py offset=8 limit=40" in user
    assert _BIG in user
    # Replay sits after the write record it belongs to.
    assert user.index("## Checks") < user.index(_HOT_HEADER)

    cold = next(
        m for m in rt._compiled_messages(conv, packet, "", hot=False)
        if m["role"] == "user"
    )["content"]
    assert _HOT_HEADER not in cold
    assert _BIG not in cold


def test_packet_without_hot_adds_no_section(tmp_path):
    """No `hot` key (older packets, no reads) => the compiled wire is unchanged."""
    rt = _runtime()
    conv = _conv(tmp_path)
    packet = _compile_packet(rt, conv)
    packet.pop("hot")

    user = next(
        m for m in rt._compiled_messages(conv, packet, "") if m["role"] == "user"
    )["content"]
    assert _HOT_HEADER not in user


def test_hot_section_raises_the_wire_count(tmp_path, monkeypatch):
    monkeypatch.setattr("models.conversation._tiktoken_encoding_for", lambda model: None)
    rt = _runtime()
    conv = _conv(tmp_path)
    rt._capture_hot_window(_SK, "a.py", 0, None, _BIG)
    packet = _compile_packet(rt, conv)

    with_hot = rt._compile_token_count(
        conv, rt._compiled_messages(conv, packet, ""), None)
    without = rt._compile_token_count(
        conv, rt._compiled_messages(conv, packet, "", hot=False), None)

    assert with_hot > without


def test_over_budget_drops_hot_before_raising(tmp_path, monkeypatch):
    monkeypatch.setattr("models.conversation._tiktoken_encoding_for", lambda model: None)
    rt = _runtime()
    conv = _conv(tmp_path)
    rt._conversations[_SK] = conv
    rt._capture_hot_window(_SK, "a.py", 0, None, _BIG)
    packet = _compile_packet(rt, conv)
    without = rt._compile_token_count(
        conv, rt._compiled_messages(conv, packet, "", hot=False), None)

    with patch("agent.repo_map.build_repo_map", return_value=""):
        wired = rt._messages_for_model(
            _SK, conv, without + rt._RESPONSE_RESERVE_TOKENS + 1)

    user = next(m for m in wired if m["role"] == "user")["content"]
    assert _HOT_HEADER not in user
    assert "## Write" in user


def test_over_budget_without_hot_still_raises(tmp_path, monkeypatch):
    import pytest

    monkeypatch.setattr("models.conversation._tiktoken_encoding_for", lambda model: None)
    rt = _runtime()
    conv = _conv(tmp_path)
    rt._conversations[_SK] = conv
    _compile_packet(rt, conv)

    with (
        patch("agent.repo_map.build_repo_map", return_value=""),
        pytest.raises(RuntimeError, match="after compaction"),
    ):
        rt._messages_for_model(_SK, conv, 200)


def _compile_write(rt: AgentRuntime, conv: Conversation) -> dict:
    """Production order: the write's tool result is already in the transcript."""
    from agent.tools import ToolResult
    tc = ToolCall(
        call_id="w1", tool_name="edit_file",
        arguments={"path": "b.py", "old_text": "a", "new_text": "b"},
    )
    conv.add_assistant_message("editing", [tc])
    conv.add_tool_result("w1", "OK")
    result = ToolResult(
        success=True, output="OK", enforcement_checks=[_enforcement_ok()],
    )
    rt._maybe_compile(
        _SK, conv, "edit_file",
        {"path": "b.py", "old_text": "a", "new_text": "b"}, result,
    )
    return rt._compile[_SK]


def test_enforced_write_keeps_current_turn_reads_on_the_wire(tmp_path):
    """The reads that led to the write stay visible. The previous turn does not."""
    _write_read_file(tmp_path)
    rt = _runtime()
    conv = _conv(tmp_path)
    _plant_read(conv, "old", "OLD-BODY-PREVIOUS")
    conv.add_user_message("sweep the tests")
    new_idx = _plant_read(conv, "new", "CURRENT-TURN-READ-BODY")
    packet = _compile_write(rt, conv)

    wired = rt._compiled_messages(conv, packet, "")
    blob = "\n".join(
        (m.get("content") or "") for m in wired if isinstance(m.get("content"), str)
    )
    assert "CURRENT-TURN-READ-BODY" in blob
    assert "OLD-BODY-PREVIOUS" not in blob
    assert packet["wire_from"] <= new_idx
    assert rt._wire_cutoff(_SK, conv) == packet["wire_from"]

    # Newest body for this key is the current-turn read, which is on the wire.
    assert rt._dedup_read_output(_SK, conv, {"path": "a.py"}, _BODY) == _BODY
    again = rt._dedup_read_output(_SK, conv, {"path": "a.py"}, _BODY)
    assert again.startswith("[unchanged")
    assert "CURRENT-TURN-READ-BODY" not in again


# --- SPEC-41 §2: wire adjacency (one serializer pass) --------------------


def _declared_ids(wired: list[dict]) -> set[str]:
    ids: set[str] = set()
    for entry in wired:
        for tc in entry.get("tool_calls") or []:
            ids.add(tc["id"])
    return ids


def test_split_shape_wire_keeps_every_tool_result_adjoining(tmp_path):
    """Tool calls and their results must never be split by the slice boundary.

    Transcript: [user, assistant(call_write+call_read), tool(call_write),
    user-nudge, tool(call_read)]. The packet cut lands mid-conversation, so
    `turn` holds the declaring assistant while `tail` holds one of its
    results. Two serialize passes demote that result to a user row
    (MiniMax 2013 / Anthropic strict-ordering shape).
    """
    rt = _runtime()
    conv = _conv(tmp_path)
    conv.add_assistant_message(
        "doing both",
        [
            ToolCall(
                call_id="call_write", tool_name="edit_file",
                arguments={"path": "b.py", "old_text": "a", "new_text": "b"},
            ),
            ToolCall(
                call_id="call_read", tool_name="read_file",
                arguments={"path": "a.py"},
            ),
        ],
    )
    conv.add_tool_result("call_write", "OK")
    conv.add_user_message("nudge")
    conv.add_tool_result("call_read", _BODY)
    # The cut: turn = [assistant, tool(call_write)], tail = [nudge, tool(call_read)].
    packet = {
        "goal": "g", "path": "a.py", "hunk": "h", "checks": [],
        "after": 3, "wire_from": 1, "hot": (),
    }

    wired = rt._compiled_messages(conv, packet, "")

    declared = _declared_ids(wired)
    assert declared == {"call_write", "call_read"}
    tool_rows = [m for m in wired if m.get("role") == "tool"]
    assert {m["tool_call_id"] for m in tool_rows} == declared
    # No demoted orphan rows anywhere on the wire.
    assert not [
        m for m in wired
        if m.get("role") == "user" and str(m.get("content", "")).startswith("[tool result ")
    ]
    # Both tool rows sit immediately after the declaring assistant row.
    idx = next(i for i, m in enumerate(wired) if m.get("role") == "assistant")
    assert all(m.get("role") == "tool" for m in wired[idx + 1:idx + 1 + len(declared)])

    # Control: the old two-pass shape demoted call_read.
    from models.conversation import serialize_messages
    two_pass = [
        {"role": "user", "content": "body"},
        *serialize_messages(conv.messages[1:3], index_base=1),
        *serialize_messages(conv.messages[3:], index_base=3),
    ]
    assert [
        m for m in two_pass
        if m.get("role") == "user" and str(m.get("content", "")).startswith("[tool result ")
    ]


def test_normal_wire_is_unchanged_by_the_one_pass_switch(tmp_path):
    """A transcript with no interleave serializes byte-identically to two passes."""
    rt = _runtime()
    conv = _conv(tmp_path)
    conv.add_assistant_message(
        "reading",
        [ToolCall(call_id="r1", tool_name="read_file", arguments={"path": "a.py"})],
    )
    conv.add_tool_result("r1", _BODY)
    conv.add_assistant_message(
        "editing",
        [ToolCall(
            call_id="w1", tool_name="edit_file",
            arguments={"path": "b.py", "old_text": "a", "new_text": "b"},
        )],
    )
    conv.add_tool_result("w1", "OK")
    packet = {
        "goal": "g", "path": "b.py", "hunk": "h", "checks": [],
        "after": len(conv.messages), "wire_from": 1, "hot": (),
    }

    wired = rt._compiled_messages(conv, packet, "")

    from models.conversation import serialize_messages
    head = wired[:2]  # system + compiled user body
    two_pass = [
        *head,
        *serialize_messages(conv.messages[1:packet["after"]], index_base=2),
        *serialize_messages(conv.messages[packet["after"]:], index_base=2),
    ]
    assert wired == two_pass


def test_hot_window_line_mode_replays_as_line_not_offset(tmp_path):
    """A 1-based line window replays `line=42`, never `offset=0`."""
    rt = _runtime()
    conv = _conv(tmp_path)
    rt._capture_hot_window(_SK, "a.py", None, 40, _BIG, 42)
    packet = _compile_packet(rt, conv)

    assert packet["hot"][0]["line"] == 42
    user = next(
        m for m in rt._compiled_messages(conv, packet, "") if m["role"] == "user"
    )["content"]
    assert "### a.py line=42 limit=40" in user
    assert "offset=0" not in user


def test_hot_window_snapshot_without_line_still_replays_offset(tmp_path):
    """The byte branch survives the `is not None` switch."""
    rt = _runtime()
    conv = _conv(tmp_path)
    rt._capture_hot_window(_SK, "a.py", 8, 40, _BIG)
    packet = _compile_packet(rt, conv)

    assert packet["hot"][0]["line"] is None
    user = next(
        m for m in rt._compiled_messages(conv, packet, "") if m["role"] == "user"
    )["content"]
    assert "### a.py offset=8 limit=40" in user


def test_previous_turn_read_is_off_the_wire_after_a_write(tmp_path):
    """A read from before this turn is not in the packet, so a re-read serves it."""
    _write_read_file(tmp_path)
    rt = _runtime()
    conv = _conv(tmp_path)
    old = _plant_read(conv, "old", _BODY)
    conv.add_user_message("next task")
    packet = _compile_write(rt, conv)

    assert packet["wire_from"] > old
    wired = rt._compiled_messages(conv, packet, "")
    blob = "\n".join(
        (m.get("content") or "") for m in wired if isinstance(m.get("content"), str)
    )
    assert _BODY not in blob

    assert rt._dedup_read_output(_SK, conv, {"path": "a.py"}, _BODY) == _BODY
    served = rt._dedup_read_output(_SK, conv, {"path": "a.py"}, _BODY)
    assert served == _BODY
    assert not served.startswith("[unchanged")
