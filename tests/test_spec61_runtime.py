# tests/test_spec61_runtime.py
# SPEC-61 Wave 1 — A1 cancel lifecycle, A2 mid-stream cancel, B7 parts join.
#
# A1 (three probe tests from spec §2): a Stop must belong to the turn it was
# pressed in — never to the user's NEXT message. A2: Stop ends a streaming
# reply at the next SSE event. B7: streamed chunks collect in a list and join
# once, so per-chunk dispatch is unchanged.

import os
import threading
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

from agent.config import AgentConfig
from agent.runtime import AgentRuntime, TurnInactive, TurnStatus
from agent.tools import ToolResult
from models.conversation import MessageRole


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


# ── A3: per-turn records are pruned when a new turn registers ────────────────


def test_turn_state_prunes_older_entries_on_new_turn(tmp_path):
    rt = AgentRuntime(_make_cfg(), GLib=None)
    rt.start()
    sk = _uniq()
    rt.create_conversation("Coder", sk, str(tmp_path))
    with patch.object(rt, "_call_llm", return_value=_resp("ok")):
        rt._run_loop(sk, "one", turn_token="t1")
        rt._run_loop(sk, "two", turn_token="t2")
        rt._run_loop(sk, "three", turn_token="t3")
    remaining = [k for k in rt._turn_state if k[0] == sk]
    assert len(remaining) == 1
    assert remaining == [(sk, "t3")]
    rt.stop()


def test_turn_state_prune_is_session_scoped(tmp_path):
    """A new turn for one session must not prune another session's records."""
    rt = AgentRuntime(_make_cfg(), GLib=None)
    rt.start()
    sk_a = _uniq()
    sk_b = _uniq()
    rt.create_conversation("Coder", sk_a, str(tmp_path))
    rt.create_conversation("Coder", sk_b, str(tmp_path))
    with patch.object(rt, "_call_llm", return_value=_resp("ok")):
        rt._run_loop(sk_a, "a1", turn_token="a1")
        rt._run_loop(sk_b, "b1", turn_token="b1")
        # A new turn for session A must not prune session B's records.
        rt._run_loop(sk_a, "a2", turn_token="a2")
    assert [k for k in rt._turn_state if k[0] == sk_a] == [(sk_a, "a2")]
    assert [k for k in rt._turn_state if k[0] == sk_b] == [(sk_b, "b1")]
    assert [k for k in rt._turn_results if k[0] == sk_b] == [(sk_b, "b1")]
    rt.stop()


# ── A6: summary calls pass the full provider/model id ─────────────────────────


class _CapturingProvider:
    def __init__(self):
        self.calls = []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        return {"content": "captured"}


def test_summary_passes_full_model_id():
    from agent.config import LLMProviderConfig
    providers = {
        "openrouter": LLMProviderConfig(
            name="openrouter",
            base_url="https://x.example.com/v1",
            api_key="sk-p1",
            default_model="openrouter/deepseek/deepseek-v4-pro",
            caller="openrouter",
            reasoning_effort="off",
            supports_reasoning=False,
        )
    }
    rt = AgentRuntime(config=AgentConfig(providers=providers, default_provider="openrouter"))
    cap = _CapturingProvider()
    with patch("agent.runtime._get_provider", return_value=cap):
        rt._call_for_summary("sys", "user", model_id="openrouter/deepseek/deepseek-v4-pro")
    assert cap.calls[0]["model"] == "openrouter/deepseek/deepseek-v4-pro"


def test_summary_parses_anthropic_shaped_response():
    """A summary response shaped for the anthropic parser (no 'choices') parses.

    Pins the A7 shape-first format pick in _call_for_summary: a response
    without 'choices' must fall through to the caller key and be parsed
    with the anthropic extractor. If the pick reverts to prefix-first
    (provider_name="p1" → "openai"), extraction returns "".
    """
    from agent.config import LLMProviderConfig
    providers = {
        "p1": LLMProviderConfig(
            name="p1",
            base_url="https://x.example.com/v1",
            api_key="sk-p1",
            default_model="p1/m1",
            caller="anthropic",
            reasoning_effort="off",
            supports_reasoning=False,
        )
    }
    rt = AgentRuntime(config=AgentConfig(providers=providers, default_provider="p1"))

    class _AnthropicShapedProvider:
        def call(self, **kwargs):
            return {"content": [{"type": "text", "text": "summary text"}]}

    with patch("agent.runtime._get_provider", return_value=_AnthropicShapedProvider()):
        text = rt._call_for_summary("sys", "user", model_id="p1/m1")
    assert text == "summary text"


