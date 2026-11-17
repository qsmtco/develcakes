"""SPEC-64: Runtime turn-text continuity & streaming transport hardening tests.

Covers Findings 1A, 1B, 1C, 4A, and 4B from
docs/audits/2026-11-14-ASK-COMMAND-STREAMING-INVESTIGATION-READ-ONLY.md.
"""

from __future__ import annotations

import json
import unittest.mock
import uuid

import pytest

from agent.audit import AuditLog
from agent.llm.anthropic_provider import AnthropicProvider, _anthropic_max_output_tokens
from agent.llm.convert import convert_messages_for_anthropic
from agent.llm.extractors import extract_text_content
from agent.llm.minimax_provider import MiniMaxProvider, _handle_finish_frame
from agent.llm.openai_provider import (
    _MAX_NON_SSE_FALLBACK_BYTES,
    OpenAIProvider,
)
from agent.llm.streaming import SSEEvent, coerce_content_text, parse_sse_delta
from agent.runtime import AgentRuntime, TurnStatus
from tests.test_agent_runtime import _make_cfg, _resp


@pytest.fixture(scope="module", autouse=True)
def _hermetic_audit_flush():
    """Keep tool-loop runs hermetic by stubbing AuditLog disk flushes."""
    original = AuditLog.flush_audit_log
    AuditLog.flush_audit_log = unittest.mock.MagicMock(return_value=None)
    yield
    AuditLog.flush_audit_log = original


def _uniq() -> str:
    return f"s64_{uuid.uuid4().hex[:8]}"


class _SinglePassHTTPResponse:
    """Single-pass iterator mirroring http.client.HTTPResponse semantics."""

    def __init__(self, lines: list[bytes]) -> None:
        self._iter = iter(lines)

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        return next(self._iter)


# ── Findings 1A & 1C: Turn-text continuity (Acceptance Row R-8) ──────────────


