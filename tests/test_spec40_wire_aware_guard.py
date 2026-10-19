"""SPEC-40 Part A: wire-aware read guard (compile-packet cutoff)."""
from __future__ import annotations

from agent.runtime import AgentRuntime
from models.conversation import Conversation, MessageRole, ToolCall
from tests.test_agent_runtime import _make_cfg

_SK = "special:coder"
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