# ── A7: Anthropic streaming folds usage into OpenAI keys ──────────────────────


def test_anthropic_usage_merge_message_start_and_delta():
    from agent.llm.anthropic_provider import AnthropicProvider

    raw_sse = (
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":11,"output_tokens":1}}}\n\n'
        b'event: message_delta\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":7}}\n\n'
        b'event: message_stop\n'
        b'data: {"type":"message_stop"}\n\n'
    )

    class _FakeResp:
        def __init__(self, buf):
            self._buf = buf

        def __iter__(self):
            return iter(self._buf.splitlines(keepends=True))

    class _Ctx:
        def __enter__(self):
            return _FakeResp(raw_sse)

        def __exit__(self, *_a):
            pass

    with patch(
        "agent.llm.anthropic_provider.urlopen_with_ssl_retry",
        lambda req, timeout: _Ctx(),
    ):
        events = list(AnthropicProvider().stream(
            base_url="https://api.anthropic.com/v1",
            api_key="***",
            model="claude-3-5-sonnet",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            timeout=30.0,
            x_title="",
        ))
    usage_events = [ev for ev in events if ev.type == "usage"]
    assert usage_events, "expected at least one usage event"
    assert usage_events[-1].data["usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
    }


def test_run_loop_parses_anthropic_shaped_response(tmp_path):
    """A non-streamed anthropic-caller response without 'choices' surfaces text.

    Pins the A7 shape-first format pick in _run_loop: a response with no
    'choices' key must fall through to the caller key ("anthropic") and be
    parsed with the anthropic extractor. If the pick reverts to prefix-first
    (loop_provider="claude-3-5-sonnet" → "openai"), extraction returns ""
    and the turn fails with empty_content.
    """
    from agent.config import LLMProviderConfig
    providers = {
        "anthropic": LLMProviderConfig(
            name="anthropic",
            base_url="https://api.anthropic.com/v1",
            api_key="sk-ant-test",
            default_model="claude-3-5-sonnet",
            caller="anthropic",
            reasoning_effort="off",
            supports_reasoning=False,
        )
    }
    rt = AgentRuntime(config=AgentConfig(providers=providers, default_provider="anthropic"), GLib=None)
    rt.start()
    sk = _uniq()
    rt.create_conversation(
        "Coder", sk, str(tmp_path),
        model="claude-3-5-sonnet", provider="anthropic",
    )
    anthropic_shape = {"content": [{"type": "text", "text": "Hello from Claude"}]}
    with patch.object(rt, "_call_llm", return_value=anthropic_shape):
        rt._run_loop(sk, "hello")
    result = rt.get_last_turn_result(sk)
    assert result.status == TurnStatus.COMPLETED
    assert result.text == "Hello from Claude"
    rt.stop()


def test_anthropic_malformed_usage_does_not_fail_turn(tmp_path):
    """A non-dict usage payload in message_start must not kill the turn.

    The provider's usage payload is not guaranteed to be a JSON object.
    _merge_usage must ignore a malformed payload (string/list/int) exactly
    as the pre-fix code did. Without the type guard, AttributeError escapes
    the streaming path and the turn FAILED with reason 'exception'.
    """
    from agent.llm.anthropic_provider import AnthropicProvider

    raw_sse = (
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"usage":"boom"}}\n\n'
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","index":0,"delta":'
        b'{"type":"text_delta","text":"Hello"}}\n\n'
        b'event: message_stop\n'
        b'data: {"type":"message_stop"}\n\n'
    )

    class _FakeResp:
        def __init__(self, buf):
            self._buf = buf

        def __iter__(self):
            return iter(self._buf.splitlines(keepends=True))

    class _Ctx:
        def __enter__(self):
            return _FakeResp(raw_sse)

        def __exit__(self, *_a):
            pass

    rt = AgentRuntime(_make_cfg(), GLib=None)
    rt.start()
    sk = _uniq()
    rt.create_conversation("Coder", sk, str(tmp_path))

    def _stream_lambda(*a, **kw):
        return rt._call_llm_streaming(
            session_key=a[0],
            base_url="https://api.anthropic.com/v1",
            api_key="***",
            model="claude-3-5-sonnet",
            caller_key="anthropic",
            messages=a[1],
            tools=a[2] if len(a) > 2 else None,
            timeout=30.0,
        )

    with patch(
        "agent.llm.anthropic_provider.urlopen_with_ssl_retry",
        lambda req, timeout: _Ctx(),
    ), patch(
        "agent.runtime._get_provider",
        return_value=AnthropicProvider(),
    ), patch.object(rt, "_call_llm", _stream_lambda):
        rt._run_loop(sk, "hello")

    result = rt.get_last_turn_result(sk)
    assert result.status == TurnStatus.COMPLETED
    assert result.text == "Hello"
    rt.stop()


