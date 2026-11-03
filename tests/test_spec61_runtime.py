# tests/test_spec61_runtime.py
# SPEC-61 Wave 1 — A1 cancel lifecycle, A2 mid-stream cancel, B7 parts join.
#
# A1 (three probe tests from spec §2): a Stop must belong to the turn it was
# pressed in — never to the user's NEXT message. A2: Stop ends a streaming
# reply at the next SSE event. B7: streamed chunks collect in a list and join
# once, so per-chunk dispatch is unchanged.

import uuid
from unittest.mock import MagicMock, patch

import pytest

from agent.config import AgentConfig
from agent.runtime import AgentRuntime, TurnInactive, TurnStatus
from agent.tools import ToolResult


def _uniq():
    return f"spec61{uuid.uuid4().hex[:8]}"


def _resp(content="Done.", tool_calls=None):
    msg = {"content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "choices": [{"message": msg}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 10},
    }


def _make_cfg():
    from agent.config import LLMProviderConfig
    return AgentConfig(
        providers={
            "openai": LLMProviderConfig(
                name="openai",
                base_url="https://api.openai.com/v1",
                api_key="test-key",
                default_model="gpt-4o",
            )
        },
        default_provider="openai",
        default_model="openai/gpt-4o",
        max_tool_iterations=5,
        tool_timeout_seconds=30,
        auto_save_conversations=False,
    )


def _make_streaming_lambda(rt):
    """Forward `_call_llm`'s positional args into `rt._call_llm_streaming`.

    Same boilerplate as tests/test_agent_runtime.py's helper of the same name.
    """
    return lambda *a, **kw: rt._call_llm_streaming(
        session_key=a[0],
        base_url="https://api.openai.com/v1",
        api_key="test",
        model="openai/gpt-4o",
        caller_key="openai",
        messages=a[1],
        tools=a[2] if len(a) > 2 else None,
        timeout=30.0,
    )


# ── A1: a cancel outlives its turn no longer ────────────────────────────────


def test_stop_after_turn_end_does_not_cancel_next_turn(tmp_path):
    rt = AgentRuntime(_make_cfg(), GLib=None)
    rt.start()
    sk = "agent:spec61-a1"
    rt.create_conversation("Coder", sk, str(tmp_path))
    with patch.object(rt, "_call_llm", return_value=_resp("hi")) as llm:
        rt._run_loop(sk, "one")
        rt.cancel(sk)                       # Stop pressed after the turn ended
        rt._run_loop(sk, "two")
    assert llm.call_count == 2
    assert rt.get_last_turn_result(sk).status == TurnStatus.COMPLETED
    rt.stop()


def test_stop_mid_turn_cancels_only_that_turn(tmp_path):
    rt = AgentRuntime(_make_cfg(), GLib=None)
    rt.start()
    sk = "agent:spec61-a1b"
    rt.create_conversation("Coder", sk, str(tmp_path))
    calls = []

    def llm(*_a, **_k):
        calls.append(1)
        if len(calls) == 1:
            rt.cancel(sk)
            return _resp("", tool_calls=[{
                "id": "c1",
                "function": {"name": "list_files", "arguments": '{"path": "."}'},
            }])
        return _resp("hi")

    with patch.object(rt, "_call_llm", side_effect=llm), \
         patch(
             "agent.tools.execute_tool",
             return_value=ToolResult(success=True, output="ok"),
         ):
        rt._run_loop(sk, "one")
        first = rt.get_last_turn_result(sk)
        rt._run_loop(sk, "two")
    assert first.status == TurnStatus.CANCELLED
    assert first.metadata["reason"] == "user"
    assert rt.get_last_turn_result(sk).status == TurnStatus.COMPLETED
    assert len(calls) == 2
    rt.stop()


def test_cancel_peer_on_idle_session_leaves_no_mark():
    rt = AgentRuntime(config=AgentConfig())
    rt.cancel_peer("agent:coder")
    assert "agent:coder" not in rt._cancelled
    assert "agent:coder" in rt._cancelled_epochs


# ── A2: Stop ends a streaming reply at the next SSE event ───────────────────


def test_cancel_mid_stream_raises_turninactive_and_closes_stream(tmp_path):
    rt = AgentRuntime(_make_cfg(), GLib=None)
    rt.start()
    sk = _uniq()
    rt.create_conversation("Coder", sk, str(tmp_path))

    from agent.llm.streaming import SSEEvent

    yielded = []

    def fake_stream(**_kw):
        for i in range(3):
            yielded.append(i)
            yield SSEEvent(type="text_delta", data={"content": f"c{i}"})

    provider = MagicMock()
    provider.stream = fake_stream

    def on_first_delta(sk, text, **_kw):
        rt.cancel(sk)

    rt._on_text_delta = on_first_delta

    with patch("agent.runtime._get_provider", return_value=provider), \
         pytest.raises(TurnInactive) as excinfo:
        rt._call_llm_streaming(
            session_key=sk,
            base_url="https://api.openai.com/v1",
            api_key="test",
            model="openai/gpt-4o",
            caller_key="openai",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            timeout=30.0,
        )
    assert excinfo.value.reason == "cancelled"
    # The check sits at the top of the event loop, so only the first delta
    # is dispatched; the third event is never pulled from the generator.
    assert len(yielded) == 2
    assert sk not in rt._last_stream_text
    rt.stop()


# ── B7: per-chunk dispatch unchanged with parts-list accumulation ───────────


def test_streaming_delta_dispatch_unchanged_three_chunks(tmp_path):
    rt = AgentRuntime(_make_cfg(), GLib=None)
    rt.start()
    sk = _uniq()
    rt.create_conversation("Coder", sk, str(tmp_path))
    deltas = []
    rt._on_text_delta = lambda sk2, t: deltas.append(t)

    from agent.llm.streaming import SSEEvent

    provider = MagicMock()
    provider.stream.return_value = iter([
        SSEEvent(type="text_delta", data={"content": "Hello"}),
        SSEEvent(type="text_delta", data={"content": " world"}),
        SSEEvent(type="text_delta", data={"content": "!"}),
        SSEEvent(type="done", data={}),
    ])
    with patch("agent.runtime._get_provider", return_value=provider), \
         patch.object(rt, "_call_llm", _make_streaming_lambda(rt)):
        rt._run_loop(sk, "say hello")

    assert len(deltas) == 3, f"Expected 3 deltas, got {len(deltas)}: {deltas}"
    assert deltas[0] == "Hello"
    assert deltas[1] == " world"
    assert deltas[2] == "!"
    result = rt.get_last_turn_result(sk)
    assert result is not None
    assert result.status == TurnStatus.COMPLETED
    assert result.text == "Hello world!"
    rt.stop()