class TestTurnTextContinuity:
    """Findings 1A and 1C: multi-iteration prose and graceful-close text continuity."""

    def test_graceful_iteration_close_preserves_streamed_prose_and_relays(
        self, tmp_path, monkeypatch
    ):
        """Finding 1A reproduction: Debugger streams prose across tool calls up
        to max_tool_iterations with no DecisionModel card; TurnResult.text must
        carry both the streamed prose and the close note so AgentCommandHandler
        relays Debugger's findings back to Supervisor."""
        from ui.handlers.agent_command_handler import AgentCommandHandler

        cfg = _make_cfg()
        cfg.max_tool_iterations = 2
        cfg.tool_iteration_hard_cap = 10

        monkeypatch.setattr(
            "agent.llm.jev_client.post_decision",
            lambda state, questions: (None, "DecisionModel not configured"),
        )

        completed: list[tuple[str, str]] = []
        rt = AgentRuntime(
            cfg,
            on_response_complete=lambda sk, txt, **_kw: completed.append((sk, txt)),
        )
        rt.start()
        sk = "agent:debugger"
        rt.create_conversation("Debugger", sk, str(tmp_path))

        step = [0]

        def fake_llm(_sk, _msgs, _tools, **_kwargs):
            step[0] += 1
            return _resp(
                content=f"Investigation note {step[0]}: verified caller path.",
                tool_calls=[
                    {
                        "id": f"tc_{step[0]}",
                        "function": {"name": "list_files", "arguments": '{"path": "."}'},
                    }
                ],
            )

        try:
            with unittest.mock.patch.object(rt, "_call_llm", fake_llm):
                rt._run_loop(sk, "[Supervisor asks]: check caller path")
            turn_res = rt.get_last_turn_result(sk)
            assert turn_res is not None
            assert turn_res.status == TurnStatus.COMPLETED
            assert "Investigation note 1: verified caller path." in turn_res.text
            assert "Investigation note 2: verified caller path." in turn_res.text
            assert "[tool loop closed — iteration_judge at 2 iterations" in turn_res.text
            assert len(completed) == 1
            assert completed[0] == (sk, turn_res.text)

            # Verify end-to-end relay through AgentCommandHandler (would be
            # skipped when TurnResult.text was "" in Finding 1A).
            ach = AgentCommandHandler()
            relayed: list[tuple[str, str]] = []

            class _StubARH:
                def get_special_agents(self):
                    return {
                        "agent:debugger": "Debugger",
                        "agent:supervisor": "Supervisor",
                    }

                def send_to_special_agent(self, target_sk, text):
                    relayed.append((target_sk, text))

            ach.set_agent_runtime_handler(_StubARH())
            ach._pending_asks["agent:debugger"] = "agent:supervisor"
            if completed[0][1]:
                ach.on_agent_response(sk, completed[0][1], "develcakes")

            assert len(relayed) == 1
            assert relayed[0][0] == "agent:supervisor"
            assert relayed[0][1].startswith("[Debugger responded]: ")
            assert "Investigation note 1" in relayed[0][1]
            assert "agent:debugger" not in ach._pending_asks
        finally:
            rt.stop()

    def test_graceful_iteration_close_tool_only_turn_emits_close_note(
        self, tmp_path, monkeypatch
    ):
        """Tool-only turn (no prior prose) hitting soft cap sets TurnResult.text to close_note."""
        cfg = _make_cfg()
        cfg.max_tool_iterations = 2
        cfg.tool_iteration_hard_cap = 10
        monkeypatch.setattr(
            "agent.llm.jev_client.post_decision",
            lambda state, questions: (None, "no card"),
        )
        rt = AgentRuntime(cfg)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Debugger", sk, str(tmp_path))

        idx = [0]

        def silent_tool_llm(_sk, _msgs, _tools, **_kwargs):
            idx[0] += 1
            return _resp(
                content="",
                tool_calls=[
                    {
                        "id": f"c_{idx[0]}",
                        "function": {"name": "list_files", "arguments": '{"path": "."}'},
                    }
                ],
            )

        try:
            with unittest.mock.patch.object(rt, "_call_llm", silent_tool_llm):
                rt._run_loop(sk, "list everything")
            res = rt.get_last_turn_result(sk)
            assert res is not None
            assert res.status == TurnStatus.COMPLETED
            assert res.text.startswith("[tool loop closed — iteration_judge at 2 iterations")
        finally:
            rt.stop()

    def test_multi_iteration_prose_joined_without_duplicating_transcript(self, tmp_path):
        """Finding 1C reproduction: /ask emitted in Iteration 1 alongside a tool
        call survives into TurnResult.text when Iteration 2 finishes with a short
        status line, while conv.messages stores each segment only once."""
        from ui.handlers.agent_command_handler import _extract_quoted_commands

        rt = AgentRuntime(_make_cfg())
        rt.start()
        sk = _uniq()
        rt.create_conversation("Supervisor", sk, str(tmp_path))

        responses = [
            _resp(
                content='Delegating audit now.\n/ask @Debugger "audit agent/runtime.py"',
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {"name": "list_files", "arguments": '{"path": "."}'},
                    }
                ],
            ),
            _resp(content="Logged delegation; awaiting Debugger."),
        ]

        try:
            with unittest.mock.patch.object(
                rt, "_call_llm", side_effect=responses
            ):
                rt._run_loop(sk, "start audit")
            res = rt.get_last_turn_result(sk)
            assert res is not None
            assert res.status == TurnStatus.COMPLETED
            assert res.text == (
                'Delegating audit now.\n/ask @Debugger "audit agent/runtime.py"'
                "\n\nLogged delegation; awaiting Debugger."
            )
            cmds = _extract_quoted_commands(res.text, {"ask"})
            assert len(cmds) == 1
            assert cmds[0].agent == "@Debugger"
            assert cmds[0].payload == "audit agent/runtime.py"

            conv = rt.get_conversation(sk)
            assert conv is not None
            asst_msgs = [m for m in conv.messages if m.role.value == "assistant"]
            assert len(asst_msgs) == 2
            assert asst_msgs[0].content == 'Delegating audit now.\n/ask @Debugger "audit agent/runtime.py"'
            assert asst_msgs[1].content == "Logged delegation; awaiting Debugger."
        finally:
            rt.stop()

    def test_mid_turn_inbox_drain_preserves_prior_final_prose(self, tmp_path):
        """Prose from a finishing pass that drains _inbox and continues is retained."""
        rt = AgentRuntime(_make_cfg())
        rt.start()
        sk = _uniq()
        rt.create_conversation("Supervisor", sk, str(tmp_path))

        call_num = [0]

        def llm_with_inbox_injection(_sk, _msgs, _tools, **_kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                with rt._lock:
                    rt._inbox.setdefault(sk, []).append("follow-up question")
                return _resp(content="First part answer.")
            return _resp(content="Second part answer.")

        try:
            with unittest.mock.patch.object(rt, "_call_llm", llm_with_inbox_injection):
                rt._run_loop(sk, "initial question")
            res = rt.get_last_turn_result(sk)
            assert res is not None
            assert res.status == TurnStatus.COMPLETED
            assert res.text == "First part answer.\n\nSecond part answer."
        finally:
            rt.stop()

    def test_peek_pending_inbound_happy_and_sad_inputs(self):
        """peek_pending_inbound returns a non-destructive copy and rejects bad keys."""
        rt = AgentRuntime(_make_cfg())
        sk = _uniq()
        with rt._lock:
            rt._inbox[sk] = ["queued_1"]
            rt._stranded[sk] = ["stranded_1"]

        peeked = rt.peek_pending_inbound(sk)
        assert peeked == ["queued_1", "stranded_1"]
        peeked.append("mutated")
        assert rt.peek_pending_inbound(sk) == ["queued_1", "stranded_1"]
        assert rt.peek_pending_inbound("") == []
        assert rt.peek_pending_inbound(None) == []  # type: ignore[arg-type]
        assert rt.peek_pending_inbound(42) == []  # type: ignore[arg-type]


# ── Finding 1B: Post-tool empty follow-up (Acceptance Row R-9) ───────────────


class TestPostToolEmptyFollowup:
    """Finding 1B: prose + tool call in Iteration N followed by empty content in N+1."""

    def test_post_tool_empty_followup_completes_with_prior_prose_and_anthropic_valid_roles(
        self, tmp_path
    ):
        """Iteration 1 emits prose + tool call; Iteration 2 emits empty content
        with finish_reason='stop'. Turn must complete COMPLETED with Iteration 1's
        prose and maintain strict user/assistant alternation for Anthropic."""
        errors: list[object] = []
        completed: list[str] = []
        rt = AgentRuntime(
            _make_cfg(),
            on_error=lambda _sk, msg, **_kw: errors.append(msg),
            on_response_complete=lambda _sk, txt, **_kw: completed.append(txt),
        )
        rt.start()
        sk = _uniq()
        rt.create_conversation("Debugger", sk, str(tmp_path))

        responses = [
            _resp(
                content='Root cause confirmed in turn_ui.py.\n/ask @Supervisor "ready for fix"',
                tool_calls=[
                    {
                        "id": "call_ls",
                        "function": {"name": "list_files", "arguments": '{"path": "."}'},
                    }
                ],
            ),
            _resp(content=""),
        ]

        try:
            with unittest.mock.patch.object(rt, "_call_llm", side_effect=responses):
                rt._run_loop(sk, "diagnose bug")
            res = rt.get_last_turn_result(sk)
            assert res is not None
            assert res.status == TurnStatus.COMPLETED
            assert res.text == 'Root cause confirmed in turn_ui.py.\n/ask @Supervisor "ready for fix"'
            assert res.metadata.get("completed_from_prior_iteration_text") is True
            assert errors == []
            assert completed == [res.text]

            conv = rt.get_conversation(sk)
            assert conv is not None
            assert conv.messages[-1].role.value == "assistant"
            assert conv.messages[-1].content == "[completed after tool execution]"

            # Append next user turn and verify Anthropic message conversion has
            # zero adjacent user messages (prevents HTTP 400).
            conv.add_user_message("follow-up user turn")
            non_sys = [m for m in conv.to_api_messages() if m.get("role") != "system"]
            anth_msgs = convert_messages_for_anthropic(non_sys)
            roles = [m["role"] for m in anth_msgs]
            for idx in range(len(roles) - 1):
                assert not (roles[idx] == "user" and roles[idx + 1] == "user"), (
                    f"Adjacent user roles at {idx}..{idx + 1}: {roles}"
                )
        finally:
            rt.stop()

    def test_single_iteration_empty_response_still_fails(self, tmp_path):
        """Sad path: empty content with no prior prose must still fail empty_content."""
        rt = AgentRuntime(_make_cfg())
        rt.start()
        sk = _uniq()
        rt.create_conversation("Debugger", sk, str(tmp_path))
        try:
            with unittest.mock.patch.object(rt, "_call_llm", return_value=_resp(content="")):
                rt._run_loop(sk, "hello")
            res = rt.get_last_turn_result(sk)
            assert res is not None
            assert res.status == TurnStatus.FAILED
            assert res.metadata.get("reason") == "empty_content"
        finally:
            rt.stop()

    def test_tool_only_turn_followed_by_empty_response_still_fails(self, tmp_path):
        """Sad path: tool call with empty content followed by whitespace-only content fails."""
        rt = AgentRuntime(_make_cfg())
        rt.start()
        sk = _uniq()
        rt.create_conversation("Debugger", sk, str(tmp_path))
        responses = [
            _resp(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {"name": "list_files", "arguments": '{"path": "."}'},
                    }
                ],
            ),
            _resp(content=" \n\t\u200b "),
        ]
        try:
            with unittest.mock.patch.object(rt, "_call_llm", side_effect=responses):
                rt._run_loop(sk, "check files")
            res = rt.get_last_turn_result(sk)
            assert res is not None
            assert res.status == TurnStatus.FAILED
            assert res.metadata.get("reason") == "empty_content"
        finally:
            rt.stop()

    def test_post_tool_empty_followup_with_stream_error_still_fails(self, tmp_path):
        """Sad path: mid-stream provider error on Iteration 2 must NOT be masked as COMPLETED."""
        rt = AgentRuntime(_make_cfg())
        rt.start()
        sk = _uniq()
        rt.create_conversation("Debugger", sk, str(tmp_path))
        err_resp = {
            "choices": [{"message": {"content": "", "tool_calls": []}}],
            "usage": {},
            "_stream_error": {"code": 429, "message": "Rate limit exceeded"},
        }
        responses = [
            _resp(
                content="Partial findings before rate limit.",
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {"name": "list_files", "arguments": '{"path": "."}'},
                    }
                ],
            ),
            err_resp,
        ]
        try:
            with unittest.mock.patch.object(rt, "_call_llm", side_effect=responses):
                rt._run_loop(sk, "run check")
            res = rt.get_last_turn_result(sk)
            assert res is not None
            assert res.status == TurnStatus.FAILED
            assert res.metadata.get("reason") == "empty_content"
            assert "429" in str(res.error)
        finally:
            rt.stop()

    def test_post_tool_calling_tools_stub_still_fails(self, tmp_path):
        """Sad path: '[calling tools]' hallucination on Iteration 2 must still fail."""
        rt = AgentRuntime(_make_cfg())
        rt.start()
        sk = _uniq()
        rt.create_conversation("Debugger", sk, str(tmp_path))
        responses = [
            _resp(
                content="Step 1 done.",
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {"name": "list_files", "arguments": '{"path": "."}'},
                    }
                ],
            ),
            _resp(content="[calling tools]"),
        ]
        try:
            with unittest.mock.patch.object(rt, "_call_llm", side_effect=responses):
                rt._run_loop(sk, "continue")
            res = rt.get_last_turn_result(sk)
            assert res is not None
            assert res.status == TurnStatus.FAILED
            assert "[calling tools]" in str(res.error)
        finally:
            rt.stop()