def _run_anthropic_stream_turn(tmp_path, raw_sse, expected_text="Hello"):
    """Run one turn through the real Anthropic streaming path with the given SSE bytes.

    Exercises the full wire surface: AnthropicProvider().stream() parses
    the SSE frames, _call_llm_streaming assembles the OpenAI-shaped
    response, and _run_loop picks the format by shape. This mirrors the
    plumbing of test_anthropic_malformed_usage_does_not_fail_turn.
    """
    from agent.config import LLMProviderConfig
    from agent.llm.anthropic_provider import AnthropicProvider

    class _FakeResp:
        def __init__(self, buf):
            self._buf = buf

        def __iter__(self):
            return iter(self._buf.splitlines(keepends=True))

    class _Ctx:
        def __enter__(self):
            return _FakeResp(raw_sse)

        def __exit__(self, *_a):
            pass

    cfg = _make_cfg()
    # Bind the conversation to an anthropic-caller card so the loop's caller
    # resolution returns "anthropic". Without this the card lookup misses,
    # the caller key resolves to "" and _RESPONSE_FORMAT falls back to
    # "openai" via the model prefix — the shape-first branch becomes
    # unobservable and this helper pins nothing (mutant survives).
    cfg.providers["anthropic"] = LLMProviderConfig(
        name="anthropic",
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        default_model="claude-3-5-sonnet",
        caller="anthropic",
    )
    rt = AgentRuntime(cfg, GLib=None)
    rt.start()
    sk = _uniq()
    rt.create_conversation("Coder", sk, str(tmp_path))
    rt.get_conversation(sk).provider = "anthropic"

    def _stream_lambda(*a, **kw):
        return rt._call_llm_streaming(
            session_key=a[0],
            base_url="https://api.anthropic.com/v1",
            api_key="***",
            model="claude-3-5-sonnet",
            caller_key="anthropic",
            messages=a[1],
            tools=a[2] if len(a) > 2 else None,
            timeout=30.0,
        )

    with patch(
        "agent.llm.anthropic_provider.urlopen_with_ssl_retry",
        lambda req, timeout: _Ctx(),
    ), patch(
        "agent.runtime._get_provider",
        return_value=AnthropicProvider(),
    ), patch.object(rt, "_call_llm", _stream_lambda):
        rt._run_loop(sk, "hello")

    result = rt.get_last_turn_result(sk)
    assert result.status == TurnStatus.COMPLETED
    assert result.text == expected_text
    rt.stop()


def test_anthropic_message_start_null_usage_does_not_fail_turn(tmp_path):
    """A null usage field in message_start must not kill the turn.

    The real-world failure mode behind the audit is a missing/failed usage
    field, not only a string one: {"message":{"usage":null}}. It must fall
    back to {} without raising, and the text delta must still surface.
    """
    raw_sse = (
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"usage":null}}\n\n'
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","index":0,"delta":'
        b'{"type":"text_delta","text":"Hello"}}\n\n'
        b'event: message_stop\n'
        b'data: {"type":"message_stop"}\n\n'
    )
    _run_anthropic_stream_turn(tmp_path, raw_sse)


def test_anthropic_usage_bool_tokens_are_rejected():
    """F4: bool is an int subclass — True must not fold as prompt_tokens=1.

    Drops the ``not isinstance(..., bool)`` terms in _merge_usage and this
    fails: {"input_tokens": True} folds prompt_tokens=True (== 1).
    """
    from agent.llm.anthropic_provider import _merge_usage

    acc: dict = {}
    assert _merge_usage(acc, {"input_tokens": True, "output_tokens": False}) is False
    assert acc == {}
    assert _merge_usage(acc, {"input_tokens": 5, "output_tokens": 2}) is True
    assert acc == {"prompt_tokens": 5, "completion_tokens": 2}


