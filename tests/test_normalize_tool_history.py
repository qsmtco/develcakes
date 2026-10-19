"""Tests for agent.llm.convert.normalize_tool_history — WU minimax-2013 chunk A.

Pure-function contract: repair OpenAI-style tool_call/tool result pairing so
providers that reject unpaired histories (MiniMax error 2013) receive a
structurally valid transcript. No provider-specific branches here — the
function is pair-shape only.
"""

from __future__ import annotations

import copy

from agent.llm.convert import normalize_tool_history


def _assistant(ids: list[str], content: str | None = None) -> dict:
    msg: dict = {
        "role": "assistant",
        "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
            for cid in ids
        ],
    }
    if content is not None:
        msg["content"] = content
    return msg


def _tool(call_id: str, content: str = "ok") -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


# ── 1. orphan tool result is dropped ────────────────────────────────────────

def test_orphan_tool_result_is_dropped():
    messages = [
        {"role": "user", "content": "hi"},
        _tool("call_ghost"),
    ]

    assert normalize_tool_history(messages) == [{"role": "user", "content": "hi"}]


def test_tool_result_without_any_tool_call_message_is_dropped():
    result = normalize_tool_history([_tool("call_1")])

    assert result == []


# ── 2. unanswered call gets a synthesized empty result ──────────────────────

def test_missing_result_is_synthesized_after_its_assistant_message():
    messages = [
        {"role": "user", "content": "read it"},
        _assistant(["call_1"]),
    ]

    assert normalize_tool_history(messages) == [
        {"role": "user", "content": "read it"},
        _assistant(["call_1"]),
        {"role": "tool", "tool_call_id": "call_1", "content": ""},
    ]


# ── 3. a result separated from its call is moved up ─────────────────────────

def test_result_separated_from_call_is_moved_to_follow_the_assistant():
    messages = [
        _assistant(["call_1"]),
        {"role": "user", "content": "interrupting message"},
        _tool("call_1", "file body"),
    ]

    assert normalize_tool_history(messages) == [
        _assistant(["call_1"]),
        _tool("call_1", "file body"),
        {"role": "user", "content": "interrupting message"},
    ]


# ── 4. clean transcript is an identity conversion ───────────────────────────

def test_clean_transcript_is_identity_and_returns_a_new_list():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do it"},
        _assistant(["call_1", "call_2"]),
        _tool("call_1", "a"),
        _tool("call_2", "b"),
        {"role": "assistant", "content": "done"},
    ]
    before = copy.deepcopy(messages)

    result = normalize_tool_history(messages)

    assert result == messages
    assert isinstance(result, list)
    assert result is not messages
    assert list(result) != [] and len(result) == len(messages)
    assert messages == before  # input not mutated


# ── 5. idempotency across cases 1–3 ─────────────────────────────────────────

def test_idempotent_orphan_dropped():
    messages = [{"role": "user", "content": "hi"}, _tool("call_ghost")]
    once = normalize_tool_history(messages)

    assert normalize_tool_history(once) == once


def test_idempotent_synthesized_result():
    messages = [{"role": "user", "content": "read it"}, _assistant(["call_1"])]
    once = normalize_tool_history(messages)

    assert normalize_tool_history(once) == once


def test_idempotent_reordered_result():
    messages = [
        _assistant(["call_1"]),
        {"role": "user", "content": "interrupting message"},
        _tool("call_1", "file body"),
    ]
    once = normalize_tool_history(messages)

    assert normalize_tool_history(once) == once


# ── 6. multiple calls: one answered, one not ────────────────────────────────

def test_multiple_calls_one_answered_one_synthesized():
    messages = [
        _assistant(["call_a", "call_b"]),
        _tool("call_a", "alpha"),
    ]

    assert normalize_tool_history(messages) == [
        _assistant(["call_a", "call_b"]),
        _tool("call_a", "alpha"),
        {"role": "tool", "tool_call_id": "call_b", "content": ""},
    ]


# ── 7. non-tool content on a tool-calling assistant survives ────────────────

def test_assistant_text_content_survives_untouched():
    assistant = _assistant(["call_1"], content="Let me look that up.")
    messages = [assistant, _tool("call_1", "result")]

    result = normalize_tool_history(messages)

    assert result[0] is assistant
    assert result[0]["content"] == "Let me look that up."
    assert len(result[0]["tool_calls"]) == 1
    assert result[1] == _tool("call_1", "result")


def test_assistant_with_empty_tool_calls_is_not_a_tool_message():
    messages = [
        {"role": "assistant", "content": "no tools here", "tool_calls": []},
        {"role": "user", "content": "next"},
    ]

    assert normalize_tool_history(messages) == messages