# ── Finding 4A: Anthropic max_tokens & output truncation telemetry ───────────


class TestOutputTruncationAndAnthropicMaxTokens:
    """Finding 4A: tiered Anthropic max_tokens and non-destructive truncation flag."""

    def test_anthropic_max_output_tokens_tiers_and_invalid_inputs(self):
        assert _anthropic_max_output_tokens("claude-3-opus-20240229") == 4096
        assert _anthropic_max_output_tokens("anthropic/claude-3-haiku-20240307") == 4096
        assert _anthropic_max_output_tokens("claude-3-5-sonnet-20240620") == 4096
        assert _anthropic_max_output_tokens("anthropic/claude-3-5-sonnet-20241022") == 8192
        assert _anthropic_max_output_tokens("claude-3-5-haiku-20241022") == 8192
        assert _anthropic_max_output_tokens("claude-3-7-sonnet-20250219") == 16384
        assert _anthropic_max_output_tokens("anthropic/claude-sonnet-4-20250514") == 16384
        assert _anthropic_max_output_tokens("claude-opus-4-1-20250805") == 16384
        assert _anthropic_max_output_tokens("") == 8192
        assert _anthropic_max_output_tokens(None) == 8192  # type: ignore[arg-type]

    def test_anthropic_call_and_stream_max_tokens_and_truncation_flag(self):
        provider = AnthropicProvider()
        captured_payloads: list[dict] = []

        def fake_urlopen(req, timeout):
            del timeout
            captured_payloads.append(json.loads(req.data.decode("utf-8")))
            resp = unittest.mock.MagicMock()
            resp.read.return_value = json.dumps(
                {
                    "content": [{"type": "text", "text": "cut off"}],
                    "stop_reason": "max_tokens",
                }
            ).encode("utf-8")
            resp.__enter__.return_value = resp
            return resp

        with unittest.mock.patch(
            "agent.llm.anthropic_provider.urlopen_with_ssl_retry", fake_urlopen
        ):
            out = provider.call(
                "https://api.anthropic.com/v1",
                "sk-ant",
                "anthropic/claude-sonnet-4-20250514",
                [{"role": "user", "content": "hi"}],
                None,
                30.0,
            )
        assert captured_payloads[0]["max_tokens"] == 16384
        assert out.get("_output_truncated") is True

        sse_raw = [
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"partial"}}',
            b'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"},"usage":{"output_tokens":4096}}',
            b'data: {"type":"message_stop"}',
        ]
        with unittest.mock.patch(
            "agent.llm.anthropic_provider.urlopen_with_ssl_retry",
            return_value=_SinglePassHTTPResponse(sse_raw),
        ):
            events = list(
                provider.stream(
                    "https://api.anthropic.com/v1",
                    "sk-ant",
                    "claude-3-haiku-20240307",
                    [{"role": "user", "content": "hi"}],
                    None,
                    30.0,
                )
            )
        assert events[-1] == SSEEvent(type="done", data={"truncated": True})

    def test_openai_and_minimax_truncation_propagates_without_polluting_text(
        self, tmp_path
    ):
        """finish_reason='length' sets metadata['output_truncated']=True while
        leaving TurnResult.text untouched so Pass 2 auto-close still recovers /ask."""
        from ui.handlers.agent_command_handler import _extract_quoted_commands

        minimax_evs = list(
            _handle_finish_frame(
                {"choices": [{"delta": {"content": "part"}, "finish_reason": "length"}]}
            )
        )
        assert minimax_evs[-1] == SSEEvent(type="done", data={"truncated": True})

        sse_lines_raw = [
            b'data: {"choices":[{"delta":{"content":"/ask @Debugger \\"unclosed payload"},"finish_reason":"length"}]}',
        ]
        cfg = _make_cfg()
        cfg.providers["openai"].caller = "openai"
        rt = AgentRuntime(cfg, on_text_delta=lambda _sk, _d, **_kw: None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Supervisor", sk, str(tmp_path), model="openai/gpt-4o")

        try:
            with (
                unittest.mock.patch.object(rt, "_live_provider_card", return_value=None),
                unittest.mock.patch(
                    "agent.llm.openai_provider.urlopen_with_ssl_retry",
                    return_value=_SinglePassHTTPResponse(sse_lines_raw),
                ),
            ):
                rt._run_loop(sk, "delegate")
            res = rt.get_last_turn_result(sk)
            assert res is not None
            assert res.status == TurnStatus.COMPLETED
            assert res.metadata.get("output_truncated") is True
            assert res.text == '/ask @Debugger "unclosed payload'
            cmds = _extract_quoted_commands(res.text, {"ask"})
            assert len(cmds) == 1
            assert cmds[0].payload == "unclosed payload"
        finally:
            rt.stop()

        # Verify MiniMaxProvider.call marks _output_truncated only on finish_reason='length'
        mm = MiniMaxProvider()
        mm_resp = unittest.mock.MagicMock()
        mm_resp.read.return_value = json.dumps(
            {
                "choices": [{"message": {"content": "cut"}, "finish_reason": "length"}],
                "base_resp": {"status_code": 0, "status_msg": "ok"},
            }
        ).encode("utf-8")
        mm_resp.__enter__.return_value = mm_resp
        with unittest.mock.patch(
            "agent.llm.minimax_provider.urlopen_with_ssl_retry", return_value=mm_resp
        ):
            mm_out = mm.call(
                "https://api.minimax.chat/v1",
                "sk-mm",
                "minimax/MiniMax-M2.7",
                [{"role": "user", "content": "hi"}],
                None,
                30.0,
            )
        assert mm_out.get("_output_truncated") is True

    def test_anthropic_truncation_propagates_without_polluting_text(
        self, tmp_path
    ):
        """Flagger BUG #9: the SAME metadata contract as the OpenAI/MiniMax
        case above, end to end over the REAL runtime loop with an
        Anthropic-shaped stream. The provider's final event is
        SSEEvent(type="done", data={"truncated": True}) (stop_reason
        max_tokens); runtime._call_llm_streaming folds it into
        result["_output_truncated"], the tool loop writes
        metadata["output_truncated"], and TurnResult.text stays untouched so
        Pass 2 auto-close can still recover /ask."""
        from ui.handlers.agent_command_handler import _extract_quoted_commands

        anth_lines_raw = [
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":11}}}',
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"/ask @Debugger \\"unclosed payload"}}',
            b'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"},"usage":{"output_tokens":4096}}',
            b"data: [DONE]",
        ]
        cfg = _make_cfg()
        cfg.providers["openai"].caller = "anthropic"
        rt = AgentRuntime(cfg, on_text_delta=lambda _sk, _d, **_kw: None)
        rt.start()
        sk = _uniq()
        rt.create_conversation("Supervisor", sk, str(tmp_path), model="openai/gpt-4o")

        try:
            with (
                unittest.mock.patch.object(rt, "_live_provider_card", return_value=None),
                unittest.mock.patch(
                    "agent.llm.anthropic_provider.urlopen_with_ssl_retry",
                    return_value=_SinglePassHTTPResponse(anth_lines_raw),
                ),
            ):
                rt._run_loop(sk, "delegate")
            res = rt.get_last_turn_result(sk)
            assert res is not None
            assert res.status == TurnStatus.COMPLETED
            assert res.metadata.get("output_truncated") is True
            assert res.text == '/ask @Debugger "unclosed payload'
            cmds = _extract_quoted_commands(res.text, {"ask"})
            assert len(cmds) == 1
            assert cmds[0].payload == "unclosed payload"
        finally:
            rt.stop()

        # Provider-level: the Anthropic streamer itself emits the truncated
        # done event only for stop_reason='max_tokens' (mirror of the
        # message_delta branch assertion above).
        provider = AnthropicProvider()
        with unittest.mock.patch(
            "agent.llm.anthropic_provider.urlopen_with_ssl_retry",
            return_value=_SinglePassHTTPResponse(anth_lines_raw),
        ):
            events = list(
                provider.stream(
                    "https://api.anthropic.com/v1",
                    "sk-ant",
                    "claude-3-haiku-20240307",
                    [{"role": "user", "content": "hi"}],
                    None,
                    30.0,
                )
            )
        assert events[-1] == SSEEvent(type="done", data={"truncated": True})


# ── Finding 4B: Structured content blocks & non-SSE JSON fallback ────────────


class TestStructuredContentAndNonSSEFallback:
    """Finding 4B: list[dict] content coercion and HTTP 200 non-SSE JSON fallback."""

    def test_coerce_content_text_and_extract_text_content_shapes(self):
        assert coerce_content_text(None) is None
        assert coerce_content_text("") == ""
        assert coerce_content_text("plain") == "plain"
        assert (
            coerce_content_text(
                [
                    {"type": "thinking", "thinking": "internal"},
                    {"type": "text", "text": "Hello "},
                    {"type": "output_text", "text": "world"},
                    "!",
                ]
            )
            == "Hello world!"
        )
        assert coerce_content_text([{"type": "thinking", "thinking": "only"}]) is None
        assert coerce_content_text(12345) is None

        d_frame = {
            "choices": [
                {
                    "delta": {
                        "content": [
                            {"type": "thinking", "thinking": "plan"},
                            {"type": "text", "text": '/ask @Debugger "ok"'},
                        ]
                    }
                }
            ]
        }
        assert parse_sse_delta(d_frame) == [
            SSEEvent(type="text_delta", data={"content": '/ask @Debugger "ok"'})
        ]

        assert (
            extract_text_content(
                {
                    "choices": [
                        {
                            "message": {
                                "content": [
                                    {"type": "text", "text": "Line 1\n"},
                                    {"type": "output_text", "text": "Line 2"},
                                ]
                            }
                        }
                    ]
                },
                response_format="openai",
            )
            == "Line 1\nLine 2"
        )
        # Sad paths
        assert extract_text_content(None, response_format="openai") == ""  # type: ignore[arg-type]
        assert extract_text_content({"choices": [None]}, response_format="openai") == ""
        assert extract_text_content({"choices": [{"message": None}]}, response_format="openai") == ""

    def test_call_llm_streaming_coerces_list_content_delta_without_type_error(
        self, tmp_path
    ):
        """Defensive guard in _call_llm_streaming handles non-str content in text_delta."""
        deltas: list[str] = []
        rt = AgentRuntime(_make_cfg(), on_text_delta=lambda _sk, d, **_kw: deltas.append(d))
        rt.start()
        sk = _uniq()
        rt.create_conversation("Debugger", sk, str(tmp_path))

        mock_provider = unittest.mock.MagicMock()
        mock_provider.stream.return_value = iter(
            [
                SSEEvent(
                    type="text_delta",
                    data={
                        "content": [
                            {"type": "thinking", "thinking": "step"},
                            {"type": "text", "text": "Coerced "},
                            {"type": "output_text", "text": "cleanly"},
                        ]
                    },
                ),
                SSEEvent(type="done", data={}),
            ]
        )

        try:
            with unittest.mock.patch("agent.runtime._get_provider", return_value=mock_provider):
                out = rt._call_llm_streaming(
                    session_key=sk,
                    base_url="https://api.openai.com/v1",
                    api_key="sk-test",
                    model="openai/gpt-4o",
                    caller_key="openai",
                    messages=[{"role": "user", "content": "hi"}],
                    tools=None,
                    timeout=30.0,
                )
            assert out["choices"][0]["message"]["content"] == "Coerced cleanly"
            assert deltas == ["Coerced cleanly"]
        finally:
            rt.stop()

    def test_openai_stream_non_sse_json_fallback_text_and_tool_calls(self):
        """Proxy ignoring stream=True and returning multi-line JSON is converted to SSEEvents."""
        pretty_json = json.dumps(
            {
                "id": "chatcmpl-non-sse",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": 'Fallback works.\n/ask @Debugger "verify"',
                            "tool_calls": [
                                {
                                    "id": "call_dict_args",
                                    "function": {
                                        "name": "list_files",
                                        "arguments": {"path": "."},
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 9},
            },
            indent=2,
        ).encode("utf-8")
        lines = [line + b"\n" for line in pretty_json.splitlines()]

        provider = OpenAIProvider("openrouter")
        with unittest.mock.patch(
            "agent.llm.openai_provider.urlopen_with_ssl_retry",
            return_value=_SinglePassHTTPResponse(lines),
        ):
            events = list(
                provider.stream(
                    base_url="https://openrouter.ai/api/v1",
                    api_key="sk-or",
                    model="openrouter/auto",
                    messages=[{"role": "user", "content": "hi"}],
                    tools=None,
                    timeout=30.0,
                )
            )

        assert events == [
            SSEEvent(
                type="text_delta",
                data={"content": 'Fallback works.\n/ask @Debugger "verify"'},
            ),
            SSEEvent(
                type="tool_call_delta",
                data={
                    "index": 0,
                    "name": "list_files",
                    "arguments": '{"path": "."}',
                    "id": "call_dict_args",
                },
            ),
            SSEEvent(type="usage", data={"usage": {"prompt_tokens": 12, "completion_tokens": 9}}),
            SSEEvent(type="done", data={}),
        ]

    def test_openai_stream_non_sse_error_garbage_and_oversize_sad_paths(self):
        """Sad paths: HTTP 200 JSON error envelope yields error+done; garbage and >2MiB yield []."""
        provider = OpenAIProvider("openrouter")

        err_body = [b'{"error": {"code": 502, "message": "Upstream gateway error"}}\n']
        with unittest.mock.patch(
            "agent.llm.openai_provider.urlopen_with_ssl_retry",
            return_value=_SinglePassHTTPResponse(err_body),
        ):
            err_events = list(
                provider.stream(
                    base_url="https://openrouter.ai/api/v1",
                    api_key="sk-or",
                    model="openrouter/auto",
                    messages=[{"role": "user", "content": "hi"}],
                    tools=None,
                    timeout=30.0,
                )
            )
        assert err_events == [
            SSEEvent(
                type="error",
                data={"error": {"code": 502, "message": "Upstream gateway error"}},
            ),
            SSEEvent(type="done", data={}),
        ]

        with unittest.mock.patch(
            "agent.llm.openai_provider.urlopen_with_ssl_retry",
            return_value=_SinglePassHTTPResponse([b"<html>502 Bad Gateway</html>\n"]),
        ):
            assert (
                list(
                    provider.stream(
                        base_url="https://openrouter.ai/api/v1",
                        api_key="sk-or",
                        model="openrouter/auto",
                        messages=[{"role": "user", "content": "hi"}],
                        tools=None,
                        timeout=30.0,
                    )
                )
                == []
            )

        oversize_chunk = b"x" * (_MAX_NON_SSE_FALLBACK_BYTES + 128)
        with unittest.mock.patch(
            "agent.llm.openai_provider.urlopen_with_ssl_retry",
            return_value=_SinglePassHTTPResponse([oversize_chunk]),
        ):
            assert (
                list(
                    provider.stream(
                        base_url="https://openrouter.ai/api/v1",
                        api_key="sk-or",
                        model="openrouter/auto",
                        messages=[{"role": "user", "content": "hi"}],
                        tools=None,
                        timeout=30.0,
                    )
                )
                == []
            )