def test_anthropic_message_start_non_dict_message_does_not_fail_turn(tmp_path):
    """A non-object 'message' field in message_start must not kill the turn.

    The pre-fix call site (d.get("message") or {}).get("usage") raised
    AttributeError when d["message"] was a *truthy* non-dict (e.g. a JSON
    string), because a non-empty string skips the {} fallback. The usage
    extraction must be type-checked before .get, and the turn must still
    complete and stream its text.
    """
    raw_sse = (
        b'event: message_start\n'
        b'data: {"type":"message_start","message":"boom"}\n\n'
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","index":0,"delta":'
        b'{"type":"text_delta","text":"Hello"}}\n\n'
        b'event: message_stop\n'
        b'data: {"type":"message_stop"}\n\n'
    )
    _run_anthropic_stream_turn(tmp_path, raw_sse)


def test_anthropic_message_delta_non_dict_usage_does_not_fail_turn(tmp_path):
    """A non-dict usage payload on message_delta must not kill the turn.

    Covers both fallback paths at the message_delta call site: a truthy
    non-dict usage (int 7) must be ignored, and a missing usage key must
    fall back to {}. The text delta is still surfaced.
    """
    raw_sse = (
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":11,"output_tokens":1}}}\n\n'
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","index":0,"delta":'
        b'{"type":"text_delta","text":"Hello"}}\n\n'
        b'event: message_delta\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":7}\n\n'
        b'event: message_delta\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
        b'event: message_stop\n'
        b'data: {"type":"message_stop"}\n\n'
    )
    _run_anthropic_stream_turn(tmp_path, raw_sse)


def test_run_loop_streamed_anthropic_reply_surfaces_text(tmp_path):
    """A streamed Anthropic reply routes by shape and surfaces its text.

    Real wire path: AnthropicProvider().stream() parses the SSE frames and
    _call_llm_streaming assembles an OpenAI-shaped response ({"choices":
    ...}). _run_loop must pick openai from the shape and extract the text
    — not parse the assembled response with the anthropic extractor (which
    yields "" and fails the turn with empty_content).
    """
    raw_sse = (
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":11,"output_tokens":1}}}\n\n'
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","index":0,"delta":'
        b'{"type":"text_delta","text":"Hello from Claude"}}\n\n'
        b'event: message_stop\n'
        b'data: {"type":"message_stop"}\n\n'
    )
    _run_anthropic_stream_turn(tmp_path, raw_sse, expected_text="Hello from Claude")


# ── B3: parallel adjacent read-only tool calls ───────────────────────────────


def test_parallel_read_run_overlaps_and_preserves_order(tmp_path):
    """Two adjacent reads run concurrently; a write still fences later reads.

    [read a, read b, write c, read c] forms run {0: 2}. The first two
    reads must overlap (total wall time under 0.35 s with 0.2 s sleeps),
    the read of c must start only after the write returned, and
    conv.messages keeps tool results in call order.
    """
    target_a = tmp_path / "a.txt"
    target_b = tmp_path / "b.txt"
    target_c = tmp_path / "c.txt"
    target_a.write_text("A" * 100)
    target_b.write_text("B" * 100)
    target_c.write_text("C" * 100)

    rt = AgentRuntime(_make_cfg(), GLib=None)
    sk = _uniq()
    rt.create_conversation("Coder", sk, str(tmp_path), defer_prompt_build=True)
    conv = rt.get_conversation(sk)

    recorded = []
    lock = threading.Lock()

    def fake_execute_tool(tool_name, args, project_path, session_key, **kwargs):
        path = args.get("path", "")
        thread = threading.current_thread().name
        start = time.monotonic()
        if tool_name == "read_file":
            time.sleep(0.2)
        with lock:
            recorded.append((tool_name, path, thread, start))
        if tool_name == "read_file":
            return ToolResult(success=True, output=f"read:{os.path.basename(path)}")
        return ToolResult(success=True, output=f"wrote:{os.path.basename(path)}")

    batch = [
        ("c1", "read_file", {"path": str(target_a)}),
        ("c2", "read_file", {"path": str(target_b)}),
        ("c3", "write_file", {"path": str(target_c), "content": "C"}),
        ("c4", "read_file", {"path": str(target_c)}),
    ]
    with patch("agent.tools.execute_tool", side_effect=fake_execute_tool):
        assert rt._execute_tool_batch(sk, None, conv, "reading", batch, 1) is False

    # First two reads ran on distinct pool threads and overlapped.
    read_events = [(p, t, s) for (n, p, t, s) in recorded if n == "read_file"]
    a, b = read_events[0], read_events[1]
    assert a[1] != b[1], f"reads should run on different threads: {a[1]} vs {b[1]}"
    span = max(a[2], b[2]) + 0.2 - min(a[2], b[2])
    assert span < 0.35, f"adjacent reads did not overlap: span={span:.3f}s"

    # The read of c starts only after the write of c returned.
    def _idx(name, path):
        for i, (n, p, _t, _s) in enumerate(recorded):
            if n == name and p == path:
                return i
        raise AssertionError(f"{name} {path} not recorded")

    assert _idx("write_file", str(target_c)) < _idx("read_file", str(target_c))

    # conv.messages carries tool results in call order.
    bodies = [m.content for m in conv.messages if m.role == MessageRole.TOOL_RESULT]
    assert bodies == ["read:a.txt", "read:b.txt", "wrote:c.txt", "read:c.txt"]


# ── B2: build the wire once per iteration ────────────────────────────────────


def test_b2_wire_built_once_when_under_budget(tmp_path):
    """Under budget, compaction is a no-op and the wire is built exactly once."""
    rt = AgentRuntime(_make_cfg(), GLib=None)
    rt.start()
    sk = _uniq()
    rt.create_conversation("Coder", sk, str(tmp_path))
    calls = {"n": 0}
    real = rt._wire_messages_and_count

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    with patch.object(rt, "_wire_messages_and_count", side_effect=counting), \
         patch.object(rt, "_call_llm", lambda *a, **kw: _resp("done")):
        rt._run_loop(sk, "hello")

    result = rt.get_last_turn_result(sk)
    assert result.status == TurnStatus.COMPLETED
    assert calls["n"] == 1, f"expected one wire build, got {calls['n']}"
    rt.stop()


def test_b2_wire_rebuilt_when_compaction_bumps_gen(tmp_path):
    """When compaction trims (bumps _token_gen), the wire is rebuilt: two builds."""
    rt = AgentRuntime(_make_cfg(), GLib=None)
    rt.start()
    sk = _uniq()
    rt.create_conversation("Coder", sk, str(tmp_path), defer_prompt_build=True)
    conv = rt.get_conversation(sk)
    # A fat transcript so the (tiny) compaction ceiling forces a real trim.
    for _ in range(15):
        conv.add_user_message("u" * 2000)
        conv.add_assistant_message("a" * 2000, [])

    calls = {"n": 0}
    real = rt._wire_messages_and_count

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    with patch.object(rt, "_wire_messages_and_count", side_effect=counting), \
         patch.object(rt, "_compute_compaction_threshold", return_value=(4000, 5000)), \
         patch.object(rt, "_call_llm", lambda *a, **kw: _resp("done")):
        try:
            rt._run_loop(sk, "fix bar")
        except RuntimeError as exc:
            # The post-compaction wire may still exceed the tiny ceiling;
            # the rebuild happens before that raise, so the count stands.
            assert "after compaction" in str(exc)

    assert calls["n"] == 2, f"expected two wire builds (rebuild after compaction), got {calls['n']}"
    rt.stop()


def test_parallel_prefetched_read_exception_propagates(tmp_path):
    """An exception from a prefetched read surfaces like a serial one."""
    target = tmp_path / "a.txt"
    target.write_text("A")

    rt = AgentRuntime(_make_cfg(), GLib=None)
    sk = _uniq()
    rt.create_conversation("Coder", sk, str(tmp_path), defer_prompt_build=True)
    conv = rt.get_conversation(sk)

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    batch = [
        ("c1", "read_file", {"path": str(target)}),
        ("c2", "read_file", {"path": str(target)}),
    ]
    with patch("agent.tools.execute_tool", side_effect=boom):
        with pytest.raises(RuntimeError, match="boom"):
            rt._execute_tool_batch(sk, None, conv, "reading", batch, 1)
